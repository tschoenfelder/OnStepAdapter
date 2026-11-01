"""Daily log files, session splitting and log consolidation."""

from datetime import datetime, timedelta, timezone
import io
import json

import pytest

from onstep_adapter.capture import open_capture
from onstep_adapter.capture.analysis import CaptureView
from onstep_adapter.indi_trace import TraceRecorder
from onstep_adapter.tools import trace_consolidate
from onstep_adapter.tools.trace_compare import load_trace, load_traces, select_session, split_sessions
from onstep_adapter.tools.trace_consolidate import consolidate, format_text

MIDNIGHT = datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_daily_log_splits_at_utc_midnight_and_continues_the_session(tmp_path):
    template = str(tmp_path / "trace-{date}.jsonl")
    recorder = TraceRecorder(template, label="night")
    recorder.record("recv", at_utc=MIDNIGHT - timedelta(seconds=1), property="A")
    recorder.record("recv", at_utc=MIDNIGHT + timedelta(seconds=1), property="B")
    recorder.close()

    first = load_trace(tmp_path / "trace-2026-11-01.jsonl")
    second = load_trace(tmp_path / "trace-2026-11-02.jsonl")
    assert [r["type"] for r in first] == ["session", "recv"]
    assert [r["type"] for r in second] == ["session", "recv"]
    assert second[0]["continued"] is True
    assert second[0]["session_id"] == first[0]["session_id"] == recorder.session_id
    assert [r["seq"] for r in first + second] == [1, 2, 3, 4]

    joined = split_sessions(first + second)
    assert len(joined) == 1
    assert [r.get("property") for r in joined[0]] == [None, "A", "B"]


def test_every_capture_of_a_day_appends_to_that_days_file(tmp_path):
    template = tmp_path / "capture-{date}.jsonl"
    for label in ("evening", "late"):
        writer = open_capture(template, label=label)
        writer.recorder.record("recv", property="OnStep Status")
        writer.recorder.close()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    records = load_traces([tmp_path / f"capture-{day}.jsonl"])
    sessions = split_sessions(records)
    assert [s[0]["label"] for s in sessions] == ["evening", "late"]
    assert select_session(records, "last")[0]["label"] == "late"
    assert select_session(records, "1")[0]["label"] == "evening"
    with pytest.raises(ValueError, match="several sessions"):
        CaptureView(records)


def rec(seq, t, kind="recv", **fields):
    utc = (MIDNIGHT + timedelta(seconds=t)).isoformat()
    return {"seq": seq, "t": t, "utc": utc, "thread": "x", "type": kind, **fields}


def status(seq, t, raw, revision):
    return rec(seq, t, conn=1, tag="setTextVector", property="OnStep Status", state="Ok",
               values={":GU# return": raw}, changed={}, revision=revision)


def coords(seq, t, ra):
    return rec(seq, t, conn=1, tag="setNumberVector", property="EQUATORIAL_EOD_COORD", state="Idle",
               values={"RA": f"{ra:.6f}", "DEC": "52"}, changed={}, revision=seq)


def test_repeated_entries_become_one_entry_with_their_duration():
    records = [rec(1, 0.0, "session", session_id="s1")]
    seq = 1
    for i in range(10):  # status polled every 0.25 s, coordinates in between
        seq += 1
        records.append(status(seq, i * 0.25, "nNpEW060", seq))
        seq += 1
        records.append(coords(seq, i * 0.25 + 0.01, 1.0 + i * 0.0001))
    merged = consolidate(records)
    runs = [r for r in merged if r.get("repeat")]
    assert len(runs) == 1
    run = runs[0]
    assert run["property"] == "OnStep Status" and run["repeat"] == 10
    assert run["duration_s"] == pytest.approx(2.25) and run["t_end"] == pytest.approx(2.25)
    assert run["utc_end"] == records[-2]["utc"]
    assert sum(1 for r in merged if r.get("property") == "EQUATORIAL_EOD_COORD") == 10
    assert merged[0]["consolidated"] is True

    drifting = consolidate(records, tolerance=0.01)
    coord_run = next(r for r in drifting if r.get("property") == "EQUATORIAL_EOD_COORD")
    assert coord_run["repeat"] == 10 and coord_run["values"]["RA"] == "1.000000"
    assert coord_run["values_end"]["RA"] == "1.000900"
    assert "x10" in format_text(drifting) and "2.25 s" in format_text(drifting)


def test_a_client_write_or_a_state_change_ends_a_run():
    records = [
        status(1, 0.0, "nNPEW060", 1), status(2, 0.25, "nNPEW060", 2),
        rec(3, 0.3, "send", conn=1, tag="newSwitchVector", property="TELESCOPE_PARK", values={"UNPARK": "On"}),
        status(4, 0.5, "nNPEW060", 3), status(5, 0.75, "nNPEW060", 4),
        status(6, 1.0, "nNpEW060", 5), status(7, 1.25, "nNpEW060", 6), status(8, 1.5, "nNpEW060", 7),
    ]
    merged = consolidate(records)
    assert [(r["type"], r.get("repeat")) for r in merged] == [
        ("recv", 2), ("send", None), ("recv", 2), ("recv", 3),
    ]
    assert [r["seq"] for r in merged] == [1, 3, 4, 6]
    assert consolidate(records, min_repeat=3)[0].get("repeat") is None
    assert len(consolidate(records, min_repeat=3)) == 6


def test_a_consolidated_log_is_refused_by_the_analysis():
    merged = consolidate([status(1, 0.0, "nNpEW060", 1), status(2, 0.25, "nNpEW060", 2)])
    with pytest.raises(ValueError, match="consolidated"):
        CaptureView(merged)


def test_consolidate_command_writes_next_to_the_log(tmp_path):
    log = tmp_path / "capture-2026-11-02.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in [
        rec(1, 0.0, "session", session_id="s1"), status(2, 0.0, "nNpEW060", 1), status(3, 0.25, "nNpEW060", 2),
    ]))
    assert trace_consolidate.main([str(log), "--text"]) == 0
    out = load_trace(tmp_path / "capture-2026-11-02.consolidated.jsonl")
    assert out[1]["repeat"] == 2
    assert "x2" in (tmp_path / "capture-2026-11-02.consolidated.txt").read_text()
