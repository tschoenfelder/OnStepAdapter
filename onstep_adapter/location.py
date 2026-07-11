"""Location helpers for OnStep/LX200 site readback checks."""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in meters between two WGS-84 lat/lon points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def round_lx200_site_degrees(value: float) -> float:
    """Round a signed coordinate to LX200 ``sDD*MM`` arcminute precision."""
    if not math.isfinite(value):
        raise ValueError(f"site coordinate must be finite, got {value!r}")
    sign = -1.0 if value < 0 else 1.0
    rounded_minutes = round(abs(value) * 60.0)
    return sign * (rounded_minutes / 60.0)
