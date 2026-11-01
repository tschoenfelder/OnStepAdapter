"""Virtual OnStepX mount for testing applications that use onstep_adapter.

The mock sits below the adapter: ``SimulatedOnStepServer`` is a local INDI
server with a virtual German equatorial mount behind it. Point an unchanged
``onstep_adapter.OnStepClient`` at its host and port, drive it through the
public adapter API, and inspect ``server.mount`` and ``server.writes``.
"""

from .mount import MountSnapshot, Pose, SimConfig, VirtualMount, altaz
from .server import DEVICE, IndiWrite, SimulatedOnStepServer

__all__ = [
    "DEVICE",
    "IndiWrite",
    "MountSnapshot",
    "Pose",
    "SimConfig",
    "SimulatedOnStepServer",
    "VirtualMount",
    "altaz",
]
