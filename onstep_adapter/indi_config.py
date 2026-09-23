"""Per-installation configuration for the planned INDI client."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class IndiRuntimeConfig:
    host: str
    port: int
    device: str
    observer_lat: float
    observer_lon: float
    observer_alt_m: float
    flip_request_deg: float
    tracking_stop_deg: float
    flip_allowance_seconds: float
    reserve_seconds: float
    safe_meridian_flip_via_home: bool
    focuser_max_position: int | None = None
    home_motion_enabled: bool = False


def load_indi_config(path: str | Path) -> IndiRuntimeConfig:
    """Load explicit INDI and safety settings; never infer a site or limit."""
    with Path(path).expanduser().open("rb") as stream:
        data = tomllib.load(stream)
    try:
        indi = data["indi"]
        observer = data["observer"]
        meridian = data["meridian"]
        result = IndiRuntimeConfig(
            host=str(indi["host"]),
            port=int(indi["port"]),
            device=str(indi["device"]),
            observer_lat=float(observer["lat"]),
            observer_lon=float(observer["lon"]),
            observer_alt_m=float(observer["alt_m"]),
            flip_request_deg=float(meridian["flip_request_deg"]),
            tracking_stop_deg=float(meridian["tracking_stop_deg"]),
            flip_allowance_seconds=float(meridian["flip_allowance_seconds"]),
            reserve_seconds=float(meridian["reserve_seconds"]),
            safe_meridian_flip_via_home=indi["safe_meridian_flip_via_home"],
            focuser_max_position=(
                int(data["focuser"]["max_position"])
                if "focuser" in data and "max_position" in data["focuser"] else None
            ),
            home_motion_enabled=indi.get("home_motion_enabled", False),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid INDI configuration: {exc}") from exc
    if not result.host or not result.device or not 1 <= result.port <= 65535:
        raise ValueError("INDI endpoint must have a host, device and valid port")
    if not isinstance(result.safe_meridian_flip_via_home, bool):
        raise ValueError("safe_meridian_flip_via_home must be boolean")
    if not isinstance(result.home_motion_enabled, bool):
        raise ValueError("home_motion_enabled must be boolean")
    numeric = (
        result.observer_lat,
        result.observer_lon,
        result.observer_alt_m,
        result.flip_request_deg,
        result.tracking_stop_deg,
        result.flip_allowance_seconds,
        result.reserve_seconds,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("INDI configuration numeric values must be finite")
    if not -90 <= result.observer_lat <= 90 or not -180 <= result.observer_lon <= 180:
        raise ValueError("Observer latitude or longitude is out of range")
    if not 0 < result.flip_request_deg < result.tracking_stop_deg:
        raise ValueError("Meridian flip must precede the positive hard stop")
    if result.flip_allowance_seconds <= 0 or result.reserve_seconds < 0:
        raise ValueError("Flip allowance/reserve is invalid")
    if result.focuser_max_position is not None and result.focuser_max_position <= 0:
        raise ValueError("Configured focuser maximum must be positive")
    return result
