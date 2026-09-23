"""Bounded focuser operations through the shared INDI connection."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time

from .indi_config import IndiRuntimeConfig
from .indi_transport import IndiTransport


@dataclass(frozen=True)
class IndiFocuserSnapshot:
    position: int | None
    driver_maximum: int | None
    configured_maximum: int | None
    moving: bool | None
    move_ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class IndiFocuserMoveResult:
    target: int
    reached: bool
    final_position: int | None
    stop_confirmed: bool | None
    error: str | None


class IndiFocuser:
    """All writes share the INDI transport; no direct controller connection."""

    def __init__(self, transport: IndiTransport, config: IndiRuntimeConfig) -> None:
        self.transport = transport
        self.config = config
        self._motion_lock = threading.Lock()
        self._cancel_requested = threading.Event()

    def get_status(self) -> IndiFocuserSnapshot:
        device = self.config.device
        connection = self.transport.get_property(device, "CONNECTION")
        position_prop = self.transport.get_property(device, "ABS_FOCUS_POSITION")
        max_prop = self.transport.get_property(device, "FOCUS_MAX")
        blockers: list[str] = []
        if (not self.transport.is_open or connection is None or
                connection.values.get("CONNECT") != "On"):
            blockers.append("indi_device_disconnected")
        position = self._integer(position_prop, "FOCUS_ABSOLUTE_POSITION")
        maximum = self._integer(max_prop, "FOCUS_MAX_VALUE")
        if position is None or position < 0:
            blockers.append("focuser_position_unknown")
        if maximum is None or maximum <= 0:
            blockers.append("focuser_maximum_unknown")
        if (maximum is not None and position is not None and position > maximum):
            blockers.append("focuser_limit_conflicts_with_position")
        configured = self.config.focuser_max_position
        if configured is None:
            blockers.append("focuser_configured_maximum_missing")
        elif position is not None and position > configured:
            blockers.append("focuser_position_exceeds_configuration")
        now = time.monotonic()
        if position_prop is None or now - position_prop.received_at > 3.0:
            blockers.append("focuser_position_stale")
        # FOCUS_MAX is a static INDI property; the driver need not republish it.
        if (position_prop is not None and position_prop.state.lower() == "alert") or (
            max_prop is not None and max_prop.state.lower() == "alert"
        ):
            blockers.append("focuser_property_alert")
        moving = position_prop.state.lower() == "busy" if position_prop is not None else None
        return IndiFocuserSnapshot(
            position, maximum, configured, moving,
            not blockers and not moving, tuple(blockers),
        )

    def move_absolute(self, target: int, *, timeout: float = 30.0) -> IndiFocuserMoveResult:
        if type(target) is not int:
            raise ValueError("Focuser target must be an integer")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Focuser timeout must be positive and finite")
        with self._motion_lock:
            self._cancel_requested.clear()
            before = self.get_status()
            if not before.move_ready:
                return IndiFocuserMoveResult(
                    target, False, before.position, None,
                    f"Focuser move refused: {', '.join(before.blockers)}",
                )
            maximum = min(before.driver_maximum, before.configured_maximum)
            if target < 0 or target > maximum:
                raise ValueError("Focuser target exceeds confirmed travel limits")
            if target == before.position:
                return IndiFocuserMoveResult(target, True, target, None, None)
            device = self.config.device
            current = self.transport.get_property(device, "ABS_FOCUS_POSITION")
            revision = current.revision
            try:
                self.transport.issue_number(
                    device, "ABS_FOCUS_POSITION", "FOCUS_ABSOLUTE_POSITION", target
                )
                deadline = time.monotonic() + timeout
                stable = 0
                final = None
                while stable < 2:
                    if self._cancel_requested.is_set():
                        raise RuntimeError("Focuser move cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Focuser target was not confirmed")
                    try:
                        current = self.transport.wait_property(
                            device, "ABS_FOCUS_POSITION", timeout=min(remaining, 0.5),
                            after_revision=revision,
                        )
                    except TimeoutError:
                        continue
                    revision = current.revision
                    final = self._integer(current, "FOCUS_ABSOLUTE_POSITION")
                    if current.state.lower() == "alert" or final is None:
                        raise RuntimeError("Focuser position update is invalid")
                    latest_max = self._integer(
                        self.transport.get_property(device, "FOCUS_MAX"), "FOCUS_MAX_VALUE"
                    )
                    if latest_max is None or latest_max < max(target, final):
                        raise RuntimeError("Focuser limit became inconsistent during move")
                    stable = stable + 1 if final == target and current.state.lower() == "ok" else 0
                return IndiFocuserMoveResult(target, True, final, None, None)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
                try:
                    stopped = self._stop_unlocked(timeout=5.0)
                except (ConnectionError, RuntimeError, TimeoutError, ValueError):
                    stopped = False
                return IndiFocuserMoveResult(target, False, None, stopped, str(exc))

    def stop(self, *, timeout: float = 5.0) -> bool:
        self._cancel_requested.set()
        return self._stop_unlocked(timeout=timeout)

    def _stop_unlocked(self, *, timeout: float) -> bool:
        device = self.config.device
        current = self.transport.get_property(device, "ABS_FOCUS_POSITION")
        revision = current.revision if current is not None else -1
        self.transport.request_switch(device, "FOCUS_ABORT_MOTION", "ABORT", timeout=timeout)
        deadline = time.monotonic() + timeout
        previous = None
        stable = 0
        while stable < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                current = self.transport.wait_property(
                    device, "ABS_FOCUS_POSITION", timeout=remaining,
                    after_revision=revision,
                )
            except TimeoutError:
                return False
            revision = current.revision
            position = self._integer(current, "FOCUS_ABSOLUTE_POSITION")
            if position is None or current.state.lower() == "alert":
                return False
            stable = stable + 1 if current.state.lower() == "ok" and position == previous else 0
            previous = position
        return True

    @staticmethod
    def _integer(prop, element: str) -> int | None:
        if prop is None:
            return None
        try:
            value = float(prop.values[element])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
