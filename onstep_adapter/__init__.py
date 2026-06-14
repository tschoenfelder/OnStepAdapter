"""Public import surface for the reusable OnStep adapter SDK.

Import from the defining modules instead of relying on the compatibility
package's ``__init__`` exports. This also works when a directory deployment
leaves ``smart_telescope.adapters.onstep`` as a namespace package.
"""

from smart_telescope.adapters.onstep.client import OnStepClient
from smart_telescope.adapters.onstep.focuser import OnStepFocuser
from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.results import (
    AxisMotionResult,
    FocuserMoveResult,
    FocuserStatus,
    OnStepConnectionResult,
    OnStepMotionCalibration,
    SetParkPositionResult,
    StoredParkPosition,
)
from smart_telescope.adapters.onstep.safety import (
    OnStepSafetyConfig,
    OnStepSafetyError,
    SafetySeverity,
    SafetyViolation,
)

__version__ = "0.3.0"

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
    "SafetySeverity",
    "SafetyViolation",
    "SetParkPositionResult",
    "StoredParkPosition",
    "__version__",
]
