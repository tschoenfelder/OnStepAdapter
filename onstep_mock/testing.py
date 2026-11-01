"""Helpers for connecting the real adapter to the simulator (no pytest needed)."""

from __future__ import annotations

import time

from onstep_adapter.indi_config import IndiRuntimeConfig

from .server import DEVICE, SimulatedOnStepServer


def runtime_config(sim: SimulatedOnStepServer, **overrides) -> IndiRuntimeConfig:
    """An adapter configuration pointing at the simulator's port."""
    values = dict(
        host=sim.host, port=sim.port, device=DEVICE,
        observer_lat=sim.config.observer_lat, observer_lon=sim.config.observer_lon,
        observer_alt_m=304.0, flip_request_deg=1.0, tracking_stop_deg=1.75,
        flip_allowance_seconds=120.0, reserve_seconds=30.0,
        safe_meridian_flip_via_home=True, home_motion_enabled=True,
        focuser_max_position=50000,
    )
    values.update(overrides)
    return IndiRuntimeConfig(**values)


def wait_for_live_status(client, timeout: float = 3.0) -> None:
    """Wait until the adapter calls status and coordinates live.

    The adapter accepts a property as live only after an update newer than the
    first one it read, as on the real rig. The simulator republishes every
    0.25 s, so this normally returns within half a second.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.mount.get_status()
        if status.status_live and status.coordinates_live:
            return
        time.sleep(0.05)
    raise TimeoutError("Adapter did not see live status from the simulator")
