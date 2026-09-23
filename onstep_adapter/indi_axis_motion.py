"""Finite, feedback-checked INDI axis-angle test moves."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
from typing import Callable, Literal

from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport

Axis = Literal["ra", "dec"]
MIN_AXIS_MOVE_ARCSEC = 30.0
MAX_AXIS_MOVE_DEG = 10.0


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

    Positive RA-axis angle increases HA (westward); positive DEC is northward.
    Logical coordinates are not independent mechanical encoders.
    """

    def __init__(
        self, transport: IndiTransport, device: str, observer_lat_deg: float,
        observer_lon_deg: float, observe: Callable[[], IndiMountSnapshot],
        emergency_stop: Callable[[], object],
    ) -> None:
        self.transport = transport
        self.device = device
        self.observer_lat_deg = observer_lat_deg
        self.observer_lon_deg = observer_lon_deg
        self.observe = observe
        self.emergency_stop = emergency_stop
        self._lock = threading.Lock()

    @staticmethod
    def _check(
        snapshot: IndiMountSnapshot, *, moving: bool = False,
        require_fresh_coordinates: bool = True,
    ) -> None:
        forbidden = {
            "indi_device_disconnected", "onstep_status_not_fresh",
            "onstep_status_alert",
            "onstep_status_unavailable", "onstep_limit_or_park_fault",
            "onstep_reported_error", "pier_side_conflict", "pier_side_unknown",
        }
        if require_fresh_coordinates:
            forbidden.update({"coordinates_not_fresh", "coordinates_invalid"})
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
            target_dec = before.dec_deg + (offset_deg if axis == "dec" else 0.0)
            target_ra = (
                before.ra_hours - offset_deg / 15.0
                if axis == "ra" else before.ra_hours
            ) % 24.0
            if not -80.0 <= target_dec <= 80.0:
                raise RuntimeError("Projected DEC leaves the local movement bound")
            arrival_tolerance = min(
                0.05, max(10.0 / 3600.0, abs(offset_deg) * 0.10)
            )
            # OnStep's high-rate goto can report a transient braking overshoot
            # of roughly 0.2 degrees for a 1-degree move.  Keep a bounded
            # in-motion guard band, while retaining the much tighter final
            # arrival check below once slewing has stopped.
            overrun_tolerance = max(
                0.25, min(0.35, abs(offset_deg) * 0.15)
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
                )
                if latest.pier_side != before.pier_side:
                    raise RuntimeError("Pier side changed during axis move")
                if target_coordinates_seen:
                    if not latest.slewing:
                        break
                    continue
                if latest.coordinates_revision <= last_coord_revision:
                    continue
                last_coord_revision = latest.coordinates_revision
                count += 1
                assert latest.ra_hours is not None and latest.dec_deg is not None
                delta = (
                    self._ra_axis_delta(before.ra_hours, latest.ra_hours) if axis == "ra"
                    else latest.dec_deg - before.dec_deg
                )
                signed = delta * (1 if offset_deg > 0 else -1)
                if signed < -arrival_tolerance or signed > abs(offset_deg) + overrun_tolerance:
                    raise RuntimeError("Axis move reversed or overran its finite target")
                reached = abs(delta - offset_deg) <= arrival_tolerance
                other_axis_ok = (
                    abs(latest.dec_deg - before.dec_deg) <= 0.05 if axis == "ra"
                    # A finite DEC goto carries the original RA target, while
                    # the reported RA/HA pairing can change as OnStep switches
                    # between slew and terrestrial stop.  Pier-side stability
                    # and the controller-bounded target guard the other axis.
                    else True
                )
                if reached and other_axis_ok:
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
                if sample.pier_side != before.pier_side:
                    raise RuntimeError("Pier side changed after axis move")
                if sample.status_revision <= last_revision:
                    continue
                last_revision = sample.status_revision
                stopped_polls = stopped_polls + 1 if not sample.tracking and not sample.slewing else 0
                latest = sample
            assert latest.ra_hours is not None and latest.dec_deg is not None
            measured = (
                self._ra_axis_delta(before.ra_hours, latest.ra_hours) if axis == "ra"
                else latest.dec_deg - before.dec_deg
            )
            if abs(measured - offset_deg) > arrival_tolerance:
                raise RuntimeError("Axis move ended outside its scaled verification tolerance")
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
