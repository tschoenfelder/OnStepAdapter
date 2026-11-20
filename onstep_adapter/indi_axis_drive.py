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
#: How close a move must land, as a share of its angle (above the readout floors).
#: Field capture 9e7fd012 (2026-11-20): at 0.5 % every 1 deg step needed one or two correction
#: passes (~2.5 s each: a first pass lands within ~2-4 %), so a 1 deg step took 6-11 s.
LANDING_SHARE = 0.015
#: The least a move must land within, for steps of up to 10x this (operator 2026-11-20: a 1 deg
#: step must be quick).
LANDING_MIN_ARCSEC = 0.06 * 3600.0
#: After Off a single fresh reading proves rest once it was taken this long after the expected
#: end of the coast (times `REST_COAST_FACTOR`): capture 2c63af9f -- readings came 0.77-1.5 s
#: apart after Off, so waiting for two equal ones took ~3.3 s although the axis rested after
#: ~1.3 s.
REST_COAST_FACTOR = 1.2
REST_MARGIN_S = 0.3
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
#: Time from the Off command to the axis braking (field capture 2eb2a5cb: within ~0.05 s).
STOP_LATENCY_S = 0.05
#: A published reading shows where the axis was this long before it arrived (2eb2a5cb: the
#: driver reads the position, then publishes it, ~0.1 s later at 3.3 deg/s).
READING_LAG_S = 0.1
#: Braking (deg/s^2) assumed until a pass measured it; 2eb2a5cb measured ~0.565. Slightly LOWER:
#: it stops a little early, so a first move undershoots slightly rather than overshoots.
ASSUMED_BRAKE_DEG_PER_S2 = 0.55
#: Braking samples kept per (axis, rate) -- a new one within this share of a kept sample's
#: speed is averaged into it.
BRAKE_SAMPLES = 8
BRAKE_SAME_SPEED_SHARE = 0.15
#: A learned braking value is clipped into this range.
BRAKE_RANGE_DEG_PER_S2 = (0.1, 20.0)
#: A DEC move's coast ends at least this far inside the DEC bound.
BOUND_MARGIN_DEG = 0.2
#: Before a pass has two moving readings its progress comes from a model of the speed-up:
#: start this long after On, then accelerate at this rate (deg/s^2) -- both learned per pass.
#: Field capture 2026-11-18 21:00: after On the driver keeps its 1.5 s cycle until its next
#: poll, so a 1 deg step was nearly over before the first fast reading and overshot by up to
#: 0.28 deg. The rig fit (2eb2a5cb) is 0.39 s and 0.54 deg/s^2; the assumptions are a little
#: EARLIER and FASTER, so an unlearned step stops early rather than late.
ASSUMED_START_LATENCY_S = 0.3
ASSUMED_ACCEL_DEG_PER_S2 = 0.6
#: Learned speed-up values outside these ranges are ignored as measuring artefacts.
START_LATENCY_RANGE_S = (0.0, 2.0)
ACCEL_RANGE_DEG_PER_S2 = (0.05, 20.0)
#: A pass whose axis made no progress (`STALL_PROGRESS_ARCSEC`) over this long and at least
#: `STALL_READINGS` fresh readings has stalled: OnStep stopped it (field diag 988199c9: at
#: its meridian limit, with the switch still Busy -- the pass kept it On for 40 s).
STALL_S = 2.0
STALL_READINGS = 2
STALL_PROGRESS_ARCSEC = 5.0
#: ... counted only once the axis moved, or this long after On (start latency on top).
STALL_START_S = 4.0
#: The learned wake-early allowance for Off never exceeds this (s).
MAX_OFF_DELAY_S = 0.1
#: Moving readings the speed/speed-up estimate of a fast pass is fitted to.
SPEED_FIT_READINGS = 4
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
    # Operator (2026-11-18 21:40): after 1/5/20 deg steps there and back, PARK still moved the
    # mount ~0.2-0.4 deg -- the 3 % of each step had added up. 0.5 % keeps that below ~0.1 deg.
    # Field capture 2c63af9f (2026-11-20): OnStep starts the axis 0.3-0.6 s after On, varying
    # step to step, so identical 1 deg runs landed 0.93 and 1.06 deg; a correction pass for
    # that cost 3-4 s. A small step accepts up to LANDING_MIN_ARCSEC (never over 10 %).
    small = min(LANDING_MIN_ARCSEC, 0.1 * abs(offset_arcsec))
    return max(floor, LANDING_SHARE * abs(offset_arcsec), small)


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


#: What the drives of one mount have learned (rates, braking, speed-up), per (INDI host,
#: port, device): a reconnect builds a new drive, which then starts from what the previous one
#: measured instead of the assumptions (field capture 9e7fd012: the first steps after every
#: connect landed 3-10 % short).
_LEARNED: dict[tuple[str, int, str], dict[str, dict]] = {}


def _learned_state(mover: object) -> dict[str, dict]:
    transport = getattr(mover, "transport", None)
    key = (
        str(getattr(transport, "host", "")), int(getattr(transport, "port", 0) or 0),
        str(getattr(mover, "device", "")),
    )
    return _LEARNED.setdefault(key, {})


def _speed_up_fit(ramp: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(speed at the last reading, acceleration) of a least-squares parabola through the last
    `SPEED_FIT_READINGS` moving readings of `ramp` ((s, arcsec) pairs), or None."""
    points = [p for p in ramp if p[1] > MIN_DETECTED_ARCSEC][-SPEED_FIT_READINGS:]
    if len(points) < SPEED_FIT_READINGS:
        return None
    t_last = points[-1][0]
    sums = [0.0] * 5
    rhs = [0.0] * 3
    for t, m in points:
        x = t - t_last
        power = 1.0
        for k in range(5):
            sums[k] += power
            if k < 3:
                rhs[k] += power * m
            power *= x
    matrix = [[sums[i + j] for j in range(3)] for i in range(3)]

    def det(a: list[list[float]]) -> float:
        return (
            a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
        )

    d = det(matrix)
    if abs(d) < 1e-12:
        return None
    coefficients = []
    for column in range(3):
        replaced = [row[:] for row in matrix]
        for row in range(3):
            replaced[row][column] = rhs[row]
        coefficients.append(det(replaced) / d)
    _, slope, curve = coefficients
    return max(0.0, slope), max(0.0, 2.0 * curve)


class ControllerStoppedAxis(RuntimeError):
    """OnStep stopped the axis itself (a limit): `escape_element` moves back out."""

    def __init__(self, message: str, axis: str, escape_element: str) -> None:
        super().__init__(message)
        self.axis = axis
        self.escape_element = escape_element


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
        learned = _learned_state(mover)
        #: Measured rate (arcsec/s) per (axis, rate index), learned across moves.
        self.measured_rate: dict[tuple[str, int], float] = learned.setdefault("rate", {})
        #: Measured braking per (axis, rate index): (speed at Off arcsec/s, deg/s^2) samples,
        #: learned from each pass's coast.
        self.measured_brake: dict[tuple[str, int], list[tuple[float, float]]] = (
            learned.setdefault("brake", {})
        )
        #: Measured speed-up (deg/s^2) per (axis, fast rate index) and start latency (s) per
        #: axis, learned from the readings of each pass.
        self.measured_accel: dict[tuple[str, int], float] = learned.setdefault("accel", {})
        self.measured_start_latency: dict[str, float] = learned.setdefault("latency", {})
        #: How much later than planned Off went out, per axis (sleep granularity), learned.
        self.measured_off_delay: dict[str, float] = learned.setdefault("off_delay", {})

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
            indices = tuple(
                i for i in DRIVE_RATE_INDICES if i not in ASSUMED_FAST_RATE_ARCSEC_PER_S
            )
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

    def brake_deg_per_s2(self, axis: str, index: int, speed: float | None = None) -> float:
        """The braking (deg/s^2) from `speed` (arcsec/s): interpolated between the speeds
        passes measured it at (the rig brakes relatively weaker from low speed: a 1 deg step
        from ~0.75 deg/s coasted 0.66 deg, the 20 deg steps imply ~0.56 deg/s^2), the nearest
        one outside them, `ASSUMED_BRAKE_DEG_PER_S2` before any."""
        samples = self.measured_brake.get((axis, index))
        if not samples:
            return ASSUMED_BRAKE_DEG_PER_S2
        if speed is None:
            return samples[-1][1]
        ordered = sorted(samples)
        if speed <= ordered[0][0]:
            return ordered[0][1]
        if speed >= ordered[-1][0]:
            return ordered[-1][1]
        for (v1, a1), (v2, a2) in zip(ordered, ordered[1:]):
            if v1 <= speed <= v2:
                share = (speed - v1) / (v2 - v1) if v2 > v1 else 0.0
                return a1 + share * (a2 - a1)
        return ordered[-1][1]

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
        brake = self.brake_deg_per_s2(axis, index, speed) * 3600.0
        return speed * STOP_LATENCY_S + speed * speed / (2.0 * brake)

    def _ramp_model(
        self, axis: str, index: int, rate: float, elapsed_s: float
    ) -> tuple[float, float]:
        """(moved arcsec, speed arcsec/s) `elapsed_s` after On, from the speed-up model."""
        latency = self.measured_start_latency.get(axis, ASSUMED_START_LATENCY_S)
        moving_s = max(0.0, elapsed_s - latency)
        if index not in ASSUMED_FAST_RATE_ARCSEC_PER_S:
            return rate * moving_s, rate if moving_s > 0.0 else 0.0
        accel = self.measured_accel.get((axis, index), ASSUMED_ACCEL_DEG_PER_S2) * 3600.0
        return 0.5 * accel * moving_s * moving_s, accel * moving_s

    def _learn_ramp(self, axis: str, index: int, ramp: list[tuple[float, float]]) -> None:
        """Fit `moved = a/2 (t - latency)^2` to a fast pass's readings while it sped up."""
        if index not in ASSUMED_FAST_RATE_ARCSEC_PER_S:
            return
        points = [point for point in ramp if point[1] > 30.0]
        speeding_up: list[tuple[float, float]] = points[:2]
        for previous, current, following in zip(points, points[1:], points[2:]):
            before = (current[1] - previous[1]) / max(current[0] - previous[0], 1e-6)
            after = (following[1] - current[1]) / max(following[0] - current[0], 1e-6)
            if after < 1.02 * before:
                break  # reached its top speed
            speeding_up.append(following)
        if len(speeding_up) < 2:
            return
        (t1, m1), (t2, m2) = speeding_up[0], speeding_up[-1]
        if t2 - t1 < 0.2:
            return
        slope = (math.sqrt(m2) - math.sqrt(m1)) / (t2 - t1)  # sqrt(a/2)
        if slope <= 0.0:
            return
        accel = 2.0 * slope * slope / 3600.0
        latency = t1 - math.sqrt(m1) / slope
        if not ACCEL_RANGE_DEG_PER_S2[0] <= accel <= ACCEL_RANGE_DEG_PER_S2[1]:
            return
        if not START_LATENCY_RANGE_S[0] <= latency <= START_LATENCY_RANGE_S[1]:
            return
        old_accel = self.measured_accel.get((axis, index))
        self.measured_accel[(axis, index)] = (
            accel if old_accel is None else (old_accel + accel) / 2.0
        )
        old_latency = self.measured_start_latency.get(axis)
        self.measured_start_latency[axis] = (
            latency if old_latency is None else (old_latency + latency) / 2.0
        )

    def _learn_brake(
        self, axis: str, index: int, speed_at_off: float, coast_arcsec: float
    ) -> None:
        if speed_at_off < MIN_BRAKE_LEARN_SPEED:
            return
        low, high = BRAKE_RANGE_DEG_PER_S2
        # A coast within the reaction time: a mount that stops (almost) at once.
        braking = max(coast_arcsec - speed_at_off * STOP_LATENCY_S, 1.0)
        fresh = min(max(speed_at_off * speed_at_off / (2.0 * braking) / 3600.0, low), high)
        samples = self.measured_brake.setdefault((axis, index), [])
        for position, (speed, brake) in enumerate(samples):
            if abs(speed - speed_at_off) <= BRAKE_SAME_SPEED_SHARE * max(speed, speed_at_off):
                samples[position] = ((speed + speed_at_off) / 2.0, (brake + fresh) / 2.0)
                break
        else:
            samples.append((speed_at_off, fresh))
        del samples[:-BRAKE_SAMPLES]

    def run(
        self, axis: Axis, offset_deg: float, before: IndiMountSnapshot, *,
        poll_s: float, deadline: float, token: MotionToken,
        pause: Callable[[float], None], on_issue: Callable[[], None],
        allow_controller_error: bool = False,
    ) -> DriveOutcome:
        """`allow_controller_error`: the move leads out of an OnStep limit, whose error stays
        reported until the axis is back inside (`IndiAxisMover.move`)."""
        mover = self.mover

        def usable_sample(snapshot: IndiMountSnapshot) -> bool:
            return mover.check_in_motion(snapshot, allow_controller_error=allow_controller_error)

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
            moving_readings = 0
            ramp: list[tuple[float, float]] = []  # (s since On, moved) of every reading
            planned_off: float | None = None
            progress_mark = 0.0
            progress_at = started
            readings_since_progress = 0
            stalled = False
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
            fast = index in ASSUMED_FAST_RATE_ARCSEC_PER_S
            cruise_cap = math.inf if fast else rate

            model_accel = (
                self.measured_accel.get((axis, index), ASSUMED_ACCEL_DEG_PER_S2) * 3600.0
                if index in ASSUMED_FAST_RATE_ARCSEC_PER_S else 0.0
            )

            def predict(at: float) -> tuple[float, float]:
                """(moved, speed) at monotonic time `at`: from the readings once two of them
                show the axis moving, from the speed-up model before that (the larger)."""
                model = self._ramp_model(axis, index, rate, at - started)
                if reading is None:
                    return model
                ahead = min(max(0.0, at - reading[0]) + READING_LAG_S, MAX_PREDICTION_S)
                # Until three moving readings measure the speed-up, it comes from the model
                # (field capture 21:00: with two readings and no speed-up the stop of a 1 deg
                # step came one reading late and overshot by 0.28 deg).
                # A slow rate is at its speed at once: no speed-up to extrapolate (field run
                # 2026-11-18 21:58: a 74" correction stopped at 49").
                if not fast:
                    speed_up = 0.0
                else:
                    speed_up = accel if moving_readings >= 3 else max(accel, model_accel)
                capped = min(speed + speed_up * ahead, cruise_cap)
                measured = (
                    reading[1] + speed * ahead + 0.5 * speed_up * ahead * ahead, capped
                )
                if moving_readings >= 2 or (not fast and moving_readings >= 1):
                    return measured
                return max(measured[0], model[0]), max(measured[1], model[1])

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
                usable = usable_sample(latest)
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
                    ramp.append((read_at - started, moved_now))
                    if moved_now > MIN_DETECTED_ARCSEC:
                        moving_readings += 1
                    fit = _speed_up_fit(ramp) if fast else None
                    if fit is not None:
                        # Smoother than two readings: a parabola through the last ones
                        # (rig-model runs scattered +-3 % on two-reading estimates).
                        speed, accel = min(fit[0], cruise_cap), fit[1]
                    if moved_now > progress_mark + STALL_PROGRESS_ARCSEC:
                        progress_mark, progress_at, readings_since_progress = moved_now, now, 0
                    else:
                        readings_since_progress += 1
                    if (
                        (moving_readings or now - started > STALL_START_S)
                        and now - progress_at > STALL_S
                        and readings_since_progress >= STALL_READINGS
                    ):
                        stalled = True
                        break  # OnStep stopped the axis: switch Off, then report why
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
                # Between readings the axis keeps going: predict where it is NOW, so Off goes
                # out at the right moment and not at the next reading (0.26 s at up to 3.3
                # deg/s on the rig is ~0.9 deg).
                if past_stop_point(now):
                    break  # the axis coasts the rest of the way
                horizon = now + 3.0 * PASS_POLL_S  # a loop round can take longer than a poll
                if past_stop_point(horizon):
                    # The stop point falls before the next check: wait for it exactly.
                    low, high = now, horizon
                    for _ in range(12):
                        middle = 0.5 * (low + high)
                        low, high = (low, middle) if past_stop_point(middle) else (middle, high)
                    # Wake early by how late the previous waits woke (sleep granularity).
                    delay = self.measured_off_delay.get(axis, 0.0)
                    pause(max(0.0, high - delay - self._monotonic()))
                    planned_off = high
                    break
            off_at = self._monotonic()
            if planned_off is not None:
                late = off_at - planned_off
                old_delay = self.measured_off_delay.get(axis, 0.0)
                self.measured_off_delay[axis] = min(
                    max(old_delay + 0.5 * late, 0.0), MAX_OFF_DELAY_S
                )
            moving_s = off_at - started
            transport.issue_switch_off(device, name, element)
            transport.issue_switch(device, "TELESCOPE_TRACK_STATE", "TRACK_OFF")
            token.report("confirming_stop")
            # Where the axis was, and how fast, when Off went out.
            moved_at_off, speed = predict(off_at)
            expected_coast_s = speed / (self.brake_deg_per_s2(axis, index, speed) * 3600.0)
            latest = self._wait_at_rest(
                axis, before, latest, poll_s, pause, expected_coast_s, off_at=off_at,
                allow_controller_error=allow_controller_error or stalled,
            )
            if stalled:
                moved_deg = abs(
                    mover._angle_delta(start_pos, self.position_deg(axis, latest))
                )
                reason = mover.controller_error_text() or "no reason reported"
                escape = _ELEMENTS[(axis, -direction)][1]
                raise ControllerStoppedAxis(
                    f"OnStep stopped the {axis.upper()} axis after {moved_deg:.2f} deg: "
                    f"{reason}. Move {axis.upper()} the other way to get back inside; PARK "
                    "and all other moves are blocked until then",
                    axis, escape,
                )
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
            self._learn_ramp(axis, index, ramp)
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
                moved_at_off, speed, moved * (direction * sign),
                self.brake_deg_per_s2(axis, index, speed),
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
        *, allow_controller_error: bool = False, off_at: float | None = None,
    ) -> IndiMountSnapshot:
        """Wait until the axis is at rest after Off, and return that reading.

        At rest = two consecutive fresh coordinate readings agree (`AT_REST_ARCSEC`). OnStep's
        :GU# cannot tell: on the real rig it never flags manual motion as slewing, and it is
        silent while a switch is On (field capture 2eb2a5cb: "stopped" was confirmed from it
        while the axis still coasted 8 deg). A fresh OnStep Status after Off must also show
        tracking off (the TRACK_OFF sent with every stop). With `off_at` (when Off went out), a
        single reading taken `REST_COAST_FACTOR` x `expected_coast_s` + `REST_MARGIN_S` after it
        proves rest on its own."""
        mover = self.mover
        quiet_from = (
            off_at + REST_COAST_FACTOR * expected_coast_s + REST_MARGIN_S
            if off_at is not None else math.inf
        )
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
            if not mover.check_in_motion(sample, allow_controller_error=allow_controller_error):
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
            read_at = self._monotonic() - max(0.0, (sample.coordinates_age_ms or 0.0) / 1000.0)
            at_rest = read_at >= quiet_from or (last_position is not None and (
                abs(mover._angle_delta(last_position, position)) * 3600.0
                <= AT_REST_ARCSEC[axis]
            ))
            last_position = position
            if at_rest and tracking_off_seen:
                return sample
