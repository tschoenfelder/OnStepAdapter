"""Verified tracking enable through the shared INDI OnStep device."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from .indi_meridian import IndiMeridianState
from .indi_status import IndiMountSnapshot
from .indi_transport import IndiTransport


@dataclass(frozen=True)
class IndiTrackingResult:
    command_accepted: bool
    tracking_confirmed: bool
    consecutive_tracking_polls: int
    final_raw_status: str | None
    meridian_phase: str
    error: str | None = None


def enable_tracking_via_indi(
    transport: IndiTransport,
    device: str,
    *,
    observe: Callable[[], IndiMountSnapshot],
    meridian_status: Callable[[], IndiMeridianState],
    emergency_stop: Callable[[], object],
    timeout: float = 8.0,
) -> IndiTrackingResult:
    """Enable tracking only from a fresh, astronomically authorized state."""
    before = observe()
    meridian = meridian_status()
    blockers = {
        "indi_device_disconnected", "onstep_status_not_fresh",
        "coordinates_not_fresh", "onstep_status_alert",
        "onstep_status_unavailable", "onstep_limit_or_park_fault",
        "onstep_reported_error", "pier_side_conflict", "pier_side_unknown",
        "coordinates_invalid", "time_site_authority_unestablished",
        "hour_angle_unavailable", "home_authority_unestablished",
        "mechanical_terminal_state",
    }.intersection(before.blockers)
    allowed_phases = {
        "pre_meridian_allowed", "post_meridian_allowed", "post_flip",
    }
    if (
        blockers or not before.status_live or not before.coordinates_live or
        not before.time_site_authority or not before.home_authority or
        before.parked or before.at_home or before.slewing or before.at_limit or
        meridian.phase not in allowed_phases or meridian.tracking_stop_required
    ):
        reason = (
            f"tracking preflight refused: blockers={sorted(blockers)} "
            f"phase={meridian.phase}"
        )
        return IndiTrackingResult(
            False, False, 0, before.raw_status, meridian.phase, reason
        )
    if before.tracking:
        return IndiTrackingResult(
            False, True, 2, before.raw_status, meridian.phase, None
        )

    accepted = False
    try:
        transport.set_switch(device, "TELESCOPE_TRACK_STATE", "TRACK_ON")
        accepted = True
        deadline = time.monotonic() + timeout
        stable = 0
        last_revision = before.status_revision
        last_raw = before.raw_status
        while stable < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("Tracking ON was not confirmed by fresh OnStep status")
            time.sleep(0.15)
            current = observe()
            if current.status_revision <= last_revision:
                continue
            last_revision = current.status_revision
            last_raw = current.raw_status
            if current.at_limit or current.parked or current.at_home:
                raise RuntimeError("Unsafe state appeared while enabling tracking")
            current_meridian = meridian_status()
            if (
                current_meridian.phase not in allowed_phases or
                current_meridian.tracking_stop_required
            ):
                raise RuntimeError(
                    f"Meridian state became unsafe: {current_meridian.phase}"
                )
            stable = stable + 1 if current.tracking and not current.slewing else 0
        return IndiTrackingResult(
            True, True, stable, last_raw, meridian.phase, None
        )
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        if accepted:
            emergency_stop()
        return IndiTrackingResult(
            accepted, False, 0, before.raw_status, meridian.phase, str(exc)
        )
