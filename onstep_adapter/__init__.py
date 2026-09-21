"""Public import surface for the reusable OnStep adapter SDK."""

from onstep_adapter.client import OnStepClient
from onstep_adapter.focuser import OnStepFocuser
from onstep_adapter.location import haversine_distance_m, round_lx200_site_degrees
from onstep_adapter.mount import OnStepMount
from onstep_adapter.results import (
    AxisMotionResult,
    FocuserMoveResult,
    FocuserStatus,
    OnStepConnectionResult,
    OnStepMotionCalibration,
    SetParkPositionResult,
    StoredParkPosition,
)
from onstep_adapter.safety import (
    OnStepSafetyConfig,
    OnStepSafetyError,
    SafetySeverity,
    SafetyViolation,
)

__version__ = "0.3.5"

__all__ = [
    "AxisMotionResult",
    "FocuserMoveResult",
    "FocuserStatus",
    "OnStepClient",
    "OnStepConnectionResult",
    "OnStepFocuser",
    "OnStepMount",
    "OnStepMotionCalibration",
    "OnStepSafetyConfig",
    "OnStepSafetyError",
    "haversine_distance_m",
    "round_lx200_site_degrees",
    "SafetySeverity",
    "SafetyViolation",
    "SetParkPositionResult",
    "StoredParkPosition",
    "__version__",
]
