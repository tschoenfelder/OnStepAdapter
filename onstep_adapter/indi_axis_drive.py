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
then measures what it achieved from fresh readings after a confirmed stop.
Up to ``MAX_PASSES`` passes close the gap; each later pass uses the rate the
earlier passes measured, and a pass that overshot is followed by one in the
opposite direction of the same axis. A first pass at a rate that has not been
measured yet aims for ``SEED_FRACTION`` of the angle, so an unknown real rate
up to 1/SEED_FRACTION times the nominal one cannot overshoot.

Direction: a positive DEC-axis angle is OnStep's ``MOTION_NORTH`` jog and a
positive RA-axis angle its ``MOTION_WEST`` jog -- the mechanical jog
directions (on this rig, at pier side east, positive DEC lowers the published
sky DEC). Progress is measured as the magnitude moved in the direction the
first pass actually went, so the sky sign of the jog is learned, not assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time
from typing import TYPE_CHECKING, Callable

from .indi_gu import local_sidereal_hours
from .indi_motion import MotionToken
from .indi_status import IndiMountSnapshot

if TYPE_CHECKING:
    from .indi_axis_motion import Axis, IndiAxisMover

#: OnStep's ``:R0#``..``:R7#`` manual rates as multiples of sidereal (the
#: ``TELESCOPE_SLEW_RATE`` indices 0..7; 8 and 9 depend on the firmware's
#: configured maximum and are never used here).
NOMINAL_RATE_X = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 20.0, 48.0)
#: Rates a finite move may use, fastest first.
DRIVE_RATE_INDICES = (7, 6, 5, 4, 3)
SIDEREAL_ARCSEC_PER_S = 15.041
#: A pass is planned to last at least this long when a slower rate allows it
#: (start/stop latency then matters less than the angle moved).
MIN_PASS_S = 1.0
#: A pass never lasts longer than this.
MAX_PASS_S = 90.0
MAX_PASSES = 4
SEED_FRACTION = 0.6
#: Smallest change that counts as "the axis moved" in the first pass.
MIN_DETECTED_ARCSEC = 3.0
#: Wait for a fresh coordinate reading after a confirmed stop.
FRESH_COORDINATES_TIMEOUT_S = 5.0


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
            if measured_axis == axis
        ]
        scale = sum(ratios) / len(ratios) if ratios else 1.0
        choice = None
        for index in DRIVE_RATE_INDICES:
            measured = self.measured_rate.get((axis, index))
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

    def _wait_fresh_coordinates(
        self, after_revision: int, pause: Callable[[float], None], poll_s: float,
    ) -> IndiMountSnapshot:
        deadline = self._monotonic() + FRESH_COORDINATES_TIMEOUT_S
        while True:
            sample = self.mover.observe()
            if sample.coordinates_revision > after_revision and sample.coordinates_live:
                return sample
            if self._monotonic() >= deadline:
                raise TimeoutError("No fresh coordinates after the axis stopped")
            pause(poll_s)

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
        while len(passes) < MAX_PASSES:
            remaining = requested - achieved  # > 0: keep going; < 0: overshot
            if abs(remaining) <= tolerance and passes:
                break
            direction = sign if remaining > 0 else -sign
            index, rate, measured = self._rate_for(axis, abs(remaining))
            planned_s = abs(remaining) / rate
            if not passes and not measured:
                planned_s *= SEED_FRACTION
            planned_s = min(planned_s, MAX_PASS_S)
            if self._monotonic() + planned_s >= deadline:
                raise TimeoutError("Axis move would not finish before its deadline")
            name, element = _ELEMENTS[(axis, direction)]
            self._set_rate(index)
            token.check()
            on_issue()
            transport.issue_switch(device, name, element)
            started = self._monotonic()
            token.report("moving", fraction=max(0.0, achieved) / requested)
            while self._monotonic() - started < planned_s:
                pause(min(poll_s, max(0.0, planned_s - (self._monotonic() - started))))
                latest = mover.observe()
                samples += 1
                token.report(
                    raw_status=getattr(latest, "raw_status", None),
                    fraction=self._estimated_fraction(
                        achieved, direction * sign * rate * (self._monotonic() - started),
                        requested,
                    ),
                )
                mover._check(latest, moving=True, require_fresh_coordinates=False)
                if latest.pier_side != before.pier_side:
                    raise RuntimeError("Pier side changed during an axis move")
                if self._leaves_bound(before, latest):
                    raise RuntimeError("Axis move reached the configured movement bound")
            moving_s = self._monotonic() - started
            transport.issue_switch_off(device, name, element)
            transport.issue_switch(device, "TELESCOPE_TRACK_STATE", "TRACK_OFF")
            token.report("confirming_stop")
            stopped = self._confirm_stationary(before, latest, poll_s, pause)
            latest = self._wait_fresh_coordinates(
                stopped.coordinates_revision, pause, poll_s
            )
            position = self.position_deg(axis, latest)
            delta = mover._angle_delta(start_pos, position) * 3600.0
            if sky_sign == 0:
                if abs(delta) < MIN_DETECTED_ARCSEC:
                    raise RuntimeError(
                        f"The {axis.upper()} axis did not move ({delta:+.1f} arcsec "
                        f"after {moving_s:.2f} s at slew rate {index})"
                    )
                sky_sign = 1 if delta > 0 else -1
            new_achieved = delta * sky_sign
            moved = new_achieved - achieved
            if moving_s > 0.2 and moved * (direction * sign) > 0:
                fresh = abs(moved) / moving_s
                old = self.measured_rate.get((axis, index))
                self.measured_rate[(axis, index)] = fresh if old is None else (old + fresh) / 2
            passes.append(DrivePass(index, element, planned_s, moving_s, moved))
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

    def _confirm_stationary(
        self, before: IndiMountSnapshot, latest: IndiMountSnapshot,
        poll_s: float, pause: Callable[[float], None],
    ) -> IndiMountSnapshot:
        stopped_polls = 0
        last_revision = latest.status_revision
        deadline = self._monotonic() + 5.0
        while stopped_polls < 2:
            if self._monotonic() >= deadline:
                raise TimeoutError("Axis move stop was not confirmed")
            pause(max(poll_s, 0.15))
            sample = self.mover.observe()
            self.mover._check(sample, moving=True, require_fresh_coordinates=False)
            if sample.pier_side != before.pier_side:
                raise RuntimeError("Pier side changed after an axis move")
            if sample.status_revision <= last_revision:
                continue
            last_revision = sample.status_revision
            stopped_polls = (
                stopped_polls + 1 if not sample.tracking and not sample.slewing else 0
            )
            latest = sample
        return latest
