"""Bounded, tracking-preserving pulse guiding through INDI."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Callable, Literal

from .indi_meridian import IndiMeridianState
from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport

GuideDirection = Literal["north", "south", "east", "west"]

MIN_GUIDE_PULSE_MS = 20
MAX_GUIDE_PULSE_MS = 5000
GUIDE_CHUNK_MS = 500

_GUIDE_PROPERTIES: dict[GuideDirection, tuple[str, str]] = {
    "north": ("TELESCOPE_TIMED_GUIDE_NS", "TIMED_GUIDE_N"),
    "south": ("TELESCOPE_TIMED_GUIDE_NS", "TIMED_GUIDE_S"),
    "east": ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_E"),
    "west": ("TELESCOPE_TIMED_GUIDE_WE", "TIMED_GUIDE_W"),
}

_DIRECTION_ALIASES = {
    "n": "north", "north": "north",
    "s": "south", "south": "south",
    "e": "east", "east": "east",
    "w": "west", "west": "west",
}

_HARD_BLOCKERS = {
    "indi_device_disconnected", "onstep_status_not_fresh",
    "onstep_status_alert", "onstep_status_unavailable",
    "onstep_limit_or_park_fault", "onstep_reported_error",
}

_AUTHORITY_BLOCKERS = {
    "coordinates_not_fresh", "pier_side_conflict", "pier_side_unknown",
    "coordinates_invalid", "time_site_authority_unestablished",
    "hour_angle_unavailable", "home_authority_unestablished",
}

_STRICT_ALLOWED_PHASES = {
    "pre_meridian_allowed", "post_meridian_allowed", "post_flip",
    "flip_required",
}


@dataclass(frozen=True)
class IndiGuidePulseResult:
    direction: GuideDirection
    requested_duration_ms: int
    chunks_requested: int
    chunks_completed: int
    command_accepted: bool
    pulse_completed: bool
    tracking_preserved: bool
    final_raw_status: str | None
    meridian_phase: str
    warnings: tuple[str, ...] = ()
    error: str | None = None


class IndiGuideController:
    """Execute self-terminating guide pulses without changing tracking mode."""

    def __init__(
        self,
        transport: IndiTransport,
        device: str,
        *,
        observe: Callable[[], IndiMountSnapshot],
        meridian_status: Callable[[], IndiMeridianState],
        emergency_stop: Callable[[], object],
        authority_policy: str = "strict",
        chunk_ms: int = GUIDE_CHUNK_MS,
    ) -> None:
        if authority_policy not in {"strict", "controller_managed"}:
            raise ValueError("Unknown guiding authority policy")
        if not MIN_GUIDE_PULSE_MS <= chunk_ms <= MAX_GUIDE_PULSE_MS:
            raise ValueError("Guide chunk duration is outside the supported range")
        self.transport = transport
        self.device = device
        self.observe = observe
        self.meridian_status = meridian_status
        self.emergency_stop = emergency_stop
        self.authority_policy = authority_policy
        self.chunk_ms = chunk_ms
        self._lock = threading.Lock()

    @staticmethod
    def normalize_direction(direction: str) -> GuideDirection:
        try:
            return _DIRECTION_ALIASES[direction.strip().lower()]  # type: ignore[return-value]
        except (AttributeError, KeyError) as exc:
            raise ValueError("Guide direction must be north, south, east or west") from exc

    def _preflight(
        self,
    ) -> tuple[IndiMountSnapshot, IndiMeridianState, tuple[str, ...], str | None]:
        snapshot = self.observe()
        meridian = self.meridian_status()
        hard = _HARD_BLOCKERS.intersection(snapshot.blockers)
        authority = _AUTHORITY_BLOCKERS.intersection(snapshot.blockers)
        warnings = set(authority if self.authority_policy == "controller_managed" else ())
        if meridian.flip_required:
            warnings.add("meridian_flip_required")

        reason = None
        if hard or not snapshot.status_live:
            reason = f"guide safety inputs unavailable: {sorted(hard)}"
        elif (
            not snapshot.tracking or snapshot.parked or snapshot.at_home or
            snapshot.slewing or snapshot.at_limit
        ):
            reason = (
                "guide pulse requires fresh unparked tracking state with no "
                "slew, HOME, fault or limit"
            )
        elif meridian.tracking_stop_required or meridian.phase in {
            "hard_stop", "firmware_limit",
        }:
            reason = f"guide pulse refused at meridian phase {meridian.phase}"
        elif self.authority_policy == "strict" and (
            authority or not snapshot.coordinates_live or
            not snapshot.time_site_authority or not snapshot.home_authority or
            meridian.phase not in _STRICT_ALLOWED_PHASES
        ):
            reason = (
                f"guide astronomical authority unavailable: {sorted(authority)} "
                f"phase={meridian.phase}"
            )
        return snapshot, meridian, tuple(sorted(warnings)), reason

    def pulse(
        self,
        direction: str,
        duration_ms: int,
        *,
        command_timeout: float = 3.0,
    ) -> IndiGuidePulseResult:
        normalized = self.normalize_direction(direction)
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise ValueError("Guide duration must be an integer number of milliseconds")
        if not MIN_GUIDE_PULSE_MS <= duration_ms <= MAX_GUIDE_PULSE_MS:
            raise ValueError(
                f"Guide duration must be {MIN_GUIDE_PULSE_MS}..{MAX_GUIDE_PULSE_MS} ms"
            )
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("Guide command timeout must be positive and finite")

        chunks: list[int] = []
        remaining = duration_ms
        while remaining > self.chunk_ms:
            chunk = self.chunk_ms
            if remaining - chunk < MIN_GUIDE_PULSE_MS:
                chunk = remaining - MIN_GUIDE_PULSE_MS
            chunks.append(chunk)
            remaining -= chunk
        chunks.append(remaining)

        if not self._lock.acquire(blocking=False):
            return IndiGuidePulseResult(
                normalized, duration_ms, len(chunks), 0, False, False, False,
                None, "unknown", error="another guide pulse is active",
            )

        issued = False
        completed = 0
        warnings: set[str] = set()
        last_snapshot: IndiMountSnapshot | None = None
        last_meridian: IndiMeridianState | None = None
        try:
            for chunk in chunks:
                snapshot, meridian, current_warnings, refusal = self._preflight()
                last_snapshot = snapshot
                last_meridian = meridian
                warnings.update(current_warnings)
                if refusal:
                    return IndiGuidePulseResult(
                        normalized, duration_ms, len(chunks), completed, issued,
                        False, snapshot.tracking, snapshot.raw_status,
                        meridian.phase, tuple(sorted(warnings)), refusal,
                    )

                property_name, element = _GUIDE_PROPERTIES[normalized]
                revision = self.transport.issue_number(
                    self.device, property_name, element, chunk,
                    timeout=command_timeout,
                )
                issued = True
                self._wait_pulse_completion(
                    property_name, after_revision=revision,
                    timeout=command_timeout + chunk / 1000.0,
                )
                completed += 1

            snapshot, meridian, current_warnings, refusal = self._preflight()
            last_snapshot = snapshot
            last_meridian = meridian
            warnings.update(current_warnings)
            if refusal:
                raise RuntimeError(refusal)
            return IndiGuidePulseResult(
                normalized, duration_ms, len(chunks), completed, issued, True,
                snapshot.tracking, snapshot.raw_status, meridian.phase,
                tuple(sorted(warnings)), None,
            )
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            stop_error = None
            if issued:
                try:
                    self.emergency_stop()
                except (ConnectionError, RuntimeError, TimeoutError, ValueError) as stop_exc:
                    stop_error = f"; emergency stop failed: {stop_exc}"
            return IndiGuidePulseResult(
                normalized, duration_ms, len(chunks), completed, issued, False,
                bool(last_snapshot and last_snapshot.tracking),
                last_snapshot.raw_status if last_snapshot else None,
                last_meridian.phase if last_meridian else "unknown",
                tuple(sorted(warnings)), f"{exc}{stop_error or ''}",
            )
        finally:
            self._lock.release()

    def _wait_pulse_completion(
        self, property_name: str, *, after_revision: int, timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        revision = after_revision
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"INDI guide pulse {property_name} did not complete")
            current = self.transport.wait_property(
                self.device, property_name, timeout=remaining,
                after_revision=revision,
            )
            revision = current.revision
            state = current.state.lower()
            if state == "alert":
                raise RuntimeError(f"INDI rejected guide pulse {property_name}")
            if state in {"ok", "idle"}:
                return
