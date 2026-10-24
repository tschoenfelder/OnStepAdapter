from pathlib import Path
from types import SimpleNamespace

import pytest

from onstep_adapter import IndiOperatingMode
from onstep_adapter.indi_client import OnStepIndiClient
from onstep_adapter.indi_config import IndiRuntimeConfig
from onstep_adapter.indi_mode import IndiModeController


def parked(**changes):
    values = dict(
        status_live=True, parked=True, slewing=False, tracking=False,
        guiding=False, at_limit=False, park_evidence_consistent=True,
        status_revision=10,
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_both_mode_directions_require_fresh_consumable_explicit_park(tmp_path: Path):
    controller = IndiModeController(str(tmp_path / "mode.json"))
    with pytest.raises(RuntimeError, match="explicit verified park"):
        controller.set_mode(IndiOperatingMode.TERRESTRIAL, parked())

    controller.authorize_after_park(status_revision=10)
    first = controller.set_mode(IndiOperatingMode.TERRESTRIAL, parked())
    assert first.previous_mode is IndiOperatingMode.UNKNOWN
    assert first.mode is IndiOperatingMode.TERRESTRIAL
    assert first.mount_remains_parked
    assert not controller.status().transition_authorized

    with pytest.raises(RuntimeError, match="explicit verified park"):
        controller.set_mode(IndiOperatingMode.ASTRONOMICAL, parked())
    controller.authorize_after_park(status_revision=11)
    second = controller.set_mode(
        IndiOperatingMode.ASTRONOMICAL, parked(status_revision=11)
    )
    assert second.previous_mode is IndiOperatingMode.TERRESTRIAL
    assert second.park_authorization_consumed


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"status_live": False}, "Fresh verified"),
        ({"parked": False}, "Fresh verified"),
        ({"slewing": True}, "idle"),
        ({"tracking": True}, "idle"),
        ({"guiding": True}, "idle"),
        ({"at_limit": True}, "idle"),
        ({"park_evidence_consistent": False}, "conflicting"),
    ],
)
def test_transition_rejects_stale_pending_or_conflicting_park(
    tmp_path: Path, changes, message,
):
    controller = IndiModeController(str(tmp_path / "mode.json"))
    controller.authorize_after_park(status_revision=10)
    with pytest.raises(RuntimeError, match=message):
        controller.set_mode(IndiOperatingMode.TERRESTRIAL, parked(**changes))


def test_other_client_generation_invalidates_local_park_authorization(tmp_path: Path):
    path = str(tmp_path / "mode.json")
    first = IndiModeController(path)
    second = IndiModeController(path)
    first.authorize_after_park(status_revision=10)
    second.authorize_after_park(status_revision=10)
    second.set_mode(IndiOperatingMode.TERRESTRIAL, parked())
    with pytest.raises(RuntimeError, match="another client"):
        first.set_mode(IndiOperatingMode.ASTRONOMICAL, parked())


def test_restart_sees_shared_mode_but_not_transition_authorization(tmp_path: Path):
    path = str(tmp_path / "mode.json")
    first = IndiModeController(path)
    first.authorize_after_park(status_revision=10)
    first.set_mode(IndiOperatingMode.TERRESTRIAL, parked())

    restarted = IndiModeController(path)
    assert restarted.status().mode is IndiOperatingMode.TERRESTRIAL
    assert not restarted.status().transition_authorized
    with pytest.raises(RuntimeError, match="explicit verified park"):
        restarted.set_mode(IndiOperatingMode.ASTRONOMICAL, parked())


def test_missing_shared_state_configuration_fails_closed():
    controller = IndiModeController(None)
    assert controller.status().mode is IndiOperatingMode.UNKNOWN
    with pytest.raises(RuntimeError, match="not_configured"):
        controller.authorize_after_park(status_revision=1)


def test_terrestrial_mode_blocks_astronomical_guiding(tmp_path: Path):
    state = str(tmp_path / "mode.json")
    config = IndiRuntimeConfig(
        host="127.0.0.1", port=7624, device="LX200 OnStep",
        observer_lat=50.336, observer_lon=8.533, observer_alt_m=304,
        flip_request_deg=1, tracking_stop_deg=1.75,
        flip_allowance_seconds=120, reserve_seconds=30,
        safe_meridian_flip_via_home=True, mode_state_path=state,
    )
    transport = SimpleNamespace(is_open=True)
    client = OnStepIndiClient(config=config, transport=transport)
    client._mode_controller.authorize_after_park(status_revision=10)
    client._mode_controller.set_mode(IndiOperatingMode.TERRESTRIAL, parked())
    client._guide_controller = SimpleNamespace(
        pulse=lambda *args, **kwargs: pytest.fail("guide command was sent")
    )
    with pytest.raises(RuntimeError, match="astronomical"):
        client.guide_pulse("east", 250)
