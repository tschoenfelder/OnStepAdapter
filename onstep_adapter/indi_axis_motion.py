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

from .indi_motion import NO_STOP, MotionStopped, MotionToken
from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport
from .indi_gu import local_sidereal_hours
from .indi_axis_drive import _ELEMENTS, ControllerStoppedAxis, SingleAxisDrive

Axis = Literal["ra", "dec"]
MIN_AXIS_MOVE_ARCSEC = 30.0
#: Operator request 2026-11-18: 1/5/20 degree RA/Dec steps in one move (an RA step on the sky
#: is 1/cos(Dec) larger on the axis: 20 deg at Dec 60 is 40 deg). The DEC and altitude bounds
#: still apply to every move.
MAX_AXIS_MOVE_DEG = 45.0
#: The driver's POLLING_PERIOD (ms) while a finite move runs (`IndiAxisMover._fast_polling`).
FAST_POLLING_MS = 250.0
#: Sidereal seconds per solar second: how fast the published RA of a
#: stationary (tracking-off) mount grows.
SIDEREAL_PER_SOLAR = 1.00273790935
#: How long OnStep Status may stay silent right after motion commands before it counts as a
#: fault (`IndiAxisMover.check_allowing_status_gap`; field incident e515713a measured 3.1 s).
STATUS_GAP_GRACE_MS = 8000.0
LOGGER = logging.getLogger(__name__)

#: Why an RA-axis move is refused before the DEC axis has moved (see
#: ``IndiAxisMover.ra_axis_defined``).
RA_UNDEFINED_AT_HOME = (
    "RA is not defined at HOME or right after connecting: move the DEC axis "
    "first, then RA (nothing was sent)"
)


class RaAxisUndefinedError(RuntimeError):
    """An RA-axis move was requested before a DEC move defined RA."""

    def __init__(self) -> None:
        super().__init__(RA_UNDEFINED_AT_HOME)


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
    Logical coordinates are not independent mechanical encoders.

    DEC first: at HOME the mount points at the pole, where RA has a value but
    no meaning, so an RA target computed from it is undefined. After
    connecting (a new mover per connection), after ``mark_ra_undefined()``
    (HOME was commanded) and whenever a fresh reading reports HOME, an RA move
    is refused unsent until one DEC move has completed and been verified.
    """

    def __init__(
        self, transport: IndiTransport, device: str, observer_lat_deg: float,
        observer_lon_deg: float, observe: Callable[[], IndiMountSnapshot],
        emergency_stop: Callable[[], object],
        *, min_alt_deg: float = -2.0, min_dec_deg: float = -80.0,
        max_dec_deg: float = 80.0,
        monotonic: Callable[[], float] = time.monotonic,
        single_axis: bool = True,
        utc_clock: Callable[[], datetime] | None = None,
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
        self._monotonic = monotonic
        #: True (the default): a finite move drives only its own axis with OnStep's
        #: single-axis motion (``indi_axis_drive``); False: the former goto to a computed
        #: RA/DEC target, which also commands the other axis (kept for comparison/tests).
        self.single_axis = single_axis
        self._drive = SingleAxisDrive(self, utc_clock=utc_clock, monotonic=monotonic)
        self._lock = threading.Lock()
        self._ra_defined = False
        #: (pier side, True when OnStep's NORTH raises the published DEC) as the last DEC move
        #: measured it; None until then. Field capture fd84c75f (2026-11-18): from PARK at pier
        #: side east NORTH RAISED DEC, while capture b977fe85 (after HOME) saw it lower DEC --
        #: the sign is measured, never assumed.
        self._north_raises_dec: tuple[str, bool] | None = None
        #: (axis, motion element) that leads back out of the OnStep limit a move ran into
        #: (field diag 988199c9); None when no such limit is known.
        self._limit_escape: tuple[str, str] | None = None

    @property
    def ra_axis_defined(self) -> bool:
        """False until a verified DEC move after connecting or reaching HOME."""
        return self._ra_defined

    def mark_ra_undefined(self) -> None:
        """RA becomes undefined again, e.g. because the mount was sent HOME."""
        self._ra_defined = False
        self._north_raises_dec = None

    @staticmethod
    def _fold_dec(dec_deg: float) -> float:
        """A DEC beyond a pole, as the mount publishes it after crossing that pole."""
        if dec_deg > 90.0:
            return 180.0 - dec_deg
        if dec_deg < -90.0:
            return -180.0 - dec_deg
        return dec_deg

    def projected_dec_targets(self, before: IndiMountSnapshot, offset_deg: float) -> list[float]:
        """Where a DEC-axis move of `offset_deg` can end: the measured direction when the last
        DEC move at this pier side measured it, otherwise BOTH directions."""
        assert before.dec_deg is not None
        learned = self._north_raises_dec
        if learned is not None and learned[0] == before.pier_side and not before.at_home:
            sign = 1.0 if learned[1] else -1.0
            return [self._fold_dec(before.dec_deg + sign * offset_deg)]
        return [
            self._fold_dec(before.dec_deg + offset_deg),
            self._fold_dec(before.dec_deg - offset_deg),
        ]

    def _learn_dec_direction(
        self, before: IndiMountSnapshot, after: IndiMountSnapshot, offset_deg: float
    ) -> None:
        if (
            before.dec_deg is None or after.dec_deg is None or before.at_home
            or after.pier_side != before.pier_side or before.pier_side is None
        ):
            return
        delta = after.dec_deg - before.dec_deg
        if abs(delta) * 3600.0 < 3.0:
            return
        self._north_raises_dec = (before.pier_side, (delta > 0.0) == (offset_deg > 0.0))

    def require_ra_defined(self, axis: Axis) -> None:
        """Refuse an RA move while RA is undefined; never sends anything."""
        if axis == "ra" and not self._ra_defined:
            raise RaAxisUndefinedError()

    #: The blockers `_check(moving=True, require_fresh_coordinates=False)` refuses on.
    _FORBIDDEN_IN_MOTION = frozenset({
        "indi_device_disconnected", "onstep_status_not_fresh", "onstep_status_alert",
        "onstep_status_unavailable", "onstep_limit_or_park_fault", "onstep_reported_error",
        "pier_side_conflict", "pier_side_unknown",
    })

    @staticmethod
    def _check(
        snapshot: IndiMountSnapshot, *, moving: bool = False,
        require_fresh_coordinates: bool = True,
        allow_transient_secondary_pier_conflict: bool = False,
        tolerated: set[str] | frozenset[str] = frozenset(),
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
        forbidden -= set(tolerated)
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

    def check_allowing_status_gap(self, snapshot: IndiMountSnapshot) -> bool:
        """The in-motion safety check, but a SHORT gap in OnStep Status is a wait, not a fault.

        Field incident e515713a: after a stop the real lx200_OnStep driver publishes no
        OnStep Status for 2.6-3.1 s while it works through the stop and TRACK_OFF commands;
        the 3 s freshness limit then tripped and the stop confirmation sent an emergency
        ABORT although nothing was wrong. Returns True when `snapshot` passed the full check,
        False when the only problem is OnStep Status older than the freshness limit but within
        `STATUS_GAP_GRACE_MS` (the caller waits for the next sample). Every other blocker, and
        a gap longer than the grace, still raises as before.
        """
        try:
            self._check(snapshot, moving=True, require_fresh_coordinates=False)
            return True
        except RuntimeError:
            age = snapshot.status_age_ms
            only_stale = set(snapshot.blockers) & self._FORBIDDEN_IN_MOTION == {
                "onstep_status_not_fresh"
            }
            if only_stale and age is not None and age <= STATUS_GAP_GRACE_MS:
                return False
            raise

    def controller_error_text(self) -> str | None:
        """OnStep Status.Error as the driver reports it ("Meridian Limit (W) Exceeded"), or
        None when it reports none."""
        get_property = getattr(self.transport, "get_property", None)
        prop = get_property(self.device, "OnStep Status") if callable(get_property) else None
        text = prop.values.get("Error", "") if prop is not None else ""
        return None if text in {"", "None", "0"} else text

    @property
    def limit_escape(self) -> tuple[str, str] | None:
        """(axis, motion element) that leads out of the limit OnStep stopped a move at."""
        return self._limit_escape

    def check_in_motion(
        self, snapshot: IndiMountSnapshot, *, allow_controller_error: bool = False
    ) -> bool:
        """The safety check while an axis move drives or coasts.

        Field capture 2eb2a5cb: the real lx200_OnStep driver publishes NO OnStep Status while a
        motion switch is On (7 s and longer), yet its coordinates keep coming every ~0.26 s.
        So stale OnStep Status alone is not a fault here: True when `snapshot` passed the full
        check, or when the only problem is stale OnStep Status and the coordinates are live;
        False when it is stale and the coordinates are not (the caller waits, and stops a pass
        that stays blind). Every other blocker raises.
        """
        tolerated = {"onstep_reported_error"} if allow_controller_error else set()
        try:
            self._check(
                snapshot, moving=True, require_fresh_coordinates=False, tolerated=tolerated
            )
            return True
        except RuntimeError:
            only_stale = set(snapshot.blockers) & self._FORBIDDEN_IN_MOTION - tolerated == {
                "onstep_status_not_fresh"
            }
            if only_stale:
                return snapshot.coordinates_live
            raise

    def _fast_polling(self) -> Callable[[], None]:
        """Set the driver's POLLING_PERIOD to `FAST_POLLING_MS` for one move; returns what
        restores the previous value.

        Operator (2026-11-18 21:58): the wait after a step must fit the step. At the rig's
        1000 ms the driver reports a position every ~1.5 s, so proving a 1 deg step at rest
        took ~3 s after its ~1.3 s coast whatever its size. Between moves the period is the
        operator's own, for every INDI client. A driver without the property, or one that
        refuses the change, keeps its period (logged)."""
        prop = self.transport.get_property(self.device, "POLLING_PERIOD")
        try:
            previous = float(prop.values["PERIOD_MS"]) if prop is not None else None
        except (KeyError, ValueError):
            previous = None
        if previous is None or previous <= FAST_POLLING_MS:
            return lambda: None
        try:
            self.transport.set_number(
                self.device, "POLLING_PERIOD", {"PERIOD_MS": FAST_POLLING_MS}
            )
        except Exception as exc:  # noqa: BLE001 -- a slower period only costs time
            LOGGER.warning("POLLING_PERIOD not set to %.0f ms: %s", FAST_POLLING_MS, exc)
            return lambda: None
        LOGGER.info("POLLING_PERIOD %.0f -> %.0f ms for this move", previous, FAST_POLLING_MS)

        def restore() -> None:
            try:
                self.transport.set_number(self.device, "POLLING_PERIOD", {"PERIOD_MS": previous})
            except Exception as exc:  # noqa: BLE001 -- reported, never hidden
                LOGGER.warning(
                    "POLLING_PERIOD not restored to %.0f ms after the move: %s", previous, exc
                )

        return restore

    def _preflight_check(self, before: IndiMountSnapshot, axis: Axis, offset_deg: float) -> bool:
        """The start check of a move; True when it is the move back out of an OnStep limit.

        Field diag 988199c9: after OnStep stopped an RA move at its meridian limit, OnStep
        kept reporting the error and every command was refused -- PARK too -- with no way
        out. While that error is reported, the one move allowed is back the way it came."""
        try:
            self._check(before)
        except RuntimeError:
            escape = self._limit_escape
            only_error = (
                set(before.blockers) & self._FORBIDDEN_IN_MOTION == {"onstep_reported_error"}
            )
            if not only_error or escape is None:
                raise
            element = _ELEMENTS[(axis, 1 if offset_deg > 0 else -1)][1]
            if escape != (axis, element):
                reason = self.controller_error_text() or "a controller error"
                raise RuntimeError(
                    f"OnStep reports {reason}: only a {escape[0].upper()} move the other way "
                    f"({escape[1]}) leads back inside; PARK works again after it"
                ) from None
            self._check(before, tolerated={"onstep_reported_error"})
            return True
        if self._limit_escape is not None and self.controller_error_text() is None:
            self._limit_escape = None
        return False

    def _dec_outside_bound(self, dec_deg: float) -> float:
        """How far ``dec_deg`` lies outside the DEC bound (0 inside it)."""
        return max(self.min_dec_deg - dec_deg, dec_deg - self.max_dec_deg, 0.0)

    @staticmethod
    def _ra_axis_delta(initial_ra: float, current_ra: float) -> float:
        # Increasing HA is the opposite of increasing RA.
        return -((current_ra - initial_ra + 12.0) % 24.0 - 12.0) * 15.0

    @staticmethod
    def _angle_delta(initial_deg: float, current_deg: float) -> float:
        return (current_deg - initial_deg + 180.0) % 360.0 - 180.0

    def _projected_altitude(self, ha_deg: float, dec_deg: float) -> float:
        return self._horizontal_from_equatorial(ha_deg, dec_deg)[0]

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

    def _stationary_ra_drift_hours(
        self, snapshot: IndiMountSnapshot, observed_at: float,
    ) -> float:
        """How far the published RA has advanced since ``snapshot`` was taken.

        A finite move runs with tracking off, so the mount is stationary and
        its RA grows at the sidereal rate.  ``snapshot``'s RA is
        ``coordinates_age_ms`` old when observed at ``observed_at``
        (monotonic); a goto target built from it unchanged would drive the
        RA axis back by that age.  Real capture b977fe85: every DEC move
        dragged the RA axis by 12-20 arcsec of hour angle this way.
        """
        age_s = max(0.0, (snapshot.coordinates_age_ms or 0.0) / 1000.0)
        elapsed_s = max(0.0, self._monotonic() - observed_at) + age_s
        return elapsed_s * SIDEREAL_PER_SOLAR / 3600.0

    @staticmethod
    def validate_request(
        axis: Axis, offset_deg: float, *, timeout_s: float = 30.0,
        poll_s: float = 0.1,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
    ) -> AxisMotionMode:
        """Reject a malformed request before any motion thread is started."""
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
        return requested_mode

    def move(
        self, axis: Axis, offset_deg: float, *, timeout_s: float = 30.0,
        poll_s: float = 0.1,
        mode: AxisMotionMode | str = AxisMotionMode.TERRESTRIAL,
        token: MotionToken = NO_STOP,
    ) -> IndiAxisMoveResult:
        """Run one finite move to arrival; blocks the calling thread.

        ``OnStepIndiClient.move_axis_deg`` runs it in a worker behind a
        ``MotionHandle``; ``token`` lets that handle stop it.
        """
        requested_mode = self.validate_request(
            axis, offset_deg, timeout_s=timeout_s, poll_s=poll_s, mode=mode
        )
        pause = time.sleep if token is NO_STOP else token.sleep
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another axis motion is active")
        issued = False
        try:
            token.report("preflight", fraction=0.0)
            before = self.observe()
            observed_at = self._monotonic()
            escaping = self._preflight_check(before, axis, offset_deg)
            if before.at_home:
                self._ra_defined = False
            self.require_ra_defined(axis)
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
            candidates = [target_dec]
            if axis == "dec" and self.single_axis:
                candidates = self.projected_dec_targets(before, offset_deg)
            unknown_direction = len(candidates) > 1
            for candidate in candidates:
                if not self.min_dec_deg <= candidate <= self.max_dec_deg and not (
                    axis == "dec" and self._dec_outside_bound(candidate)
                    < self._dec_outside_bound(before.dec_deg)
                ):
                    # A DEC move that starts outside the bound (HOME is DEC +90)
                    # may still run when it only brings DEC closer to the bound:
                    # that is the DEC-first move RA needs at HOME.
                    note = (
                        " (which way this DEC move goes is measured by the first DEC move "
                        "after connecting or HOME; until then both ways must stay inside)"
                        if unknown_direction else ""
                    )
                    raise RuntimeError(
                        f"Projected DEC target {candidate:+.3f} deg from "
                        f"{before.dec_deg:+.3f} deg is outside configured bounds "
                        f"{self.min_dec_deg:+.3f}..{self.max_dec_deg:+.3f} deg{note}"
                    )
                target_alt, _ = self._horizontal_from_equatorial(
                    target_ha_deg, candidate
                )
                if target_alt < self.min_alt_deg - 1e-9:
                    raise RuntimeError(
                        "Projected altitude leaves the local movement bound"
                    )
            if self.single_axis:
                deadline = self._monotonic() + timeout_s + abs(offset_deg) * 3600.0 / 60.0
                restore_polling = self._fast_polling()

                def mark_issued() -> None:
                    nonlocal issued
                    issued = True

                try:
                    outcome = self._drive.run(
                        axis, offset_deg, before, poll_s=poll_s, deadline=deadline,
                        token=token, pause=pause, on_issue=mark_issued,
                        allow_controller_error=escaping,
                    )
                except ControllerStoppedAxis as stopped:
                    self._limit_escape = (stopped.axis, stopped.escape_element)
                    raise
                finally:
                    restore_polling()
                if escaping and self.controller_error_text() is None:
                    self._limit_escape = None  # back inside: the limit error cleared
                latest = outcome.final
                if axis == "dec":
                    self._learn_dec_direction(before, latest, offset_deg)
                if axis == "dec" and not latest.at_home:
                    self._ra_defined = True
                return IndiAxisMoveResult(
                    axis, offset_deg, offset_deg * 3600.0,
                    outcome.achieved_arcsec / 3600.0,
                    before.ra_hours, latest.ra_hours if latest.ra_hours is not None
                    else before.ra_hours,
                    before.dec_deg, latest.dec_deg if latest.dec_deg is not None
                    else before.dec_deg,
                    before.pier_side, True, outcome.samples,
                    motion_mode=requested_mode.value,
                )
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
            token.check()
            self.transport.set_switch(self.device, "ON_COORD_SET", "SLEW")
            issued = True
            # Hold the mechanical HA, not the stale RA: advance the target RA
            # by the sidereal drift since the reading, computed as late as
            # possible.  The other axis then gets no commanded motion beyond
            # OnStep's own 1 s RA resolution.
            drift_hours = self._stationary_ra_drift_hours(before, observed_at)
            target_ra = (target_ra + drift_hours) % 24.0
            LOGGER.info(
                "Axis target RA advanced by %.3f s of sidereal drift to %.9f h",
                drift_hours * 3600.0, target_ra,
            )
            self.transport.issue_numbers(
                self.device, "EQUATORIAL_EOD_COORD",
                {"RA": target_ra, "DEC": target_dec},
            )
            token.report("moving")
            deadline = time.monotonic() + timeout_s
            count = 0
            latest = before
            last_coord_revision = before.coordinates_revision
            target_coordinates_seen = False
            early_track_off_sent = False
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Axis move did not reach its finite target")
                pause(poll_s)
                latest = self.observe()
                token.report(raw_status=getattr(latest, "raw_status", None))
                self._check(
                    latest, moving=True,
                    require_fresh_coordinates=not target_coordinates_seen,
                    allow_transient_secondary_pier_conflict=True,
                )
                if latest.slewing and not early_track_off_sent:
                    # OnStep may start sidereal tracking by itself when the
                    # goto ends (real capture b977fe85, first move after
                    # unpark: ~1.5 s of tracking before the post-arrival
                    # TRACK_OFF landed).  Switch it off as soon as the slew
                    # is seen; the same capture shows OnStep finishing the
                    # goto normally after a mid-slew TRACK_OFF.  The
                    # post-arrival TRACK_OFF below stays the confirmed stop.
                    self.transport.issue_switch(
                        self.device, "TELESCOPE_TRACK_STATE", "TRACK_OFF"
                    )
                    early_track_off_sent = True
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
                travelled = abs(offset_deg) - remaining
                token.report(fraction=max(0.0, travelled) / abs(offset_deg))
                if remaining > abs(offset_deg) + overrun_tolerance:
                    raise RuntimeError("Axis move reversed or overran its finite target")
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
            token.report("confirming_stop")
            stopped_polls = 0
            last_revision = latest.status_revision
            stop_deadline = time.monotonic() + 5.0
            while stopped_polls < 2:
                if time.monotonic() >= stop_deadline:
                    raise TimeoutError("Axis move tracking-off was not confirmed")
                pause(max(poll_s, 0.15))
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
            if axis == "dec" and not latest.at_home:
                self._ra_defined = True
            return IndiAxisMoveResult(
                axis, offset_deg, offset_deg * 3600.0, measured,
                before.ra_hours, latest.ra_hours,
                before.dec_deg, latest.dec_deg, before.pier_side, True, count,
                motion_mode=requested_mode.value,
            )
        except MotionStopped:
            raise  # whoever requested the stop has already sent it
        except BaseException:
            if issued:
                self.emergency_stop()
            raise
        finally:
            self._lock.release()
