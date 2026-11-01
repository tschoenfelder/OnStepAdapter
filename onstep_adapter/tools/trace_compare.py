"""Summarize one adapter trace or compare an observed trace with an expected one.

Typical use: record a supervised run on the real mount, record the same
application steps against a reference (an earlier accepted run or, later, the
simulator) and diff them:

    python3 -m onstep_adapter.tools.trace_compare summary real.jsonl
    python3 -m onstep_adapter.tools.trace_compare diff expected.jsonl real.jsonl

Each top-level public call (``call`` record) opens an operation. The operation
contains every record up to the next top-level call, so the asynchronous tail
of a movement (late status and coordinate updates) is kept with the command
that caused it. For each operation the tool derives:

* the INDI writes sent, in order;
* the sequence of distinct OnStep ``:GU#`` motion states observed;
* the sequence of distinct EQUATORIAL_EOD_COORD property states;
* timings relative to the call: duration, first write, motion start, motion end;
* how long tracking was on inside the operation;
* the outcome (return or error type) and the boolean fields of the result;
* the final motion state, pier side and coordinates.

``diff`` pairs operations by position and reports every behavioral mismatch.
Timing differences beyond the tolerance are reported as warnings because the
real mount and a reference never move with identical speed.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import sys
from typing import Any, Iterable

BEHAVIOR_FIELDS = ("op", "sends", "motion_states", "coord_states", "outcome", "result_flags")


@dataclass
class Operation:
    index: int
    op: str
    call_id: int
    args: list[Any]
    kwargs: dict[str, Any]
    t_call: float
    sends: list[str] = field(default_factory=list)
    motion_states: list[str] = field(default_factory=list)
    coord_states: list[str] = field(default_factory=list)
    outcome: str = "pending"
    result_flags: dict[str, bool] = field(default_factory=dict)
    duration_s: float | None = None
    first_send_s: float | None = None
    motion_start_s: float | None = None
    motion_end_s: float | None = None
    tracking_on_s: float = 0.0
    final_motion_state: str | None = None
    final_pier_side: str | None = None
    final_ra_hours: float | None = None
    final_dec_deg: float | None = None


def load_trace(path: str | Path) -> list[dict[str, Any]]:
    records = []
    with Path(path).expanduser().open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid trace line") from exc
    return records


def _send_label(record: dict[str, Any]) -> str:
    name = record.get("property") or record.get("tag") or "?"
    values = record.get("values") or {}
    if not values:
        return str(name)
    if record.get("tag") == "newSwitchVector":
        parts = [f"{key}={value}" for key, value in values.items()]
    else:
        parts = sorted(values)
    return f"{name}:{','.join(parts)}"


def _append_distinct(sequence: list[str], value: str | None) -> None:
    if value is not None and (not sequence or sequence[-1] != value):
        sequence.append(value)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_operations(records: Iterable[dict[str, Any]]) -> list[Operation]:
    """Split a trace into top-level operations and derive their behavior."""
    operations: list[Operation] = []
    open_calls: dict[str, list[int]] = {}
    current: Operation | None = None
    gu: dict[str, Any] | None = None
    coord_state: str | None = None
    pier: str | None = None
    ra = dec = None
    tracking_since: float | None = None

    def close_tracking(op: Operation | None, at: float) -> None:
        nonlocal tracking_since
        if op is not None and tracking_since is not None:
            op.tracking_on_s += max(0.0, at - max(tracking_since, op.t_call))

    last_t = 0.0
    for record in records:
        kind = record.get("type")
        t = float(record.get("t", 0.0))
        last_t = t
        thread = str(record.get("thread", ""))
        if kind == "call":
            stack = open_calls.setdefault(thread, [])
            stack.append(record["call_id"])
            if len(stack) > 1:
                continue  # nested call belongs to the enclosing operation
            if current is not None:
                close_tracking(current, t)
            current = Operation(
                index=len(operations), op=record.get("op", "?"),
                call_id=record["call_id"], args=record.get("args", []),
                kwargs=record.get("kwargs", {}), t_call=t,
            )
            operations.append(current)
            if gu is not None:
                current.motion_states.append(gu["motion_state"])
            if coord_state is not None:
                current.coord_states.append(coord_state)
            continue
        if kind in {"return", "error"}:
            stack = open_calls.get(thread, [])
            if record.get("call_id") in stack:
                stack.remove(record["call_id"])
            if current is not None and record.get("call_id") == current.call_id:
                current.duration_s = _float(record.get("duration_s"))
                if kind == "error":
                    current.outcome = f"error:{record.get('error_type', '?')}"
                else:
                    current.outcome = "return"
                    result = record.get("result")
                    if isinstance(result, dict):
                        current.result_flags = {
                            key: value for key, value in result.items()
                            if isinstance(value, bool)
                        }
                    elif isinstance(result, bool):
                        current.result_flags = {"value": result}
            continue
        if kind == "send" and current is not None:
            current.sends.append(_send_label(record))
            if current.first_send_s is None:
                current.first_send_s = round(t - current.t_call, 6)
            continue
        if kind != "recv":
            continue
        name = record.get("property")
        values = record.get("values") or {}
        if name == "OnStep Status" and record.get("gu"):
            previous = gu
            gu = record["gu"]
            if gu.get("tracking") and tracking_since is None:
                tracking_since = t
            elif not gu.get("tracking") and tracking_since is not None:
                close_tracking(current, t)
                tracking_since = None
            if current is not None:
                _append_distinct(current.motion_states, gu["motion_state"])
                was_slewing = bool(previous and previous.get("slewing"))
                if gu.get("slewing") and not was_slewing and current.motion_start_s is None:
                    current.motion_start_s = round(t - current.t_call, 6)
                if was_slewing and not gu.get("slewing"):
                    current.motion_end_s = round(t - current.t_call, 6)
                current.final_motion_state = gu["motion_state"]
        elif name == "EQUATORIAL_EOD_COORD":
            coord_state = record.get("state")
            ra, dec = _float(values.get("RA")), _float(values.get("DEC"))
            if current is not None:
                _append_distinct(current.coord_states, coord_state)
                current.final_ra_hours, current.final_dec_deg = ra, dec
        elif name == "TELESCOPE_PIER_SIDE":
            pier = "east" if values.get("PIER_EAST") == "On" else (
                "west" if values.get("PIER_WEST") == "On" else None
            )
            if current is not None:
                current.final_pier_side = pier
    close_tracking(current, last_t)
    for op in operations:
        op.tracking_on_s = round(op.tracking_on_s, 3)
    return operations


def format_summary(operations: list[Operation]) -> str:
    lines = []
    for op in operations:
        lines.append(
            f"#{op.index} {op.op} -> {op.outcome}"
            f" ({_fmt(op.duration_s)} s, motion {_fmt(op.motion_start_s)}..{_fmt(op.motion_end_s)} s,"
            f" tracking on {op.tracking_on_s:.2f} s)"
        )
        if op.sends:
            lines.append("    sends:  " + " | ".join(op.sends))
        if op.motion_states:
            lines.append("    motion: " + " > ".join(op.motion_states))
        if op.coord_states:
            lines.append("    coords: " + " > ".join(op.coord_states))
        if op.result_flags:
            lines.append("    result: " + ", ".join(f"{k}={v}" for k, v in op.result_flags.items()))
        if op.final_ra_hours is not None:
            lines.append(
                f"    final:  RA {op.final_ra_hours:.5f} h, DEC {op.final_dec_deg:.4f} deg,"
                f" pier {op.final_pier_side}"
            )
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def compare(
    expected: list[Operation],
    observed: list[Operation],
    *,
    time_tolerance: float = 0.5,
    time_floor_s: float = 1.0,
) -> dict[str, Any]:
    """Return mismatches (behavior) and warnings (timing) between two runs."""
    mismatches: list[str] = []
    warnings: list[str] = []
    if len(expected) != len(observed):
        mismatches.append(
            f"operation count differs: expected {len(expected)}, observed {len(observed)}"
        )
    for exp, obs in zip(expected, observed):
        prefix = f"#{exp.index} {exp.op}"
        for name in BEHAVIOR_FIELDS:
            want, got = getattr(exp, name), getattr(obs, name)
            if want != got:
                mismatches.append(f"{prefix}: {name} expected {want!r}, observed {got!r}")
        for name in ("duration_s", "motion_start_s", "motion_end_s", "tracking_on_s"):
            want, got = getattr(exp, name), getattr(obs, name)
            if want is None or got is None:
                if (want is None) != (got is None):
                    warnings.append(f"{prefix}: {name} expected {want}, observed {got}")
                continue
            allowed = max(time_floor_s, abs(want) * time_tolerance)
            if abs(got - want) > allowed:
                warnings.append(
                    f"{prefix}: {name} expected {want:.2f} s, observed {got:.2f} s"
                    f" (allowed +/-{allowed:.2f} s)"
                )
    return {
        "match": not mismatches,
        "mismatches": mismatches,
        "warnings": warnings,
        "expected": [asdict(op) for op in expected],
        "observed": [asdict(op) for op in observed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="summarize one trace")
    summary.add_argument("trace")
    summary.add_argument("--json", help="write derived operations as JSON")
    diff = commands.add_parser("diff", help="compare an observed trace with an expected one")
    diff.add_argument("expected")
    diff.add_argument("observed")
    diff.add_argument("--time-tolerance", type=float, default=0.5,
                      help="relative timing tolerance (default 0.5 = 50%%)")
    diff.add_argument("--time-floor", type=float, default=1.0,
                      help="minimum absolute timing tolerance in seconds (default 1.0)")
    diff.add_argument("--json", help="write the full comparison as JSON")
    args = parser.parse_args(argv)

    if args.command == "summary":
        operations = build_operations(load_trace(args.trace))
        print(format_summary(operations))
        if args.json:
            Path(args.json).write_text(
                json.dumps([asdict(op) for op in operations], indent=2), encoding="utf-8"
            )
        return 0

    report = compare(
        build_operations(load_trace(args.expected)),
        build_operations(load_trace(args.observed)),
        time_tolerance=args.time_tolerance, time_floor_s=args.time_floor,
    )
    for line in report["mismatches"]:
        print("MISMATCH " + line)
    for line in report["warnings"]:
        print("TIMING   " + line)
    print("MATCH" if report["match"] else "DIFFERENT")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
