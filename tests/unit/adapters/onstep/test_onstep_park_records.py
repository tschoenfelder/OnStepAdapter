from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from onstep_adapter import OnStepSafetyError
from smart_telescope.adapters.onstep.mount import OnStepMount
from smart_telescope.adapters.onstep.safety import OnStepSafetyConfig

from .fake_serial import FakeOnStepSerial


def _mount(tmp_path, *, state: str = "unparked", set_park_reply: str = "1") -> tuple[OnStepMount, FakeOnStepSerial]:
    config = OnStepSafetyConfig(
        observer_lat=50.0,
        observer_lon=8.0,
        min_alt_deg=-5.0,
        max_alt_deg=90.0,
        ha_east_limit_h=-5.5,
        ha_west_limit_h=5.0 / 15.0,
        require_home_confirmation=False,
        time_trust_source="user_confirmed",
        mechanical_calibration_file=str(tmp_path / "mechanical.json"),
        state_file=str(tmp_path / "state.json"),
    )
    fake = FakeOnStepSerial(
        initial_state=state,
        initial_ra=4.25,
        initial_dec=32.5,
        sidereal_hours=4.0,
        set_park_reply=set_park_reply,
    )
    mount = OnStepMount("/dev/fake", safety_config=config)
    mount._serial = fake
    return mount, fake


def test_set_park_captures_current_position_and_provenance(tmp_path) -> None:
    mount, fake = _mount(tmp_path)

    result = mount.set_park_position_from_current(confirmed_safe=True)

    assert result.ok is True
    assert result.controller_updated is True
    assert result.local_record_persisted is True
    assert result.record is not None
    assert result.record.ra == pytest.approx(4.25, abs=1e-3)
    assert result.record.dec == pytest.approx(32.5, abs=1e-3)
    assert result.record.source == "captured_when_set"
    assert result.record.controller_readback_supported is False
    assert result.record.controller_match == "unverifiable"
    assert b":hQ#" in fake.commands_received
    assert b":GpA#" not in fake.commands_received
    assert b":GpD#" not in fake.commands_received

    payload = json.loads((tmp_path / "mechanical.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "onstep-mechanical-calibration-v2"
    assert payload["stored_park_position"]["ra"] == pytest.approx(4.25, abs=1e-3)


def test_legacy_get_park_position_uses_local_record_without_controller_probe(tmp_path) -> None:
    mount, fake = _mount(tmp_path)
    mount.set_park_position_from_current(confirmed_safe=True)
    fake.commands_received.clear()

    position = mount.get_park_position()

    assert position is not None
    assert position.ra == pytest.approx(4.25, abs=1e-3)
    assert position.dec == pytest.approx(32.5, abs=1e-3)
    assert b":GpA#" not in fake.commands_received
    assert b":GpD#" not in fake.commands_received


def test_set_park_requires_application_confirmation(tmp_path) -> None:
    mount, fake = _mount(tmp_path)

    with pytest.raises(OnStepSafetyError, match="park_position_confirmation_required"):
        mount.set_park_position_from_current(confirmed_safe=False)

    assert b":hQ#" not in fake.commands_received


def test_set_park_refuses_home_without_explicit_override(tmp_path) -> None:
    mount, fake = _mount(tmp_path, state="home")

    with pytest.raises(OnStepSafetyError, match="set_park_at_home_refused"):
        mount.set_park_position_from_current(confirmed_safe=True)

    assert b":hQ#" not in fake.commands_received

    result = mount.set_park_position_from_current(
        confirmed_safe=True,
        allow_at_home=True,
    )
    assert result.ok is True


def test_set_park_rejection_does_not_persist_record(tmp_path) -> None:
    mount, _ = _mount(tmp_path, set_park_reply="0")

    result = mount.set_park_position_from_current(confirmed_safe=True)

    assert result.ok is False
    assert result.controller_updated is False
    assert result.local_record_persisted is False
    assert not (tmp_path / "mechanical.json").exists()


def test_set_park_reports_controller_updated_when_final_commit_fails(tmp_path) -> None:
    mount, _ = _mount(tmp_path)
    real_replace = __import__("os").replace

    def fail_only_final(source, destination):
        if str(source).endswith(".park-pending"):
            raise OSError("disk failure")
        return real_replace(source, destination)

    with patch("smart_telescope.adapters.onstep.mount.os.replace", side_effect=fail_only_final):
        result = mount.set_park_position_from_current(confirmed_safe=True)

    assert result.ok is False
    assert result.controller_updated is True
    assert result.local_record_persisted is False
    assert result.error is not None
    assert result.error.startswith("local_record_persistence_failed:")


def test_stored_record_becomes_untrusted_after_external_motion(tmp_path) -> None:
    mount, _ = _mount(tmp_path)
    mount.set_park_position_from_current(confirmed_safe=True)

    mount.note_external_motion("clutch_repositioned")
    record = mount.get_stored_park_position()

    assert record is not None
    assert record.trusted is False
    assert "clutch_repositioned" in record.invalidation_reasons


def test_stored_record_becomes_untrusted_after_firmware_change(tmp_path) -> None:
    mount, fake = _mount(tmp_path)
    mount.set_park_position_from_current(confirmed_safe=True)
    fake._firmware_version = "11.0"

    record = mount.get_stored_park_position()

    assert record is not None
    assert record.trusted is False
    assert "firmware_identity_changed" in record.invalidation_reasons


def test_legacy_v1_record_does_not_invent_ra_dec(tmp_path) -> None:
    path = tmp_path / "mechanical.json"
    path.write_text(
        json.dumps({
            "schema": "onstep-mechanical-calibration-v1",
            "park_pose": {"axis1_deg": 1.0, "axis2_deg": 2.0},
        }),
        encoding="utf-8",
    )
    mount, _ = _mount(tmp_path)

    assert mount.get_stored_park_position() is None
    assert mount.get_park_position() is None
