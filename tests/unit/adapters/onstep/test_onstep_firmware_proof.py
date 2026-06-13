from smart_telescope.adapters.onstep.firmware_proof import (
    PROOF_SCHEMA,
    validate_firmware_proof,
    write_firmware_proof,
)
from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.safety import OnStepSafetyConfig

from .fake_serial import FakeOnStepSerial


IDENTITY = {
    "product": "On-Step",
    "version": "10.19d-smarttscope",
    "date": "Feb 29 2024",
}


def _proof() -> dict[str, object]:
    return {
        "schema": PROOF_SCHEMA,
        "test_id": "watched_onstep_dual_pier_firmware_stop",
        "result": "pass",
        "proven_pier_sides": ["west", "east"],
        "firmware_identity": IDENTITY,
        "west_limit_minutes": 20.0,
    }


def _axis1_proof() -> dict[str, object]:
    return {
        "schema": PROOF_SCHEMA,
        "test_id": "watched_onstep_axis1_fallback_stop",
        "result": "pass",
        "pier_side": "east",
        "firmware_fallback_type": "axis1_max",
        "firmware_fallback_deg": 180.0,
        "physically_safe_confirmed": True,
        "firmware_identity": IDENTITY,
        "west_limit_minutes": 20.0,
        "axis_limits": {
            "axis1_min_deg": -180.0,
            "axis1_max_deg": 180.0,
            "axis2_min_deg": -90.0,
            "axis2_max_deg": 90.0,
        },
        "observer": {"lat": 50.0, "lon": 8.0},
        "auto_meridian_flip_enabled": False,
    }


def test_matching_dual_pier_proof_allows_unattended_classification() -> None:
    result = validate_firmware_proof(
        _proof(),
        firmware_identity=IDENTITY,
        dual_pier_enabled=True,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
    )

    assert result["valid"] is True
    assert result["reasons"] == []


def test_firmware_change_invalidates_proof() -> None:
    result = validate_firmware_proof(
        _proof(),
        firmware_identity={**IDENTITY, "version": "10.20"},
        dual_pier_enabled=True,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
    )

    assert result["valid"] is False
    assert "firmware_identity_changed_since_proof" in result["reasons"]


def test_limit_change_invalidates_proof() -> None:
    result = validate_firmware_proof(
        _proof(),
        firmware_identity=IDENTITY,
        dual_pier_enabled=True,
        west_limit_minutes=24.0,
        requested_west_stop_h=20.0 / 60.0,
    )

    assert result["valid"] is False
    assert "west_meridian_limit_no_longer_matches_policy" in result["reasons"]
    assert "west_meridian_limit_changed_since_proof" in result["reasons"]


def test_single_pier_evidence_is_rejected() -> None:
    proof = _proof()
    proof["proven_pier_sides"] = ["east"]

    result = validate_firmware_proof(
        proof,
        firmware_identity=IDENTITY,
        dual_pier_enabled=True,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
    )

    assert result["valid"] is False
    assert "firmware_safeguard_both_pier_sides_not_proven" in result["reasons"]


def test_matching_stock_axis1_proof_is_valid() -> None:
    result = validate_firmware_proof(
        _axis1_proof(),
        firmware_identity=IDENTITY,
        dual_pier_enabled=False,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
        axis_limits={
            "axis1_min_deg": -180.0,
            "axis1_max_deg": 180.0,
            "axis2_min_deg": -90.0,
            "axis2_max_deg": 90.0,
        },
        observer={"lat": 50.0, "lon": 8.0},
        auto_meridian_flip_enabled=False,
    )

    assert result["valid"] is True
    assert result["proof_mode"] == "axis1_fallback"


def test_stock_axis1_proof_is_invalidated_by_limit_or_observer_change() -> None:
    result = validate_firmware_proof(
        _axis1_proof(),
        firmware_identity=IDENTITY,
        dual_pier_enabled=False,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
        axis_limits={
            "axis1_min_deg": -180.0,
            "axis1_max_deg": 179.0,
            "axis2_min_deg": -90.0,
            "axis2_max_deg": 90.0,
        },
        observer={"lat": 51.0, "lon": 8.0},
        auto_meridian_flip_enabled=False,
    )

    assert "axis_limits_changed_since_proof" in result["reasons"]
    assert "observer_changed_since_proof" in result["reasons"]


def test_stock_axis1_proof_is_invalidated_when_automatic_flip_is_enabled() -> None:
    proof = _axis1_proof()
    result = validate_firmware_proof(
        proof,
        firmware_identity=IDENTITY,
        dual_pier_enabled=False,
        west_limit_minutes=20.0,
        requested_west_stop_h=20.0 / 60.0,
        axis_limits=proof["axis_limits"],
        observer=proof["observer"],
        auto_meridian_flip_enabled=True,
    )

    assert "automatic_meridian_flip_state_changed_since_proof" in result["reasons"]


def test_mount_reports_unattended_allowed_only_with_matching_physical_proof(tmp_path) -> None:
    proof_path = tmp_path / "firmware-proof.json"
    write_firmware_proof(proof_path, {key: value for key, value in _proof().items() if key != "schema"})
    cfg = OnStepSafetyConfig(
        observer_lat=50.0,
        observer_lon=8.0,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=20.0 / 60.0,
        require_home_confirmation=False,
        time_trust_source="user_confirmed",
        meridian_margin_deg=3.0,
        firmware_proof_file=str(proof_path),
    )
    fake = FakeOnStepSerial(
        initial_state="parked",
        horizon_limit=-5.0,
        overhead_limit=90.0,
        meridian_west_minutes=20,
        dual_pier_west_ha_stop=True,
        firmware_version="10.19d-smarttscope",
    )
    mount = OnStepMount("/dev/fake-onstep", safety_config=cfg)
    mount._serial = fake
    mount.refresh_safety_state()

    protection = mount._onstep_firmware_protection()

    assert protection["status"] == "proven"
    assert protection["unattended_tracking_allowed"] is True
    assert protection["meridian_path_coverage"]["full_path_firmware_protected"] is True


def test_mount_accepts_physically_proven_stock_axis1_fallback(tmp_path) -> None:
    proof_path = tmp_path / "axis1-proof.json"
    write_firmware_proof(
        proof_path,
        {key: value for key, value in _axis1_proof().items() if key != "schema"},
    )
    cfg = OnStepSafetyConfig(
        observer_lat=50.0,
        observer_lon=8.0,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=20.0 / 60.0,
        require_home_confirmation=False,
        time_trust_source="user_confirmed",
        meridian_margin_deg=3.0,
        firmware_proof_file=str(proof_path),
    )
    fake = FakeOnStepSerial(
        initial_state="parked",
        horizon_limit=-5.0,
        overhead_limit=90.0,
        meridian_west_minutes=20,
        axis1_min_deg=-180,
        axis1_max_deg=180,
        dual_pier_west_ha_stop=False,
        firmware_version="10.19d-smarttscope",
    )
    mount = OnStepMount("/dev/fake-onstep", safety_config=cfg)
    mount._serial = fake
    mount.refresh_safety_state()

    protection = mount._onstep_firmware_protection()

    assert protection["status"] == "proven"
    assert protection["unattended_tracking_allowed"] is True
    assert protection["operational_stop_deg"] == 5.0
    assert protection["firmware_fallback_type"] == "axis1_max"
    assert protection["firmware_fallback_deg"] == 180.0
    assert protection["firmware_fallback_proven"] is True
    assert protection["firmware_fallback_physically_safe"] is True
