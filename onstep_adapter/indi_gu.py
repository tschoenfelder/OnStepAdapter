"""Decode controller-reported compact OnStep status carried by INDI."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re


def decode_gu(value: str) -> dict[str, object]:
    raw = value.strip().rstrip("#")
    if (
        not raw or "|" in raw or not re.fullmatch(r"[A-Za-z]*\d{3}", raw) or
        not any(flag in raw for flag in "pIPF")
    ):
        raise ValueError("Compact OnStep :GU# status is required")
    flags = set(raw)
    parked = "P" in flags
    park_failed = "F" in flags
    at_limit = "l" in flags
    tracking = "n" not in flags
    slewing = "N" not in flags
    at_home = "H" in flags
    guiding = "G" in flags
    pier_side = "east" if "T" in flags else "west" if "W" in flags else None
    if parked:
        motion_state = "parked"
    elif park_failed:
        motion_state = "park_failed"
    elif at_limit:
        motion_state = "at_limit"
    elif slewing:
        motion_state = "slewing"
    elif tracking:
        motion_state = "tracking"
    elif at_home:
        motion_state = "home"
    else:
        motion_state = "unparked"
    return {
        "raw": raw,
        "parked": parked,
        "not_parked": "p" in flags and not parked,
        "park_failed": park_failed,
        "at_limit": at_limit,
        "tracking": tracking,
        "slewing": slewing,
        "not_slewing": not slewing,
        "at_home": at_home,
        "guiding": guiding,
        "pier_side": pier_side,
        "motion_state": motion_state,
    }


def local_sidereal_hours(observer_lon: float, now: datetime) -> float:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware time is required")
    utc = now.astimezone(timezone.utc)
    julian_days = 2440587.5 + utc.timestamp() / 86400.0
    gmst = 18.697374558 + 24.06570982441908 * (julian_days - 2451545.0)
    return (gmst + observer_lon / 15.0) % 24.0


def equatorial_to_horizontal(
    ra_hours: float,
    dec_deg: float,
    observer_lat_deg: float,
    observer_lon_deg: float,
    now: datetime,
) -> tuple[float, float]:
    """Derive apparent geometric altitude/azimuth from INDI sky coordinates."""
    hour_angle = math.radians(
        ((local_sidereal_hours(observer_lon_deg, now) - ra_hours + 12.0) % 24.0 - 12.0)
        * 15.0
    )
    declination = math.radians(dec_deg)
    latitude = math.radians(observer_lat_deg)
    sin_altitude = (
        math.sin(declination) * math.sin(latitude)
        + math.cos(declination) * math.cos(latitude) * math.cos(hour_angle)
    )
    altitude = math.asin(max(-1.0, min(1.0, sin_altitude)))
    y = -math.sin(hour_angle) * math.cos(declination)
    x = (
        math.sin(declination) * math.cos(latitude)
        - math.cos(declination) * math.sin(latitude) * math.cos(hour_angle)
    )
    azimuth = math.atan2(y, x)
    return math.degrees(altitude), math.degrees(azimuth) % 360.0
