"""pytest fixtures: ``onstep_sim`` (the virtual mount) and ``onstep_client``.

Enable in the top-level conftest.py with ``pytest_plugins = ["onstep_mock.pytest_plugin"]``,
or import the fixtures into a nested conftest.py.
Override ``onstep_sim_config`` to change the virtual mount's behavior.
"""

from __future__ import annotations

import pytest

from onstep_adapter import OnStepClient

from .mount import SimConfig
from .server import SimulatedOnStepServer
from .testing import runtime_config, wait_for_live_status


@pytest.fixture
def onstep_sim_config() -> SimConfig:
    return SimConfig(speed=20.0)


@pytest.fixture
def onstep_sim(onstep_sim_config):
    with SimulatedOnStepServer(onstep_sim_config) as sim:
        yield sim


@pytest.fixture
def onstep_client(onstep_sim):
    with OnStepClient(config=runtime_config(onstep_sim)) as client:
        wait_for_live_status(client)
        yield client
