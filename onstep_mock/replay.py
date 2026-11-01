"""Replay the client writes of a capture against the mock and capture the result.

The replay only ever talks to a ``SimulatedOnStepServer`` it starts itself;
it has no host or port parameter and cannot reach a real indiserver. The
mock's traffic is recorded through the same ``CaptureProxy`` that records
the real mount, so both captures can be checked and diffed the same way.

Timing: writes keep their recorded spacing (divided by ``speed``). When the
real mount was stationary before a write, the replay first waits until the
mock is stationary too (a slower or faster mock slew does not shift the
command into a different state); the rest of the timeline moves with it.

Writes that must never be sent (SET HOME, SET PARK, SYNC) are skipped and
reported, as are connection and ``getProperties`` traffic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import io
import json
import socket
import threading
import time
from typing import Any, Iterable

from onstep_adapter.capture.analysis import CaptureView
from onstep_adapter.capture.proxy import CaptureProxy
from onstep_adapter.capture.stream import forbidden_indi, open_capture
from onstep_adapter.indi_gu import local_sidereal_hours

from .mount import Pose, SimConfig
from .server import IndiWrite, SimulatedOnStepServer

SKIPPED_PROPERTIES = {"CONNECTION"}


@dataclass
class ReplayResult:
    records: list[dict[str, Any]]  # the mock's capture, ``t`` in recorded time
    config: SimConfig
    sent: int = 0
    skipped: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[IndiWrite] = field(default_factory=list)
    sync_timeouts: int = 0


def initial_config(view: CaptureView, base: SimConfig | None = None) -> SimConfig:
    """Start the mock where the recorded mount started: site, pose, park state."""
    config = base or SimConfig()
    overrides: dict[str, Any] = {}
    if view.latitude is not None:
        overrides["observer_lat"] = view.latitude
    lon = view.longitude
    if lon is not None:
        overrides["observer_lon"] = lon - 360.0 if lon > 180.0 else lon
    gu = view.status[0]["gu"] if view.status else None
    coord = next((r for r in view.recv if r.get("property") == "EQUATORIAL_EOD_COORD"), None)
    pier = next((r for r in view.recv if r.get("property") == "TELESCOPE_PIER_SIDE"), None)
    if coord is not None:
        site_lon = overrides.get("observer_lon", config.observer_lon)
        lst = local_sidereal_hours(site_lon, datetime.fromisoformat(coord["utc"]))
        ra, dec = float(coord["values"]["RA"]), float(coord["values"]["DEC"])
        side = "west"
        if pier is not None and (pier.get("values") or {}).get("PIER_EAST") == "On":
            side = "east"
        pose = Pose(ha_h=(lst - ra + 12.0) % 24.0 - 12.0, dec_deg=dec, pier_side=side)
        if gu is not None and gu.get("at_home"):
            pose = config.home_pose
        parked = bool(gu and gu.get("parked"))
        overrides["start_parked"] = parked
        if parked:
            overrides["park_pose"] = pose  # the recorded park pose is the stored one
            overrides["start_pose"] = None
        else:
            overrides["start_pose"] = pose
    return replace(config, **overrides)


def replay_capture(
    records: Iterable[dict[str, Any]],
    *,
    config: SimConfig | None = None,
    speed: float = 1.0,  # replay this many times faster than recorded
    sync_timeout_s: float = 300.0,
    tail_s: float | None = None,
) -> ReplayResult:
    view = records if isinstance(records, CaptureView) else CaptureView(records)
    config = initial_config(view, config)
    # ``config.speed`` is the speed of the recorded mount (1 for hardware);
    # the replay runs ``speed`` times faster than the recording.
    config = replace(config, speed=config.speed * speed)
    plan = _plan(view)
    stream = io.StringIO()
    writer = open_capture(stream, label="replay", source="replay")
    result = ReplayResult(records=[], config=config, skipped=plan["skipped"])
    with SimulatedOnStepServer(config) as sim:
        if not config.start_parked and view.status and view.status[0]["gu"].get("tracking"):
            sim.place(tracking=True)
        proxy = CaptureProxy(writer, upstream=(sim.host, sim.port), listen=("127.0.0.1", 0))
        proxy.start()
        try:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5.0)
            drain = threading.Thread(target=_drain, args=(sock,), name="replay-drain", daemon=True)
            drain.start()
            sock.sendall(b'<getProperties version="1.7"/>')
            time.sleep(0.5 / speed + 0.3)
            started = time.monotonic()
            shift = 0.0
            for step in plan["steps"]:
                due = started + shift + step["at"] / speed
                if step["wait_stationary"]:
                    try:
                        sim.wait_until(lambda m: m.motion == "IDLE", timeout=sync_timeout_s / speed)
                    except TimeoutError:
                        result.sync_timeouts += 1
                    # Keep the recorded delay after the mount came to rest.
                    rest_due = time.monotonic() + step["after_rest"] / speed
                    if rest_due > due:
                        shift += rest_due - due
                        due = rest_due
                _sleep_until(due)
                received = len(sim.writes)
                sock.sendall(step["xml"].encode("utf-8"))
                result.sent += 1
                # Wait until the mock has applied the write, so the next
                # step's state check cannot see the state from before it.
                _wait_for(lambda: len(sim.writes) > received, 2.0)
            tail = plan["tail"] if tail_s is None else tail_s
            time.sleep(min(max(tail / speed, 1.0), 30.0))
            sock.close()
        finally:
            proxy.stop()
        result.rejected = [w for w in sim.writes if not w.accepted]
    result.records = rescale(
        [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()], speed,
    )
    return result


def _plan(view: CaptureView) -> dict[str, Any]:
    steps, skipped = [], []
    sends = [s for s in view.sends if s.get("xml")]
    if not sends:
        return {"steps": [], "skipped": [], "tail": 0.0}
    t0 = sends[0]["t"]
    previous_t = t0
    for send in sends:
        label = forbidden_indi(send.get("property"), send.get("values") or {})
        if label or send.get("property") in SKIPPED_PROPERTIES:
            skipped.append({"t": send["t"], "property": send.get("property"),
                            "values": send.get("values"), "reason": label or "connection control"})
            continue
        before = view.gu_before(send["t"])
        stationary = bool(before is not None and not before["gu"]["slewing"])
        rest_t = before["t"] if stationary else send["t"]
        # When did the real mount come to rest before this write?
        if stationary:
            for r in reversed([g for g in view.status if g["t"] < send["t"]]):
                if r["gu"]["slewing"]:
                    break
                rest_t = r["t"]
        steps.append({
            "at": send["t"] - t0, "xml": send["xml"], "wait_stationary": stationary,
            "after_rest": max(0.0, send["t"] - max(rest_t, previous_t)),
        })
        previous_t = send["t"]
    last_t = max(float(r.get("t", 0.0)) for r in view.records)
    return {"steps": steps, "skipped": skipped, "tail": last_t - sends[-1]["t"]}


def _wait_for(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.002)
    return True


def rescale(records: list[dict[str, Any]], speed: float) -> list[dict[str, Any]]:
    """Stretch a replay's ``t`` back to the recorded time base.

    ``utc`` is left alone: the mock's sky (LST, drift) runs in real time.
    """
    if speed == 1.0:
        return records
    return [{**r, "t": round(float(r.get("t", 0.0)) * speed, 6)} for r in records]


def _sleep_until(deadline: float) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def _drain(sock: socket.socket) -> None:
    try:
        while sock.recv(65536):
            pass
    except OSError:
        pass
