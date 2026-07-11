from unittest.mock import patch

from onstep_adapter.mount import OnStepMount
from onstep_adapter.ports.mount import MountState
from onstep_adapter.safety import OnStepSafetyConfig

from .fake_serial import FakeOnStepSerial


def _config() -> OnStepSafetyConfig:
    return OnStepSafetyConfig(
        observer_lat=50.0,
        observer_lon=8.0,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=0.333,
        require_home_confirmation=False,
        time_trust_source="user_confirmed",
    )


def test_onstep_mount_satisfies_mount_port_abstract_contract() -> None:
    mount = OnStepMount("/dev/fake-onstep", safety_config=_config())

    assert isinstance(mount, OnStepMount)


def test_go_home_uses_mechanical_home_command() -> None:
    fake = FakeOnStepSerial(initial_state="unparked")
    mount = OnStepMount("/dev/fake-onstep", safety_config=_config())
    mount._serial = fake
    mount.refresh_safety_state()

    assert mount.go_home() is True
    assert b":hC#" in fake.commands_received


def test_unpark_actively_stops_firmware_auto_tracking() -> None:
    fake = FakeOnStepSerial(initial_state="parked", unpark_auto_tracks=True)
    mount = OnStepMount("/dev/fake-onstep", safety_config=_config())
    mount._serial = fake

    assert mount.unpark() is True
    assert mount.get_state() == MountState.UNPARKED
    assert b":hR#" in fake.commands_received
    assert b":Td#" in fake.commands_received


def test_get_state_stops_unrequested_tracking() -> None:
    fake = FakeOnStepSerial(initial_state="tracking")
    mount = OnStepMount("/dev/fake-onstep", safety_config=_config())
    mount._serial = fake

    assert mount.get_state() == MountState.UNPARKED
    assert b":Td#" in fake.commands_received


def test_explicit_enable_tracking_allows_tracking_status() -> None:
    fake = FakeOnStepSerial(initial_state="unparked")
    mount = OnStepMount("/dev/fake-onstep", safety_config=_config())
    mount._serial = fake

    with patch.object(mount, "_check_target_safe", return_value=None):
        assert mount.enable_tracking() is True
    assert mount.get_state() == MountState.TRACKING
    assert fake.commands_received.count(b":Td#") == 0
