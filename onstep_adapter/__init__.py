"""INDI-only public API for the OnStep adapter."""

from .indi_client import IndiStartupStatus, IndiSyncResult, OnStepIndiClient
from .indi_axis_motion import IndiAxisMoveResult
from .indi_config import IndiRuntimeConfig, load_indi_config
from .indi_focuser import IndiFocuser, IndiFocuserMoveResult, IndiFocuserSnapshot
from .indi_guiding import (
    GUIDE_CHUNK_MS,
    MAX_GUIDE_PULSE_MS,
    MIN_GUIDE_PULSE_MS,
    IndiGuideCalibration,
    IndiGuideCorrectionResult,
    IndiGuidePulseResult,
)
from .indi_home import IndiHomeRouteResult, IndiPositionResult, IndiUnparkResult
from .indi_meridian import IndiMeridianState
from .indi_mode import (
    IndiModeStatus, IndiModeTransitionResult, IndiOperatingMode,
)
from .indi_mount import IndiMount
from .indi_status import IndiMountSnapshot
from .indi_stop import IndiStopResult
from .indi_tracking import IndiTrackingResult
from .indi_terrestrial import IndiTerrestrialMoveResult
from .meridian_policy import MeridianPolicy

OnStepClient = OnStepIndiClient
OnStepMount = IndiMount
OnStepFocuser = IndiFocuser

__version__ = "0.7.0"

__all__ = [
    "IndiFocuserMoveResult",
    "IndiAxisMoveResult",
    "IndiFocuserSnapshot",
    "IndiGuidePulseResult",
    "IndiGuideCalibration",
    "IndiGuideCorrectionResult",
    "IndiHomeRouteResult",
    "IndiMeridianState",
    "IndiModeStatus",
    "IndiModeTransitionResult",
    "IndiOperatingMode",
    "IndiMountSnapshot",
    "IndiPositionResult",
    "IndiRuntimeConfig",
    "IndiStartupStatus",
    "IndiStopResult",
    "IndiSyncResult",
    "IndiTrackingResult",
    "IndiTerrestrialMoveResult",
    "IndiUnparkResult",
    "MeridianPolicy",
    "GUIDE_CHUNK_MS",
    "MAX_GUIDE_PULSE_MS",
    "MIN_GUIDE_PULSE_MS",
    "OnStepClient",
    "OnStepFocuser",
    "OnStepMount",
    "load_indi_config",
    "__version__",
]
