from onstep_adapter import (
    OnStepClient,
    OnStepFocuser,
    OnStepMount,
    IndiRuntimeConfig,
    IndiGuideCalibration,
    IndiGuideCorrectionResult,
    IndiGuidePulseResult,
    load_indi_config,
    __version__,
)
import onstep_adapter
from onstep_adapter.mount import _counterweight_safety_state
from onstep_adapter.safety import OnStepSafetyConfig
from pathlib import Path


def test_public_release_surface() -> None:
    assert __version__ == "0.6.2"
    assert OnStepClient is not None
    assert OnStepMount is not None
    assert OnStepFocuser is not None
    assert IndiRuntimeConfig is not None
    assert load_indi_config is not None
    assert IndiGuidePulseResult is not None
    assert IndiGuideCalibration is not None
    assert IndiGuideCorrectionResult is not None
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


def test_062_wheel_manifest_is_indi_only() -> None:
    root = Path(__file__).resolve().parents[4]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    setup = (root / "setup.py").read_text(encoding="utf-8")

    assert 'version = "0.6.2"' in pyproject
    assert 'onstep_adapter = ["config.indi.example.toml"]' in pyproject
    assert "pyserial" not in pyproject
    adapter_modules = _wheel_allowlist(root)["onstep_adapter"]
    for excluded in ("client", "serial_bus", "mount", "focuser"):
        assert excluded not in adapter_modules
    assert '"indi_client"' in setup
    assert '"indi_focuser"' in setup
    assert '"indi_guiding"' in setup


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


def _wheel_allowlist(root: Path) -> dict[str, set[str]]:
    import ast

    tree = ast.parse((root / "setup.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(getattr(t, "id", None) == "_allowed_modules" for t in targets):
                return ast.literal_eval(node.value)
    raise AssertionError("setup.py has no _allowed_modules")


def _imported_project_modules(path: Path, package: str) -> set[str]:
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[: len(package.split(".")) - node.level + 1]
                name = ".".join(base + ([node.module] if node.module else []))
            else:
                name = node.module or ""
            if name.split(".")[0] in {"onstep_adapter", "onstep_mock"}:
                found.add(name)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] in {"onstep_adapter", "onstep_mock"})
    return found


def test_wheel_ships_every_module_its_shipped_modules_import() -> None:
    """A module left out of the allowlist makes the installed wheel fail on import."""
    root = Path(__file__).resolve().parents[4]
    allowed = _wheel_allowlist(root)
    shipped = {f"{pkg}.{mod}" if mod != "__init__" else pkg for pkg, mods in allowed.items() for mod in mods}
    missing = set()
    for pkg, mods in allowed.items():
        for mod in mods:
            source = root.joinpath(*pkg.split("."), f"{mod}.py")
            assert source.is_file(), f"allowlisted module does not exist: {source}"
            for name in _imported_project_modules(source, pkg):
                if name not in shipped:
                    missing.add(f"{pkg}.{mod} -> {name}")
    assert not missing, sorted(missing)


def test_wheel_ships_the_mock_and_the_capture_tools() -> None:
    allowed = _wheel_allowlist(Path(__file__).resolve().parents[4])
    assert {"server", "testing", "mount"} <= allowed["onstep_mock"]
    assert {"proxy", "stream"} <= allowed["onstep_adapter.capture"]
