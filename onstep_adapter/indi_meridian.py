"""Application-lifetime meridian observation and hard-stop enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

from .indi_status import IndiMountSnapshot
from .indi_stop import IndiStopResult
from .meridian_policy import MeridianPolicy, SIDEREAL_DEGREES_PER_SECOND


@dataclass(frozen=True)
class IndiMeridianState:
    phase: str
    ha_deg: float | None
    pier_side: str | None
    flip_required: bool
    tracking_stop_required: bool
    seconds_to_flip_boundary: float | None
    seconds_to_hard_stop: float | None
    latest_safe_flip_start_seconds: float | None
    limit_warning: bool
    blockers: tuple[str, ...]


def classify_meridian(snapshot: IndiMountSnapshot, policy: MeridianPolicy) -> IndiMeridianState:
    """Never turn missing authority into an unsafe-motion verdict."""
    if snapshot.parked or snapshot.at_home:
        return IndiMeridianState(
            "mechanical_terminal", snapshot.ha_deg, snapshot.pier_side,
            False, False, None, None, None, False, snapshot.blockers,
        )
    if snapshot.at_limit and snapshot.status_live:
        return IndiMeridianState(
            "firmware_limit", snapshot.ha_deg, snapshot.pier_side,
            False, snapshot.tracking or snapshot.slewing, None, None, None,
            True, snapshot.blockers,
        )
    if (
        not snapshot.status_live or not snapshot.coordinates_live or
        not snapshot.time_site_authority or snapshot.ha_deg is None or
        snapshot.pier_side is None or "pier_side_conflict" in snapshot.blockers or
        "indi_device_disconnected" in snapshot.blockers
    ):
        return IndiMeridianState(
            "unknown", snapshot.ha_deg, snapshot.pier_side,
            False, False, None, None, None, False, snapshot.blockers,
        )
    ha = snapshot.ha_deg
    to_flip = (policy.flip_request_deg - ha) / SIDEREAL_DEGREES_PER_SECOND
    to_stop = (policy.hard_stop_deg - ha) / SIDEREAL_DEGREES_PER_SECOND
    latest = to_stop - policy.flip_allowance_seconds - policy.reserve_seconds
    if snapshot.pier_side == "west":
        phase = "post_flip"
        flip = False
        stop = False
    elif ha < 0:
        phase = "pre_meridian_allowed"
        flip = False
        stop = False
    elif ha < policy.flip_request_deg:
        phase = "post_meridian_allowed"
        flip = False
        stop = False
    elif ha < policy.hard_stop_deg:
        phase = "flip_required"
        flip = True
        stop = False
    else:
        phase = "hard_stop"
        flip = True
        stop = snapshot.tracking or snapshot.slewing
    return IndiMeridianState(
        phase, ha, snapshot.pier_side, flip, stop, to_flip, to_stop,
        latest, flip or stop, snapshot.blockers,
    )


class IndiMeridianSupervisor:
    """No daemon: the worker exists only while this client keeps it alive."""

    def __init__(
        self, *, observe: Callable[[], IndiMountSnapshot],
        policy: MeridianPolicy, stop: Callable[[], IndiStopResult],
        on_transition: Callable[[IndiMeridianState], None] | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Supervisor poll interval must be positive")
        self.observe = observe
        self.policy = policy
        self.stop = stop
        self.on_transition = on_transition
        self.poll_seconds = poll_seconds
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_phase: str | None = None
        self._last_stop_at = 0.0
        self.latest: IndiMeridianState | None = None
        self.last_stop_result: IndiStopResult | None = None
        self.last_error: str | None = None

    def tick(self) -> IndiMeridianState:
        state = classify_meridian(self.observe(), self.policy)
        self.latest = state
        if state.phase != self._last_phase:
            self._last_phase = state.phase
            if self.on_transition is not None:
                self.on_transition(state)
        if state.tracking_stop_required and time.monotonic() - self._last_stop_at >= 3.0:
            self._last_stop_at = time.monotonic()
            self.last_stop_result = self.stop()
        return state

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._run, name="onstep-indi-meridian", daemon=True
        )
        self._thread.start()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        self._shutdown.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(15.0, self.poll_seconds + 5.0))
            if self._thread.is_alive():
                raise TimeoutError("INDI meridian supervisor did not stop")
        self._thread = None

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self.tick()
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
                self.last_error = str(exc)
            self._shutdown.wait(self.poll_seconds)
