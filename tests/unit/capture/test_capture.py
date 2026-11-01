"""Passive capture: proxy, pcap import, LX200 decoding and assumption checks."""

import io
import json
import socket
import struct
import threading
import time

import pytest

from onstep_adapter.capture import CaptureProxy, check_assumptions, import_pcap, open_capture, segment_operations
from onstep_adapter.capture.analysis import FAIL, INCONCLUSIVE, NOT_OBSERVED, PASS
from onstep_adapter.tools.trace_compare import build_operations

DEVICE = "LX200 OnStep"


def records_of(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class FakeIndiServer:
    """Upstream that records every byte it gets and answers with canned XML."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.received = bytearray()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                self.received += data
                if b"getProperties" in data:
                    conn.sendall(self.reply)


DEFINE = (
    f'<defSwitchVector device="{DEVICE}" name="TELESCOPE_PARK" state="Ok" perm="rw" rule="OneOfMany">'
    '<defSwitch name="PARK">On</defSwitch><defSwitch name="UNPARK">Off</defSwitch></defSwitchVector>'
    f'<defTextVector device="{DEVICE}" name="OnStep Status" state="Ok" perm="ro">'
    '<defText name=":GU# return">nNPEW060</defText></defTextVector>'
    f'<setSwitchVector device="{DEVICE}" name="TELESCOPE_PARK" state="Ok">'
    '<oneSwitch name="PARK">Off</oneSwitch><oneSwitch name="UNPARK">On</oneSwitch></setSwitchVector>'
).encode()


def test_proxy_forwards_bytes_unchanged_and_adds_nothing():
    upstream = FakeIndiServer(DEFINE)
    stream = io.StringIO()
    client_bytes = (
        b'<getProperties version="1.7"/>'
        b'<newSwitchVector device="LX200 OnStep" name="TELESCOPE_PARK">'
        b'<oneSwitch name="UNPARK">On</oneSwitch></newSwitchVector>'
    )
    with CaptureProxy(open_capture(stream), upstream=("127.0.0.1", upstream.port), listen=("127.0.0.1", 0)) as proxy:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            # Split mid-element: forwarding must not wait for whole elements.
            client.sendall(client_bytes[:40])
            time.sleep(0.05)
            client.sendall(client_bytes[40:])
            got = b""
            client.settimeout(2.0)
            while len(got) < len(DEFINE):
                got += client.recv(65536)
        time.sleep(0.2)
    assert got == DEFINE
    assert bytes(upstream.received) == client_bytes
    records = records_of(stream)
    sends = [r for r in records if r["type"] == "send"]
    assert [s["tag"] for s in sends] == ["getProperties", "newSwitchVector"]
    assert sends[1]["values"] == {"UNPARK": "On"} and "forbidden" not in sends[1]
    recv = [r for r in records if r["type"] == "recv"]
    assert [r["property"] for r in recv] == ["TELESCOPE_PARK", "OnStep Status", "TELESCOPE_PARK"]
    assert recv[0]["perm"] == "rw" and recv[0]["rule"] == "OneOfMany"
    assert recv[1]["gu"]["parked"] is True
    assert recv[2]["changed"] == {"PARK": "Off", "UNPARK": "On"} and recv[2]["revision"] == 2
    assert [r["type"] for r in records if r["type"] in {"connect", "close"}] == ["connect", "close"]


def test_proxy_keeps_forwarding_when_recording_fails():
    upstream = FakeIndiServer(DEFINE)
    writer = open_capture(io.StringIO())

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    writer.data = broken
    with CaptureProxy(writer, upstream=("127.0.0.1", upstream.port), listen=("127.0.0.1", 0)) as proxy:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(b'<getProperties version="1.7"/>')
            client.settimeout(2.0)
            got = b""
            while len(got) < len(DEFINE):
                got += client.recv(65536)
    assert got == DEFINE
    assert writer.recorder.write_failures >= 1


def test_forbidden_indi_writes_are_flagged():
    stream = io.StringIO()
    writer = open_capture(stream)
    writer.connect(1, "indi", "test")
    writer.data(1, "c2s", (
        f'<newSwitchVector device="{DEVICE}" name="TELESCOPE_HOME"><oneSwitch name="SET">On</oneSwitch></newSwitchVector>'
        f'<newSwitchVector device="{DEVICE}" name="ON_COORD_SET"><oneSwitch name="SYNC">On</oneSwitch></newSwitchVector>'
        f'<newSwitchVector device="{DEVICE}" name="TELESCOPE_PARK_OPTION"><oneSwitch name="PARK_CURRENT">On</oneSwitch></newSwitchVector>'
        f'<newSwitchVector device="{DEVICE}" name="TELESCOPE_HOME"><oneSwitch name="GO">On</oneSwitch></newSwitchVector>'
    ).encode())
    labels = [r.get("forbidden") for r in records_of(stream) if r["type"] == "send"]
    assert labels == ["SET HOME", "SYNC", "SET PARK", None]
    assert check_assumptions(records_of(stream))[0].status == FAIL


def test_lx200_commands_are_split_and_forbidden_ones_flagged():
    stream = io.StringIO()
    writer = open_capture(stream)
    writer.connect(1, "lx200", "driver")
    writer.data(1, "c2s", b"\x06:GU#:GR")
    writer.data(1, "s2c", b"nNpEW060#")
    writer.data(1, "c2s", b"#:hQ#:CM#:hC#:hF#")
    commands = [(r["data"], r.get("forbidden")) for r in records_of(stream) if r.get("dir") == "cmd"]
    assert commands == [
        ("<ACK>", None), (":GU#", None), (":GR#", None), (":hQ#", "SET PARK (:hQ#)"),
        (":CM#", "SYNC (:CM#)"), (":hC#", None), (":hF#", "SET HOME (:hF#)"),
    ]
    finding = check_assumptions(records_of(stream))[1]
    assert finding.id == "A02" and finding.status == FAIL


def test_open_capture_never_appends(tmp_path):
    path = tmp_path / "capture.jsonl"
    open_capture(path).recorder.close()
    with pytest.raises(FileExistsError):
        open_capture(path)


# ------------------------------------------------------------------- pcap
def _packet(src_port, dst_port, seq, payload=b"", flags=0x18):
    tcp = struct.pack("!HHIIBBHHH", src_port, dst_port, seq, 0, 5 << 4, flags, 65535, 0, 0)
    total = 20 + len(tcp) + len(payload)
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 6, 0,
                     bytes([127, 0, 0, 1]), bytes([127, 0, 0, 1]))
    ether = b"\x00" * 12 + b"\x08\x00"
    return ether + ip + tcp + payload


def _pcap(frames, ts0=1_790_000_000.0):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for i, frame in enumerate(frames):
        ts = ts0 + i * 0.1
        out += struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(frame), len(frame)) + frame
    return out


def test_pcap_import_reassembles_both_directions(tmp_path):
    new = (f'<newSwitchVector device="{DEVICE}" name="TELESCOPE_PARK">'
           '<oneSwitch name="UNPARK">On</oneSwitch></newSwitchVector>').encode()
    status = (f'<setTextVector device="{DEVICE}" name="OnStep Status" state="Ok">'
              '<oneText name=":GU# return">pNEW060</oneText></setTextVector>').encode()
    client, server = 50000, 7624
    frames = [
        _packet(client, server, 999, flags=0x02),        # SYN
        _packet(server, client, 4999, flags=0x12),       # SYN-ACK
        _packet(client, server, 1000, new[:30]),
        _packet(client, server, 1030 + 20, new[50:]),    # out of order
        _packet(client, server, 1030, new[30:50]),
        _packet(client, server, 1000, new[:30]),         # retransmission
        _packet(server, client, 5000, status),
        _packet(client, server, 1000 + len(new), flags=0x11),  # FIN
    ]
    path = tmp_path / "onstep.pcap"
    path.write_bytes(_pcap(frames))
    stream = io.StringIO()
    counts = import_pcap(path, open_capture(stream))
    records = records_of(stream)
    sends = [r for r in records if r["type"] == "send"]
    recv = [r for r in records if r["type"] == "recv"]
    assert counts["connections"] == 1 and counts["retransmitted"] == 30
    assert len(sends) == 1 and sends[0]["values"] == {"UNPARK": "On"}
    assert recv[0]["gu"]["parked"] is False
    assert sends[0]["utc"].startswith("2026-")
    assert recv[0]["t"] == pytest.approx(0.6, abs=1e-3)
    assert records[-1]["type"] == "close"


def test_pcapng_is_refused_with_a_hint(tmp_path):
    path = tmp_path / "x.pcapng"
    path.write_bytes(b"\x0a\x0d\x0d\x0a" + b"\x00" * 40)
    with pytest.raises(ValueError, match="editcap"):
        import_pcap(path, open_capture(io.StringIO()))


# --------------------------------------------------------------- analysis
def rec(seq, t, kind, **fields):
    return {"seq": seq, "t": t, "utc": "2026-11-01T21:00:00+00:00", "thread": "x", "type": kind, **fields}


def gu(seq, t, raw):
    flags = set(raw)
    return rec(seq, t, "recv", conn=1, property="OnStep Status", state="Ok",
               values={":GU# return": raw},
               gu={"motion_state": "parked" if "P" in flags else "slewing" if "N" not in flags else
                   "tracking" if "n" not in flags else "unparked",
                   "parked": "P" in flags, "tracking": "n" not in flags, "slewing": "N" not in flags,
                   "at_home": "H" in flags, "guiding": False, "at_limit": False, "pier_side": "west"})


def send(seq, t, prop, **values):
    return rec(seq, t, "send", conn=1, tag="newSwitchVector", property=prop, values=values)


def test_unpark_without_auto_tracking_fails_the_assumption():
    records = [gu(1, 0.0, "nNPEW060"), send(2, 1.0, "TELESCOPE_PARK", UNPARK="On"),
               gu(3, 1.2, "nNpEW060"), gu(4, 1.45, "nNpEW060"),
               send(5, 3.0, "TELESCOPE_TRACK_STATE", TRACK_OFF="On")]
    finding = next(f for f in check_assumptions(records) if f.id == "A03")
    assert finding.status == FAIL and finding.values["auto_track_on_unpark"] is False


def test_unpark_tracking_suppressed_before_any_status_is_inconclusive():
    records = [gu(1, 0.0, "nNPEW060"), send(2, 1.0, "TELESCOPE_PARK", UNPARK="On"),
               send(3, 1.01, "TELESCOPE_TRACK_STATE", TRACK_OFF="On"), gu(4, 1.2, "nNpEW060")]
    finding = next(f for f in check_assumptions(records) if f.id == "A03")
    assert finding.status == INCONCLUSIVE


def test_abort_settle_is_measured_from_status():
    records = [gu(1, 0.0, "nNpEW060"), rec(2, 0.5, "send", conn=1, tag="newNumberVector",
                                        property="EQUATORIAL_EOD_COORD", values={"RA": "1", "DEC": "40"}),
               gu(3, 0.75, "npEW060"), send(4, 2.0, "TELESCOPE_ABORT_MOTION", ABORT="On"),
               gu(5, 2.25, "npEW060"), gu(6, 3.6, "nNpEW060")]
    findings = {f.id: f for f in check_assumptions(records)}
    assert findings["A04"].status == PASS
    assert findings["A04"].values["abort_settle_s"] == pytest.approx(1.6)
    assert findings["A14"].status == NOT_OBSERVED


def test_segment_operations_split_a_capture_into_actions():
    records = [gu(1, 0.0, "nNPEW060"), send(2, 1.0, "TELESCOPE_PARK", UNPARK="On"),
               gu(3, 1.1, "NpEW060"), send(4, 1.2, "TELESCOPE_TRACK_STATE", TRACK_OFF="On"),
               send(5, 1.3, "ON_COORD_SET", SLEW="On"), gu(6, 1.5, "nNpEW060")]
    operations = build_operations(segment_operations(records))
    assert [op.op for op in operations] == ["unpark", "track_off"]
    assert operations[1].sends == ["TELESCOPE_TRACK_STATE:TRACK_OFF=On", "ON_COORD_SET:SLEW=On"]
    assert operations[0].motion_states == ["parked", "tracking"]
