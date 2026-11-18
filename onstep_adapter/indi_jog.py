"""Hold-to-move axis jogs with a deadman lease.

A jog moves one axis with OnStep's manual motion (``TELESCOPE_MOTION_WE`` /
``TELESCOPE_MOTION_NS``) for as long as the caller keeps renewing its lease,
for example while a button is held. It ends when the caller releases it, when
the lease is not renewed in time (the application hung or lost its
connection), after ``max_duration_s``, or when the next position would leave
the configured DEC or altitude bound. Every ending switches the motion off,
turns tracking off and confirms two fresh stationary status readings; a
failure after motion was started sends the emergency stop instead.

The DEC-first rule of ``IndiAxisMover`` applies: an RA jog is refused unsent
until a DEC move (finite or jog) has moved DEC after connecting or HOME.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable

from .indi_axis_motion import Axis, IndiAxisMover
from .indi_motion import NO_STOP, MotionToken
from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport

#: Lease bounds: how long a jog keeps moving without a renewal.
MIN_JOG_LEASE_S = 0.2
MAX_JOG_LEASE_S = 2.0
DEFAULT_JOG_LEASE_S = 0.5
#: Longest single jog, however often it is renewed.
MAX_JOG_DURATION_S = 120.0
DEFAULT_JOG_DURATION_S = 60.0

JOG_RELEASED = "released"
JOG_LEASE_EXPIRED = "lease_expired"
JOG_MAX_DURATION = "max_duration"
JOG_LIMIT = "limit"

_MOTION = {
    ("ra", 1): ("TELESCOPE_MOTION_WE", "MOTION_WEST"),
    ("ra", -1): ("TELESCOPE_MOTION_WE", "MOTION_EAST"),
    ("dec", 1): ("TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    ("dec", -1): ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
}


class JogLease:
    """The deadman: the jog keeps moving only while this is renewed in time."""

    def __init__(
        self, lease_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.lease_s = lease_s
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline = clock() + lease_s
        self._released = False

    def renew(self) -> bool:
        """Extend the lease; False once the jog was released or has expired."""
        with self._lock:
            if self._released or self._clock() >= self._deadline:
                return False
            self._deadline = self._clock() + self.lease_s
            return True

    def release(self) -> None:
        with self._lock:
            self._released = True

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def expired(self) -> bool:
        with self._lock:
            return self._released or self._clock() >= self._deadline


@dataclass(frozen=True)
class IndiJogResult:
    axis: Axis
    direction: int
    rate_index: int | None
    ended_by: str
    moving_s: float
    initial_ra_hours: float | None
    final_ra_hours: float | None
    initial_dec_deg: float | None
    final_dec_deg: float | None
    stop_confirmed: bool
    samples: int


def validate_jog_request(
    axis: str, direction: int, *, rate_index: int | None, lease_s: float,
    max_duration_s: float, poll_s: float,
) -> None:
    """Reject a malformed jog before any motion thread is started."""
    if axis not in {"ra", "dec"}:
        raise ValueError("axis must be ra or dec")
    if isinstance(direction, bool) or direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    if rate_index is not None and (
        isinstance(rate_index, bool) or not isinstance(rate_index, int) or rate_index < 0
    ):
        raise ValueError("rate_index must be a non-negative int or None")
    if not math.isfinite(lease_s) or not MIN_JOG_LEASE_S <= lease_s <= MAX_JOG_LEASE_S:
        raise ValueError(
            f"Jog lease must be {MIN_JOG_LEASE_S:g}..{MAX_JOG_LEASE_S:g} seconds"
        )
    if not math.isfinite(max_duration_s) or not 0 < max_duration_s <= MAX_JOG_DURATION_S:
        raise ValueError(f"Jog duration must be 0..{MAX_JOG_DURATION_S:g} seconds")
    if not math.isfinite(poll_s) or not 0.05 <= poll_s <= 0.25:
        raise ValueError("Jog poll interval must be 0.05..0.25 seconds")


class IndiAxisJogger:
    """Run one hold-to-move jog; shares the DEC-first state and the safety
    checks of the finite ``IndiAxisMover``."""

    def __init__(
        self, transport: IndiTransport, device: str, mover: IndiAxisMover,
        observe: Callable[[], IndiMountSnapshot],
    ) -> None:
        self.transport = transport
        self.device = device
        self.mover = mover
        self.observe = observe

    def _leaves_bound(
        self, before: IndiMountSnapshot, latest: IndiMountSnapshot
    ) -> bool:
        """True when the mount is outside the DEC or altitude bound and getting
        further out (moving back toward the bound is allowed, e.g. from HOME)."""
        mover = self.mover
        if latest.dec_deg is not None and before.dec_deg is not None:
            now_out = mover._dec_outside_bound(latest.dec_deg)
            if now_out > 0 and now_out >= mover._dec_outside_bound(before.dec_deg):
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

    def _set_rate(self, rate_index: int) -> None:
        prop = self.transport.get_property(self.device, "TELESCOPE_SLEW_RATE")
        if prop is None:
            raise RuntimeError("TELESCOPE_SLEW_RATE is not available")
        elements = list(prop.values)
        if rate_index >= len(elements):
            raise ValueError(
                f"rate_index {rate_index} is outside the controller's "
                f"{len(elements)} slew rates"
            )
        self.transport.set_switch(self.device, "TELESCOPE_SLEW_RATE", elements[rate_index])

    def run(
        self, axis: Axis, direction: int, lease: JogLease, *,
        rate_index: int | None = None,
        max_duration_s: float = DEFAULT_JOG_DURATION_S, poll_s: float = 0.1,
        token: MotionToken = NO_STOP,
        clock: Callable[[], float] = time.monotonic,
    ) -> IndiJogResult:
        validate_jog_request(
            axis, direction, rate_index=rate_index, lease_s=lease.lease_s,
            max_duration_s=max_duration_s, poll_s=poll_s,
        )
        mover = self.mover
        mover.require_ra_defined(axis)
        pause = time.sleep if token is NO_STOP else token.sleep
        if not mover._lock.acquire(blocking=False):
            raise RuntimeError("Another axis motion is active")
        issued = False
        try:
            token.report("preflight", fraction=0.0)
            before = self.observe()
            mover._check(before)
            if before.at_home:
                mover.mark_ra_undefined()
            mover.require_ra_defined(axis)
            if lease.expired():  # released before the motion could start
                return IndiJogResult(
                    axis, direction, rate_index, JOG_RELEASED, 0.0,
                    before.ra_hours, before.ra_hours, before.dec_deg,
                    before.dec_deg, True, 0,
                )
            if rate_index is not None:
                self._set_rate(rate_index)
            name, element = _MOTION[(axis, direction)]
            token.check()
            issued = True
            self.transport.issue_switch(self.device, name, element)
            started = clock()
            token.report("moving")
            latest = before
            samples = 0
            ended_by = JOG_RELEASED
            while True:
                pause(poll_s)
                if lease.expired():
                    ended_by = JOG_RELEASED if lease.released else JOG_LEASE_EXPIRED
                    break
                if clock() - started >= max_duration_s:
                    ended_by = JOG_MAX_DURATION
                    break
                latest = self.observe()
                samples += 1
                token.report(raw_status=getattr(latest, "raw_status", None))
                mover._check(latest, moving=True, require_fresh_coordinates=False)
                if latest.pier_side != before.pier_side:
                    raise RuntimeError("Pier side changed during jog")
                if self._leaves_bound(before, latest):
                    ended_by = JOG_LIMIT
                    break
            moving_s = clock() - started
            self.transport.issue_switch_off(self.device, name, element)
            self.transport.issue_switch(
                self.device, "TELESCOPE_TRACK_STATE", "TRACK_OFF"
            )
            token.report("confirming_stop")
            latest = self._confirm_stationary(before, latest, poll_s, pause)
            if (
                axis == "dec" and not latest.at_home
                and latest.dec_deg is not None and before.dec_deg is not None
                and latest.dec_deg != before.dec_deg
            ):
                mover._ra_defined = True
            return IndiJogResult(
                axis, direction, rate_index, ended_by, moving_s,
                before.ra_hours, latest.ra_hours, before.dec_deg,
                latest.dec_deg, True, samples,
            )
        except BaseException:
            if issued:
                mover.emergency_stop()
            raise
        finally:
            mover._lock.release()

    def _confirm_stationary(
        self, before: IndiMountSnapshot, latest: IndiMountSnapshot,
        poll_s: float, pause: Callable[[float], None],
    ) -> IndiMountSnapshot:
        stopped_polls = 0
        last_revision = latest.status_revision
        deadline = time.monotonic() + 5.0
        while stopped_polls < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("Jog stop was not confirmed")
            pause(max(poll_s, 0.15))
            sample = self.observe()
            self.mover._check(sample, moving=True, require_fresh_coordinates=False)
            if sample.pier_side != before.pier_side:
                raise RuntimeError("Pier side changed after jog")
            if sample.status_revision <= last_revision:
                continue
            last_revision = sample.status_revision
            stopped_polls = (
                stopped_polls + 1 if not sample.tracking and not sample.slewing else 0
            )
            latest = sample
        return latest


class JogHandle:
    """A running jog: ``renew()`` while held, ``release()`` to end it normally.

    ``stop()`` is the emergency stop (ABORT and tracking off) of the
    underlying ``MotionHandle``; ``wait()`` returns the ``IndiJogResult``.
    """

    def __init__(self, motion, lease: JogLease) -> None:
        self.motion = motion
        self.lease = lease

    def renew(self) -> bool:
        """Keep moving; False once the jog has ended or its lease expired."""
        return not self.motion.done and self.lease.renew()

    def release(self) -> None:
        self.lease.release()

    def stop(self):
        self.lease.release()
        return self.motion.stop()

    def wait(self, timeout: float | None = None):
        return self.motion.wait(timeout=timeout)

    def progress(self):
        return self.motion.progress()

    @property
    def done(self) -> bool:
        return self.motion.done

    @property
    def state(self) -> str:
        return self.motion.state
