"""1/5/20 degree steps on the rig model (`SimConfig.rig_2eb2a5cb`), in real time.

Field capture 2eb2a5cb (2026-11-18): a 10 deg DEC move overshot to +26 deg, swung back and
forth and gave up after 8 passes, because (a) the live stop never ran -- the driver publishes
no OnStep Status while a motion switch is On, and the drive skipped its stop check on every
such sample -- (b) the stop allowed ~0.5 deg for braking where the rig needs ~10 deg, and (c)
"at rest" was taken from :GU#, which never flags manual motion, so a pass was measured while
the axis still coasted. The model reproduces that capture (test_rig_2eb2a5cb_kinematics).
"""

from __future__ import annotations

import time

import pytest

from onstep_mock import SimConfig, SimulatedOnStepServer

#: Operator request 2026-11-18: RA/Dec steps of 1, 5 and 20 degrees.
STEPS_DEG = (1.0, 5.0, 20.0)


@pytest.fixture
def rig_sim():
    with SimulatedOnStepServer(SimConfig.rig_2eb2a5cb()) as sim:
        yield sim


@pytest.fixture
def rig_client(rig_sim):
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    rig_sim.place(ha_h=-3.0, dec_deg=30.0, parked=False)
    with OnStepClient(config=runtime_config(rig_sim)) as client:
        wait_for_live_status(client)
        yield client


def _passes(sim, prop: str) -> int:
    return sum(
        1 for w in sim.writes if w.property == prop and "On" in (w.values or {}).values()
    )


def _motion_s(degrees: float) -> float:
    """Rig time for a step: reaction, speeding up and braking at ~0.55 deg/s^2, 3.27 deg/s."""
    a, v = 0.55, 3.27
    ramp = v * v / a  # angle used by speeding up AND braking at full speed
    if degrees <= ramp:
        return 0.4 + 2.0 * (degrees / a) ** 0.5
    return 0.4 + 2.0 * v / a + (degrees - ramp) / v


@pytest.mark.parametrize("degrees", STEPS_DEG)
def test_a_dec_step_lands_on_target_promptly_and_never_moves_ra(rig_sim, rig_client, degrees):
    dec_before, ha_before = rig_sim.mount.dec_deg, rig_sim.mount.ha_h
    started = time.monotonic()
    result = rig_client.mount.move_dec_axis_deg(degrees).wait(timeout=120)
    elapsed = time.monotonic() - started
    moved = abs(rig_sim.mount.dec_deg - dec_before)
    passes = _passes(rig_sim, "TELESCOPE_MOTION_NS")
    assert result.stop_confirmed
    assert moved == pytest.approx(degrees, abs=degrees * 0.03), (moved, passes)
    assert rig_sim.mount.ha_h == ha_before
    assert passes <= 2, passes
    # One move plus at most one short correction, each followed by the at-rest check
    # (two readings at the rig's 1.5 s idle cadence).
    assert elapsed < _motion_s(degrees) + 9.0, (elapsed, passes)


@pytest.mark.parametrize("degrees", STEPS_DEG)
def test_an_ra_step_lands_on_target_promptly_and_never_moves_dec(rig_sim, rig_client, degrees):
    rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)  # DEC first defines the RA axis
    dec_before, ha_before = rig_sim.mount.dec_deg, rig_sim.mount.ha_h
    started = time.monotonic()
    result = rig_client.mount.move_ra_axis_deg(degrees).wait(timeout=120)
    elapsed = time.monotonic() - started
    moved = abs(rig_sim.mount.ha_h - ha_before) * 15.0
    passes = _passes(rig_sim, "TELESCOPE_MOTION_WE")
    assert result.stop_confirmed
    assert moved == pytest.approx(degrees, abs=degrees * 0.03), (moved, passes)
    assert rig_sim.mount.dec_deg == dec_before
    assert passes <= 2, passes
    assert elapsed < _motion_s(degrees) + 9.0, (elapsed, passes)


def test_repeated_steps_use_the_learned_braking_and_need_one_pass(rig_sim, rig_client):
    """The first move learns the braking from its coast; the next ones need no correction."""
    for _ in range(3):
        rig_client.mount.move_dec_axis_deg(5.0).wait(timeout=120)
    before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
    dec_before = rig_sim.mount.dec_deg
    rig_client.mount.move_dec_axis_deg(-5.0).wait(timeout=120)
    assert _passes(rig_sim, "TELESCOPE_MOTION_NS") - before == 1
    assert abs(rig_sim.mount.dec_deg - dec_before) == pytest.approx(5.0, abs=0.15)


def test_small_steps_land_in_one_pass_although_the_first_fast_reading_comes_late(
    rig_sim, rig_client
):
    """Field capture 2026-11-18 21:00: five 1 degree DEC steps; two overshot by 0.17-0.28 deg
    and needed one or two correction passes. After a switch turns On the driver keeps its
    1.5 s cycle until its next poll, so a 1 degree step is nearly over before the first fast
    reading: the stop must come from the learned speed-up, not wait for readings."""
    rig_client.mount.move_dec_axis_deg(5.0).wait(timeout=120)  # learns braking + speed-up
    for pause_s in (0.0, 0.3, 0.6, 0.9, 1.2):  # the press falls anywhere in the poll cycle
        time.sleep(pause_s)
        before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
        dec_before = rig_sim.mount.dec_deg
        started = time.monotonic()
        rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)
        elapsed = time.monotonic() - started
        passes = _passes(rig_sim, "TELESCOPE_MOTION_NS") - before
        moved = abs(rig_sim.mount.dec_deg - dec_before)
        assert moved == pytest.approx(1.0, abs=0.03), (moved, passes)
        assert passes == 1, (moved, passes)
        assert elapsed < 7.0, elapsed
