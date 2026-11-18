"""DEC moves never run past the DEC bound (field capture fd84c75f, 2026-11-18).

From PARK (DEC 51.8, pier side east) a +20 deg DEC step went to 71.9 deg; NORTH raised DEC
there. The next +20 deg step was projected as "positive lowers DEC" (51.9 deg, inside the
+-80 deg bound) and started; the in-motion bound check stopped it at 80.0 deg and the axis
coasted on to 88.3 deg. Now the projection uses the direction the last DEC move measured
(both directions while it is unknown), and a pass stops so that its coast ends at the bound.
"""

from __future__ import annotations

import pytest

from onstep_mock import SimConfig, SimulatedOnStepServer


@pytest.fixture
def rig_sim():
    with SimulatedOnStepServer(SimConfig.rig_2eb2a5cb()) as sim:
        yield sim


def _client(sim, *, ha_h: float, dec_deg: float, pier_side: str):
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    sim.place(ha_h=ha_h, dec_deg=dec_deg, parked=False, pier_side=pier_side)
    client = OnStepClient(config=runtime_config(sim))
    client.__enter__()
    wait_for_live_status(client)
    return client


def _motion_writes(sim) -> int:
    return sum(1 for w in sim.writes if w.property == "TELESCOPE_MOTION_NS")


def test_a_step_toward_the_bound_is_refused_once_the_direction_is_measured(rig_sim):
    client = _client(rig_sim, ha_h=-2.0, dec_deg=51.8, pier_side="west")  # NORTH raises DEC
    try:
        client.mount.move_dec_axis_deg(20.0).wait(timeout=120)
        assert rig_sim.mount.dec_deg == pytest.approx(71.8, abs=0.6)
        writes = _motion_writes(rig_sim)
        with pytest.raises(RuntimeError, match=r"outside configured bounds"):
            client.mount.move_dec_axis_deg(20.0).wait(timeout=120)
        assert _motion_writes(rig_sim) == writes  # nothing was sent
        assert rig_sim.mount.dec_deg <= 80.0
    finally:
        client.__exit__(None, None, None)


def test_while_the_direction_is_unknown_both_ways_must_stay_inside(rig_sim):
    client = _client(rig_sim, ha_h=-2.0, dec_deg=72.0, pier_side="east")  # NORTH lowers DEC
    try:
        writes = _motion_writes(rig_sim)
        with pytest.raises(RuntimeError, match=r"both ways must stay inside"):
            client.mount.move_dec_axis_deg(15.0).wait(timeout=120)  # 87 or 57
        assert _motion_writes(rig_sim) == writes
        client.mount.move_dec_axis_deg(1.0).wait(timeout=120)  # measures: NORTH lowers here
        client.mount.move_dec_axis_deg(15.0).wait(timeout=120)  # now known: towards 56
        assert rig_sim.mount.dec_deg == pytest.approx(56.0, abs=0.6)
    finally:
        client.__exit__(None, None, None)


def test_a_move_that_runs_toward_the_bound_stops_so_it_coasts_no_further(rig_sim):
    """Even with a wrong projection the axis must not coast past the bound (fd84c75f: 8 deg)."""
    client = _client(rig_sim, ha_h=-2.0, dec_deg=62.0, pier_side="west")  # NORTH raises DEC
    try:
        mover = client._axis_mover
        assert mover is not None
        mover._north_raises_dec = ("west", False)  # the wrong belief fd84c75f acted on
        with pytest.raises(RuntimeError, match=r"would have passed the configured DEC bound"):
            client.mount.move_dec_axis_deg(20.0).wait(timeout=120)
        assert rig_sim.mount.motion == "IDLE"
        assert 70.0 < rig_sim.mount.dec_deg <= 80.0
    finally:
        client.__exit__(None, None, None)
