from onstep_adapter import (
    OnStepClient,
    OnStepFocuser,
    OnStepMount,
    OnStepMotionCalibration,
    OnStepSafetyConfig,
    OnStepSafetyError,
    __version__,
)
import onstep_adapter
from onstep_adapter.mount import _counterweight_safety_state
from pathlib import Path


def test_public_release_surface() -> None:
    assert __version__ == "0.3.1"
    assert OnStepClient is not None
    assert OnStepMount is not None
    assert OnStepFocuser is not None
    assert OnStepMotionCalibration is not None


def test_public_surface_uses_only_onstep_adapter_namespace() -> None:
    source = open(onstep_adapter.__file__, encoding="utf-8").read()

    assert "smart_telescope" not in source
    assert "from onstep_adapter.results import" in source
    assert OnStepSafetyError is not None


def test_standalone_packaging_does_not_ship_smart_telescope_namespace() -> None:
    root = Path(__file__).resolve().parents[4]
    pyproject = root / "pyproject.toml"
    setup = root / "setup.py"

    for path in (pyproject, setup):
        text = path.read_text(encoding="utf-8")
        assert '"smart_telescope' not in text
        assert "smart_telescope." not in text


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
