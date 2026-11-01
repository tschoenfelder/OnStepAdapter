"""Transaction trace for comparing real-mount and expected adapter behavior.

A trace is a JSON Lines file. Every line is one record with a common header:

    seq     monotonically increasing record number within the session
    t       seconds since the session started (monotonic clock)
    utc     wall-clock UTC timestamp (ISO 8601)
    thread  name of the Python thread that produced the record
    type    session | connect | close | send | recv | call | return | error

``send`` records hold every INDI vector this client writes, ``recv`` records
every property definition or update it receives, and ``call``/``return``/
``error`` records frame each public mount operation. Together they let a run
against the real mount be compared with a run against a simulator or with an
earlier reference run (see ``onstep_adapter.tools.trace_compare``).

Recording never changes adapter behavior: write failures are swallowed and
counted so that tracing cannot interrupt a STOP or any other command.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
import json
from pathlib import Path
import threading
import time
import uuid
from typing import IO, Any, Callable
import xml.etree.ElementTree as ET

from .indi_gu import decode_gu

TRACE_FORMAT_VERSION = 1
# In a trace path, replaced by the UTC day (YYYY-MM-DD): one file per day.
DAY_PLACEHOLDER = "{date}"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return repr(value)


def _parse_vector(xml: str) -> dict[str, Any]:
    """Return tag, device, property and element values of one INDI vector."""
    try:
        node = ET.fromstring(xml)
    except ET.ParseError:
        return {"tag": None, "device": None, "property": None, "values": {}}
    return {
        "tag": node.tag,
        "device": node.get("device"),
        "property": node.get("name"),
        "values": {
            child.get("name"): (child.text or "").strip()
            for child in node if child.get("name")
        },
    }


GU_SUMMARY_FIELDS = (
    "motion_state", "parked", "tracking", "slewing",
    "at_home", "guiding", "at_limit", "pier_side",
)


def gu_summary(name: str | None, values: dict[str, str]) -> dict[str, Any] | None:
    """Decoded ``:GU#`` fields of an ``OnStep Status`` update, else None."""
    raw = values.get(":GU# return") if name == "OnStep Status" else None
    if not raw:
        return None
    try:
        decoded = decode_gu(raw)
    except ValueError:
        return None
    return {key: decoded[key] for key in GU_SUMMARY_FIELDS}


class TraceRecorder:
    """Thread-safe JSON Lines recorder shared by transport and mount facade."""

    def __init__(
        self,
        target: str | Path | IO[str],
        *,
        source: str = "real",
        label: str = "",
        include_raw_xml: bool = False,
    ) -> None:
        self._template: str | None = None
        self._day: str | None = None
        if isinstance(target, (str, Path)) and DAY_PLACEHOLDER in str(target):
            # One file per UTC day; a session crossing midnight continues in
            # the next day's file with a ``continued`` session record.
            # The file is opened with the first record after the session
            # record, so an imported recording lands in the day it was made.
            self._template = str(Path(target).expanduser())
            self._stream: IO[str] | None = None
            self._owns_stream = True
            self.path = None
        elif isinstance(target, (str, Path)):
            path = Path(target).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")
            self._owns_stream = True
            self.path: Path | None = path
        else:
            self._stream = target
            self._owns_stream = False
            self.path = None
        self.source = source
        self.label = label
        self.include_raw_xml = include_raw_xml
        self.write_failures = 0
        self.session_id = uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._seq = 0
        self._call_id = 0
        self._start = time.monotonic()
        self.started_at = self._start
        self._closed = False
        self.record("session", **self._session_fields())

    def _session_fields(self) -> dict[str, Any]:
        from . import __version__
        return {
            "format": TRACE_FORMAT_VERSION, "source": self.source, "label": self.label,
            "adapter_version": __version__, "session_id": self.session_id,
        }

    def _open_day(self, day: str) -> IO[str]:
        path = Path(self._template.replace(DAY_PLACEHOLDER, day))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        return path.open("a", encoding="utf-8")

    def _open_first(self, day: str) -> None:
        self._day = day
        self._stream = self._open_day(day)
        header = getattr(self, "_header", None)
        if header is not None:
            self._stream.write(json.dumps(header, separators=(",", ":")) + "\n")
            self._header = None

    def _rotate(self, day: str, header: dict[str, Any]) -> None:
        """Switch to ``day``'s file and repeat the session record there."""
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                self.write_failures += 1
        self._day = day
        self._stream = self._open_day(day)
        line = {key: header[key] for key in ("seq", "t", "utc", "thread")}
        line.update(type="session", **self._session_fields(), continued=True)
        self._stream.write(json.dumps(line, separators=(",", ":")) + "\n")

    def record(
        self, record_type: str, *, at: float | None = None,
        at_utc: datetime | None = None, **fields: Any,
    ) -> None:
        """Write one record.

        ``at`` (a ``time.monotonic()`` value) and ``at_utc`` override the
        record's timestamps; a passive capture uses them to stamp a record
        with the moment its bytes arrived rather than when it was decoded.
        """
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            line = {
                "seq": self._seq,
                "t": round((time.monotonic() if at is None else at) - self._start, 6),
                "utc": (at_utc or datetime.now(timezone.utc)).isoformat(timespec="microseconds"),
                "thread": threading.current_thread().name,
                "type": record_type,
            }
            line.update(_jsonable(fields))
            try:
                if self._template is not None:
                    if self._stream is None and record_type == "session":
                        self._header = line  # written with the first record
                        return
                    if self._stream is None:
                        self._open_first(line["utc"][:10])
                    day = line["utc"][:10]
                    if day != self._day:
                        self._rotate(day, line)
                        self._seq += 1
                        line["seq"] = self._seq
                self._stream.write(json.dumps(line, separators=(",", ":")) + "\n")
                self._stream.flush()
            except Exception:  # tracing must never break mount control
                self.write_failures += 1

    def next_call_id(self) -> int:
        with self._lock:
            self._call_id += 1
            return self._call_id

    def sent(self, xml: str) -> None:
        parsed = _parse_vector(xml)
        if self.include_raw_xml:
            parsed["xml"] = xml
        self.record("send", **parsed)

    def received(
        self, *, device: str, name: str, kind: str, state: str,
        changed: dict[str, str], values: dict[str, str], revision: int, tag: str,
    ) -> None:
        fields: dict[str, Any] = {
            "tag": tag, "device": device, "property": name, "kind": kind,
            "state": state, "changed": changed, "values": values, "revision": revision,
        }
        gu = gu_summary(name, values)
        if gu is not None:
            fields["gu"] = gu
        self.record("recv", **fields)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._template is not None and self._stream is None:
                try:  # a session without records still leaves its header
                    self._open_first(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
                except Exception:
                    self.write_failures += 1
            if self._owns_stream and self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    self.write_failures += 1


def as_recorder(trace: TraceRecorder | str | Path | None, **kwargs: Any) -> TraceRecorder | None:
    if trace is None or isinstance(trace, TraceRecorder):
        return trace
    return TraceRecorder(trace, **kwargs)


def traced_operation(get_trace: Callable[[Any], TraceRecorder | None]):
    """Frame a public operation with ``call`` and ``return``/``error`` records."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        operation = function.__name__

        @wraps(function)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            trace = get_trace(self)
            if trace is None:
                return function(self, *args, **kwargs)
            call_id = trace.next_call_id()
            trace.record("call", call_id=call_id, op=operation, args=args, kwargs=kwargs)
            started = time.monotonic()
            try:
                result = function(self, *args, **kwargs)
            except BaseException as exc:
                trace.record(
                    "error", call_id=call_id, op=operation,
                    duration_s=round(time.monotonic() - started, 6),
                    error_type=type(exc).__name__, error=str(exc),
                )
                raise
            trace.record(
                "return", call_id=call_id, op=operation,
                duration_s=round(time.monotonic() - started, 6), result=result,
            )
            return result

        return wrapper

    return decorate
