"""Decode captured INDI and LX200 byte streams into trace records.

A capture is written in the trace format of :mod:`onstep_adapter.indi_trace`
(JSON Lines, ``session`` record first) so that ``trace_compare`` and the
analysis in :mod:`onstep_adapter.capture.analysis` read it unchanged. Extra
record types and fields:

``capture``  metadata: mode (proxy | pcap), endpoints, protocol.
``connect``  ``conn`` id, ``protocol`` (indi | lx200), ``peer``.
``close``    ``conn`` id.
``send``     one INDI element written by the client (``conn``, ``tag``,
             ``device``, ``property``, ``values``, raw ``xml``,
             ``forbidden`` when it is a SET HOME, SET PARK or SYNC write).
``recv``     one INDI element sent by indiserver to that client, with the
             same fields as an adapter trace (``state``, ``changed``,
             ``revision``, decoded ``gu``) plus ``conn``.
``lx200``    ``dir`` cmd | reply, ``conn``, ``data``; commands are split on
             ``#``; ``forbidden`` marks a set-park, set-home or sync command.
``decode_error``  a stream that could not be parsed (``conn``, ``dir``).

Decoding never feeds anything back into a stream: the caller has already
forwarded (or merely observed) the bytes.
"""

from __future__ import annotations

import codecs
from datetime import datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from ..indi_trace import TraceRecorder, gu_summary

CAPTURE_SOURCE = "capture"

# INDI writes that change a stored reference or the pointing model. They must
# never be sent; a capture flags them so the analysis can fail loudly.
FORBIDDEN_INDI = {
    ("TELESCOPE_HOME", "SET"): "SET HOME",
    ("ON_COORD_SET", "SYNC"): "SYNC",
    ("TELESCOPE_PARK_OPTION", None): "SET PARK",
}

# The matching OnStepX LX200 commands seen on the driver-to-controller link.
FORBIDDEN_LX200 = {
    ":hQ": "SET PARK (:hQ#)",
    ":hF": "SET HOME (:hF#)",
    ":CS": "SYNC (:CS#)",
    ":CM": "SYNC (:CM#)",
}


def forbidden_indi(prop: str | None, values: dict[str, str]) -> str | None:
    on = {key for key, value in values.items() if value == "On"}
    for (name, element), label in FORBIDDEN_INDI.items():
        if prop == name and (element is None or element in on):
            return label
    return None


def forbidden_lx200(command: str) -> str | None:
    for prefix, label in FORBIDDEN_LX200.items():
        if command.startswith(prefix):
            return label
    return None


class IndiElementDecoder:
    """Incremental parser that yields complete top-level INDI elements."""

    def __init__(self) -> None:
        self.errors = 0
        self._reset()

    def _reset(self) -> None:
        self._parser = ET.XMLPullParser(["start", "end"])
        self._parser.feed("<stream>")
        self._text = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._depth = 0
        self._root: ET.Element | None = None

    def feed(self, data: bytes) -> list[ET.Element]:
        elements: list[ET.Element] = []
        try:
            self._parser.feed(self._text.decode(data))
            for event, node in self._parser.read_events():
                if event == "start":
                    self._depth += 1
                    if self._depth == 1:
                        self._root = node
                    continue
                self._depth -= 1
                if self._depth == 1:
                    elements.append(node)
                    if self._root is not None:
                        self._root.remove(node)
        except ET.ParseError:
            # A capture that starts mid-element cannot be parsed; restart at
            # the next chunk. Elements already decoded are kept.
            self.errors += 1
            self._reset()
        return elements


def _values(node: ET.Element) -> dict[str, str]:
    return {
        child.get("name"): (child.text or "").strip()
        for child in node if child.get("name")
    }


class CaptureWriter:
    """Turn raw stream chunks into capture records.

    ``data()`` is the only entry point for bytes. It is called from a single
    thread (the proxy's recorder thread or the pcap importer) with the time
    the bytes were seen, so record order follows arrival order.
    """

    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder
        self._protocol: dict[int, str] = {}
        self._decoders: dict[tuple[int, str], IndiElementDecoder] = {}
        self._lx_buffer: dict[int, str] = {}
        self._props: dict[tuple[int, str, str], dict[str, Any]] = {}
        self.forbidden: list[dict[str, Any]] = []

    def meta(self, **fields: Any) -> None:
        self.recorder.record("capture", **fields)

    def connect(self, conn: int, protocol: str, peer: str, *, at: float | None = None,
                at_utc: datetime | None = None) -> None:
        self._protocol[conn] = protocol
        self.recorder.record("connect", at=at, at_utc=at_utc, conn=conn, protocol=protocol, peer=peer)

    def close(self, conn: int, *, at: float | None = None, at_utc: datetime | None = None) -> None:
        if conn in self._protocol:
            self.recorder.record("close", at=at, at_utc=at_utc, conn=conn)

    def data(self, conn: int, direction: str, data: bytes, *, at: float | None = None,
             at_utc: datetime | None = None) -> None:
        """``direction`` is ``c2s`` (client to server) or ``s2c``."""
        if self._protocol.get(conn, "indi") == "lx200":
            self._lx200(conn, direction, data, at, at_utc)
            return
        decoder = self._decoders.setdefault((conn, direction), IndiElementDecoder())
        errors = decoder.errors
        for node in decoder.feed(data):
            if direction == "c2s":
                self._send(conn, node, at, at_utc)
            else:
                self._recv(conn, node, at, at_utc)
        if decoder.errors != errors:
            self.recorder.record("decode_error", at=at, at_utc=at_utc, conn=conn, dir=direction)

    def _send(self, conn: int, node: ET.Element, at, at_utc) -> None:
        values = _values(node)
        prop = node.get("name")
        fields: dict[str, Any] = {
            "conn": conn, "tag": node.tag, "device": node.get("device"),
            "property": prop, "values": values,
            "xml": ET.tostring(node, encoding="unicode").strip(),
        }
        label = forbidden_indi(prop, values) if node.tag.startswith("new") else None
        if label:
            fields["forbidden"] = label
            self.forbidden.append(fields)
        self.recorder.record("send", at=at, at_utc=at_utc, **fields)

    def _recv(self, conn: int, node: ET.Element, at, at_utc) -> None:
        tag = node.tag
        device, name = node.get("device") or "", node.get("name") or ""
        fields: dict[str, Any] = {"conn": conn, "tag": tag, "device": device, "property": name}
        if tag == "message":
            fields["message"] = node.get("message")
            self.recorder.record("recv", at=at, at_utc=at_utc, **fields)
            return
        if tag == "delProperty":
            self._props.pop((conn, device, name), None)
            self.recorder.record("recv", at=at, at_utc=at_utc, **fields)
            return
        key = (conn, device, name)
        prop = self._props.get(key)
        values = _values(node)
        if tag.startswith("def") or prop is None:
            kind = tag[3:-6] if tag.endswith("Vector") else tag
            prop = {"kind": kind, "values": {}, "revision": 0}
            if tag.startswith("def"):
                prop["perm"] = node.get("perm")
                prop["rule"] = node.get("rule")
            self._props[key] = prop
        changed = {k: v for k, v in values.items() if prop["values"].get(k) != v}
        prop["values"] = {**prop["values"], **values}
        prop["revision"] += 1
        fields.update(
            kind=prop["kind"], state=node.get("state") or "", changed=changed,
            values=dict(prop["values"]), revision=prop["revision"],
        )
        if tag.startswith("def"):
            fields.update(perm=prop.get("perm"), rule=prop.get("rule"), elements=sorted(values))
        gu = gu_summary(name, prop["values"])
        if gu is not None:
            fields["gu"] = gu
        self.recorder.record("recv", at=at, at_utc=at_utc, **fields)

    def _lx200(self, conn: int, direction: str, data: bytes, at, at_utc) -> None:
        text = data.decode("latin-1")
        if direction == "s2c":
            self.recorder.record("lx200", at=at, at_utc=at_utc, conn=conn, dir="reply", data=text)
            return
        buffer = self._lx_buffer.get(conn, "") + text
        while buffer:
            if buffer[0] == "\x06":  # ACK, the only command without '#'
                self.recorder.record("lx200", at=at, at_utc=at_utc, conn=conn, dir="cmd", data="<ACK>")
                buffer = buffer[1:]
                continue
            end = buffer.find("#")
            if end < 0:
                break
            command, buffer = buffer[: end + 1], buffer[end + 1:]
            fields: dict[str, Any] = {"conn": conn, "dir": "cmd", "data": command}
            label = forbidden_lx200(command.lstrip())
            if label:
                fields["forbidden"] = label
                self.forbidden.append(fields)
            self.recorder.record("lx200", at=at, at_utc=at_utc, **fields)
        self._lx_buffer[conn] = buffer[-256:]


def open_capture(target, *, label: str = "", source: str = CAPTURE_SOURCE) -> CaptureWriter:
    """A writer for a new capture file (path) or stream.

    A capture file holds exactly one session, so an existing file is never
    appended to.
    """
    if isinstance(target, (str, Path)):
        path = Path(target).expanduser()
        if path.exists() and path.stat().st_size:
            raise FileExistsError(f"{path} exists; a capture is never appended to")
    return CaptureWriter(TraceRecorder(target, source=source, label=label))
