"""INDI-only public API for the OnStep adapter."""

from .indi_client import IndiStartupStatus, IndiSyncResult, OnStepIndiClient
from .indi_axis_motion import AxisMotionMode, IndiAxisMoveResult
from .indi_config import IndiRuntimeConfig, load_indi_config
from .indi_focuser import IndiFocuser, IndiFocuserMoveResult, IndiFocuserSnapshot
from .indi_home import IndiHomeRouteResult, IndiPositionResult, IndiUnparkResult
from .indi_meridian import IndiMeridianState
from .indi_mount import IndiMount
from .indi_status import IndiMountSnapshot
from .indi_stop import IndiStopResult
from .indi_tracking import IndiTrackingResult
from .meridian_policy import MeridianPolicy

OnStepClient = OnStepIndiClient
OnStepMount = IndiMount
OnStepFocuser = IndiFocuser

__version__ = "0.4.0"

__all__ = [
    "IndiFocuserMoveResult",
    "AxisMotionMode",
    "IndiAxisMoveResult",
    "IndiFocuserSnapshot",
    "IndiHomeRouteResult",
    "IndiMeridianState",
    "IndiMountSnapshot",
    "IndiPositionResult",
    "IndiRuntimeConfig",
    "IndiStartupStatus",
    "IndiStopResult",
    "IndiSyncResult",
    "IndiTrackingResult",
    "IndiUnparkResult",
    "MeridianPolicy",
    "OnStepClient",
    "OnStepFocuser",
    "OnStepMount",
    "load_indi_config",
    "__version__",
]
