"""Validate the mock against a capture of the real mount.

    python3 -m onstep_mock.validate capture.jsonl --report report.md

1. checks the capture against every documented assumption
   (``onstep_adapter.capture.analysis``);
2. replays the capture's client writes against a fresh mock that starts in
   the recorded pose (``onstep_mock.replay``) and captures the mock's answers;
3. runs the same checks on the mock's capture and lists every assumption
   where the mock behaves differently from the real mount;
4. diffs both captures operation by operation with ``trace_compare``;
5. compares the INDI properties the real driver defined with the mock's;
6. prints the ``SimConfig`` values measured on the real mount.

Exit status 0 means the real mount met every checked assumption and the mock
matched it; 1 means at least one difference or failed assumption.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import sys
from typing import Any

from onstep_adapter.capture.analysis import (
    FAIL, NOT_OBSERVED, PASS, CaptureView, Finding, calibrate, check_assumptions,
    property_inventory, segment_operations,
)
from onstep_adapter.tools.trace_compare import build_operations, compare, load_trace

from .mount import Pose, SimConfig
from .replay import ReplayResult, replay_capture
from .server import DEVICE

# Values the replay speed does not scale (status period, network latency),
# compared only when the replay ran at the recorded speed.
UNSCALED_VALUES = {"status_period_s", "abort_ack_s", "unpark_tracking_delay_s", "unpark_tracking_on_s"}
NUMERIC_TOLERANCE = 0.25  # relative
NUMERIC_FLOOR = {"abort_settle_s": 0.15, "status_period_s": 0.1, "abort_ack_s": 0.1,
                 "unpark_tracking_delay_s": 0.3, "unpark_tracking_on_s": 0.3,
                 "home_ra_minus_lst_h": 0.05, "drift_rate_h_per_h": 0.01}


@dataclass
class Validation:
    real: list[Finding]
    mock: list[Finding]
    differences: list[str]
    operations: dict[str, Any]
    inventory: dict[str, Any]
    calibration: dict[str, Any]
    replay: ReplayResult
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.differences and not any(f.status == FAIL for f in self.real)


def compare_findings(real: list[Finding], mock: list[Finding], speed: float = 1.0) -> list[str]:
    differences = []
    mock_by_id = {f.id: f for f in mock}
    for r in real:
        m = mock_by_id.get(r.id)
        if m is None:
            continue
        if r.status in {PASS, FAIL} and m.status in {PASS, FAIL} and r.status != m.status:
            differences.append(f"{r.id} {r.title}: real {r.status} ({r.observed}), mock {m.status} ({m.observed})")
        elif r.status in {PASS, FAIL} and m.status == NOT_OBSERVED:
            differences.append(f"{r.id} {r.title}: real {r.status}, the mock replay never reached this situation")
        for name, want in r.values.items():
            got = m.values.get(name)
            if want is None or got is None or (speed != 1.0 and name in UNSCALED_VALUES):
                continue
            if isinstance(want, bool) or isinstance(got, bool) or not isinstance(want, (int, float)):
                if want != got:
                    differences.append(f"{r.id} {name}: real {want!r}, mock {got!r}")
                continue
            allowed = max(abs(want) * NUMERIC_TOLERANCE, NUMERIC_FLOOR.get(name, 0.0))
            if abs(got - want) > allowed:
                differences.append(f"{r.id} {name}: real {want:.4g}, mock {got:.4g} (allowed +/-{allowed:.3g})")
    return differences


def compare_inventory(real_view: CaptureView, mock_view: CaptureView) -> dict[str, Any]:
    real = property_inventory(real_view, DEVICE)
    mock = property_inventory(mock_view, DEVICE)
    if not real:
        return {"compared": False}
    mismatched = {}
    for name, spec in mock.items():
        if name not in real:
            continue
        diff = {key: {"real": real[name][key], "mock": spec[key]}
                for key in ("kind", "perm", "rule", "elements")
                if real[name][key] != spec[key] and real[name][key] is not None}
        if diff:
            mismatched[name] = diff
    return {
        "compared": True,
        "mock_only": sorted(set(mock) - set(real)),
        "not_modelled": sorted(set(real) - set(mock)),
        "mismatched": mismatched,
    }


def validate(records: list[dict[str, Any]], *, speed: float = 1.0, calibrated: bool = False) -> Validation:
    view = CaptureView(records)
    real = check_assumptions(view)
    measured = calibrate(view)
    base = SimConfig()
    notes = []
    if calibrated:
        fields = {k: v for k, v in measured.items() if k in SimConfig.__dataclass_fields__}
        if isinstance(fields.get("park_pose"), dict):
            pose = fields["park_pose"]
            fields["park_pose"] = Pose(pose["ha_h"], pose["dec_deg"], pose.get("pier_side") or "west")
        base = replace(base, **fields)
        notes.append(f"mock calibrated from the capture: {sorted(fields)}")
    result = replay_capture(view, config=base, speed=speed)
    mock_view = CaptureView(result.records)
    mock = check_assumptions(mock_view)
    differences = compare_findings(real, mock, speed)
    operations = compare(
        build_operations(segment_operations(view.records)),
        build_operations(segment_operations(result.records)),
    )
    differences += [f"operation {line}" for line in operations["mismatches"]]
    inventory = compare_inventory(view, mock_view)
    for name, diff in inventory.get("mismatched", {}).items():
        differences.append(f"property {name} differs: {diff}")
    for name in inventory.get("mock_only", []):
        differences.append(f"property {name} is published by the mock but not by the real driver")
    for write in result.rejected:
        if write.reason and "not permitted" not in write.reason:
            differences.append(f"mock rejected a write the real driver received: {write.property} "
                               f"{write.values} ({write.reason})")
    if speed != 1.0:
        notes.append(f"replayed at speed {speed}; mock times are scaled back before comparing, "
                     f"and {', '.join(sorted(UNSCALED_VALUES))} are not compared")
    if result.sync_timeouts:
        notes.append(f"{result.sync_timeouts} writes waited the full timeout for the mock to stop")
    return Validation(real, mock, differences, operations, inventory, measured, result, notes)


def format_report(v: Validation, title: str = "OnStepX capture validation") -> str:
    lines = [f"# {title}", ""]
    lines.append("Result: " + ("**mock matches the recorded mount**" if v.ok else "**differences found**"))
    lines += [""] + [f"- {note}" for note in v.notes] + ([""] if v.notes else [])
    lines += ["## Assumptions", "", "| ID | Assumption | Real | Mock | Real observation |", "|---|---|---|---|---|"]
    mock_by_id = {f.id: f for f in v.mock}
    for f in v.real:
        m = mock_by_id.get(f.id)
        lines.append(f"| {f.id} | {f.title} | {f.status} | {m.status if m else '-'} | {f.observed} |")
    lines += ["", "## Differences between the real mount and the mock", ""]
    lines += [f"- {d}" for d in v.differences] or ["- none"]
    if v.operations["warnings"]:
        lines += ["", "## Timing warnings", ""] + [f"- {w}" for w in v.operations["warnings"][:50]]
    inv = v.inventory
    if inv.get("compared"):
        lines += ["", "## INDI properties", "",
                  f"- defined by the real driver but not modelled by the mock: {', '.join(inv['not_modelled']) or 'none'}",
                  f"- published only by the mock: {', '.join(inv['mock_only']) or 'none'}",
                  f"- definition mismatches: {len(inv['mismatched'])}"]
    lines += ["", "## Measured SimConfig values", "", "```json",
              json.dumps(v.calibration, indent=2, default=str), "```"]
    if v.replay.skipped:
        lines += ["", "## Writes not replayed", ""]
        lines += [f"- t={s['t']:.2f} {s['property']} {s['values']} ({s['reason']})" for s in v.replay.skipped]
    lines += ["", "## Spec references", "",
              "BEHAVIOR = ONSTEPX_LX200_INDI_BEHAVIOR.md, MOCK = docs/MOCK.md and onstep_mock."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", help="capture file (JSON Lines) from indi_capture")
    parser.add_argument("--speed", type=float, default=1.0, help="mock speed factor for the replay (default 1)")
    parser.add_argument("--calibrated", action="store_true",
                        help="configure the mock with the values measured in the capture")
    parser.add_argument("--report", help="write the Markdown report to this file")
    parser.add_argument("--json", help="write findings and differences as JSON")
    parser.add_argument("--save-replay", help="write the mock's capture (JSON Lines) to this file")
    args = parser.parse_args(argv)
    validation = validate(load_trace(args.capture), speed=args.speed, calibrated=args.calibrated)
    report = format_report(validation, f"Validation of {Path(args.capture).name}")
    print(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "ok": validation.ok, "real": [asdict(f) for f in validation.real],
            "mock": [asdict(f) for f in validation.mock], "differences": validation.differences,
            "inventory": validation.inventory, "calibration": validation.calibration,
        }, indent=2, default=str), encoding="utf-8")
    if args.save_replay:
        with Path(args.save_replay).open("w", encoding="utf-8") as stream:
            for record in validation.replay.records:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    return 0 if validation.ok else 1


if __name__ == "__main__":
    sys.exit(main())
