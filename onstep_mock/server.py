"""In-process INDI server that impersonates indiserver + LX200 OnStep + mount.

The real ``onstep_adapter`` connects to it over TCP exactly as it connects to
the Raspberry's indiserver, so every adapter layer above the socket runs
unchanged. One device thread moves the virtual mount in real time and
publishes property updates; client threads only queue state changes, so a
slew keeps running while an adapter call waits and an ABORT is honored at
any moment.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from datetime import datetime, timezone
import socket
import threading
import time
from typing import Callable
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from .mount import MountSnapshot, SimConfig, VirtualMount

DEVICE = "LX200 OnStep"

# Writes that would change a stored reference or the pointing model.
FORBIDDEN_WRITES = {
    ("TELESCOPE_HOME", "SET"): "SET HOME is not permitted",
    ("ON_COORD_SET", "SYNC"): "SYNC is not permitted",
    ("TELESCOPE_PARK_OPTION", None): "SET PARK is not permitted",
}


@dataclass
class Prop:
    kind: str  # Switch | Number | Text
    perm: str
    elements: dict[str, str]
    state: str = "Idle"
    rule: str = "OneOfMany"


@dataclass(frozen=True)
class IndiWrite:
    """One INDI write received from a client."""

    t: float  # seconds since the server started
    utc: str
    property: str
    values: dict[str, str]
    accepted: bool
    reason: str | None = None


@dataclass
class _Client:
    connection: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)


class SimulatedOnStepServer:
    """A virtual OnStepX mount behind a local INDI server.

    Use as a context manager or call ``start()``/``stop()``. Point the adapter
    at ``host``/``port``. Inspect ``mount``, ``writes`` and ``forbidden``.
    """

    def __init__(self, config: SimConfig | None = None, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.config = config or SimConfig()
        self.host = host
        self._requested_port = port
        self.port = 0
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._mount = VirtualMount.create(self.config)
        self._props = self._build_properties()
        self._writes: list[IndiWrite] = []
        self._clients: list[_Client] = []
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._running = False
        self._t0 = time.monotonic()
        self._status_frozen = False
        self._reject_next: dict[str, str] = {}
        self._focuser_target: int | None = None
        self._focuser_position = 15000.0
        self._last_publish = 0.0
        self._guide_props: set[str] = set()

    # ------------------------------------------------------------------ setup
    def _build_properties(self) -> dict[str, Prop]:
        cfg = self.config
        props = {
            "CONNECTION": Prop("Switch", "rw", {"CONNECT": "On", "DISCONNECT": "Off"}, "Ok"),
            "OnStep Status": Prop("Text", "ro", {":GU# return": "", "Error": "None"}, "Ok"),
            "EQUATORIAL_EOD_COORD": Prop("Number", "rw", {"RA": "0", "DEC": "0"}),
            "ON_COORD_SET": Prop("Switch", "rw", {"TRACK": "Off", "SLEW": "On", "SYNC": "Off"}, "Ok"),
            "TELESCOPE_PARK": Prop("Switch", "rw", {"PARK": "On", "UNPARK": "Off"}, "Ok"),
            "TELESCOPE_PARK_OPTION": Prop("Switch", "rw", {
                "PARK_CURRENT": "Off", "PARK_DEFAULT": "Off",
                "PARK_WRITE_DATA": "Off", "PARK_PURGE_DATA": "Off",
            }, "Idle", "AtMostOne"),
            "TELESCOPE_HOME": Prop("Switch", "rw", {"GO": "Off", "SET": "Off"}, "Idle", "AtMostOne"),
            "TELESCOPE_TRACK_STATE": Prop("Switch", "rw", {"TRACK_ON": "Off", "TRACK_OFF": "On"}, "Ok"),
            "TELESCOPE_ABORT_MOTION": Prop("Switch", "rw", {"ABORT": "Off"}, "Idle", "AtMostOne"),
            "TELESCOPE_MOTION_NS": Prop("Switch", "rw", {"MOTION_NORTH": "Off", "MOTION_SOUTH": "Off"}, "Idle", "AtMostOne"),
            "TELESCOPE_MOTION_WE": Prop("Switch", "rw", {"MOTION_WEST": "Off", "MOTION_EAST": "Off"}, "Idle", "AtMostOne"),
            "TELESCOPE_SLEW_RATE": Prop("Switch", "rw", {str(i): ("On" if i == 6 else "Off") for i in range(10)}, "Ok"),
            "TELESCOPE_TIMED_GUIDE_NS": Prop("Number", "rw", {"TIMED_GUIDE_N": "0", "TIMED_GUIDE_S": "0"}),
            "TELESCOPE_TIMED_GUIDE_WE": Prop("Number", "rw", {"TIMED_GUIDE_W": "0", "TIMED_GUIDE_E": "0"}),
            "TELESCOPE_PIER_SIDE": Prop("Switch", "ro", {"PIER_WEST": "On", "PIER_EAST": "Off"}, "Ok"),
            "TIME_UTC": Prop("Text", "rw", {
                "UTC": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "OFFSET": "0.00",
            }, "Ok"),
            "GEOGRAPHIC_COORD": Prop("Number", "rw", {
                "LAT": f"{cfg.observer_lat:.12g}", "LONG": f"{cfg.observer_lon:.12g}", "ELEV": "0",
            }, "Ok"),
            "SAFE_MERIDIAN_FLIP": Prop("Switch", "rw", {"ON": "On", "OFF": "Off"}, "Ok"),
            "Minutes Past Meridian": Prop("Number", "rw", {
                "East": f"{cfg.guard_minutes_east:.12g}", "West": f"{cfg.guard_minutes_west:.12g}",
            }, "Ok"),
            "ABS_FOCUS_POSITION": Prop("Number", "rw", {"FOCUS_ABSOLUTE_POSITION": "15000"}, "Ok"),
            "FOCUS_MAX": Prop("Number", "rw", {"FOCUS_MAX_VALUE": str(cfg.focuser_max)}, "Ok"),
            "FOCUS_ABORT_MOTION": Prop("Switch", "rw", {"ABORT": "Off"}, "Idle", "AtMostOne"),
        }
        return props

    # --------------------------------------------------------------- lifecycle
    def start(self) -> SimulatedOnStepServer:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self._requested_port))
        listener.listen(8)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._running = True
        with self._lock:
            self._refresh_mount_props(time.monotonic())
        for target, name in ((self._accept_loop, "onstep-mock-accept"), (self._device_loop, "onstep-mock-device")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._running = False
        self.drop_connections()
        if self._listener is not None:
            self._listener.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._threads.clear()

    def __enter__(self) -> SimulatedOnStepServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -------------------------------------------------------------- inspection
    @property
    def mount(self) -> MountSnapshot:
        with self._lock:
            return self._mount.snapshot(time.monotonic())

    @property
    def writes(self) -> list[IndiWrite]:
        with self._lock:
            return list(self._writes)

    @property
    def forbidden(self) -> list[IndiWrite]:
        return [w for w in self.writes if w.reason and "not permitted" in w.reason]

    def assert_no_forbidden(self) -> None:
        bad = self.forbidden
        if bad:
            raise AssertionError(f"Forbidden INDI writes received: {bad}")

    def wait_until(self, predicate: Callable[[MountSnapshot], bool], timeout: float = 10.0) -> MountSnapshot:
        """Block the caller (not the device) until predicate(mount) holds."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while True:
                snapshot = self._mount.snapshot(time.monotonic())
                if predicate(snapshot):
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Mount condition not reached; last state {snapshot}")
                self._changed.wait(min(remaining, 0.05))

    # ---------------------------------------------------------- test controls
    def place(self, *, ha_h: float | None = None, dec_deg: float | None = None,
              pier_side: str | None = None, parked: bool | None = None,
              tracking: bool | None = None) -> None:
        """Test setup only: set internal truth directly (not an INDI command)."""
        now = time.monotonic()
        with self._lock:
            mount = self._mount
            mount.motion, mount.target_kind = "IDLE", None
            mount.manual_ns = mount.manual_we = 0
            if ha_h is not None:
                mount.ha_h = ha_h
            if dec_deg is not None:
                mount.dec_deg = dec_deg
            if pier_side is not None:
                mount.pier_side = pier_side
            if parked is not None:
                mount.parked = parked
                if parked:
                    pose = self.config.park_pose
                    if ha_h is None:
                        mount.ha_h = pose.ha_h
                    if dec_deg is None:
                        mount.dec_deg = pose.dec_deg
                    if pier_side is None:
                        mount.pier_side = pose.pier_side
            if tracking is not None:
                mount.set_tracking(tracking, now)
            mount.at_home = False
            self._publish_mount(now)

    def inject_fault(self, fault: str = "LIMIT") -> None:
        """LIMIT sets the :GU# limit flag; ERROR sets OnStep Status.Error."""
        with self._lock:
            self._mount.set_fault(fault, time.monotonic())
            self._publish_mount(time.monotonic())

    def clear_fault(self) -> None:
        with self._lock:
            self._mount.set_fault(None, time.monotonic())
            self._publish_mount(time.monotonic())

    def reject_next(self, property_name: str, reason: str = "injected rejection") -> None:
        with self._lock:
            self._reject_next[property_name] = reason

    def freeze_status(self, frozen: bool = True) -> None:
        """Stop (or resume) publishing OnStep Status to test stale handling."""
        with self._lock:
            self._status_frozen = frozen

    def drop_connections(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.connection.close()

    # ------------------------------------------------------------ networking
    def _accept_loop(self) -> None:
        while self._running:
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            connection.settimeout(0.2)
            client = _Client(connection)
            with self._lock:
                self._clients.append(client)
            thread = threading.Thread(target=self._client_loop, args=(client,), name="onstep-mock-client", daemon=True)
            thread.start()

    def _client_loop(self, client: _Client) -> None:
        parser = ET.XMLPullParser(["start", "end"])
        parser.feed("<stream>")
        decoder = codecs.getincrementaldecoder("utf-8")()
        root = None
        try:
            while self._running:
                try:
                    chunk = client.connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                parser.feed(decoder.decode(chunk))
                for event, node in parser.read_events():
                    if event == "start" and node.tag == "stream":
                        root = node
                        continue
                    if event != "end" or node.tag == "stream" or (root is not None and node not in list(root)):
                        continue
                    if node.tag == "getProperties":
                        self._send_definitions(client)
                    elif node.tag.startswith("new") and node.tag.endswith("Vector"):
                        self._handle_write(node)
                    if root is not None:
                        root.remove(node)
        except (OSError, ET.ParseError):
            return
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)

    def _send_definitions(self, client: _Client) -> None:
        with self._lock:
            payload = "".join(self._vector_xml("def", name, prop) for name, prop in self._props.items())
        self._send(client, payload)

    def _vector_xml(self, prefix: str, name: str, prop: Prop, only: dict[str, str] | None = None) -> str:
        kind = prop.kind
        element_tag = f"{prefix}{kind}" if prefix == "def" else f"one{kind}"
        attrs = f"device={quoteattr(DEVICE)} name={quoteattr(name)} state={quoteattr(prop.state)}"
        if prefix == "def":
            attrs += f" perm={quoteattr(prop.perm)}"
            if kind == "Switch":
                attrs += f" rule={quoteattr(prop.rule)}"
        body = "".join(
            f"<{element_tag} name={quoteattr(key)}>{escape(value)}</{element_tag}>"
            for key, value in (only or prop.elements).items()
        )
        return f"<{prefix}{kind}Vector {attrs}>{body}</{prefix}{kind}Vector>"

    def _send(self, client: _Client, payload: str) -> None:
        try:
            with client.lock:
                client.connection.sendall(payload.encode("utf-8"))
        except OSError:
            pass

    def _publish(self, name: str) -> None:
        """Broadcast one property update; caller holds the lock."""
        payload = self._vector_xml("set", name, self._props[name])
        for client in list(self._clients):
            self._send(client, payload)
        self._changed.notify_all()

    # ---------------------------------------------------------------- writes
    def _handle_write(self, node: ET.Element) -> None:
        name = node.get("name") or ""
        values = {child.get("name"): (child.text or "").strip() for child in node if child.get("name")}
        now = time.monotonic()
        with self._lock:
            reason = self._apply_write(name, node.tag, values, now)
            accepted = reason is None
            self._writes.append(IndiWrite(
                t=round(now - self._t0, 6), utc=datetime.now(timezone.utc).isoformat(),
                property=name, values=values, accepted=accepted, reason=reason,
            ))
            if name in self._props:
                prop = self._props[name]
                if not accepted:
                    prop.state = "Alert"
                self._publish(name)
            self._publish_mount(now)

    def _apply_write(self, name: str, tag: str, values: dict[str, str], now: float) -> str | None:
        on = [key for key, value in values.items() if value == "On"]
        for (prop_name, element), reason in FORBIDDEN_WRITES.items():
            if name == prop_name and (element is None or element in on):
                return reason
        if name in self._reject_next:
            return self._reject_next.pop(name)
        prop = self._props.get(name)
        if prop is None:
            return f"unknown property {name}"
        if prop.perm == "ro":
            return f"{name} is read-only"
        if not set(values) <= set(prop.elements):
            return f"unknown element in {name}"
        mount = self._mount
        if prop.kind == "Switch":
            return self._apply_switch(name, prop, values, on, now)
        if name == "EQUATORIAL_EOD_COORD":
            try:
                ra, dec = float(values["RA"]), float(values["DEC"])
            except (KeyError, ValueError):
                return "RA and DEC are required"
            # SYNC can never be selected (rejected above), so a target is a goto.
            reason = mount.start_goto("radec", now, ra, dec)
            if reason is None:
                prop.state = "Busy"
            return reason
        if name in {"TELESCOPE_TIMED_GUIDE_NS", "TELESCOPE_TIMED_GUIDE_WE"}:
            element, raw = next(iter(values.items()))
            ms = float(raw)
            axis = "ns" if name.endswith("NS") else "we"
            direction = {"TIMED_GUIDE_N": 1, "TIMED_GUIDE_S": -1, "TIMED_GUIDE_W": 1, "TIMED_GUIDE_E": -1}[element]
            reason = mount.guide(axis, direction, ms, now)
            if reason is None:
                prop.elements.update(values)
                prop.state = "Busy"
                self._guide_props.add(name)
            return reason
        if name == "ABS_FOCUS_POSITION":
            target = int(float(values["FOCUS_ABSOLUTE_POSITION"]))
            if not 0 <= target <= self.config.focuser_max:
                return "focuser target out of range"
            self._focuser_target = target
            prop.state = "Busy"
            return None
        prop.elements.update(values)
        prop.state = "Ok"
        return None

    def _apply_switch(self, name: str, prop: Prop, values: dict[str, str], on: list[str], now: float) -> str | None:
        mount = self._mount
        if name == "TELESCOPE_ABORT_MOTION":
            mount.abort(now)
            for motion in ("TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"):
                self._props[motion].elements = dict.fromkeys(self._props[motion].elements, "Off")
                self._props[motion].state = "Idle"
                self._publish(motion)
            for guide in self._guide_props:
                self._props[guide].state = "Ok"
            prop.state = "Ok"
            return None
        if name == "FOCUS_ABORT_MOTION":
            self._focuser_target = None
            self._props["ABS_FOCUS_POSITION"].state = "Ok"
            prop.state = "Ok"
            return None
        if name in {"TELESCOPE_MOTION_NS", "TELESCOPE_MOTION_WE"}:
            axis = "ns" if name.endswith("NS") else "we"
            positive = "MOTION_NORTH" if axis == "ns" else "MOTION_WEST"
            negative = "MOTION_SOUTH" if axis == "ns" else "MOTION_EAST"
            previous = dict(prop.elements)
            prop.elements.update(values)
            if prop.elements[positive] == "On" and prop.elements[negative] == "On":
                prop.elements = previous
                return "only one direction may be On"
            direction = 1 if prop.elements[positive] == "On" else -1 if prop.elements[negative] == "On" else 0
            if direction and previous == prop.elements:
                # Same direction without an OFF re-arm: accepted, nothing new starts.
                prop.state = "Busy"
                return None
            reason = mount.start_manual(axis, direction, now)
            if reason is not None:
                prop.elements = previous
                return reason
            prop.state = "Busy" if direction else "Idle"
            return None
        if len(on) != 1 and prop.rule == "OneOfMany":
            return f"{name} needs exactly one On element"
        element = on[0] if on else None
        reason = None
        if name == "TELESCOPE_PARK":
            reason = mount.start_goto("park", now) if element == "PARK" else mount.unpark(now)
            if reason is None and element == "PARK" and not mount.parked:
                prop.state = "Busy"
                prop.elements = {"PARK": "Off", "UNPARK": "On"}
                return None
        elif name == "TELESCOPE_HOME" and element == "GO":
            reason = mount.start_goto("home", now)
            if reason is None:
                prop.state = "Busy"
                prop.elements = {"GO": "On", "SET": "Off"}
                return None
        elif name == "TELESCOPE_TRACK_STATE":
            reason = mount.set_tracking(element == "TRACK_ON", now)
        elif name == "TELESCOPE_SLEW_RATE":
            mount.slew_rate = int(element)
        elif name == "CONNECTION" and element == "DISCONNECT":
            return "the mock device stays connected"
        if reason is not None:
            return reason
        if prop.rule == "OneOfMany" or element is not None:
            prop.elements = {key: ("On" if key == element else "Off") for key in prop.elements}
        prop.state = "Ok"
        return None

    # ------------------------------------------------------------ device loop
    def _device_loop(self) -> None:
        last = time.monotonic()
        while self._running:
            time.sleep(self.config.tick_s)
            now = time.monotonic()
            with self._lock:
                edge = self._mount.tick(now - last, now)
                self._tick_peripherals(now - last, now)
                if edge or now - self._last_publish >= self.config.status_period_s:
                    self._publish_mount(now)
                self._changed.notify_all()
            last = now

    def _tick_peripherals(self, dt: float, now: float) -> None:
        mount = self._mount
        for name in list(self._guide_props):
            if not mount.guiding(now):
                prop = self._props[name]
                prop.elements = dict.fromkeys(prop.elements, "0")
                prop.state = "Ok"
                self._guide_props.discard(name)
                self._publish(name)
        if self._focuser_target is not None:
            remaining = self._focuser_target - self._focuser_position
            step = self.config.focuser_steps_per_s * self.config.speed * dt
            if abs(remaining) <= step:
                self._focuser_position = float(self._focuser_target)
                self._focuser_target = None
                self._props["ABS_FOCUS_POSITION"].state = "Ok"
            else:
                self._focuser_position += step if remaining > 0 else -step
            self._props["ABS_FOCUS_POSITION"].elements["FOCUS_ABSOLUTE_POSITION"] = str(int(round(self._focuser_position)))
            self._publish("ABS_FOCUS_POSITION")

    def _refresh_mount_props(self, now: float) -> None:
        mount = self._mount
        status = self._props["OnStep Status"]
        status.elements[":GU# return"] = mount.raw_gu(now)
        status.elements["Error"] = "None" if mount.fault != "ERROR" else "General error"
        status.state = "Alert" if mount.fault else "Ok"
        coords = self._props["EQUATORIAL_EOD_COORD"]
        coords.elements = {"RA": f"{mount.ra_h():.8f}", "DEC": f"{mount.dec_deg:.8f}"}
        if mount.fault:
            coords.state = "Alert"
        elif mount.motion == "SLEWING":
            coords.state = "Busy"
        elif coords.state == "Busy":
            coords.state = "Ok"
        self._props["TELESCOPE_PIER_SIDE"].elements = {
            "PIER_WEST": "On" if mount.pier_side == "west" else "Off",
            "PIER_EAST": "On" if mount.pier_side == "east" else "Off",
        }
        track = self._props["TELESCOPE_TRACK_STATE"]
        track.elements = {"TRACK_ON": "On" if mount.tracking else "Off", "TRACK_OFF": "Off" if mount.tracking else "On"}
        park = self._props["TELESCOPE_PARK"]
        if mount.motion != "SLEWING" or park.state != "Busy":
            park.elements = {"PARK": "On" if mount.parked else "Off", "UNPARK": "Off" if mount.parked else "On"}
            if park.state == "Busy":
                park.state = "Ok" if mount.parked else "Alert"
        home = self._props["TELESCOPE_HOME"]
        if home.state == "Busy" and mount.motion != "SLEWING":
            home.state = "Ok" if mount.at_home else "Alert"
            home.elements = {"GO": "Off", "SET": "Off"}

    def _publish_mount(self, now: float) -> None:
        """Publish status first, then coordinates, as OnStepX does at arrival."""
        previous = {
            name: (dict(self._props[name].elements), self._props[name].state)
            for name in ("TELESCOPE_TRACK_STATE", "TELESCOPE_PARK", "TELESCOPE_HOME")
        }
        self._refresh_mount_props(now)
        if not self._status_frozen:
            self._publish("OnStep Status")
        self._publish("EQUATORIAL_EOD_COORD")
        self._publish("TELESCOPE_PIER_SIDE")
        for name, (elements, state) in previous.items():
            prop = self._props[name]
            if prop.elements != elements or prop.state != state:
                self._publish(name)
        self._last_publish = now
