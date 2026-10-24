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
    park_motion_enabled: bool = False
    tracking_authority_policy: str = "strict"
    movement_min_alt_deg: float = -2.0
    movement_min_dec_deg: float = -80.0
    movement_max_dec_deg: float = 80.0
    mode_state_path: str | None = None
    terrestrial_step_deg: float = 10.0
    terrestrial_max_step_deg: float = 10.0
    terrestrial_horizon_margin_deg: float = 2.0
    terrestrial_brake_lead_deg: float = 0.5
    terrestrial_stop_hold_seconds: float = 3.0
    terrestrial_slew_rate_element: str = "9"


def load_indi_config(path: str | Path) -> IndiRuntimeConfig:
    """Load explicit INDI and safety settings; never infer a site or limit."""
    with Path(path).expanduser().open("rb") as stream:
        data = tomllib.load(stream)
    try:
        indi = data["indi"]
        observer = data["observer"]
        meridian = data["meridian"]
        movement = data.get("movement", {})
        terrestrial = data.get("terrestrial", {})
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
            park_motion_enabled=indi.get("park_motion_enabled", False),
            tracking_authority_policy=str(
                indi.get("tracking_authority_policy", "strict")
            ),
            movement_min_alt_deg=float(movement.get("min_alt_deg", -2.0)),
            movement_min_dec_deg=float(movement.get("min_dec_deg", -80.0)),
            movement_max_dec_deg=float(movement.get("max_dec_deg", 80.0)),
            mode_state_path=(
                str(Path(terrestrial["state_file"]).expanduser())
                if "state_file" in terrestrial else None
            ),
            terrestrial_step_deg=float(terrestrial.get("step_deg", 10.0)),
            terrestrial_max_step_deg=float(terrestrial.get("max_step_deg", 10.0)),
            terrestrial_horizon_margin_deg=float(
                terrestrial.get("horizon_margin_deg", 2.0)
            ),
            terrestrial_brake_lead_deg=float(
                terrestrial.get("brake_lead_deg", 0.5)
            ),
            terrestrial_stop_hold_seconds=float(
                terrestrial.get("stop_hold_seconds", 3.0)
            ),
            terrestrial_slew_rate_element=str(
                terrestrial.get("slew_rate_element", "9")
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid INDI configuration: {exc}") from exc
    if not result.host or not result.device or not 1 <= result.port <= 65535:
        raise ValueError("INDI endpoint must have a host, device and valid port")
    if not isinstance(result.safe_meridian_flip_via_home, bool):
        raise ValueError("safe_meridian_flip_via_home must be boolean")
    if not isinstance(result.home_motion_enabled, bool):
        raise ValueError("home_motion_enabled must be boolean")
    if not isinstance(result.park_motion_enabled, bool):
        raise ValueError("park_motion_enabled must be boolean")
    if result.tracking_authority_policy not in {"strict", "controller_managed"}:
        raise ValueError(
            "tracking_authority_policy must be 'strict' or 'controller_managed'"
        )
    numeric = (
        result.observer_lat,
        result.observer_lon,
        result.observer_alt_m,
        result.flip_request_deg,
        result.tracking_stop_deg,
        result.flip_allowance_seconds,
        result.reserve_seconds,
        result.movement_min_alt_deg,
        result.movement_min_dec_deg,
        result.movement_max_dec_deg,
        result.terrestrial_step_deg,
        result.terrestrial_max_step_deg,
        result.terrestrial_horizon_margin_deg,
        result.terrestrial_brake_lead_deg,
        result.terrestrial_stop_hold_seconds,
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
    if not -90.0 <= result.movement_min_alt_deg <= 90.0:
        raise ValueError("Movement minimum altitude must be within -90..90 degrees")
    if not (
        -90.0 <= result.movement_min_dec_deg
        < result.movement_max_dec_deg <= 90.0
    ):
        raise ValueError(
            "Movement DEC limits must satisfy -90 <= min_dec < max_dec <= 90"
        )
    if not 0 < result.terrestrial_step_deg <= result.terrestrial_max_step_deg <= 30:
        raise ValueError("Terrestrial steps must satisfy 0 < step <= max_step <= 30 degrees")
    if not 0 <= result.terrestrial_horizon_margin_deg <= 20:
        raise ValueError("Terrestrial horizon margin must be within 0..20 degrees")
    if not 0 <= result.terrestrial_brake_lead_deg < result.terrestrial_max_step_deg:
        raise ValueError("Terrestrial brake lead must be non-negative and below max step")
    if not 0 <= result.terrestrial_stop_hold_seconds <= 30:
        raise ValueError("Terrestrial stop hold must be within 0..30 seconds")
    if not result.terrestrial_slew_rate_element:
        raise ValueError("Terrestrial slew-rate element must not be empty")
    return result
