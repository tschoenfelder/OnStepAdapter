"""The real onstep_adapter driven end to end against the virtual OnStepX mount."""

import threading
import time

import pytest

from onstep_adapter import MotionStopped
from onstep_mock import Pose, SimConfig, SimulatedOnStepServer, VirtualMount, altaz

DEVICE = "LX200 OnStep"


def test_unpark_stops_onstepx_auto_tracking_at_once(onstep_sim, onstep_client):
    result = onstep_client.mount.unpark()

    assert result.unparked_confirmed
    mount = onstep_sim.mount
    assert not mount.parked and not mount.tracking and mount.motion == "IDLE"
    assert 0.0 < mount.tracking_on_seconds < 1.0
    written = [(w.property, w.values) for w in onstep_sim.writes]
    assert written.index(("TELESCOPE_PARK", {"UNPARK": "On"})) < written.index(
        ("TELESCOPE_TRACK_STATE", {"TRACK_OFF": "On"})
    )
    onstep_sim.assert_no_forbidden()


def test_go_home_reaches_fixed_pole_pose(onstep_sim, onstep_client):
    onstep_client.mount.unpark()
    result = onstep_client.mount.go_home().wait(timeout=30)

    assert result.confirmed
    mount = onstep_sim.mount
    assert mount.at_home and "H" in mount.raw_gu
    assert mount.dec_deg == pytest.approx(90.0)
    assert mount.ra_h == pytest.approx((mount.lst_h + 6.0) % 24.0, abs=1e-3)
    assert mount.home_pose == Pose(-6.0, 90.0, "west")


def test_park_returns_to_the_single_stored_pose(onstep_sim, onstep_client):
    onstep_sim.place(ha_h=-2.0, dec_deg=30.0, parked=False)
    time.sleep(0.4)  # let the client receive the new pose
    result = onstep_client.mount.park().wait(timeout=30)

    assert result.confirmed
    mount = onstep_sim.mount
    assert mount.parked and not mount.tracking
    assert (mount.ha_h, mount.dec_deg) == pytest.approx((0.0, 52.0))


def test_axis_move_ends_on_target_without_tracking(onstep_sim, onstep_client):
    onstep_sim.place(ha_h=-2.0, dec_deg=40.0, parked=False)
    time.sleep(0.4)
    result = onstep_client.mount.move_dec_axis_deg(1.5).wait(timeout=30)

    assert result.stop_confirmed
    mount = onstep_sim.mount
    assert mount.dec_deg == pytest.approx(41.5, abs=1e-3)
    assert not mount.tracking and mount.motion == "IDLE"
    assert mount.pier_side == "west"


@pytest.fixture
def slow_sim():
    with SimulatedOnStepServer(SimConfig(speed=1.0)) as sim:
        yield sim


@pytest.fixture
def slow_client(slow_sim):
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    slow_sim.place(ha_h=-2.0, dec_deg=30.0, parked=False)
    with OnStepClient(config=runtime_config(slow_sim)) as client:
        wait_for_live_status(client)
        yield client


def test_go_home_returns_at_once_and_its_handle_stops_the_slew(slow_sim, slow_client):
    started = time.monotonic()
    move = slow_client.mount.go_home()
    assert time.monotonic() - started < 0.5  # the caller is never blocked

    slow_sim.wait_until(lambda m: m.motion == "SLEWING", timeout=3)
    time.sleep(0.5)
    progress = move.progress()
    assert (progress.kind, progress.state, progress.stage) == ("home", "running", "moving")
    assert not move.done

    stopped = move.stop()

    assert stopped.stopped_confirmed
    with pytest.raises(MotionStopped) as raised:
        move.wait(timeout=2)
    assert raised.value.stop_result is stopped
    assert move.state == "stopped" and move.progress().stage == "stopped"
    mount = slow_sim.mount
    assert mount.motion == "IDLE" and not mount.at_home and not mount.tracking
    assert 30.0 < mount.dec_deg < 90.0
    assert "TELESCOPE_ABORT_MOTION" in [w.property for w in slow_sim.writes]


def test_mount_stop_also_ends_a_running_handle(slow_sim, slow_client):
    move = slow_client.mount.park()
    slow_sim.wait_until(lambda m: m.motion == "SLEWING", timeout=3)

    stopped = slow_client.mount.stop()

    assert stopped.stopped_confirmed
    with pytest.raises(MotionStopped):
        move.wait(timeout=2)
    assert move.stop_result is stopped
    assert not slow_sim.mount.parked and slow_sim.mount.motion == "IDLE"


def test_only_one_move_runs_at_a_time(slow_sim, slow_client):
    move = slow_client.mount.go_home()
    with pytest.raises(RuntimeError, match="active"):
        slow_client.mount.park()
    with pytest.raises(RuntimeError, match="active"):
        slow_client.mount.move_dec_axis_deg(1.0)
    with pytest.raises(RuntimeError, match="active"):
        slow_client.mount.unpark()
    move.stop()
    with pytest.raises(MotionStopped):
        move.wait(timeout=2)
    assert slow_client.active_motion is None


def test_axis_move_reports_fraction_and_can_be_stopped(slow_sim, slow_client):
    move = slow_client.mount.move_dec_axis_deg(8.0)
    slow_sim.wait_until(lambda m: m.dec_deg > 32.0, timeout=5)
    deadline = time.monotonic() + 2
    while (move.progress().fraction or 0.0) <= 0.0 and time.monotonic() < deadline:
        time.sleep(0.05)
    fraction = move.progress().fraction
    assert fraction is not None and 0.0 < fraction < 1.0

    assert move.stop().stopped_confirmed
    with pytest.raises(MotionStopped):
        move.wait(timeout=2)
    mount = slow_sim.mount
    assert 30.0 < mount.dec_deg < 38.0 and mount.motion == "IDLE"


def test_invalid_axis_move_is_refused_before_anything_starts(onstep_sim, onstep_client):
    onstep_sim.place(ha_h=-2.0, dec_deg=40.0, parked=False)
    writes = len(onstep_sim.writes)
    with pytest.raises(ValueError):
        onstep_client.mount.move_dec_axis_deg(20.0)
    assert onstep_client.active_motion is None
    assert len(onstep_sim.writes) == writes


def test_close_stops_a_move_that_is_still_running(slow_sim):
    from onstep_mock.testing import runtime_config, wait_for_live_status
    from onstep_adapter import OnStepClient

    slow_sim.place(ha_h=-2.0, dec_deg=30.0, parked=False)
    with OnStepClient(config=runtime_config(slow_sim)) as client:
        wait_for_live_status(client)
        move = client.mount.go_home()
        slow_sim.wait_until(lambda m: m.motion == "SLEWING", timeout=3)
    assert move.done
    slow_sim.wait_until(lambda m: m.motion == "IDLE", timeout=3)
    assert not slow_sim.mount.at_home


@pytest.mark.parametrize("property_name, element", [
    ("TELESCOPE_HOME", "SET"),
    ("ON_COORD_SET", "SYNC"),
    ("TELESCOPE_PARK_OPTION", "PARK_CURRENT"),
    ("TELESCOPE_PARK_OPTION", "PARK_WRITE_DATA"),
])
def test_set_home_set_park_and_sync_are_rejected_and_logged(
    onstep_sim, onstep_client, property_name, element,
):
    before = onstep_sim.mount
    with pytest.raises(RuntimeError, match="INDI rejected"):
        onstep_client._transport.request_switch(DEVICE, property_name, element)

    after = onstep_sim.mount
    assert (after.home_pose, after.park_pose) == (before.home_pose, before.park_pose)
    assert [w.property for w in onstep_sim.forbidden] == [property_name]
    with pytest.raises(AssertionError):
        onstep_sim.assert_no_forbidden()


def test_injected_limit_blocks_unpark(onstep_sim, onstep_client):
    onstep_sim.inject_fault("LIMIT")
    time.sleep(0.4)

    result = onstep_client.mount.unpark()

    assert not result.unparked_confirmed
    assert "l" in onstep_sim.mount.raw_gu and onstep_sim.mount.parked


def test_manual_motion_repeat_without_off_rearm_starts_nothing(onstep_sim, onstep_client):
    onstep_sim.place(ha_h=-2.0, dec_deg=40.0, parked=False)
    transport = onstep_client._transport
    transport.issue_switch(DEVICE, "TELESCOPE_MOTION_NS", "MOTION_NORTH")
    onstep_sim.wait_until(lambda m: m.motion == "MANUAL", timeout=2)
    time.sleep(0.3)
    assert onstep_sim.mount.dec_deg > 40.0

    # Motion ends without the switch being reset (as after a controller stop).
    onstep_sim.place()
    transport.issue_switch(DEVICE, "TELESCOPE_MOTION_NS", "MOTION_NORTH")
    time.sleep(0.3)
    assert onstep_sim.mount.motion == "IDLE"

    # An explicit all-OFF re-arm makes the same direction start again.
    transport._send(
        '<newSwitchVector device="LX200 OnStep" name="TELESCOPE_MOTION_NS">'
        '<oneSwitch name="MOTION_NORTH">Off</oneSwitch>'
        '<oneSwitch name="MOTION_SOUTH">Off</oneSwitch></newSwitchVector>'
    )
    time.sleep(0.2)
    transport.issue_switch(DEVICE, "TELESCOPE_MOTION_NS", "MOTION_NORTH")
    onstep_sim.wait_until(lambda m: m.motion == "MANUAL", timeout=2)
    transport.issue_switch(DEVICE, "TELESCOPE_ABORT_MOTION", "ABORT")
    onstep_sim.wait_until(lambda m: m.motion == "IDLE", timeout=3)


def test_non_tracking_pose_is_earth_fixed_and_ra_drifts():
    mount = VirtualMount.create(SimConfig(start_parked=False, start_pose=Pose(-1.0, 45.0)))
    ra_before, alt_before = mount.ra_h(), altaz(mount.ha_h, mount.dec_deg, 50.336)
    mount.tick(60.0, time.monotonic())
    assert mount.ha_h == -1.0
    assert altaz(mount.ha_h, mount.dec_deg, 50.336) == alt_before
    assert (mount.ra_h() - ra_before) % 24.0 > 0.0

    mount.set_tracking(True, time.monotonic())
    mount.tick(3600.0, time.monotonic())
    assert mount.ha_h == pytest.approx(-1.0 + 1.0027379, abs=1e-6)


def test_wait_until_reports_last_state_on_timeout(onstep_sim):
    with pytest.raises(TimeoutError, match="last state"):
        onstep_sim.wait_until(lambda m: not m.parked, timeout=0.2)
