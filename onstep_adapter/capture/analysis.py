"""Check recorded OnStepX behavior against the assumptions the code relies on.

Every check is named after one assumption from ``ONSTEPX_LX200_INDI_BEHAVIOR.md``
(the hardware behavior reference), ``docs/MOCK.md`` and ``onstep_mock``. A
check reads only what was recorded; it never talks to a mount. Results:

``PASS``          the recording shows the assumed behavior.
``FAIL``          the recording contradicts it.
``INCONCLUSIVE``  the situation occurred, but the recording cannot decide.
``NOT_OBSERVED``  the situation did not occur in this recording.
``INFO``          a measurement without a pass criterion (e.g. a rate).

``values`` holds the measured numbers and flags. ``onstep_mock.validate``
compares them between a real capture and its replay against the mock, and
``calibrate`` turns them into ``SimConfig`` overrides.

Works on passive captures (``send``/``recv`` records with ``conn``) and on
adapter traces (the same records without ``conn``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import statistics
from typing import Any, Iterable

from ..indi_gu import local_sidereal_hours
from .stream import forbidden_indi

SIDEREAL_RATE = 1.00273790935
DEFAULT_LONGITUDE = 8.533  # tested site, used only if GEOGRAPHIC_COORD is missing
# :GU# letters the mock emits. Others seen on the real controller are reported.
MOCK_GU_FLAGS = set("nNpPHGlETW")

PASS, FAIL, INCONCLUSIVE, NOT_OBSERVED, INFO = "PASS", "FAIL", "INCONCLUSIVE", "NOT_OBSERVED", "INFO"


@dataclass
class Finding:
    id: str
    title: str
    spec: str
    status: str
    expected: str
    observed: str
    samples: int = 0
    values: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- the capture
class CaptureView:
    """Records split into client writes and one client's received updates.

    indiserver sends every update to every client; the analysis uses the
    updates of the connection that received the most (the primary client) so
    nothing is counted twice. Writes are taken from all clients.
    """

    def __init__(self, records: Iterable[dict[str, Any]]) -> None:
        self.records = sorted(records, key=lambda r: (float(r.get("t", 0.0)), r.get("seq", 0)))
        counts: dict[Any, int] = {}
        for r in self.records:
            if r.get("type") == "recv":
                counts[r.get("conn")] = counts.get(r.get("conn"), 0) + 1
        self.primary = max(counts, key=counts.get) if counts else None
        self.sends = [
            r for r in self.records
            if r.get("type") == "send" and str(r.get("tag", "")).startswith("new")
        ]
        self.recv = [
            r for r in self.records
            if r.get("type") == "recv" and r.get("conn") == self.primary
        ]
        self.status = [r for r in self.recv if r.get("property") == "OnStep Status" and r.get("gu")]
        self.lx200 = [r for r in self.records if r.get("type") == "lx200"]
        self.longitude = self._number("GEOGRAPHIC_COORD", "LONG")
        self.latitude = self._number("GEOGRAPHIC_COORD", "LAT")

    def _number(self, prop: str, element: str) -> float | None:
        for r in self.recv:
            if r.get("property") == prop:
                try:
                    return float(r["values"][element])
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    def updates(self, prop: str, after: float = float("-inf"), before: float = float("inf")) -> list[dict]:
        return [r for r in self.recv if r.get("property") == prop and after < r["t"] <= before]

    def gu_after(self, t: float, before: float = float("inf")) -> list[dict]:
        return [r for r in self.status if t < r["t"] <= before]

    def gu_before(self, t: float) -> dict | None:
        last = None
        for r in self.status:
            if r["t"] >= t:
                break
            last = r
        return last

    def coord_before(self, t: float) -> dict | None:
        last = None
        for r in self.recv:
            if r["t"] >= t:
                break
            if r.get("property") == "EQUATORIAL_EOD_COORD":
                last = r
        return last

    def writes(self, prop: str, element: str | None = None, value: str = "On") -> list[dict]:
        return [
            s for s in self.sends
            if s.get("property") == prop
            and (element is None or (s.get("values") or {}).get(element) == value)
        ]

    def lst(self, record: dict) -> float:
        lon = self.longitude if self.longitude is not None else DEFAULT_LONGITUDE
        if lon > 180.0:
            lon -= 360.0
        return local_sidereal_hours(lon, datetime.fromisoformat(record["utc"]))

    def ha(self, coord: dict) -> float:
        return _wrap_h(self.lst(coord) - float(coord["values"]["RA"]))


def _wrap_h(value: float) -> float:
    return (value + 12.0) % 24.0 - 12.0


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _fmt(values: list[float], unit: str = "s") -> str:
    if not values:
        return "none"
    if len(values) == 1:
        return f"{values[0]:.3f} {unit}"
    return f"median {statistics.median(values):.3f} {unit} (min {min(values):.3f}, max {max(values):.3f}, n={len(values)})"


def _motion_end(view: CaptureView, t_send: float, limit: float = 600.0) -> tuple[dict | None, dict | None]:
    """First slewing status after ``t_send`` and the first stationary one after it."""
    start = end = None
    for r in view.gu_after(t_send, t_send + limit):
        if start is None and r["gu"]["slewing"]:
            start = r
        elif start is not None and not r["gu"]["slewing"]:
            end = r
            break
    return start, end


def _aborted_between(view: CaptureView, t0: float, t1: float) -> bool:
    return any(t0 < s["t"] < t1 for s in view.writes("TELESCOPE_ABORT_MOTION"))


# --------------------------------------------------------------------- checks
def check_forbidden_indi(view: CaptureView) -> Finding:
    bad = [s for s in view.sends if forbidden_indi(s.get("property"), s.get("values") or {})]
    status = FAIL if bad else PASS if view.sends else NOT_OBSERVED
    observed = (
        "; ".join(f"t={s['t']:.2f} {s['property']} {s.get('values')}" for s in bad[:5])
        if bad else f"{len(view.sends)} writes, none forbidden"
    )
    return Finding("A01", "No SET HOME, SET PARK or SYNC INDI write", "BEHAVIOR 4.1, 4.12; MOCK forbidden writes",
                   status, "no TELESCOPE_HOME.SET, ON_COORD_SET.SYNC or TELESCOPE_PARK_OPTION write",
                   observed, len(view.sends), {"forbidden": len(bad)})


def check_forbidden_lx200(view: CaptureView) -> Finding:
    commands = [r for r in view.lx200 if r.get("dir") == "cmd"]
    bad = [r for r in commands if r.get("forbidden")]
    status = FAIL if bad else PASS if commands else NOT_OBSERVED
    observed = (
        "; ".join(f"t={r['t']:.2f} {r['data']} ({r['forbidden']})" for r in bad[:5])
        if bad else f"{len(commands)} LX200 commands, none forbidden" if commands else "no LX200 link recorded"
    )
    return Finding("A02", "Driver sends no set-park, set-home or sync LX200 command",
                   "BEHAVIOR 1 (command path)", status, "no :hQ#, :hF#, :CS#, :CM#",
                   observed, len(commands), {"forbidden": len(bad)})


def check_unpark_tracking(view: CaptureView) -> Finding:
    delays, durations, verdicts = [], [], []
    for unpark in view.writes("TELESCOPE_PARK", "UNPARK"):
        before = view.gu_before(unpark["t"])
        if before is not None and not before["gu"]["parked"]:
            continue  # already unparked: nothing to learn
        track_off = next((s["t"] for s in view.writes("TELESCOPE_TRACK_STATE", "TRACK_OFF")
                          if s["t"] > unpark["t"]), float("inf"))
        window = view.gu_after(unpark["t"], min(track_off + 2.0, unpark["t"] + 30.0))
        tracking = [r for r in window if r["gu"]["tracking"] and not r["gu"]["parked"]]
        if tracking:
            first = tracking[0]
            delays.append(first["t"] - unpark["t"])
            off = next((r["t"] for r in view.gu_after(first["t"]) if not r["gu"]["tracking"]), None)
            if off is not None:
                durations.append(off - first["t"])
            verdicts.append(True)
        elif len([r for r in window if r["t"] < track_off and not r["gu"]["parked"]]) >= 2:
            verdicts.append(False)
        else:
            verdicts.append(None)
    if not verdicts:
        return Finding("A03", "UNPARK starts tracking automatically", "BEHAVIOR 4.9; MOCK UNPARK auto-tracking",
                       NOT_OBSERVED, "tracking appears after UNPARK until TRACK_OFF", "no UNPARK from PARKED")
    if True in verdicts:
        status = PASS
    elif False in verdicts:
        status = FAIL
    else:
        status = INCONCLUSIVE
    observed = (f"tracking seen after {sum(v is True for v in verdicts)}/{len(verdicts)} UNPARKs, "
                f"delay {_fmt(delays)}, on for {_fmt(durations)}")
    return Finding("A03", "UNPARK starts tracking automatically", "BEHAVIOR 4.9; MOCK UNPARK auto-tracking",
                   status, "tracking appears after UNPARK until TRACK_OFF", observed, len(verdicts),
                   {"auto_track_on_unpark": True if True in verdicts else False if False in verdicts else None,
                    "unpark_tracking_delay_s": _median(delays), "unpark_tracking_on_s": _median(durations)})


def check_abort_settle(view: CaptureView) -> Finding:
    settle = []
    for abort in view.writes("TELESCOPE_ABORT_MOTION"):
        before = view.gu_before(abort["t"])
        if before is None or not before["gu"]["slewing"]:
            continue
        stop = next((r for r in view.gu_after(abort["t"], abort["t"] + 30.0) if not r["gu"]["slewing"]), None)
        if stop is not None:
            settle.append(stop["t"] - abort["t"])
    if not settle:
        return Finding("A04", "ABORT during motion: stationary status after about 1.45 s",
                       "BEHAVIOR 4.8; MOCK abort_settle_s", NOT_OBSERVED,
                       "1.40-1.50 s from ABORT to first stationary :GU#", "no ABORT while moving")
    median = statistics.median(settle)
    status = PASS if abs(median - 1.45) <= 0.25 else FAIL
    return Finding("A04", "ABORT during motion: stationary status after about 1.45 s",
                   "BEHAVIOR 4.8; MOCK abort_settle_s", status,
                   "1.40-1.50 s from ABORT to first stationary :GU#", _fmt(settle), len(settle),
                   {"abort_settle_s": round(median, 4)})


def check_abort_ack(view: CaptureView) -> Finding:
    acks = []
    for abort in view.writes("TELESCOPE_ABORT_MOTION"):
        ack = next(iter(view.updates("TELESCOPE_ABORT_MOTION", abort["t"], abort["t"] + 5.0)), None)
        if ack is not None:
            acks.append(ack["t"] - abort["t"])
    if not acks:
        return Finding("A05", "ABORT is acknowledged within 0.2 s", "BEHAVIOR 4.8", NOT_OBSERVED,
                       "59-163 ms ABORT dispatch to acknowledgment", "no ABORT acknowledged")
    return Finding("A05", "ABORT is acknowledged within 0.2 s", "BEHAVIOR 4.8",
                   PASS if max(acks) <= 0.2 else FAIL, "59-163 ms ABORT dispatch to acknowledgment",
                   _fmt(acks), len(acks), {"abort_ack_s": _median(acks)})


def check_abort_clears_motion(view: CaptureView) -> Finding:
    verdicts = []
    for abort in view.writes("TELESCOPE_ABORT_MOTION"):
        for prop in ("TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"):
            update = next(iter(view.updates(prop, abort["t"], abort["t"] + 3.0)), None)
            if update is None:
                verdicts.append(None)
            else:
                verdicts.append(all(v == "Off" for v in (update.get("values") or {}).values()))
    if not verdicts:
        status, observed = NOT_OBSERVED, "no ABORT"
    elif False in verdicts:
        status, observed = FAIL, "a manual-motion vector stayed On after ABORT"
    elif True in verdicts:
        status = PASS
        observed = f"{verdicts.count(True)} all-Off updates, {verdicts.count(None)} vectors not republished"
    else:
        status, observed = INCONCLUSIVE, "driver did not republish NS/WE within 3 s of ABORT"
    return Finding("A06", "ABORT leaves both manual-motion vectors all Off",
                   "BEHAVIOR 4.8; MOCK publishes NS/WE at ABORT", status,
                   "TELESCOPE_MOTION_NS/WE republished all Off", observed, len(verdicts),
                   {"motion_republished_on_abort": True in verdicts or False in verdicts})


def _gotos(view: CaptureView) -> list[tuple[dict, dict | None, dict | None]]:
    """Each coordinate goto that ran to arrival, with its motion start and end status."""
    result = []
    for goto in view.writes("EQUATORIAL_EOD_COORD"):
        start, end = _motion_end(view, goto["t"])
        if end is not None and _aborted_between(view, goto["t"], end["t"]):
            continue
        result.append((goto, start, end))
    return result


def check_goto_coord_states(view: CaptureView) -> Finding:
    busy_seen, finals = [], []
    for goto, start, end in _gotos(view):
        if end is None:
            continue
        states = [u.get("state") for u in view.updates("EQUATORIAL_EOD_COORD", goto["t"], end["t"] + 5.0)]
        busy_seen.append("Busy" in states)
        after_busy = states[states.index("Busy") + 1:] if "Busy" in states else states
        finals.append(next((s for s in after_busy if s != "Busy"), None))
    if not finals:
        return Finding("A07", "Goto: EQUATORIAL_EOD_COORD Busy during the slew, then not Busy",
                       "BEHAVIOR 4.2; MOCK Busy then Ok", NOT_OBSERVED, "Busy, then Idle (spec) / Ok (mock)",
                       "no goto that ran to arrival")
    final_states = sorted({str(f) for f in finals})
    status = PASS if all(busy_seen) and all(f in {"Idle", "Ok"} for f in finals) else FAIL
    return Finding("A07", "Goto: EQUATORIAL_EOD_COORD Busy during the slew, then not Busy",
                   "BEHAVIOR 4.2; MOCK Busy then Ok", status, "Busy, then Idle (spec) / Ok (mock)",
                   f"Busy seen in {sum(busy_seen)}/{len(busy_seen)}, final state {', '.join(final_states)}",
                   len(finals), {"final_coord_state": final_states[0] if len(final_states) == 1 else final_states})


def check_arrival_order(view: CaptureView) -> Finding:
    tracking_at_arrival, tracking_first, idle_after_coords = [], [], []
    for goto, start, end in _gotos(view):
        if start is None or end is None:
            continue
        track = next((r for r in view.gu_after(start["t"], end["t"] + 3.0) if r["gu"]["tracking"]), None)
        tracking_at_arrival.append(track is not None)
        final_coord = next((u for u in view.updates("EQUATORIAL_EOD_COORD", start["t"], end["t"] + 5.0)
                            if u.get("state") != "Busy"), None)
        if final_coord is None:
            continue
        if track is not None:
            tracking_first.append(track["t"] <= final_coord["t"])
        idle_after_coords.append(end["t"] > final_coord["t"])
    if not tracking_at_arrival:
        return Finding("A08", "Goto arrival: tracking starts, status may lead coordinates",
                       "BEHAVIOR 5; MOCK arrival tracking", NOT_OBSERVED,
                       "tracking at arrival; tracking before final coordinates; idle :GU# after them",
                       "no goto that ran to arrival")
    arrival = all(tracking_at_arrival)
    status = PASS if arrival else FAIL
    observed = (f"tracking at arrival {sum(tracking_at_arrival)}/{len(tracking_at_arrival)}, "
                f"tracking before final coordinates {sum(tracking_first)}/{len(tracking_first)}, "
                f"idle :GU# after final coordinates {sum(idle_after_coords)}/{len(idle_after_coords)}")
    return Finding("A08", "Goto arrival: tracking starts, status may lead coordinates",
                   "BEHAVIOR 5; MOCK arrival tracking", status,
                   "tracking at arrival; tracking before final coordinates; idle :GU# after them",
                   observed, len(tracking_at_arrival),
                   {"auto_track_on_goto_arrival": arrival,
                    "tracking_before_final_coords": all(tracking_first) if tracking_first else None,
                    "idle_status_after_final_coords": any(idle_after_coords) if idle_after_coords else None})


def _coord_at_rest(view: CaptureView, status: dict) -> dict | None:
    """Coordinates of the pose a stationary status reports.

    The first coordinate update after the status; if the driver sent none
    within 3 s, the last one before it (the final coordinate update can
    precede the idle status, BEHAVIOR 5).
    """
    after = view.updates("EQUATORIAL_EOD_COORD", status["t"] - 1e-6, status["t"] + 3.0)
    if after:
        return after[0]
    return view.coord_before(status["t"])


def _arrived_pose(view: CaptureView, send: dict, flag: str) -> tuple[dict, dict] | None:
    for r in view.gu_after(send["t"], send["t"] + 600.0):
        if r["gu"][flag] and not r["gu"]["slewing"]:
            coord = _coord_at_rest(view, r)
            return (r, coord) if coord is not None else None
    return None


def check_home(view: CaptureView) -> Finding:
    errors_ra, errors_dec, verdicts = [], [], []
    for go in view.writes("TELESCOPE_HOME", "GO"):
        arrived = _arrived_pose(view, go, "at_home")
        if arrived is None:
            verdicts.append(None)
            continue
        _, coord = arrived
        ra, dec = float(coord["values"]["RA"]), float(coord["values"]["DEC"])
        ra_err = _wrap_h(ra - (view.lst(coord) + 6.0))
        errors_ra.append(ra_err)
        errors_dec.append(dec - 90.0)
        verdicts.append(abs(ra_err) <= 0.1 and abs(dec - 90.0) <= 0.1)
    if not verdicts:
        status, observed = NOT_OBSERVED, "no GO HOME"
    elif False in verdicts:
        status = FAIL
    elif True in verdicts:
        status = PASS
    else:
        status = INCONCLUSIVE
    if verdicts:
        observed = (f"{verdicts.count(True)}/{len(verdicts)} at HOME pose; RA - (LST + 6 h) "
                    f"{_fmt(errors_ra, 'h')}, DEC - 90 {_fmt(errors_dec, 'deg')}")
        if view.longitude is None:
            observed += f" (LST from default longitude {DEFAULT_LONGITUDE})"
    return Finding("A09", "GO HOME reaches DEC +90, RA = LST + 6 h with the H flag",
                   "BEHAVIOR 2.3, 4.11; MOCK fixed HOME", status, "DEC 90 +/-0.1 deg, RA LST+6h +/-0.1 h",
                   observed, len(verdicts), {"home_ra_minus_lst_h": _median([e + 6.0 for e in errors_ra])})


def check_park(view: CaptureView) -> Finding:
    poses, verdicts = [], []
    for park in view.writes("TELESCOPE_PARK", "PARK"):
        arrived = _arrived_pose(view, park, "parked")
        if arrived is None:
            verdicts.append(False if view.gu_after(park["t"] + 120.0) else None)
            continue
        status_r, coord = arrived
        prop = next(iter(view.updates("TELESCOPE_PARK", park["t"], status_r["t"] + 5.0)[-1:]), None)
        verdicts.append(prop is None or (prop.get("values") or {}).get("PARK") == "On")
        pier = next(iter(view.updates("TELESCOPE_PIER_SIDE", before=status_r["t"] + 3.0)[-1:]), None)
        pier_side = None
        if pier is not None:
            pier_side = "east" if pier["values"].get("PIER_EAST") == "On" else "west"
        poses.append({"ha_h": round(view.ha(coord), 4), "dec_deg": round(float(coord["values"]["DEC"]), 4),
                      "pier_side": pier_side})
    if not verdicts:
        status, observed = NOT_OBSERVED, "no PARK"
    else:
        status = FAIL if False in verdicts else PASS if True in verdicts else INCONCLUSIVE
        observed = f"{verdicts.count(True)}/{len(verdicts)} parked with PARK=On; poses {poses}"
    return Finding("A10", "PARK slews to the stored pose and ends PARKED with PARK=On",
                   "BEHAVIOR 2.4, 4.10; MOCK fixed PARK", status, ":GU# P and TELESCOPE_PARK.PARK=On",
                   observed, len(verdicts), {"park_pose": poses[-1] if poses else None})


def check_drift(view: CaptureView, min_window_s: float = 20.0) -> Finding:
    rates, dec_drifts = [], []
    run: list[dict] = []

    def close() -> None:
        if len(run) >= 2 and run[-1]["t"] - run[0]["t"] >= min_window_s:
            first, last = run[0], run[-1]
            dt_h = (datetime.fromisoformat(last["utc"]) - datetime.fromisoformat(first["utc"])).total_seconds() / 3600
            dra = _wrap_h(float(last["values"]["RA"]) - float(first["values"]["RA"]))
            rates.append(dra / dt_h)
            dec_drifts.append(abs(float(last["values"]["DEC"]) - float(first["values"]["DEC"])))
        run.clear()

    send_times = [s["t"] for s in view.sends]
    gu = None
    events = sorted(view.status + view.updates("EQUATORIAL_EOD_COORD"), key=lambda r: (r["t"], r.get("seq", 0)))
    si = 0
    for r in events:
        while si < len(send_times) and send_times[si] <= r["t"]:
            si += 1
            close()  # any write may change the pose
        if r.get("property") == "OnStep Status":
            gu = r["gu"]
            if gu["slewing"] or gu["tracking"] or gu["parked"] or gu["guiding"]:
                close()
            continue
        if gu is None or gu["slewing"] or gu["tracking"] or gu["parked"]:
            continue
        if abs(float(r["values"]["DEC"])) > 89.5:
            continue  # RA is a convention at the pole
        run.append(r)
    close()
    if not rates:
        return Finding("A11", "Stationary, tracking OFF: RA advances at the sidereal rate, DEC fixed",
                       "BEHAVIOR 2.2; MOCK non-tracking drift", NOT_OBSERVED,
                       f"dRA/dt = {SIDEREAL_RATE:.4f} h/h, DEC fixed",
                       f"no stationary non-tracking window of {min_window_s:.0f} s or more")
    ok = all(abs(rate - SIDEREAL_RATE) <= 0.02 for rate in rates) and max(dec_drifts) <= 0.01
    return Finding("A11", "Stationary, tracking OFF: RA advances at the sidereal rate, DEC fixed",
                   "BEHAVIOR 2.2; MOCK non-tracking drift", PASS if ok else FAIL,
                   f"dRA/dt = {SIDEREAL_RATE:.4f} h/h, DEC fixed",
                   f"dRA/dt {_fmt(rates, 'h/h')}, max DEC change {max(dec_drifts):.5f} deg",
                   len(rates), {"drift_rate_h_per_h": _median(rates)})


def check_status_period(view: CaptureView) -> Finding:
    gaps = []
    for previous, current in zip(view.status, view.status[1:]):
        if not previous["gu"]["slewing"] and not current["gu"]["slewing"]:
            gaps.append(current["t"] - previous["t"])
    # Updates sent together with a state change (gap under 50 ms) are not
    # the periodic republish.
    gaps = [g for g in gaps if 0.05 <= g < 30.0]
    if len(gaps) < 3:
        return Finding("A12", "OnStep Status republish period while stationary", "MOCK status_period_s 0.25 s",
                       NOT_OBSERVED, "about 0.25 s", "fewer than 4 stationary status updates")
    return Finding("A12", "OnStep Status republish period while stationary", "MOCK status_period_s 0.25 s",
                   INFO, "about 0.25 s", _fmt(gaps), len(gaps), {"status_period_s": _median(gaps)})


def check_slew_speed(view: CaptureView) -> Finding:
    speeds = []
    sends = view.writes("EQUATORIAL_EOD_COORD") + view.writes("TELESCOPE_HOME", "GO") + view.writes("TELESCOPE_PARK", "PARK")
    for send in sends:
        start, end = _motion_end(view, send["t"])
        if start is None or end is None or _aborted_between(view, send["t"], end["t"]):
            continue
        before = view.coord_before(send["t"])
        after = _coord_at_rest(view, end)
        if before is None or after is None:
            continue
        dec0, dec1 = float(before["values"]["DEC"]), float(after["values"]["DEC"])
        if max(abs(dec0), abs(dec1)) > 89.0:
            continue  # hour angle is undefined at the pole
        distance = max(abs(_wrap_h(view.ha(after) - view.ha(before))) * 15.0, abs(dec1 - dec0))
        duration = end["t"] - start["t"]
        if distance >= 2.0 and duration > 0:
            speeds.append(distance / duration)
    if not speeds:
        return Finding("A13", "Goto slew speed (longer axis / slew time)", "MOCK slew_deg_per_s 4.0",
                       NOT_OBSERVED, "about 4 deg/s", "no measurable slew of 2 deg or more away from the pole")
    return Finding("A13", "Goto slew speed (longer axis / slew time)", "MOCK slew_deg_per_s 4.0",
                   INFO, "about 4 deg/s", _fmt(speeds, "deg/s"), len(speeds),
                   {"slew_deg_per_s": _median(speeds)})


def check_manual_rearm(view: CaptureView) -> Finding:
    repeats, starts = [], []
    last: dict[str, dict[str, str]] = {}
    for send in view.sends:
        prop = send.get("property")
        if prop not in {"TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"}:
            continue
        values = send.get("values") or {}
        on = {k for k, v in values.items() if v == "On"}
        previous = last.get(prop, {})
        last[prop] = {**previous, **values}
        if not on:
            continue
        before = view.gu_before(send["t"])
        if before is None or before["gu"]["slewing"]:
            continue
        moved = any(r["gu"]["slewing"] for r in view.gu_after(send["t"], send["t"] + 1.5))
        if all(previous.get(k) == "On" for k in on) and previous:
            repeats.append(moved)
        else:
            starts.append(moved)
    values = {"manual_starts_motion": all(starts) if starts else None,
              "repeat_starts_motion": any(repeats) if repeats else None}
    if not starts and not repeats:
        return Finding("A14", "Manual motion starts after OFF re-arm; a repeated On starts nothing",
                       "BEHAVIOR 4.6; MOCK manual motion", NOT_OBSERVED,
                       "re-armed On moves; repeated On without OFF does not", "no manual motion from standstill")
    failed = (starts and not all(starts)) or any(repeats)
    observed = (f"re-armed On moved {sum(starts)}/{len(starts)}, "
                f"repeated On moved {sum(repeats)}/{len(repeats)}")
    return Finding("A14", "Manual motion starts after OFF re-arm; a repeated On starts nothing",
                   "BEHAVIOR 4.6; MOCK manual motion", FAIL if failed else PASS,
                   "re-armed On moves; repeated On without OFF does not", observed,
                   len(starts) + len(repeats), values)


def check_track_off(view: CaptureView) -> Finding:
    verdicts = []
    for send in view.writes("TELESCOPE_TRACK_STATE", "TRACK_OFF"):
        before = view.gu_before(send["t"])
        if before is None or before["gu"]["slewing"]:
            continue
        next_send = next((o["t"] for o in view.sends if o["t"] > send["t"]), float("inf"))
        after = view.gu_after(send["t"], min(send["t"] + 2.0, next_send))
        if not after:
            continue
        verdicts.append(not any(r["gu"]["slewing"] for r in after) and not after[-1]["gu"]["tracking"])
    if not verdicts:
        return Finding("A15", "TRACK_OFF stops tracking and moves nothing", "BEHAVIOR 4.7",
                       NOT_OBSERVED, ":GU# n within 2 s, no motion", "no TRACK_OFF while stationary")
    return Finding("A15", "TRACK_OFF stops tracking and moves nothing", "BEHAVIOR 4.7",
                   PASS if all(verdicts) else FAIL, ":GU# n within 2 s, no motion",
                   f"{sum(verdicts)}/{len(verdicts)} as expected", len(verdicts))


def check_gu_flags(view: CaptureView) -> Finding:
    raws = [r["values"].get(":GU# return", "") for r in view.recv if r.get("property") == "OnStep Status"]
    letters = set("".join(ch for raw in raws for ch in raw.rstrip("#") if ch.isalpha()))
    undecodable = sum(1 for r in view.recv if r.get("property") == "OnStep Status"
                      and r["values"].get(":GU# return") and not r.get("gu"))
    unknown = sorted(letters - MOCK_GU_FLAGS)
    if not raws:
        return Finding("A16", ":GU# flags are the ones the mock models", "BEHAVIOR 6; MOCK raw_gu",
                       NOT_OBSERVED, f"letters within {''.join(sorted(MOCK_GU_FLAGS))}", "no OnStep Status")
    status = FAIL if undecodable else INFO if unknown else PASS
    observed = f"letters {''.join(sorted(letters))}; not modelled: {''.join(unknown) or 'none'}; undecodable {undecodable}"
    return Finding("A16", ":GU# flags are the ones the mock models", "BEHAVIOR 6; MOCK raw_gu", status,
                   f"letters within {''.join(sorted(MOCK_GU_FLAGS))}", observed, len(raws),
                   {"gu_letters": "".join(sorted(letters)), "gu_unknown": "".join(unknown)})


def property_inventory(view: CaptureView, device: str | None = None) -> dict[str, dict[str, Any]]:
    """Property definitions the driver published: kind, perm, rule, elements."""
    inventory: dict[str, dict[str, Any]] = {}
    for r in view.recv:
        if not str(r.get("tag", "")).startswith("def") or (device and r.get("device") != device):
            continue
        inventory[r["property"]] = {
            "kind": r.get("kind"), "perm": r.get("perm"), "rule": r.get("rule"),
            "elements": r.get("elements") or sorted(r.get("values") or {}),
        }
    return inventory


CHECKS = (
    check_forbidden_indi, check_forbidden_lx200, check_unpark_tracking, check_abort_settle,
    check_abort_ack, check_abort_clears_motion, check_goto_coord_states, check_arrival_order,
    check_home, check_park, check_drift, check_status_period, check_slew_speed,
    check_manual_rearm, check_track_off, check_gu_flags,
)


def check_assumptions(records: Iterable[dict[str, Any]]) -> list[Finding]:
    view = records if isinstance(records, CaptureView) else CaptureView(records)
    return [check(view) for check in CHECKS]


def calibrate(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """``SimConfig`` field overrides measured in a capture (only what was seen)."""
    view = records if isinstance(records, CaptureView) else CaptureView(records)
    values: dict[str, Any] = {}
    for finding in check_assumptions(view):
        values.update(finding.values)
    overrides: dict[str, Any] = {}
    if view.latitude is not None:
        overrides["observer_lat"] = view.latitude
    if view.longitude is not None:
        overrides["observer_lon"] = view.longitude - 360.0 if view.longitude > 180.0 else view.longitude
    for key in ("abort_settle_s", "status_period_s", "slew_deg_per_s",
                "auto_track_on_unpark", "auto_track_on_goto_arrival", "park_pose"):
        if values.get(key) is not None:
            overrides[key] = values[key]
    return overrides


# ---------------------------------------------------------- operation splits
ACTIONS = {
    ("TELESCOPE_PARK", "PARK"): "park",
    ("TELESCOPE_PARK", "UNPARK"): "unpark",
    ("TELESCOPE_HOME", "GO"): "go_home",
    ("TELESCOPE_ABORT_MOTION", "ABORT"): "abort",
    ("TELESCOPE_TRACK_STATE", "TRACK_ON"): "track_on",
    ("TELESCOPE_TRACK_STATE", "TRACK_OFF"): "track_off",
    ("TELESCOPE_MOTION_NS", "MOTION_NORTH"): "move_north",
    ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"): "move_south",
    ("TELESCOPE_MOTION_WE", "MOTION_WEST"): "move_west",
    ("TELESCOPE_MOTION_WE", "MOTION_EAST"): "move_east",
}


def action_name(send: dict[str, Any]) -> str | None:
    """The mount action a client write requests, or None for a setup write."""
    prop, values = send.get("property"), send.get("values") or {}
    label = forbidden_indi(prop, values)
    if label:
        return f"FORBIDDEN {label}"
    if prop == "EQUATORIAL_EOD_COORD":
        return "goto"
    if prop in {"TELESCOPE_TIMED_GUIDE_NS", "TELESCOPE_TIMED_GUIDE_WE"}:
        return "guide" if any(_nonzero(v) for v in values.values()) else None
    for element, value in values.items():
        if value == "On" and (prop, element) in ACTIONS:
            return ACTIONS[(prop, element)]
    return None


def _nonzero(value: str) -> bool:
    try:
        return float(value) != 0.0
    except ValueError:
        return False


def segment_operations(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert synthetic ``call``/``return`` records before each action write.

    ``trace_compare.build_operations`` then splits a passive capture into
    operations (one per park, goto, abort, ...) exactly as it splits an
    adapter trace, so a real capture and its replay can be diffed. An adapter
    trace that already has ``call`` records is returned unchanged.
    """
    records = list(records)
    if any(r.get("type") == "call" for r in records):
        return records
    view = CaptureView(records)
    keep = [r for r in view.records if r.get("type") != "recv" or r.get("conn") == view.primary]
    out: list[dict[str, Any]] = []
    call_id = 0
    for r in keep:
        name = action_name(r) if r.get("type") == "send" and str(r.get("tag", "")).startswith("new") else None
        if name is not None:
            call_id += 1
            base = {"t": r["t"], "utc": r.get("utc"), "thread": "capture", "seq": r.get("seq")}
            out.append({**base, "type": "call", "call_id": call_id, "op": name,
                        "args": [r.get("values")], "kwargs": {}})
            out.append({**base, "type": "return", "call_id": call_id, "op": name,
                        "duration_s": 0.0, "result": None})
        out.append(r)
    return out
