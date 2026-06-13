"""Public import surface for the reusable OnStep adapter SDK."""

from smart_telescope.adapters.onstep import (
    FocuserMoveResult,
    FocuserStatus,
    OnStepClient,
    OnStepConnectionResult,
    OnStepFocuser,
    OnStepMount,
    OnStepSafetyConfig,
    OnStepSafetyError,
    SafetySeverity,
    SafetyViolation,
)

__version__ = "0.2.0"

__all__ = [
    "FocuserMoveResult",
    "FocuserStatus",
    "OnStepClient",
    "OnStepConnectionResult",
    "OnStepFocuser",
    "OnStepMount",
    "OnStepSafetyConfig",
    "OnStepSafetyError",
    "SafetySeverity",
    "SafetyViolation",
    "__version__",
]
