"""Finite axis moves with OnStep's single-axis motion: the other axis never moves.

A goto to "the current RA, a new DEC" still commands the RA motor: OnStep
takes the RA target to the whole second (15 arcsec of hour angle), and with
tracking off the published RA keeps growing at the sidereal rate. Real
capture b977fe85 (2026-11-17): every DEC goto also turned the RA axis by
12-20 arcsec. A DEC move must not move RA at all, except at HOME, where RA
has no meaning anyway.

So a finite move drives only the requested axis, with OnStep's manual motion
(``TELESCOPE_MOTION_NS`` for DEC, ``TELESCOPE_MOTION_WE`` for RA) at one
entry of ``TELESCOPE_SLEW_RATE``, for the time the remaining angle needs, and
stops it from the LIVE position: as soon as the remaining angle is within the
distance the axis still covers after the stop at its current speed (reaction
time plus braking, ``v * STOP_LATENCY_S + v^2 / 2a``). It then measures what it
achieved once fresh readings show the axis at rest. Up to ``MAX_PASSES`` passes
close the gap; a pass that overshot is followed by one in the opposite
direction of the same axis.

Field capture 2eb2a5cb (2026-11-18) is the reference: at the fastest rate the
rig reached 3.27 deg/s and needed ~10 deg to stop; the real lx200_OnStep
driver published no OnStep Status while a motion switch was On and never
flagged manual motion as slewing (so "at rest" is judged from the coordinates,
which keep coming every ~0.26 s while a switch is On and every ~1.5 s after).
The braking ``a`` is learned from each pass's coast.

Direction: a positive DEC-axis angle is OnStep's ``MOTION_NORTH`` jog and a
positive RA-axis angle its ``MOTION_WEST`` jog -- the mechanical jog
directions (on this rig, at pier side east, positive DEC lowers the published
sky DEC). Progress is measured as the magnitude moved in the direction the
first pass actually went, so the sky sign of the jog is learned, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import math
import time
from typing import TYPE_CHECKING, Callable

from .indi_gu import local_sidereal_hours
from .indi_motion import MotionToken
from .indi_status import IndiMountSnapshot

if TYPE_CHECKING:
    from .indi_axis_motion import Axis, IndiAxisMover

_LOG = logging.getLogger(__name__)

#: OnStep's ``:R0#``..``:R7#`` manual rates as multiples of sidereal (the
#: ``TELESCOPE_SLEW_RATE`` indices 0..7).
NOMINAL_RATE_X = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 20.0, 48.0)
#: ``TELESCOPE_SLEW_RATE`` 8 and 9 (OnStepX: half of / the full configured maximum slew)
#: depend on the firmware's configured maximum, so they have no nominal value. A pass at one
#: that has not been measured yet plans with this deliberately HIGH assumption (deg/s x
#: 3600), so it undershoots rather than overshoots; later passes use the measured rate.
#: Needed for coarse steps (operator request 2026-11-18: 1/5/20 degree RA/Dec steps -- at
#: 48x a 20 degree step took ~100 s).
ASSUMED_FAST_RATE_ARCSEC_PER_S = {8: 6.0 * 3600.0, 9: 12.0 * 3600.0}
#: Rates a finite move may use, fastest first.
DRIVE_RATE_INDICES = (9, 8, 7, 6, 5, 4, 3)
SIDEREAL_ARCSEC_PER_S = 15.041
#: A pass at a slow rate is planned to last at least this long when a slower rate allows it
#: (start/stop latency then matters less than the angle moved).
MIN_PASS_S = 0.75
#: A pass never lasts longer than this.
MAX_PASS_S = 90.0
MAX_PASSES = 8
#: Angles from this size on are driven at the fastest rate: the live braking stop bounds the
#: speed, so a short move simply never gets fast. Smaller remainders (corrections) use the
#: slower rates, where the reaction time costs only arcseconds.
FAST_DRIVE_MIN_ARCSEC = 0.5 * 3600.0
#: Time from the Off command to the axis braking, plus the age of a published reading
#: (field capture 2eb2a5cb: braking began within ~0.05 s; readings lag ~0.1 s).
STOP_LATENCY_S = 0.15
#: Braking (deg/s^2) assumed until a pass measured it; 2eb2a5cb measured ~0.565. Slightly LOWER:
#: it stops a little early, so a first move undershoots slightly rather than overshoots.
ASSUMED_BRAKE_DEG_PER_S2 = 0.55
#: A learned braking value is clipped into this range.
BRAKE_RANGE_DEG_PER_S2 = (0.1, 20.0)
#: A DEC move's coast ends at least this far inside the DEC bound.
BOUND_MARGIN_DEG = 0.2
#: A pass checks its stop this often (s), predicting the position between readings.
PASS_POLL_S = 0.05
#: How far past the last reading the position is predicted (a later reading is overdue).
MAX_PREDICTION_S = 0.6
#: Below this speed (arcsec/s) a coast says nothing about the braking.
MIN_BRAKE_LEARN_SPEED = 0.2 * 3600.0
#: Acceleration (deg/s^2) a fast pass's time limit allows for: deliberately LOW (a slower
#: mount then still finishes the pass on its live stop, not on the limit).
SLOW_ACCEL_DEG_PER_S2 = 0.25
#: A pass stops at once when no fresh reading arrived for this long: it can no longer see
#: where the axis is (2eb2a5cb: the first reading of a pass came up to 1.5 s after On).
COORDINATE_SILENCE_STOP_S = 2.5
#: The axis counts as at rest when two fresh readings agree to within this (RA reads to 1 s
#: of time = 15 arcsec).
AT_REST_ARCSEC = {"dec": 2.0, "ra": 20.0}
#: Smallest change that counts as "the axis moved" in the first pass.
MIN_DETECTED_ARCSEC = 3.0
#: Waiting for the axis to be at rest after a pass ends: this, plus its expected coast.
AT_REST_TIMEOUT_S = 15.0


def drive_tolerance_arcsec(axis: str, offset_arcsec: float) -> float:
    """How close a finite single-axis move must land.

    DEC reads to 1 arcsec; RA only to 1 s of time (15 arcsec of HA), so an RA
    move gets a wider floor.
    """
    floor = 20.0 if axis == "ra" else 10.0
    return max(floor, 0.03 * abs(offset_arcsec))


@dataclass(frozen=True)
class DrivePass:
    rate_index: int
    element: str
    planned_s: float
    moving_s: float
    moved_arcsec: float  # signed along the first pass's direction


@dataclass(frozen=True)
class DriveOutcome:
    achieved_arcsec: float  # signed, in the requested direction
    passes: tuple[DrivePass, ...]
    final: IndiMountSnapshot
    samples: int


_ELEMENTS = {
    ("ra", 1): ("TELESCOPE_MOTION_WE", "MOTION_WEST"),
    ("ra", -1): ("TELESCOPE_MOTION_WE", "MOTION_EAST"),
    ("dec", 1): ("TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    ("dec", -1): ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
}


class SingleAxisDrive:
    """Run one finite move of one axis; owned by an ``IndiAxisMover``."""

    def __init__(
        self, mover: IndiAxisMover, *,
        utc_clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mover = mover
        self._utc = utc_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        #: Measured rate (arcsec/s) per (axis, rate index), learned across moves.
        self.measured_rate: dict[tuple[str, int], float] = {}
        #: Measured braking (deg/s^2) per (axis, rate index), learned from each pass's coast.
        self.measured_brake: dict[tuple[str, int], float] = {}

    # ---- positions ----------------------------------------------------------
    def position_deg(self, axis: Axis, snapshot: IndiMountSnapshot) -> float:
        """DEC, or the mechanical hour angle (the RA axis position) at the
        moment the coordinates were read: LST then minus the published RA."""
        if axis == "dec":
            assert snapshot.dec_deg is not None
            return snapshot.dec_deg
        assert snapshot.ra_hours is not None
        age_s = max(0.0, (snapshot.coordinates_age_ms or 0.0) / 1000.0)
        read_at = self._utc() - timedelta(seconds=age_s)
        lst = local_sidereal_hours(self.mover.observer_lon_deg, read_at)
        return ((lst - snapshot.ra_hours + 12.0) % 24.0 - 12.0) * 15.0

    # ---- one pass -----------------------------------------------------------
    def _rate_for(self, axis: Axis, remaining_arcsec: float) -> tuple[int, float, bool]:
        """(rate index, expected arcsec/s, measured?) for the remaining angle."""
        # An unmeasured rate is the nominal one scaled by how the measured rates of this axis
        # compare with theirs (a controller that runs every rate faster or slower).
        ratios = [
            rate / (NOMINAL_RATE_X[index] * SIDEREAL_ARCSEC_PER_S)
            for (measured_axis, index), rate in self.measured_rate.items()
            if measured_axis == axis and index < len(NOMINAL_RATE_X)
        ]
        scale = sum(ratios) / len(ratios) if ratios else 1.0
        choice = None
        indices = DRIVE_RATE_INDICES
        if remaining_arcsec < FAST_DRIVE_MIN_ARCSEC:
            indices = tuple(i for i in DRIVE_RATE_INDICES if i not in ASSUMED_FAST_RATE_ARCSEC_PER_S)
        else:
            indices = DRIVE_RATE_INDICES[:1]
        for index in indices:
            measured = self.measured_rate.get((axis, index))
            if index in ASSUMED_FAST_RATE_ARCSEC_PER_S:  # no nominal: measured, or assumed high
                # A controller whose measured rates run faster than nominal is assumed to run
                # its fast rates faster too (never slower: the assumption must stay HIGH).
                rate = measured or ASSUMED_FAST_RATE_ARCSEC_PER_S[index] * max(1.0, scale)
                choice = (index, rate, measured is not None)
            else:
                rate = measured or NOMINAL_RATE_X[index] * SIDEREAL_ARCSEC_PER_S * scale
                choice = (index, rate, bool(ratios))
            if remaining_arcsec / rate >= MIN_PASS_S:
                return choice
        assert choice is not None
        return choice

    def _set_rate(self, rate_index: int) -> None:
        transport, device = self.mover.transport, self.mover.device
        prop = transport.get_property(device, "TELESCOPE_SLEW_RATE")
        if prop is None:
            raise RuntimeError("TELESCOPE_SLEW_RATE is not available")
        elements = list(prop.values)
        if rate_index >= len(elements):
            raise RuntimeError(
                f"the controller offers {len(elements)} slew rates; rate {rate_index} is needed"
            )
        transport.set_switch(device, "TELESCOPE_SLEW_RATE", elements[rate_index])

    def brake_deg_per_s2(self, axis: str, index: int) -> float:
        return self.measured_brake.get((axis, index), ASSUMED_BRAKE_DEG_PER_S2)

    def _pass_limit_s(self, index: int, rate: float, angle_arcsec: float) -> float:
        """The longest a pass may run. Its live stop normally ends it far earlier; this only
        bounds a pass whose stop never triggers."""
        if index in ASSUMED_FAST_RATE_ARCSEC_PER_S:
            angle_deg = angle_arcsec / 3600.0
            ramp_s = 2.0 * math.sqrt(angle_deg / SLOW_ACCEL_DEG_PER_S2)
            limit = 2.0 + 1.5 * ramp_s + angle_deg  # + a 1 deg/s cruise for long moves
        else:
            limit = 1.5 + 1.5 * max(angle_arcsec / rate, MIN_PASS_S)
        return min(limit, MAX_PASS_S)

    def _stop_distance(self, axis: str, index: int, speed: float) -> float:
        """Arcsec the axis still moves when Off is sent now at `speed` (arcsec/s): the
        reaction time plus the braking distance."""
        brake = self.brake_deg_per_s2(axis, index) * 3600.0
        return speed * STOP_LATENCY_S + speed * speed / (2.0 * brake)

    def _learn_brake(
        self, axis: str, index: int, speed_at_off: float, coast_arcsec: float
    ) -> None:
        if speed_at_off < MIN_BRAKE_LEARN_SPEED:
            return
        low, high = BRAKE_RANGE_DEG_PER_S2
        # A coast within the reaction time: a mount that stops (almost) at once.
        braking = max(coast_arcsec - speed_at_off * STOP_LATENCY_S, 1.0)
        fresh = min(max(speed_at_off * speed_at_off / (2.0 * braking) / 3600.0, low), high)
        old = self.measured_brake.get((axis, index))
        self.measured_brake[(axis, index)] = fresh if old is None else (old + fresh) / 2.0

    def run(
        self, axis: Axis, offset_deg: float, before: IndiMountSnapshot, *,
        poll_s: float, deadline: float, token: MotionToken,
        pause: Callable[[float], None], on_issue: Callable[[], None],
    ) -> DriveOutcome:
        mover = self.mover
        transport, device = mover.transport, mover.device
        requested = abs(offset_deg) * 3600.0
        tolerance = drive_tolerance_arcsec(axis, requested)
        sign = 1 if offset_deg > 0 else -1
        start_pos = self.position_deg(axis, before)
        #: +1/-1: which way the position goes for the requested element (learned).
        sky_sign = 0
        achieved = 0.0
        passes: list[DrivePass] = []
        samples = 0
        latest = before
        direction = sign

        def along_of(snapshot: IndiMountSnapshot) -> float:
            """Signed progress (arcsec) of `snapshot` in the requested direction."""
            along = mover._angle_delta(start_pos, self.position_deg(axis, snapshot)) * 3600.0
            return along * sky_sign if sky_sign else abs(along)

        while len(passes) < MAX_PASSES:
            remaining = requested - achieved  # > 0: keep going; < 0: overshot
            if abs(remaining) <= tolerance and passes:
                break
            direction = sign if remaining > 0 else -sign
            index, rate, _ = self._rate_for(axis, abs(remaining))
            pass_target = abs(remaining)
            limit_s = self._pass_limit_s(index, rate, pass_target)
            if self._monotonic() + limit_s >= deadline:
                raise TimeoutError("Axis move would not finish before its deadline")
            name, element = _ELEMENTS[(axis, direction)]
            self._set_rate(index)
            token.check()
            on_issue()
            transport.issue_switch(device, name, element)
            started = self._monotonic()
            token.report("moving", fraction=max(0.0, achieved) / requested)
            seen_revision = latest.coordinates_revision
            last_reading_at = started
            moved_now = 0.0
            speed = 0.0  # arcsec/s at `reading[0]`
            accel = 0.0  # arcsec/s^2 at `reading[0]`
            peak_speed = 0.0
            reading: tuple[float, float] | None = None  # (read at, moved)
            reading_dec: float | None = None  # DEC of that reading
            dec_direction = 0.0  # +1/-1: which way the published DEC goes in this pass
            bound_stop = [False]
            start_outside = (
                mover._dec_outside_bound(before.dec_deg) if before.dec_deg is not None else 0.0
            )
            interval_speed: float | None = None  # mean speed between the last two readings
            # A slow rate is reached at once, so a speed-up estimate must not exceed it. The
            # fastest rates speed up for seconds (2eb2a5cb: 6 s to 3.27 deg/s) and stay
            # uncapped: a cap below their real top speed would make every stop late.
            cruise_cap = math.inf if index in ASSUMED_FAST_RATE_ARCSEC_PER_S else 1.1 * rate

            def predict(at: float) -> tuple[float, float]:
                """(moved, speed) at monotonic time `at`, from the last reading."""
                assert reading is not None
                ahead = min(max(0.0, at - reading[0]), MAX_PREDICTION_S)
                capped = min(speed + accel * ahead, cruise_cap)
                return reading[1] + speed * ahead + 0.5 * accel * ahead * ahead, capped

            def past_stop_point(at: float) -> bool:
                moved_at, speed_at = predict(at)
                stop = self._stop_distance(axis, index, speed_at)
                if axis == "dec" and dec_direction and reading_dec is not None and reading:
                    # Field capture fd84c75f: a move stopped AT the DEC bound coasted 8 deg
                    # past it. Stop where the coast ends at the bound at the latest.
                    final = reading_dec + dec_direction * (
                        (moved_at - reading[1] + stop) / 3600.0 + BOUND_MARGIN_DEG
                    )
                    if mover._dec_outside_bound(final) > start_outside + 1e-9:
                        bound_stop[0] = True
                        return True
                return moved_at + stop >= pass_target
            while self._monotonic() - started < limit_s:
                pause(min(poll_s, PASS_POLL_S))
                latest = mover.observe()
                samples += 1
                now = self._monotonic()
                token.report(
                    raw_status=getattr(latest, "raw_status", None),
                    fraction=self._estimated_fraction(
                        achieved, direction * sign * moved_now, requested
                    ),
                )
                usable = mover.check_in_motion(latest)
                if usable and latest.pier_side != before.pier_side:
                    raise RuntimeError("Pier side changed during an axis move")
                if usable and self._leaves_bound(before, latest):
                    raise RuntimeError("Axis move reached the configured movement bound")
                fresh = (
                    usable and latest.coordinates_revision > seen_revision
                    and latest.dec_deg is not None and latest.ra_hours is not None
                )
                if fresh:
                    seen_revision = latest.coordinates_revision
                    read_at = now - max(0.0, (latest.coordinates_age_ms or 0.0) / 1000.0)
                    last_reading_at = now
                    moved_now = (along_of(latest) - achieved) * (direction * sign)
                    if reading is None and read_at - started > 0.05:
                        # The first reading of a pass: at least its mean speed since the start
                        # (a mount that is at full speed at once is then not missed).
                        speed = min(max(0.0, moved_now) / (read_at - started), cruise_cap)
                    elif reading is not None and read_at - reading[0] > 0.05:
                        dt = read_at - reading[0]
                        mean = max(0.0, (moved_now - reading[1]) / dt)
                        # The axis speeds up between readings: the mean of an interval is its
                        # speed half an interval ago (2eb2a5cb: +0.54 deg/s^2 at rate 9).
                        if interval_speed is not None:
                            accel = max(0.0, (mean - interval_speed) / dt)
                        speed = min(mean + 0.5 * accel * dt, cruise_cap)
                        interval_speed = mean
                        peak_speed = max(peak_speed, speed)
                    reading = (read_at, moved_now)
                    assert latest.dec_deg is not None
                    if reading_dec is not None and latest.dec_deg != reading_dec:
                        dec_direction = 1.0 if latest.dec_deg > reading_dec else -1.0
                    reading_dec = latest.dec_deg
                    _LOG.debug(
                        "Axis drive reading: %.2f s moved %.0f\" at %.0f\"/s, %.0f\"/s^2",
                        read_at - started, moved_now, speed, accel,
                    )
                elif now - last_reading_at > COORDINATE_SILENCE_STOP_S:
                    break  # blind: stop, the next pass measures where the axis is
                if reading is None:
                    continue
                # Between readings the axis keeps going: predict where it is NOW, so Off goes
                # out at the right moment and not at the next reading (0.26 s at up to 3.3
                # deg/s on the rig is ~0.9 deg).
                if past_stop_point(now):
                    break  # the axis coasts the rest of the way
                horizon = now + 1.5 * PASS_POLL_S
                if past_stop_point(horizon):
                    # The stop point falls before the next check: wait for it exactly.
                    low, high = now, horizon
                    for _ in range(12):
                        middle = 0.5 * (low + high)
                        low, high = (low, middle) if past_stop_point(middle) else (middle, high)
                    pause(max(0.0, high - self._monotonic()))
                    break
            off_at = self._monotonic()
            moving_s = off_at - started
            transport.issue_switch_off(device, name, element)
            transport.issue_switch(device, "TELESCOPE_TRACK_STATE", "TRACK_OFF")
            token.report("confirming_stop")
            if reading is not None:  # where the axis was, and how fast, when Off went out
                moved_at_off, speed = predict(off_at)
            else:
                moved_at_off = moved_now
            expected_coast_s = speed / (self.brake_deg_per_s2(axis, index) * 3600.0)
            latest = self._wait_at_rest(axis, before, latest, poll_s, pause, expected_coast_s)
            if bound_stop[0]:
                raise RuntimeError(
                    f"DEC move stopped at {latest.dec_deg:+.2f} deg: going on would have passed "
                    f"the configured DEC bound {mover.min_dec_deg:+.1f}..{mover.max_dec_deg:+.1f}"
                    " deg"
                )
            delta = mover._angle_delta(start_pos, self.position_deg(axis, latest)) * 3600.0
            if sky_sign == 0:
                if abs(delta) < MIN_DETECTED_ARCSEC:
                    raise RuntimeError(
                        f"The {axis.upper()} axis did not move ({delta:+.1f} arcsec "
                        f"after {moving_s:.2f} s at slew rate {index})"
                    )
                sky_sign = 1 if delta > 0 else -1
            new_achieved = delta * sky_sign
            moved = new_achieved - achieved
            if reading is not None:
                self._learn_brake(axis, index, speed, moved * (direction * sign) - moved_at_off)
            if peak_speed > 0.0 and index not in ASSUMED_FAST_RATE_ARCSEC_PER_S:
                old = self.measured_rate.get((axis, index))
                self.measured_rate[(axis, index)] = (
                    peak_speed if old is None else (old + peak_speed) / 2
                )
            elif peak_speed > 0.0:  # the fastest speed seen so far at a fast rate
                self.measured_rate[(axis, index)] = max(
                    peak_speed, self.measured_rate.get((axis, index), 0.0)
                )
            passes.append(DrivePass(index, element, limit_s, moving_s, moved))
            _LOG.info(
                "Axis drive pass %d: %s %s at rate %d for %.2f s, aimed %.0f\", Off at %.0f\" "
                "and %.0f\"/s, at rest after %.0f\" (braking now %.3f deg/s^2)",
                len(passes), axis.upper(), element, index, moving_s, pass_target,
                moved_at_off, speed, moved * (direction * sign), self.brake_deg_per_s2(axis, index),
            )
            achieved = new_achieved
        if abs(requested - achieved) > tolerance:
            raise RuntimeError(
                f"Axis move ended {requested - achieved:+.1f} arcsec from its target "
                f"after {len(passes)} passes (tolerance {tolerance:.0f} arcsec)"
            )
        return DriveOutcome(achieved * sign, tuple(passes), latest, samples)

    @staticmethod
    def _estimated_fraction(achieved: float, moving: float, requested: float) -> float:
        """Progress shown while a pass runs (the measured value replaces it after the stop)."""
        return min(0.99, max(0.0, (achieved + moving) / requested))

    def _leaves_bound(self, before: IndiMountSnapshot, latest: IndiMountSnapshot) -> bool:
        mover = self.mover
        if latest.dec_deg is not None and before.dec_deg is not None:
            now_out = mover._dec_outside_bound(latest.dec_deg)
            if now_out > 0 and now_out > mover._dec_outside_bound(before.dec_deg):
                return True
        if (
            latest.ha_deg is not None and latest.dec_deg is not None
            and before.ha_deg is not None and before.dec_deg is not None
        ):
            alt = mover._projected_altitude(latest.ha_deg, latest.dec_deg)
            start_alt = mover._projected_altitude(before.ha_deg, before.dec_deg)
            if alt < mover.min_alt_deg - 1e-9 and alt <= start_alt:
                return True
        return False

    def _wait_at_rest(
        self, axis: Axis, before: IndiMountSnapshot, latest: IndiMountSnapshot,
        poll_s: float, pause: Callable[[float], None], expected_coast_s: float,
    ) -> IndiMountSnapshot:
        """Wait until the axis is at rest after Off, and return that reading.

        At rest = two consecutive fresh coordinate readings agree (`AT_REST_ARCSEC`). OnStep's
        :GU# cannot tell: on the real rig it never flags manual motion as slewing, and it is
        silent while a switch is On (field capture 2eb2a5cb: "stopped" was confirmed from it
        while the axis still coasted 8 deg). A fresh OnStep Status after Off must also show
        tracking off (the TRACK_OFF sent with every stop)."""
        mover = self.mover
        deadline = self._monotonic() + AT_REST_TIMEOUT_S + expected_coast_s
        seen_coordinates = latest.coordinates_revision
        seen_status = latest.status_revision
        tracking_off_seen = False
        last_position: float | None = None
        while True:
            if self._monotonic() >= deadline:
                raise TimeoutError("Axis move stop was not confirmed")
            pause(max(poll_s, 0.1))
            sample = mover.observe()
            if not mover.check_in_motion(sample):
                continue
            if sample.pier_side != before.pier_side:
                raise RuntimeError("Pier side changed after an axis move")
            if sample.status_live and sample.status_revision > seen_status:
                seen_status = sample.status_revision
                tracking_off_seen = not sample.tracking
            if (
                sample.coordinates_revision <= seen_coordinates or not sample.coordinates_live
                or sample.dec_deg is None or sample.ra_hours is None
            ):
                continue
            seen_coordinates = sample.coordinates_revision
            position = self.position_deg(axis, sample)
            at_rest = last_position is not None and (
                abs(mover._angle_delta(last_position, position)) * 3600.0
                <= AT_REST_ARCSEC[axis]
            )
            last_position = position
            if at_rest and tracking_off_seen:
                return sample
