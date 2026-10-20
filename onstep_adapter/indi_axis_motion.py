"""Finite, feedback-checked INDI axis-angle test moves."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import math
import threading
import time
from typing import Callable, Literal

from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport
from .indi_gu import local_sidereal_hours

Axis = Literal["ra", "dec"]
MIN_AXIS_MOVE_ARCSEC = 30.0
MAX_AXIS_MOVE_DEG = 15.0
LOGGER = logging.getLogger(__name__)


class AxisMotionMode(str, Enum):
    """Reference frame for a bounded axis-angle request."""

    TERRESTRIAL = "terrestrial"
    SIDEREAL = "sidereal"
    LUNAR = "lunar"
    SOLAR = "solar"


@dataclass(frozen=True)
class IndiAxisMoveResult:
    axis: Axis
    requested_deg: float
    requested_arcsec: float
    measured_deg: float
    initial_ra_hours: float
    final_ra_hours: float
    initial_dec_deg: float
    final_dec_deg: float
    pier_side: str
    stop_confirmed: bool
    samples: int
    verification_required: bool = True
    motion_mode: str = AxisMotionMode.TERRESTRIAL.value


class IndiAxisMover:
    """Use a finite target, never an indefinite manual-motion switch.

    Positive RA-axis angle increases HA (westward). Positive DEC follows the
    OnStep mechanical jog direction, which decreases the published sky DEC.
    """

    def __init__(
        self, transport: IndiTransport, device: str, observer_lat_deg: float,
        observer_lon_deg: float, observe: Callable[[], IndiMountSnapshot],
        emergency_stop: Callable[[], object],
        *, min_alt_deg: float = -2.0, min_dec_deg: float = -80.0,
        max_dec_deg: float = 80.0,
    ) -> None:
        if not all(math.isfinite(value) for value in (
            observer_lat_deg, observer_lon_deg, min_alt_deg,
            min_dec_deg, max_dec_deg,
        )):
            raise ValueError("Axis movement configuration must be finite")
        if not -90.0 <= min_alt_deg <= 90.0:
            raise ValueError("Axis movement minimum altitude is out of range")
        if not -90.0 <= min_dec_deg < max_dec_deg <= 90.0:
            raise ValueError("Axis movement DEC bounds are invalid")
        self.transport = transport
        self.device = device
        self.observer_lat_deg = observer_lat_deg
        self.observer_lon_deg = observer_lon_deg
        self.min_alt_deg = min_alt_deg
        self.min_dec_deg = min_dec_deg
        self.max_dec_deg = max_dec_deg
        self.observe = observe
        self.emergency_stop = emergency_stop
        self._lock = threading.Lock()

    @staticmethod
    def _check(
        snapshot: IndiMountSnapshot, *, moving: bool = False,
        require_fresh_coordinates: bool = True,
        allow_transient_secondary_pier_conflict: bool = False,
    ) -> None:
        forbidden = {
            "indi_device_disconnected", "onstep_status_not_fresh",
            "onstep_status_alert",
            "onstep_status_unavailable", "onstep_limit_or_park_fault",
            "onstep_reported_error", "pier_side_conflict", "pier_side_unknown",
        }
        if require_fresh_coordinates:
            forbidden.update({"coordinates_not_fresh", "coordinates_invalid"})
        if allow_transient_secondary_pier_conflict:
            forbidden.discard("pier_side_conflict")
        blockers = forbidden.intersection(snapshot.blockers)
        if (
            blockers or not snapshot.status_live or
            (require_fresh_coordinates and not snapshot.coordinates_live)
        ):
            raise RuntimeError(f"Axis motion safety inputs unavailable: {sorted(blockers)}")
        if (
            snapshot.at_limit or snapshot.parked or
            (snapshot.tracking and not moving) or
            (snapshot.slewing and not moving) or
                snapshot.pier_side not in {"east", "west"} or
                (require_fresh_coordinates and (
                    snapshot.ra_hours is None or snapshot.dec_deg is None
                ))
        ):
            raise RuntimeError(
                "Local axis motion requires fresh unparked, stationary, "
                "non-tracking and fault-free state"
            )

    @staticmethod
    def _ra_axis_delta(initial_ra: float, current_ra: float) -> float:
        # Increasing HA is the opposite of increasing RA.
        return -((current_ra - initial_ra + 12.0) % 24.0 - 12.0) * 15.0

    @staticmethod
    def _angle_delta(initial_deg: float, current_deg: float) -> float:
        return (current_deg - initial_deg + 180.0) % 360.0 - 180.0

    def _horizontal_from_equatorial(
        self, ha_deg: float, dec_deg: float,
    ) -> tuple[float, float]:
        """Return altitude and north-through-east azimuth in degrees."""
        latitude = math.radians(self.observer_lat_deg)
        hour_angle = math.radians(ha_deg)
        declination = math.radians(dec_deg)
        east = -math.cos(declination) * math.sin(hour_angle)
        north = (
            math.sin(declination) * math.cos(latitude)
            - math.cos(declination) * math.cos(hour_angle) * math.sin(latitude)
        )
        up = (
            math.sin(declination) * math.sin(latitude)
            + math.cos(declination) * math.cos(hour_angle) * math.cos(latitude)
        )
        altitude = math.degrees(math.asin(max(-1.0, min(1.0, up))))
        azimuth = math.degrees(math.atan2(east, north)) % 360.0
        return altitude, azimuth

    def _equatorial_from_horizontal(
        self, altitude_deg: float, azimuth_deg: float, lst_hours: float,
    ) -> tuple[float, float]:
        """Translate an Alt/Az target to the single canonical RA/DEC point."""
        latitude = math.radians(self.observer_lat_deg)
        altitude = math.radians(altitude_deg)
        azimuth = math.radians(azimuth_deg)
        east = math.cos(altitude) * math.sin(azimuth)
        north = math.cos(altitude) * math.cos(azimuth)
        up = math.sin(altitude)
        sin_dec = up * math.sin(latitude) + north * math.cos(latitude)
        declination = math.asin(max(-1.0, min(1.0, sin_dec)))
        cos_dec = max(1e-15, math.cos(declination))
        sin_ha = -east / cos_dec
        cos_ha = (up * math.cos(latitude) - north * math.sin(latitude)) / cos_dec
        hour_angle = math.atan2(sin_ha, cos_ha)
        ra_hours = (lst_hours - math.degrees(hour_angle) / 15.0) % 24.0
        return ra_hours, math.degrees(declination)

    @staticmethod
    def _fold_altitude_path(
        altitude_deg: float, azimuth_deg: float,
    ) -> tuple[float, float]:
        """Fold a continuous vertical path through zenith/nadir."""
        while altitude_deg > 90.0 or altitude_deg < -90.0:
            if altitude_deg > 90.0:
                altitude_deg = 180.0 - altitude_deg
            else:
                altitude_deg = -180.0 - altitude_deg
            azimuth_deg += 180.0
        return altitude_deg, azimuth_deg % 360.0

    @staticmethod
    def _sky_separation_deg(
        ra1_hours: float, dec1_deg: float, ra2_hours: float, dec2_deg: float,
    ) -> float:
        ra_delta = math.radians((ra2_hours - ra1_hours) * 15.0)
        dec1 = math.radians(dec1_deg)
        dec2 = math.radians(dec2_deg)
        cosine = (
            math.sin(dec1) * math.sin(dec2)
            + math.cos(dec1) * math.cos(dec2) * math.cos(ra_delta)
        )
        return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))

    @staticmethod
    def _equatorial_axis_target(
        ra_hours: float,
        dec_deg: float,
        ha_deg: float,
        axis: Axis,
        offset_deg: float,
    ) -> tuple[float, float, float]:
        """Return a finite target while keeping the other mount axis fixed.

        Positive RA-axis motion increases hour angle, so equatorial RA
        decreases by the same angle. OnStep's positive physical DEC jog
        direction decreases the published equatorial DEC coordinate; RA and
        HA remain unchanged.
        There is deliberately no zenith or pier-side folding here.
        """
        if axis == "dec":
            return ra_hours, dec_deg - offset_deg, ha_deg
        return (ra_hours - offset_deg / 15.0) % 24.0, dec_deg, ha_deg + offset_deg

    def move(
        self, axis: Axis, offset_deg: float, *, timeout_s: float = 30.0,
        poll_s: float = 0.1,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ) -> IndiAxisMoveResult:
        try:
            requested_mode = AxisMotionMode(mode)
        except ValueError as exc:
            raise ValueError(f"Unsupported axis motion mode: {mode!r}") from exc
        if requested_mode is not AxisMotionMode.TERRESTRIAL:
            raise ValueError(
                "Finite axis jog currently supports mode='terrestrial' only; "
                "sidereal/lunar/solar tracking modes are never changed implicitly"
            )
        if axis not in {"ra", "dec"}:
            raise ValueError("axis must be ra or dec")
        minimum_deg = MIN_AXIS_MOVE_ARCSEC / 3600.0
        if not math.isfinite(offset_deg) or not minimum_deg <= abs(offset_deg) <= MAX_AXIS_MOVE_DEG:
            raise ValueError(
                f"Axis move must be between {MIN_AXIS_MOVE_ARCSEC:g} arcseconds "
                f"and {MAX_AXIS_MOVE_DEG:g} degrees; requests are never rounded"
            )
        if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 60:
            raise ValueError("Axis move timeout must be between 1 and 60 seconds")
        if not math.isfinite(poll_s) or not 0.05 <= poll_s <= 0.25:
            raise ValueError("Axis move poll interval must be 0.05..0.25 seconds")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another axis motion is active")
        issued = False
        try:
            before = self.observe()
            self._check(before)
            assert before.ra_hours is not None and before.dec_deg is not None
            # Some INDI sessions do not publish a trusted HA even though their
            # equatorial coordinates are fresh.  HA is derived data: calculate
            # it from the Raspberry UTC clock, configured longitude and RA
            # instead of refusing an otherwise valid first move.
            if before.ha_deg is None:
                lst_hours = local_sidereal_hours(
                    self.observer_lon_deg, datetime.now(timezone.utc)
                )
                initial_ha_deg = (
                    (lst_hours - before.ra_hours + 12.0) % 24.0 - 12.0
                ) * 15.0
            else:
                initial_ha_deg = before.ha_deg
                lst_hours = (before.ra_hours + initial_ha_deg / 15.0) % 24.0
            target_ra, target_dec, target_ha_deg = self._equatorial_axis_target(
                before.ra_hours, before.dec_deg, initial_ha_deg, axis, offset_deg
            )
            LOGGER.info(
                "Axis target: axis=%s requested_deg=%+.6f "
                "start_ra_h=%.9f start_dec_deg=%+.9f start_ha_deg=%+.9f "
                "target_ra_h=%.9f target_dec_deg=%+.9f target_ha_deg=%+.9f",
                axis, offset_deg, before.ra_hours, before.dec_deg, initial_ha_deg,
                target_ra, target_dec, target_ha_deg,
            )
            if not self.min_dec_deg <= target_dec <= self.max_dec_deg:
                raise RuntimeError("Projected DEC leaves the configured movement bound")
            target_alt, _ = self._horizontal_from_equatorial(
                target_ha_deg, target_dec
            )
            if target_alt < self.min_alt_deg - 1e-9:
                raise RuntimeError(
                    "Projected altitude leaves the local movement bound"
                )
            arrival_tolerance = min(
                0.05, max(10.0 / 3600.0, abs(offset_deg) * 0.10)
            )
            self.transport.set_switch(self.device, "ON_COORD_SET", "SLEW")
            issued = True
            self.transport.issue_numbers(
                self.device, "EQUATORIAL_EOD_COORD",
                {"RA": target_ra, "DEC": target_dec},
            )
            deadline = time.monotonic() + timeout_s
            count = 0
            latest = before
            last_coord_revision = before.coordinates_revision
            target_coordinates_seen = False
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Axis move did not reach its finite target")
                time.sleep(poll_s)
                latest = self.observe()
                self._check(
                    latest, moving=True,
                    require_fresh_coordinates=not target_coordinates_seen,
                    allow_transient_secondary_pier_conflict=True,
                )
                if target_coordinates_seen:
                    if not latest.slewing:
                        break
                    continue
                if latest.coordinates_revision <= last_coord_revision:
                    continue
                last_coord_revision = latest.coordinates_revision
                count += 1
                assert latest.ra_hours is not None and latest.dec_deg is not None
                remaining = self._sky_separation_deg(
                    latest.ra_hours, latest.dec_deg, target_ra, target_dec
                )
                if remaining <= arrival_tolerance:
                    target_coordinates_seen = True
                    if not latest.slewing:
                        break
            # lx200_OnStep can execute TRACK_OFF without publishing a fresh
            # TELESCOPE_TRACK_STATE vector.  Dispatch immediately; the two
            # fresh OnStep Status samples below are the safety confirmation.
            self.transport.issue_switch(
                self.device, "TELESCOPE_TRACK_STATE", "TRACK_OFF"
            )
            stopped_polls = 0
            last_revision = latest.status_revision
            stop_deadline = time.monotonic() + 5.0
            while stopped_polls < 2:
                if time.monotonic() >= stop_deadline:
                    raise TimeoutError("Axis move tracking-off was not confirmed")
                time.sleep(max(poll_s, 0.15))
                sample = self.observe()
                self._check(
                    sample, moving=True, require_fresh_coordinates=False
                )
                if sample.status_revision <= last_revision:
                    continue
                last_revision = sample.status_revision
                stopped_polls = stopped_polls + 1 if not sample.tracking and not sample.slewing else 0
                latest = sample
            assert latest.ra_hours is not None and latest.dec_deg is not None
            final_error = self._sky_separation_deg(
                latest.ra_hours, latest.dec_deg, target_ra, target_dec
            )
            if final_error > arrival_tolerance:
                raise RuntimeError("Axis move ended outside its scaled verification tolerance")
            measured = offset_deg
            return IndiAxisMoveResult(
                axis, offset_deg, offset_deg * 3600.0, measured,
                before.ra_hours, latest.ra_hours,
                before.dec_deg, latest.dec_deg, before.pier_side, True, count,
                motion_mode=requested_mode.value,
            )
        except BaseException:
            if issued:
                self.emergency_stop()
            raise
        finally:
            self._lock.release()
