from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.safety import OnStepSafetyConfig

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
