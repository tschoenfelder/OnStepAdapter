from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from onstep_adapter.tools.axis_motion_smoke import _time_location_preflight
from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.safety import OnStepSafetyConfig


def test_time_location_preflight_requires_all_three_authorities() -> None:
    mount = MagicMock()
    mount.read_onstep_clock.return_value = {"warning": False}
    mount.read_onstep_site.return_value = {"available": True, "lat": 50.333, "lon": 8.533}
    mount.read_onstep_sidereal_consistency.return_value = {"ok": True}
    mount.safety_snapshot.return_value = {
        "location_readiness": {"ready": True},
    }

    result = _time_location_preflight(SimpleNamespace(mount=mount))

    assert result["ready"] is True


def test_time_location_preflight_blocks_sidereal_mismatch() -> None:
    mount = MagicMock()
    mount.read_onstep_clock.return_value = {"warning": False}
    mount.read_onstep_site.return_value = {"available": True, "lat": 50.333, "lon": 8.533}
    mount.read_onstep_sidereal_consistency.return_value = {
        "ok": False,
        "reason": "onstep_sidereal_time_mismatch",
    }
    mount.safety_snapshot.return_value = {
        "location_readiness": {"ready": True},
    }

    result = _time_location_preflight(SimpleNamespace(mount=mount))

    assert result["ready"] is False


def test_system_clock_sanity_uses_installed_package_when_app_is_absent(tmp_path) -> None:
    mount = OnStepMount(
        "/dev/test",
        safety_config=OnStepSafetyConfig(
            observer_lat=50.336,
            observer_lon=8.533,
            min_alt_deg=-5.0,
            max_alt_deg=90.0,
            ha_east_limit_h=-5.5,
            ha_west_limit_h=5.0 / 15.0,
            time_trust_source="user_confirmed",
            state_file=str(tmp_path / "state.json"),
        ),
    )

    result = mount.read_system_clock_sanity()

    assert result["valid"] is True
    assert result["reference_source"] in {
        "smart_telescope_application",
        "installed_onstep_package",
    }
