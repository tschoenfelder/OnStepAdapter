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


def _landing(degrees: float, axis: str = "dec") -> float:
    """How close a step must land: the drive's own tolerance (1.5 %, at least 0.06 deg for a
    small step -- field captures 9e7fd012/2c63af9f), in degrees."""
    from onstep_adapter.indi_axis_drive import drive_tolerance_arcsec

    return drive_tolerance_arcsec(axis, degrees * 3600.0) / 3600.0 + 1e-9


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
    assert moved == pytest.approx(degrees, abs=_landing(degrees)), (moved, passes)
    assert rig_sim.mount.ha_h == ha_before
    assert passes <= 2, passes
    # One move plus at most one short correction, each followed by the at-rest check
    # (two readings; POLLING_PERIOD is 250 ms during the move).
    assert elapsed < _motion_s(degrees) + 6.5, (elapsed, passes)


@pytest.mark.parametrize("degrees", STEPS_DEG)
def test_an_ra_step_lands_on_target_promptly_and_never_moves_dec(rig_sim, rig_client, degrees):
    """One RA move (plus at most one correction), within 0.5 %."""
    rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)  # DEC first defines the RA axis
    dec_before, ha_before = rig_sim.mount.dec_deg, rig_sim.mount.ha_h
    started = time.monotonic()
    result = rig_client.mount.move_ra_axis_deg(degrees).wait(timeout=120)
    elapsed = time.monotonic() - started
    moved = abs(rig_sim.mount.ha_h - ha_before) * 15.0
    passes = _passes(rig_sim, "TELESCOPE_MOTION_WE")
    assert result.stop_confirmed
    assert moved == pytest.approx(degrees, abs=_landing(degrees)), (moved, passes)
    assert rig_sim.mount.dec_deg == dec_before
    assert passes <= 2, passes
    assert elapsed < _motion_s(degrees) + 6.5, (elapsed, passes)


def test_repeated_steps_use_the_learned_braking(rig_sim, rig_client):
    """The first move learns the braking from its coast; later ones need at most one short
    correction to land within 0.5 %."""
    for _ in range(3):
        rig_client.mount.move_dec_axis_deg(5.0).wait(timeout=120)
    before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
    dec_before = rig_sim.mount.dec_deg
    rig_client.mount.move_dec_axis_deg(-5.0).wait(timeout=120)
    assert _passes(rig_sim, "TELESCOPE_MOTION_NS") - before <= 2
    assert abs(rig_sim.mount.dec_deg - dec_before) == pytest.approx(5.0, abs=_landing(5.0))


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
        assert moved == pytest.approx(1.0, abs=_landing(1.0)), (moved, passes)
        assert passes <= 2, (moved, passes)  # at most one short correction for 0.5 %
        # Operator (21:58): the wait must fit the step -- with POLLING_PERIOD at 250 ms during
        # the move a 1 deg step (correction included) is done in a few seconds.
        assert elapsed < 7.0, elapsed  # ~3.9 s; a first learning step may add a correction


def test_a_move_runs_at_fast_polling_and_restores_the_operator_period(rig_sim, rig_client):
    """Operator (2026-11-18 21:58): the wait after a step must fit the step -- POLLING_PERIOD
    is 250 ms while a move runs and the operator's own value again afterwards."""
    rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)
    sent = [w.values for w in rig_sim.writes if w.property == "POLLING_PERIOD"]
    assert [float(v["PERIOD_MS"]) for v in sent] == [250.0, 1000.0]
    assert float(rig_sim._props["POLLING_PERIOD"].elements["PERIOD_MS"]) == 1000.0


def test_what_a_drive_learned_survives_a_reconnect(rig_sim, rig_client):
    """Field capture 9e7fd012: every connect started from the assumptions again, so the first
    1 deg steps landed 3-10 % short. A new connection to the same mount keeps what was learned."""
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    for _ in range(2):
        rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)
    rig_client.close()
    with OnStepClient(config=runtime_config(rig_sim)) as again:
        wait_for_live_status(again)
        before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
        dec_before = rig_sim.mount.dec_deg
        again.mount.move_dec_axis_deg(-1.0).wait(timeout=60)
        assert _passes(rig_sim, "TELESCOPE_MOTION_NS") - before == 1
        assert abs(rig_sim.mount.dec_deg - dec_before) == pytest.approx(1.0, abs=_landing(1.0))


def test_a_small_step_is_done_soon_after_the_coast(rig_sim, rig_client):
    """Field capture 2c63af9f: rest was proven only ~3.3 s after Off (two equal readings
    0.77-1.5 s apart) although the axis rested after ~1.3 s; and a +-6 % landing then often
    added a correction pass. A 1 deg step now ends one reading after its expected coast."""
    rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)  # learns
    for degrees in (-1.0, 1.0, -1.0):
        before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
        started = time.monotonic()
        rig_client.mount.move_dec_axis_deg(degrees).wait(timeout=60)
        elapsed = time.monotonic() - started
        assert _passes(rig_sim, "TELESCOPE_MOTION_NS") - before == 1
        assert elapsed < 5.5, elapsed  # was ~6-7 s with a correction pass


def test_small_steps_stay_single_with_the_rigs_irregular_readings(rig_sim, rig_client):
    """Field capture be8c66bf (0.7.23): readings after Off came 0.77 s or 1.5 s apart; a single
    late reading taken mid-coast counted as rest, the braking learned from it drifted and every
    1 deg step then landed 0.87 / 1.12 deg and needed a correction. Eight 1 deg steps back and
    forth must each take one pass."""
    rig_client.mount.move_dec_axis_deg(5.0).wait(timeout=60)
    for degrees in (1.0, -1.0) * 4:
        before = _passes(rig_sim, "TELESCOPE_MOTION_NS")
        dec_before = rig_sim.mount.dec_deg
        rig_client.mount.move_dec_axis_deg(degrees).wait(timeout=60)
        moved = abs(rig_sim.mount.dec_deg - dec_before)
        assert _passes(rig_sim, "TELESCOPE_MOTION_NS") - before == 1, moved
        assert moved == pytest.approx(1.0, abs=_landing(1.0)), moved


def test_a_wrong_learned_braking_never_ends_a_step_mid_coast(rig_sim, rig_client):
    """Field capture be8c66bf: with a too strong learned braking, the single-reading rest came
    early, a reading still mid-coast was taken as the end of the step, and the step was
    reported done while the mount kept going. The rest check must not trust that value."""
    rig_client.mount.move_dec_axis_deg(1.0).wait(timeout=60)
    drive = rig_client._axis_mover._drive
    drive.measured_brake[("dec", 9)] = [(2600.0, 2.5)]  # far stronger than the rig's ~0.55
    dec_before = rig_sim.mount.dec_deg
    rig_client.mount.move_dec_axis_deg(-1.0).wait(timeout=60)
    time.sleep(3.0)  # let anything still coasting come to rest
    moved = abs(rig_sim.mount.dec_deg - dec_before)
    assert moved == pytest.approx(1.0, abs=_landing(1.0)), moved
