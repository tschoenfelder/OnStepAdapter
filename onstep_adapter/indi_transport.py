"""A local INDI client transport that does not own the shared device."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import math
import socket
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr


@dataclass(frozen=True)
class IndiProperty:
    device: str
    name: str
    kind: str
    state: str
    permission: str
    values: dict[str, str]
    received_at: float
    revision: int


class IndiTransport:
    """Subscribe to properties and send bounded INDI requests on one socket."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 7624) -> None:
        self.host = host
        self.port = port
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._properties: dict[tuple[str, str], IndiProperty] = {}
        self._failure: BaseException | None = None
        self._closed = True
        self._revision = 0

    @property
    def is_open(self) -> bool:
        with self._condition:
            return not self._closed and self._failure is None

    def connect(self, *, timeout: float = 3.0) -> None:
        with self._condition:
            if not self._closed:
                return
            self._socket = socket.create_connection((self.host, self.port), timeout=timeout)
            self._socket.settimeout(0.5)
            self._closed = False
            self._failure = None
            self._properties.clear()
            self._reader = threading.Thread(target=self._read_loop, name="onstep-indi-reader", daemon=True)
            self._reader.start()
        self._send('<getProperties version="1.7"/>')

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            connection = self._socket
            reader = self._reader
            self._socket = None
            self._condition.notify_all()
        if connection is not None:
            connection.close()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)

    def get_property(self, device: str, name: str) -> IndiProperty | None:
        with self._condition:
            return self._properties.get((device, name))

    def wait_property(
        self,
        device: str,
        name: str,
        *,
        timeout: float,
        after_revision: int = -1,
    ) -> IndiProperty:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                prop = self._properties.get((device, name))
                if prop is not None and prop.revision > after_revision:
                    return prop
                if self._failure is not None:
                    raise ConnectionError("INDI reader failed") from self._failure
                if self._closed:
                    raise ConnectionError("INDI connection closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"INDI property {device}.{name} did not update")
                self._condition.wait(remaining)

    def set_switch(
        self,
        device: str,
        name: str,
        element: str,
        *,
        timeout: float = 3.0,
    ) -> IndiProperty:
        before = self.wait_property(device, name, timeout=timeout)
        if before.kind != "Switch" or before.permission == "ro" or element not in before.values:
            raise ValueError(f"{device}.{name}.{element} is not a writable INDI switch")
        payload = (
            f"<newSwitchVector device={quoteattr(device)} name={quoteattr(name)}>"
            f"<oneSwitch name={quoteattr(element)}>On</oneSwitch>"
            "</newSwitchVector>"
        )
        self._send(payload)
        deadline = time.monotonic() + timeout
        revision = before.revision
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"INDI switch {device}.{name}.{element} was not confirmed")
            after = self.wait_property(device, name, timeout=remaining, after_revision=revision)
            if after.state.lower() == "alert":
                raise RuntimeError(f"INDI rejected {device}.{name}.{element}")
            if after.values.get(element) == "On" and after.state.lower() == "ok":
                return after
            revision = after.revision

    def request_switch(
        self,
        device: str,
        name: str,
        element: str,
        *,
        timeout: float = 3.0,
    ) -> IndiProperty:
        """Send a momentary switch whose element may reset to Off immediately."""
        before = self.wait_property(device, name, timeout=timeout)
        if before.kind != "Switch" or before.permission == "ro" or element not in before.values:
            raise ValueError(f"{device}.{name}.{element} is not a writable INDI switch")
        self._send(
            f"<newSwitchVector device={quoteattr(device)} name={quoteattr(name)}>"
            f"<oneSwitch name={quoteattr(element)}>On</oneSwitch></newSwitchVector>"
        )
        deadline = time.monotonic() + timeout
        revision = before.revision
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"INDI momentary switch {device}.{name} was not confirmed")
            after = self.wait_property(device, name, timeout=remaining, after_revision=revision)
            if after.state.lower() == "alert":
                raise RuntimeError(f"INDI rejected {device}.{name}.{element}")
            if after.state.lower() == "ok":
                return after
            revision = after.revision

    def issue_switch(self, device: str, name: str, element: str, *, timeout: float = 3.0) -> int:
        """Issue a long-running action; caller must verify live device status."""
        before = self.wait_property(device, name, timeout=timeout)
        if before.kind != "Switch" or before.permission == "ro" or element not in before.values:
            raise ValueError(f"{device}.{name}.{element} is not a writable INDI switch")
        self._send(
            f"<newSwitchVector device={quoteattr(device)} name={quoteattr(name)}>"
            f"<oneSwitch name={quoteattr(element)}>On</oneSwitch></newSwitchVector>"
        )
        return before.revision

    def set_text(
        self,
        device: str,
        name: str,
        values: dict[str, str],
        *,
        timeout: float = 3.0,
    ) -> IndiProperty:
        return self._set_values(device, name, "Text", values, timeout=timeout)

    def set_number(
        self,
        device: str,
        name: str,
        values: dict[str, float],
        *,
        timeout: float = 3.0,
    ) -> IndiProperty:
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("INDI number values must be finite")
        return self._set_values(
            device, name, "Number", {key: format(value, ".12g") for key, value in values.items()},
            timeout=timeout,
        )

    def issue_number(
        self, device: str, name: str, element: str, value: float,
        *, timeout: float = 3.0,
    ) -> None:
        """Start a long-running number action; caller verifies live completion."""
        if not math.isfinite(value):
            raise ValueError("INDI number value must be finite")
        before = self.wait_property(device, name, timeout=timeout)
        if before.kind != "Number" or before.permission == "ro" or element not in before.values:
            raise ValueError(f"{device}.{name}.{element} is not a writable INDI number")
        self._send(
            f"<newNumberVector device={quoteattr(device)} name={quoteattr(name)}>"
            f"<oneNumber name={quoteattr(element)}>{format(value, '.12g')}</oneNumber>"
            "</newNumberVector>"
        )

    def issue_numbers(
        self, device: str, name: str, values: dict[str, float],
        *, timeout: float = 3.0,
    ) -> None:
        """Issue one atomic multi-element target; verify completion separately."""
        if not values or not all(math.isfinite(value) for value in values.values()):
            raise ValueError("Finite INDI target values are required")
        before = self.wait_property(device, name, timeout=timeout)
        if (
            before.kind != "Number" or before.permission == "ro" or
            not values.keys() <= before.values.keys()
        ):
            raise ValueError(f"{device}.{name} is not a writable INDI number target")
        body = "".join(
            f"<oneNumber name={quoteattr(key)}>{format(value, '.12g')}</oneNumber>"
            for key, value in values.items()
        )
        self._send(
            f"<newNumberVector device={quoteattr(device)} name={quoteattr(name)}>"
            f"{body}</newNumberVector>"
        )

    def _set_values(
        self,
        device: str,
        name: str,
        kind: str,
        values: dict[str, str],
        *,
        timeout: float,
    ) -> IndiProperty:
        before = self.wait_property(device, name, timeout=timeout)
        if before.kind != kind or before.permission == "ro" or not values:
            raise ValueError(f"{device}.{name} is not a writable INDI {kind.lower()} property")
        if not values.keys() <= before.values.keys():
            raise ValueError(f"Unknown elements in {device}.{name}")
        body = "".join(
            f"<one{kind} name={quoteattr(key)}>{escape(value)}</one{kind}>"
            for key, value in values.items()
        )
        self._send(
            f"<new{kind}Vector device={quoteattr(device)} name={quoteattr(name)}>"
            f"{body}</new{kind}Vector>"
        )
        deadline = time.monotonic() + timeout
        revision = before.revision
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"INDI {kind.lower()} {device}.{name} was not confirmed")
            after = self.wait_property(device, name, timeout=remaining, after_revision=revision)
            if after.state.lower() == "alert":
                raise RuntimeError(f"INDI rejected {device}.{name}")
            if after.state.lower() == "ok" and all(
                self._matches_value(kind, after.values.get(key), value)
                for key, value in values.items()
            ):
                return after
            revision = after.revision

    @staticmethod
    def _matches_value(kind: str, observed: str | None, requested: str) -> bool:
        if observed is None:
            return False
        if kind == "Text":
            return observed == requested
        try:
            return abs(float(observed) - float(requested)) <= 1e-6
        except ValueError:
            return False

    def _send(self, xml: str) -> None:
        with self._send_lock:
            with self._condition:
                connection = self._socket
                if self._closed or connection is None:
                    raise ConnectionError("INDI connection closed")
            connection.sendall(xml.encode("utf-8"))

    def _read_loop(self) -> None:
        parser = ET.XMLPullParser(["start", "end"])
        parser.feed("<stream>")
        decoder = codecs.getincrementaldecoder("utf-8")()
        root: ET.Element | None = None
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    connection = self._socket
                if connection is None:
                    return
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("INDI server closed the connection")
                parser.feed(decoder.decode(chunk))
                for event, node in parser.read_events():
                    if event == "start" and node.tag == "stream":
                        root = node
                    elif event == "end" and node.tag.startswith(("def", "set")) and node.tag.endswith("Vector"):
                        self._record(node)
                        if root is not None:
                            root.remove(node)
        except (OSError, ET.ParseError, UnicodeError) as exc:
            with self._condition:
                if not self._closed:
                    self._failure = exc
                    self._condition.notify_all()

    def _record(self, node: ET.Element) -> None:
        device = node.get("device")
        name = node.get("name")
        if not device or not name:
            return
        kind = node.tag[3:-6]
        values = {child.get("name"): (child.text or "").strip() for child in node if child.get("name")}
        with self._condition:
            previous = self._properties.get((device, name))
            merged = dict(previous.values) if previous is not None else {}
            merged.update(values)
            self._revision += 1
            self._properties[(device, name)] = IndiProperty(
                device=device,
                name=name,
                kind=kind,
                state=node.get("state", previous.state if previous else "Idle"),
                permission=node.get("perm", previous.permission if previous else "ro"),
                values=merged,
                received_at=time.monotonic(),
                revision=self._revision,
            )
            self._condition.notify_all()
