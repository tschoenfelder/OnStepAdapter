from onstep_adapter import (
    OnStepClient,
    OnStepFocuser,
    OnStepMount,
    IndiRuntimeConfig,
    load_indi_config,
    __version__,
)
import onstep_adapter
from onstep_adapter.mount import _counterweight_safety_state
from onstep_adapter.safety import OnStepSafetyConfig
from pathlib import Path


def test_public_release_surface() -> None:
    assert __version__ == "0.4.0"
    assert OnStepClient is not None
    assert OnStepMount is not None
    assert OnStepFocuser is not None
    assert IndiRuntimeConfig is not None
    assert load_indi_config is not None
    assert OnStepClient.__module__ == "onstep_adapter.indi_client"


def test_public_surface_uses_only_onstep_adapter_namespace() -> None:
    source = open(onstep_adapter.__file__, encoding="utf-8").read()

    assert "smart_telescope" not in source
    assert "serial_bus" not in source
    assert "from .indi_client import" in source


def test_standalone_packaging_does_not_ship_smart_telescope_namespace() -> None:
    root = Path(__file__).resolve().parents[4]
    pyproject = root / "pyproject.toml"
    setup = root / "setup.py"

    for path in (pyproject, setup):
        text = path.read_text(encoding="utf-8")
        assert '"smart_telescope' not in text
        assert "smart_telescope." not in text


def test_040_wheel_manifest_is_indi_only() -> None:
    root = Path(__file__).resolve().parents[4]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    setup = (root / "setup.py").read_text(encoding="utf-8")

    assert 'version = "0.4.0"' in pyproject
    assert "pyserial" not in pyproject
    for excluded in ('"client"', '"serial_bus"', '"mount"', '"focuser"'):
        assert excluded not in setup
    assert '"indi_client"' in setup
    assert '"indi_focuser"' in setup


def test_home_confirmation_is_required_by_default() -> None:
    config = OnStepSafetyConfig(
        observer_lat=50.336,
        observer_lon=8.533,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=5.0 / 15.0,
    )

    assert config.require_home_confirmation is True


def test_hard_meridian_limit_is_inclusive() -> None:
    result = _counterweight_safety_state(
        ha_hours=5.0 / 15.0,
        pier_side="west",
        east_limit_h=-5.5,
        west_limit_h=5.0 / 15.0,
        warning_margin_deg=3.0,
        preflip_pier_side="west",
    )

    assert result["hard_limit_reached"] is True
    assert result["counterweight_state"] == "hard_limit_reached"
    assert result["operational_limit_margin_deg"] == 0.0


def test_home_and_park_are_not_counterweight_limit_decisions() -> None:
    result = _counterweight_safety_state(
        ha_hours=12.0,
        pier_side="east",
        east_limit_h=-5.5,
        west_limit_h=5.0 / 15.0,
        warning_margin_deg=3.0,
        terminal_state=True,
    )

    assert result["applicable"] is False
    assert result["hard_limit_reached"] is False
