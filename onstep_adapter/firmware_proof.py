"""Persistence and validation for physical OnStep firmware-safeguard proof."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROOF_SCHEMA = "onstep-firmware-safeguard-v1"
DUAL_PIER_TEST_ID = "watched_onstep_dual_pier_firmware_stop"
AXIS1_FALLBACK_TEST_ID = "watched_onstep_axis1_fallback_stop"


def load_firmware_proof(path: str | Path) -> dict[str, Any] | None:
    proof_path = Path(path).expanduser()
    try:
        with proof_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_firmware_proof(path: str | Path, evidence: dict[str, Any]) -> Path:
    proof_path = Path(path).expanduser()
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(evidence)
    payload["schema"] = PROOF_SCHEMA
    payload["recorded_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = proof_path.with_suffix(proof_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, proof_path)
    return proof_path


def validate_firmware_proof(
    proof: dict[str, Any] | None,
    *,
    firmware_identity: dict[str, object],
    dual_pier_enabled: bool,
    west_limit_minutes: float | None,
    requested_west_stop_h: float,
    axis_limits: dict[str, float | None] | None = None,
    observer: dict[str, float] | None = None,
    auto_meridian_flip_enabled: bool | None = None,
) -> dict[str, object]:
    reasons: list[str] = []
    if not isinstance(proof, dict):
        reasons.append("firmware_safeguard_proof_missing")
        return {"valid": False, "reasons": reasons, "proof": proof}
    if proof.get("schema") != PROOF_SCHEMA:
        reasons.append("firmware_safeguard_proof_schema_mismatch")
    test_id = proof.get("test_id")
    if test_id not in {DUAL_PIER_TEST_ID, AXIS1_FALLBACK_TEST_ID}:
        reasons.append("firmware_safeguard_wrong_test")
    if proof.get("result") != "pass":
        reasons.append("firmware_safeguard_test_not_passed")
    proof_mode = "dual_pier" if test_id == DUAL_PIER_TEST_ID else "axis1_fallback"
    if proof_mode == "dual_pier":
        if set(proof.get("proven_pier_sides") or []) != {"east", "west"}:
            reasons.append("firmware_safeguard_both_pier_sides_not_proven")
        if not dual_pier_enabled:
            reasons.append("dual_pier_west_ha_stop_not_enabled")
    else:
        if proof.get("pier_side") != "east":
            reasons.append("axis1_fallback_pier_east_not_proven")
        if proof.get("firmware_fallback_type") != "axis1_max":
            reasons.append("axis1_fallback_type_mismatch")
        if proof.get("physically_safe_confirmed") is not True:
            reasons.append("axis1_fallback_physical_safety_not_confirmed")

    expected_identity = proof.get("firmware_identity")
    current_identity = {
        "product": firmware_identity.get("product"),
        "version": firmware_identity.get("version"),
        "date": firmware_identity.get("date"),
    }
    if expected_identity != current_identity:
        reasons.append("firmware_identity_changed_since_proof")

    expected_minutes = requested_west_stop_h * 60.0
    if west_limit_minutes is None:
        reasons.append("west_meridian_limit_unreadable")
    elif abs(float(west_limit_minutes) - expected_minutes) > 0.5:
        reasons.append("west_meridian_limit_no_longer_matches_policy")
    if proof.get("west_limit_minutes") is None or west_limit_minutes is None:
        reasons.append("proof_west_meridian_limit_missing")
    elif abs(float(proof["west_limit_minutes"]) - float(west_limit_minutes)) > 0.5:
        reasons.append("west_meridian_limit_changed_since_proof")

    if proof_mode == "axis1_fallback":
        current_axis_limits = {
            key: (float(value) if isinstance(value, (int, float)) else None)
            for key, value in (axis_limits or {}).items()
        }
        expected_axis_limits = proof.get("axis_limits")
        if expected_axis_limits != current_axis_limits:
            reasons.append("axis_limits_changed_since_proof")
        current_observer = {
            key: round(float(value), 6)
            for key, value in (observer or {}).items()
            if key in {"lat", "lon"}
        }
        if proof.get("observer") != current_observer:
            reasons.append("observer_changed_since_proof")
        if auto_meridian_flip_enabled is None:
            reasons.append("automatic_meridian_flip_state_unreadable")
        elif proof.get("auto_meridian_flip_enabled") is not auto_meridian_flip_enabled:
            reasons.append("automatic_meridian_flip_state_changed_since_proof")

    return {
        "valid": not reasons,
        "reasons": reasons,
        "proof": proof,
        "proof_mode": proof_mode,
        "firmware_identity": current_identity,
        "west_limit_minutes": west_limit_minutes,
    }
