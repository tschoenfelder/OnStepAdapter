"""Safety policy and structured errors for the OnStep adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetySeverity(str, Enum):
    BLOCKED = "blocked"
    LIMIT_HIT = "limit_hit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OnStepLimits:
    horizon_deg: float | None = None
    overhead_deg: float | None = None


@dataclass(frozen=True)
class OverheadCorridor:
    az_center_deg: float
    az_half_width_deg: float
    max_alt_deg: float

    def contains(self, az_deg: float) -> bool:
        delta = ((az_deg - self.az_center_deg + 180.0) % 360.0) - 180.0
        return abs(delta) <= self.az_half_width_deg


@dataclass(frozen=True)
class SafetyViolation:
    reason: str
    command: str
    severity: SafetySeverity = SafetySeverity.BLOCKED
    axis: str | None = None
    current_value: float | int | None = None
    target_value: float | int | None = None
    limit_value: float | int | None = None
    onstep_reply: str | None = None
    recovery_hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = {
            "error": "onstep_safety",
            "reason": self.reason,
            "command": self.command,
            "severity": self.severity.value,
        }
        optional = {
            "axis": self.axis,
            "current_value": self.current_value,
            "target_value": self.target_value,
            "limit_value": self.limit_value,
            "onstep_reply": self.onstep_reply,
            "recovery_hint": self.recovery_hint,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        return data


class OnStepSafetyError(RuntimeError):
    """Raised when the adapter rejects a motion command for safety reasons."""

    def __init__(self, violation: SafetyViolation) -> None:
        self.violation = violation
        super().__init__(violation.reason)


class OnStepLimitError(OnStepSafetyError):
    """Raised when OnStep reports or implies that a hard limit was reached."""


@dataclass(frozen=True)
class OnStepSafetyConfig:
    observer_lat: float
    observer_lon: float
    min_alt_deg: float
    max_alt_deg: float
    ha_east_limit_h: float
    ha_west_limit_h: float
    observer_alt_m: float = 0.0
    time_offset_s: float = 0.0
    dec_min_deg: float = -90.0
    dec_max_deg: float = 90.0
    horizon_path: str = ""
    require_home_confirmation: bool = True
    meridian_margin_deg: float = 3.0
    sync_limits_to_onstep: bool = False
    configured_horizon_limit_deg: float | None = None
    configured_overhead_limit_deg: float | None = None
    focuser_min_position: int = 0
    focuser_max_position: int = 50000
    indi_fallback_enabled: bool = False
    indi_host: str = "localhost"
    indi_port: int = 7624
    indi_device: str = "OnStep"
    state_file: str = ""
    state_write_interval_s: float = 30.0
    clock_warning_threshold_s: float = 120.0
    time_trust_source: str = "raspberry_plausible"
    require_onstep_limits: bool = False
    allow_broad_onstep_limits: bool = False
    location_warning_threshold_m: float = 100.0
    altitude_warning_threshold_m: float = 50.0
    mechanical_axis1_min_deg: float | None = None
    mechanical_axis1_max_deg: float | None = None
    mechanical_axis2_min_deg: float | None = None
    mechanical_axis2_max_deg: float | None = None
    mechanical_calibration_file: str = ""
    firmware_proof_file: str = ""
    home_park_settle_s: float = 15.0
    safe_overhead_corridors: tuple[OverheadCorridor, ...] = ()
