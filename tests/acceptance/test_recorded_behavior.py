"""Acceptance tests built from recordings of the mount.

Every ``recordings/*.jsonl`` capture is checked against the documented
assumptions and replayed against ``onstep_mock``; the test fails when the
recorded mount broke an assumption or when the mock answers differently.
Drop a new capture of the real mount into ``recordings/`` and it becomes a
test case; when it fails, the report in the failure message says whether the
spec, the mock or both must change.

``ONSTEP_REPLAY_SPEED`` (default 5) replays that many times faster than
recorded. Use 1 for a faithful-timing run of a hardware capture.
"""

import os
from pathlib import Path

import pytest

from onstep_adapter.capture.analysis import CaptureView, FAIL, PASS, check_assumptions
from onstep_adapter.tools.trace_compare import load_trace
from onstep_mock.validate import format_report, validate

RECORDINGS = sorted((Path(__file__).parent / "recordings").glob("*.jsonl"))
SPEED = float(os.environ.get("ONSTEP_REPLAY_SPEED", "5"))
SEED = Path(__file__).parent / "recordings" / "mock-seed-unpark-axis-abort-home-park.jsonl"


@pytest.mark.parametrize("recording", RECORDINGS, ids=[p.stem for p in RECORDINGS])
def test_mock_reproduces_recorded_mount(recording):
    validation = validate(load_trace(recording), speed=SPEED)
    assert validation.ok, format_report(validation, f"Validation of {recording.name}")


def test_seed_recording_exercises_the_core_assumptions():
    findings = {f.id: f for f in check_assumptions(load_trace(SEED))}
    for check in ("A01", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10", "A11", "A15", "A16"):
        assert findings[check].status == PASS, findings[check]
    assert findings["A04"].values["abort_settle_s"] == pytest.approx(1.45, abs=0.1)


def test_validation_reports_a_mount_that_differs_from_the_mock():
    # The same run as the seed, but with a driver that ends a goto with
    # EQUATORIAL_EOD_COORD Idle (as the spec text says) instead of Ok, and a
    # client that sent a forbidden SYNC selection.
    records = []
    for record in load_trace(SEED):
        if record.get("property") == "EQUATORIAL_EOD_COORD" and record.get("state") == "Ok":
            record = {**record, "state": "Idle"}
        records.append(record)
    first_send = next(r for r in records if r["type"] == "send" and r.get("property"))
    records.append({**first_send, "seq": 10**6, "property": "ON_COORD_SET", "values": {"SYNC": "On"},
                    "xml": '<newSwitchVector device="LX200 OnStep" name="ON_COORD_SET">'
                           '<oneSwitch name="SYNC">On</oneSwitch></newSwitchVector>'})

    validation = validate(records, speed=SPEED)

    assert not validation.ok
    real = {f.id: f for f in validation.real}
    assert real["A01"].status == FAIL
    assert any("final_coord_state" in d and "'Idle'" in d for d in validation.differences)
    assert [s["reason"] for s in validation.replay.skipped] == ["SYNC"]
    assert not any(w.property == "ON_COORD_SET" and w.values.get("SYNC") == "On"
                   for w in validation.replay.rejected)  # never sent, not even to the mock


def test_capture_view_uses_one_client_for_received_updates():
    view = CaptureView(load_trace(SEED))
    assert view.primary == 1 and view.status
