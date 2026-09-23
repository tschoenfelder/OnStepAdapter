"""Verified INDI mount stop; never equate switch acknowledgement with cessation."""

from __future__ import annotations

from dataclasses import dataclass
import time

from .indi_transport import IndiTransport
from .indi_gu import decode_gu


@dataclass(frozen=True)
class IndiStopResult:
    abort_accepted: bool
    tracking_off_accepted: bool
    stopped_confirmed: bool
    consecutive_stopped_polls: int
    last_raw_status: str | None
    errors: tuple[str, ...]


def stop_mount_via_indi(
    transport: IndiTransport,
    device: str,
    *,
    command_timeout: float = 3.0,
    confirmation_timeout: float = 5.0,
) -> IndiStopResult:
    """Abort manual/goto motion, disable tracking, then verify live raw GU.

    The two commands are attempted independently, including when one fails.
    This does not prevent a different INDI client from restarting motion.
    """
    errors: list[str] = []
    abort_accepted = False
    tracking_off_accepted = False
    try:
        transport.issue_switch(
            device, "TELESCOPE_ABORT_MOTION", "ABORT", timeout=command_timeout
        )
        abort_accepted = True
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        errors.append(f"abort: {exc}")
    try:
        transport.issue_switch(
            device, "TELESCOPE_TRACK_STATE", "TRACK_OFF", timeout=command_timeout
        )
        tracking_off_accepted = True
    except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
        errors.append(f"tracking_off: {exc}")

    deadline = time.monotonic() + confirmation_timeout
    current = transport.get_property(device, "OnStep Status")
    revision = current.revision if current is not None else -1
    stopped_polls = 0
    last_raw = None
    while stopped_polls < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("stop_confirmation_timeout")
            break
        try:
            current = transport.wait_property(
                device, "OnStep Status", timeout=remaining, after_revision=revision
            )
        except (ConnectionError, TimeoutError) as exc:
            errors.append(f"status: {exc}")
            break
        revision = current.revision
        last_raw = current.values.get(":GU# return")
        try:
            decoded = decode_gu(last_raw) if last_raw else {}
        except ValueError:
            decoded = {}
        if (current.state.lower() != "alert" and decoded and
                not decoded["tracking"] and not decoded["slewing"] and
                not decoded["park_failed"]):
            stopped_polls += 1
        else:
            stopped_polls = 0
    return IndiStopResult(
        abort_accepted=abort_accepted,
        tracking_off_accepted=tracking_off_accepted,
        stopped_confirmed=stopped_polls >= 2 and not errors,
        consecutive_stopped_polls=stopped_polls,
        last_raw_status=last_raw,
        errors=tuple(errors),
    )
