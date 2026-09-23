"""Derive operational meridian boundaries from controller guard limits."""

from __future__ import annotations

from dataclasses import dataclass
import math


SIDEREAL_DEGREES_PER_SECOND = 360.0 / 86164.0905


@dataclass(frozen=True)
class MeridianPolicy:
    firmware_guard_deg: float
    flip_request_deg: float
    hard_stop_deg: float
    seconds_flip_to_stop: float
    max_exposure_seconds_at_flip: float
    flip_allowance_seconds: float
    reserve_seconds: float


def derive_meridian_policy(
    *,
    east_guard_minutes: float,
    west_guard_minutes: float,
    flip_lead_deg: float = 1.0,
    stop_margin_deg: float = 0.25,
    flip_allowance_seconds: float = 120.0,
    reserve_seconds: float = 30.0,
    flip_request_deg: float | None = None,
    hard_stop_deg: float | None = None,
) -> MeridianPolicy:
    """Use the stricter guard until the active pier branch is proven.

    INDI reports minutes of time; four minutes of time are one degree of HA.
    A policy is rejected when there is insufficient time to flip and reserve.
    """
    values = (
        east_guard_minutes,
        west_guard_minutes,
        flip_lead_deg,
        stop_margin_deg,
        flip_allowance_seconds,
        reserve_seconds,
    )
    values += tuple(value for value in (flip_request_deg, hard_stop_deg) if value is not None)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Meridian policy inputs must be finite")
    if min(east_guard_minutes, west_guard_minutes) <= 0:
        raise ValueError("Both firmware guard limits must be positive and readable")
    if flip_lead_deg <= stop_margin_deg or stop_margin_deg <= 0:
        raise ValueError("The flip lead must exceed the stop margin")
    if flip_allowance_seconds <= 0 or reserve_seconds < 0:
        raise ValueError("Flip allowance must be positive and reserve nonnegative")

    guard_deg = min(east_guard_minutes, west_guard_minutes) / 4.0
    flip_deg = guard_deg - flip_lead_deg if flip_request_deg is None else flip_request_deg
    stop_deg = guard_deg - stop_margin_deg if hard_stop_deg is None else hard_stop_deg
    if not 0 < flip_deg < stop_deg < guard_deg:
        raise ValueError("Configured flip/stop must be ordered below both firmware guards")

    seconds_to_stop = (stop_deg - flip_deg) / SIDEREAL_DEGREES_PER_SECOND
    exposure_budget = seconds_to_stop - flip_allowance_seconds - reserve_seconds
    if exposure_budget < 0:
        raise ValueError("Firmware guard leaves insufficient time for flip and reserve")

    return MeridianPolicy(
        firmware_guard_deg=guard_deg,
        flip_request_deg=flip_deg,
        hard_stop_deg=stop_deg,
        seconds_flip_to_stop=seconds_to_stop,
        max_exposure_seconds_at_flip=exposure_budget,
        flip_allowance_seconds=flip_allowance_seconds,
        reserve_seconds=reserve_seconds,
    )
