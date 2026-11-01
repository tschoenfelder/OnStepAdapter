"""Convert a ``tcpdump`` packet capture into a capture file.

This is the fully passive path: ``tcpdump`` only observes, nothing sits in
the data path, and the client keeps talking to indiserver directly. Classic
pcap files (tcpdump's default ``-w`` format) with Ethernet, Linux cooked
(``-i any``), BSD loopback or raw IP link layers are supported; pcapng is
not (convert with ``editcap -F pcap``).

TCP payloads are reassembled per flow in sequence order; retransmitted
bytes are dropped. A flow towards ``indi_port`` is INDI, a flow towards
``lx200_port`` is the driver's LX200 link to OnStepX over TCP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import struct
from typing import Iterator

from .stream import CaptureWriter

LINK_NULL, LINK_ETHERNET, LINK_RAW, LINK_SLL, LINK_SLL2 = 0, 1, 101, 113, 276


@dataclass
class Segment:
    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    seq: int
    flags: int
    payload: bytes


def read_pcap(path: str | Path) -> Iterator[Segment]:
    data = Path(path).expanduser().read_bytes()
    if len(data) < 24:
        raise ValueError("not a pcap file")
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    elif magic == b"\x0a\x0d\x0d\x0a":
        raise ValueError("pcapng is not supported; convert with: editcap -F pcap in.pcapng out.pcap")
    else:
        raise ValueError("not a pcap file")
    nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
    link = struct.unpack(endian + "I", data[20:24])[0] & 0x0FFFFFFF
    offset = 24
    while offset + 16 <= len(data):
        sec, frac, caplen, _ = struct.unpack(endian + "IIII", data[offset:offset + 16])
        offset += 16
        frame = data[offset:offset + caplen]
        offset += caplen
        ts = sec + frac / (1e9 if nano else 1e6)
        segment = _decode_frame(link, frame, ts)
        if segment is not None:
            yield segment


def _decode_frame(link: int, frame: bytes, ts: float) -> Segment | None:
    if link == LINK_ETHERNET:
        if len(frame) < 14:
            return None
        ethertype, start = struct.unpack("!H", frame[12:14])[0], 14
        while ethertype == 0x8100 and len(frame) >= start + 4:  # VLAN tag
            ethertype, start = struct.unpack("!H", frame[start + 2:start + 4])[0], start + 4
        if ethertype != 0x0800:
            return None
        ip = frame[start:]
    elif link == LINK_SLL:
        if len(frame) < 16 or struct.unpack("!H", frame[14:16])[0] != 0x0800:
            return None
        ip = frame[16:]
    elif link == LINK_SLL2:
        if len(frame) < 20 or struct.unpack("!H", frame[0:2])[0] != 0x0800:
            return None
        ip = frame[20:]
    elif link == LINK_NULL:
        if len(frame) < 4 or frame[:4] not in (b"\x02\x00\x00\x00", b"\x00\x00\x00\x02"):
            return None
        ip = frame[4:]
    elif link == LINK_RAW:
        ip = frame
    else:
        raise ValueError(f"unsupported pcap link type {link}")
    if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 6:  # IPv4 + TCP only
        return None
    ihl = (ip[0] & 0x0F) * 4
    total = struct.unpack("!H", ip[2:4])[0]
    src_ip = ".".join(str(b) for b in ip[12:16])
    dst_ip = ".".join(str(b) for b in ip[16:20])
    tcp = ip[ihl:total] if total else ip[ihl:]
    if len(tcp) < 20:
        return None
    src_port, dst_port, seq = struct.unpack("!HHI", tcp[:8])
    data_offset = (tcp[12] >> 4) * 4
    return Segment(ts, (src_ip, src_port), (dst_ip, dst_port), seq, tcp[13], bytes(tcp[data_offset:]))


SYN, FIN, RST = 0x02, 0x01, 0x04


@dataclass
class _Flow:
    next_seq: int | None = None
    pending: dict[int, tuple[float, bytes]] = field(default_factory=dict)


def import_pcap(
    path: str | Path,
    writer: CaptureWriter,
    *,
    indi_port: int = 7624,
    lx200_port: int | None = 9999,
) -> dict[str, int]:
    """Feed a pcap's INDI and LX200 flows into ``writer``; return counters."""
    conns: dict[tuple, int] = {}
    flows: dict[tuple, _Flow] = {}
    t0: float | None = None
    counts = {"segments": 0, "bytes": 0, "connections": 0, "retransmitted": 0}
    writer.meta(mode="pcap", file=str(path), indi_port=indi_port, lx200_port=lx200_port)
    recorder = writer.recorder
    start = recorder.started_at  # timestamps are re-based on the first packet

    def stamp(ts: float) -> tuple[float, datetime]:
        return start + (ts - t0), datetime.fromtimestamp(ts, timezone.utc)

    for seg in read_pcap(path):
        if seg.dst[1] == indi_port or (lx200_port and seg.dst[1] == lx200_port):
            client, server, direction = seg.src, seg.dst, "c2s"
        elif seg.src[1] == indi_port or (lx200_port and seg.src[1] == lx200_port):
            client, server, direction = seg.dst, seg.src, "s2c"
        else:
            continue
        if t0 is None:
            t0 = seg.ts
        at, at_utc = stamp(seg.ts)
        key = (client, server)
        if key not in conns and not seg.flags & RST:
            conns[key] = len(conns) + 1
            counts["connections"] += 1
            protocol = "indi" if server[1] == indi_port else "lx200"
            writer.connect(conns[key], protocol, f"{client[0]}:{client[1]}", at=at, at_utc=at_utc)
        if key not in conns:
            continue
        conn = conns[key]
        flow = flows.setdefault((key, direction), _Flow())
        if seg.flags & SYN:
            flow.next_seq = (seg.seq + 1) & 0xFFFFFFFF
            continue
        if seg.payload:
            counts["segments"] += 1
            if flow.next_seq is None:  # capture started mid-connection
                flow.next_seq = seg.seq
            flow.pending[seg.seq] = (seg.ts, seg.payload)
            while True:
                item = _take(flow)
                if item is None:
                    break
                ts, payload, dropped = item
                counts["retransmitted"] += dropped
                if payload:
                    counts["bytes"] += len(payload)
                    at, at_utc = stamp(ts)
                    writer.data(conn, direction, payload, at=at, at_utc=at_utc)
        if seg.flags & (FIN | RST) and direction == "c2s":
            writer.close(conn, at=at, at_utc=at_utc)
    recorder.close()
    return counts


def _take(flow: _Flow) -> tuple[float, bytes, int] | None:
    """Next in-order payload of a flow; old (retransmitted) bytes are cut."""
    expected = flow.next_seq
    if expected in flow.pending:
        ts, payload = flow.pending.pop(expected)
        flow.next_seq = (expected + len(payload)) & 0xFFFFFFFF
        return ts, payload, 0
    for seq in list(flow.pending):
        back = (expected - seq) & 0xFFFFFFFF
        if 0 < back < 0x80000000:  # starts before what we already have
            ts, payload = flow.pending.pop(seq)
            if back < len(payload):
                flow.next_seq = (expected + len(payload) - back) & 0xFFFFFFFF
                return ts, payload[back:], back
            return ts, b"", len(payload)
    return None  # nothing yet, or a gap: wait for the missing segment
