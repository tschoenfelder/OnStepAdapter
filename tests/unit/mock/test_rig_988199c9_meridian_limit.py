"""A move OnStep stops at its meridian limit, and the way back out (field diag 988199c9).

2026-11-20: a 20 deg RA step ran into "Meridian Limit (W) Exceeded" after 6.5 deg. OnStep
stopped the axis but left the motion switch Busy; the drive kept the switch On for 40 s
until the operator pressed Stop. OnStep kept reporting the error, so every command -- PARK
too -- was refused, with no way out offered.
"""

from __future__ import annotations

import time

import pytest

from onstep_mock import SimConfig, SimulatedOnStepServer


@pytest.fixture
def limit_sim():
    # The RA axis may go up to HA -2.6 h: from -3 h a 20 deg West step meets the limit at 6 deg.
    config = SimConfig.rig_2eb2a5cb(ha_limits_h=(-8.0, -2.6), slew_deg_per_s=40.0)
    with SimulatedOnStepServer(config) as sim:
        yield sim


@pytest.fixture
def limit_client(limit_sim):
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    limit_sim.place(ha_h=-3.0, dec_deg=50.0, parked=False)
    with OnStepClient(config=runtime_config(limit_sim)) as client:
        wait_for_live_status(client)
        client.mount.move_dec_axis_deg(1.0).wait(timeout=60)  # DEC first defines RA
        yield client


def _ra_writes(sim) -> list[dict[str, str]]:
    return [w.values for w in sim.writes if w.property == "TELESCOPE_MOTION_WE"]


def test_a_move_stopped_at_the_limit_ends_at_once_and_names_the_limit(limit_sim, limit_client):
    started = time.monotonic()
    with pytest.raises(Exception, match=r"Meridian Limit \(W\) Exceeded.*other way"):
        limit_client.mount.move_ra_axis_deg(20.0).wait(timeout=120)
    elapsed = time.monotonic() - started
    assert elapsed < 20.0, elapsed  # was: the switch stayed On for 40 s
    assert _ra_writes(limit_sim)[-1] == {"MOTION_WEST": "Off"}
    assert limit_sim.mount.motion == "IDLE"
    assert limit_sim._mount.limit_error == "W"


def test_only_the_move_back_is_allowed_and_then_park_works_again(limit_sim, limit_client):
    with pytest.raises(Exception, match=r"Meridian Limit"):
        limit_client.mount.move_ra_axis_deg(20.0).wait(timeout=120)
    writes = len(limit_sim.writes)
    with pytest.raises(RuntimeError, match=r"only a RA move the other way \(MOTION_EAST\)"):
        limit_client.mount.move_ra_axis_deg(5.0).wait(timeout=60)  # further out: refused
    assert len(limit_sim.writes) == writes
    refused = limit_client.mount.park().wait(timeout=60)  # PARK says why, and what to do
    assert not refused.confirmed
    assert "Meridian Limit (W) Exceeded" in (refused.error or "")
    assert "back the way it came" in (refused.error or "")

    result = limit_client.mount.move_ra_axis_deg(-5.0).wait(timeout=120)  # back inside
    assert result.stop_confirmed
    assert limit_sim._mount.limit_error is None
    assert limit_client.mount.park().wait(timeout=120).confirmed
    assert limit_sim.mount.parked
