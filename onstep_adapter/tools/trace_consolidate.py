"""Condense a trace or capture log: repeated entries become one entry with a duration.

    python3 -m onstep_adapter.tools.trace_consolidate capture-2026-11-02.jsonl
    python3 -m onstep_adapter.tools.trace_consolidate trace-2026-11-02.jsonl --text
    python3 -m onstep_adapter.tools.trace_consolidate capture-*.jsonl --tolerance 0.001

A mount that stands still publishes the same OnStep Status, pier side and
park state several times a second, and the driver polls OnStepX with the
same LX200 commands. Such a run of identical entries is written as its first
entry plus:

    repeat      how many entries the run had
    t_end       ``t`` of the last entry
    utc_end     ``utc`` of the last entry
    duration_s  t_end - t
    values_end  the last entry's values, when ``--tolerance`` let numbers drift

A run is one stream (the same record type, connection, direction and
property) with unchanged content. Entries of other streams in between do not
end it; a client write, a public call or its result, a motion record or a new
session ends every run, so cause and effect stay in order. ``seq``,
``thread``, ``revision`` and ``changed`` are not compared.

The output (``<name>.consolidated.jsonl`` by default) is for reading and
archiving. ``check``, ``validate`` and ``trace_compare`` need the raw log,
because update rates and timings are what they measure.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from .trace_compare import load_trace

# Records that end every open run: something happened that later updates
# may be a reaction to.
BARRIERS = {"session", "capture", "connect", "close", "call", "return", "error",
            "motion_start", "motion_end", "decode_error"}
VOLATILE = {"seq", "t", "utc", "thread", "revision", "changed"}
RUN_FIELDS = ("repeat", "t_end", "utc_end", "duration_s", "values_end")


def _is_barrier(record: dict[str, Any]) -> bool:
    kind = record.get("type")
    if kind in BARRIERS:
        return True
    return kind == "send" and str(record.get("tag", "")).startswith("new")


def _stream(record: dict[str, Any]) -> tuple:
    return (record.get("type"), record.get("conn"), record.get("dir"), record.get("tag"),
            record.get("device"), record.get("property"))


def _content(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in VOLATILE and k not in RUN_FIELDS}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same(first: dict[str, Any], record: dict[str, Any], tolerance: float) -> bool:
    a, b = _content(first), _content(record)
    if a == b:
        return True
    if tolerance <= 0 or set(a) != set(b):
        return False
    for key in a:
        if key == "values" and isinstance(a[key], dict) and isinstance(b[key], dict):
            if set(a[key]) != set(b[key]):
                return False
            for name, want in a[key].items():
                got = b[key][name]
                x, y = _number(want), _number(got)
                if want != got and (x is None or y is None or abs(x - y) > tolerance):
                    return False
        elif a[key] != b[key]:
            return False
    return True


def consolidate(
    records: Iterable[dict[str, Any]], *, tolerance: float = 0.0, min_repeat: int = 2,
) -> list[dict[str, Any]]:
    """Return the records with runs of repeated entries merged."""
    records = list(records)
    run_of: list[int | None] = [None] * len(records)  # index of the run's head
    members: dict[int, list[int]] = {}
    open_runs: dict[tuple, int] = {}  # stream -> head index
    for index, record in enumerate(records):
        if _is_barrier(record):
            open_runs.clear()
            continue
        key = _stream(record)
        head = open_runs.get(key)
        if head is not None and _same(records[head], record, tolerance):
            run_of[index] = head
            members[head].append(index)
            continue
        open_runs[key] = index
        run_of[index] = index
        members[index] = [index]

    out: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if record.get("type") == "session":
            out.append({**record, "consolidated": True})
            continue
        head = run_of[index]
        run = members.get(head, []) if head is not None else []
        if len(run) < max(2, min_repeat):
            out.append(record)
        elif head == index:
            out.append(_merged(record, records[run[-1]], len(run)))
    return out


def _merged(head: dict[str, Any], last: dict[str, Any], count: int) -> dict[str, Any]:
    merged = dict(head)
    merged["repeat"] = count
    merged["t_end"] = last.get("t")
    merged["utc_end"] = last.get("utc")
    try:
        merged["duration_s"] = round(float(last["t"]) - float(head["t"]), 6)
    except (KeyError, TypeError, ValueError):
        merged["duration_s"] = None
    if last.get("values") != head.get("values"):
        merged["values_end"] = last.get("values")
    return merged


def _clock(utc: str | None) -> str:
    if not utc:
        return "?"
    try:
        return datetime.fromisoformat(utc).strftime("%H:%M:%S.%f")[:-3]
    except ValueError:
        return utc


def format_text(records: Iterable[dict[str, Any]]) -> str:
    """One readable line per entry; merged runs show their span and count."""
    lines = []
    for r in records:
        kind = r.get("type")
        if kind == "session":
            lines.append(f"==== session {r.get('session_id', '')} {r.get('label', '')} "
                         f"{'(continued) ' if r.get('continued') else ''}{r.get('utc', '')}")
            continue
        start = _clock(r.get("utc"))
        span = (f"-{_clock(r.get('utc_end'))} {r['duration_s']:>8.2f} s x{r['repeat']:<5}"
                if r.get("repeat") else " " * 28)
        if kind in {"send", "recv"}:
            what = f"{r.get('property') or r.get('tag')}"
            if r.get("state"):
                what += f" [{r['state']}]"
            values = r.get("values") or {}
            what += " " + " ".join(f"{k}={v}" for k, v in values.items())
            if r.get("values_end") and r["values_end"] != values:
                what += " -> " + " ".join(f"{k}={v}" for k, v in r["values_end"].items())
        elif kind == "lx200":
            what = f"{r.get('dir')} {r.get('data', '')!r}"
        elif kind in {"call", "return", "error"}:
            what = f"{r.get('op')} #{r.get('call_id')}" + (f" {r.get('error_type')}" if kind == "error" else "")
        else:
            what = " ".join(f"{k}={v}" for k, v in r.items()
                            if k not in VOLATILE and k not in {"type", *RUN_FIELDS})
        conn = f"c{r['conn']} " if r.get("conn") is not None else ""
        lines.append(f"{start}{span} {conn}{kind:<7} {what}".rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", nargs="+", help="trace or capture log(s); each is consolidated on its own")
    parser.add_argument("-o", "--out", help="output file (only with a single input); default <name>.consolidated.jsonl")
    parser.add_argument("--tolerance", type=float, default=0.0,
                        help="merge entries whose numbers differ by at most this much, e.g. drifting "
                             "coordinates; the run keeps its last values as values_end (default 0: exact)")
    parser.add_argument("--min-repeat", type=int, default=2, help="shortest run that is merged (default 2)")
    parser.add_argument("--text", action="store_true", help="also write a readable .consolidated.txt")
    args = parser.parse_args(argv)
    if args.out and len(args.log) > 1:
        parser.error("--out needs a single input file")
    for name in args.log:
        source = Path(name)
        records = load_trace(source)
        merged = consolidate(records, tolerance=args.tolerance, min_repeat=args.min_repeat)
        stem = source.name[:-len(".jsonl")] if source.name.endswith(".jsonl") else source.name
        out = Path(args.out) if args.out else source.with_name(f"{stem}.consolidated.jsonl")
        with out.open("w", encoding="utf-8") as stream:
            for record in merged:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        runs = [r for r in merged if r.get("repeat")]
        print(f"{source} -> {out}: {len(records)} entries -> {len(merged)}, "
              f"{len(runs)} runs merged ({sum(r['repeat'] for r in runs)} entries)")
        if args.text:
            text = out.with_suffix(".txt")
            text.write_text(format_text(merged) + "\n", encoding="utf-8")
            print(f"  readable: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
