"""Record what an INDI client, indiserver and OnStepX say to each other.

Passive recording (nothing in the data path) with tcpdump, then convert:

    sudo tcpdump -i lo -s 0 -U -w onstep.pcap 'tcp port 7624 or tcp port 9999'
    python3 -m onstep_adapter.tools.indi_capture from-pcap onstep.pcap -o capture.jsonl

Or a transparent pass-through the client connects to instead of indiserver
(bytes are forwarded unchanged; the proxy never sends anything itself):

    python3 -m onstep_adapter.tools.indi_capture proxy --listen 7625 \\
        --upstream 127.0.0.1:7624 -o capture.jsonl

Then look at it:

    python3 -m onstep_adapter.tools.indi_capture check capture.jsonl
    python3 -m onstep_adapter.tools.indi_capture summary capture.jsonl
    python3 -m onstep_adapter.tools.indi_capture calibrate capture.jsonl
    python3 -m onstep_mock.validate capture.jsonl --report report.md
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from ..capture.analysis import FAIL, calibrate, check_assumptions, segment_operations
from ..capture.pcap import import_pcap
from ..capture.proxy import CaptureProxy
from ..capture.stream import open_capture
from .trace_compare import build_operations, format_summary, load_trace


def _endpoint(text: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return (host or default_host, int(port))


def _default_out() -> str:
    return f"capture-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    proxy = commands.add_parser("proxy", help="record through a transparent TCP pass-through")
    proxy.add_argument("--listen", default="127.0.0.1:7625", help="host:port clients connect to (default 127.0.0.1:7625)")
    proxy.add_argument("--upstream", default="127.0.0.1:7624", help="indiserver host:port (default 127.0.0.1:7624)")
    proxy.add_argument("--protocol", choices=("indi", "lx200"), default="indi",
                       help="indi between client and indiserver; lx200 between the driver and OnStepX over TCP")
    proxy.add_argument("-o", "--out", default=None, help="capture file (default capture-<utc>.jsonl)")
    proxy.add_argument("--label", default="", help="free text stored in the capture, e.g. the test step")

    pcap = commands.add_parser("from-pcap", help="convert a tcpdump recording")
    pcap.add_argument("pcap")
    pcap.add_argument("-o", "--out", default=None)
    pcap.add_argument("--indi-port", type=int, default=7624)
    pcap.add_argument("--lx200-port", type=int, default=9999, help="OnStepX TCP command port; 0 to ignore")
    pcap.add_argument("--label", default="")

    check = commands.add_parser("check", help="check a capture against the documented assumptions")
    check.add_argument("capture")
    check.add_argument("--json", help="write the findings as JSON")

    summary = commands.add_parser("summary", help="list the operations in a capture")
    summary.add_argument("capture")

    cal = commands.add_parser("calibrate", help="print SimConfig values measured in a capture")
    cal.add_argument("capture")

    args = parser.parse_args(argv)

    if args.command == "proxy":
        out = args.out or _default_out()
        writer = open_capture(out, label=args.label)
        listen = _endpoint(args.listen)
        server = CaptureProxy(writer, upstream=_endpoint(args.upstream), listen=listen, protocol=args.protocol)
        server.start()
        print(f"Recording {args.protocol} traffic {listen[0]}:{server.port} -> {args.upstream} into {out}. "
              "Point the client at the listen port. Ctrl+C ends the capture.", flush=True)
        server.serve_forever()
        server.stop()
        print(f"{server.connections} connection(s) recorded; forbidden writes seen: {len(writer.forbidden)}")
        return 0

    if args.command == "from-pcap":
        out = args.out or _default_out()
        writer = open_capture(out, label=args.label)
        counts = import_pcap(args.pcap, writer, indi_port=args.indi_port, lx200_port=args.lx200_port or None)
        print(f"{out}: {counts['connections']} connection(s), {counts['segments']} segments, "
              f"{counts['bytes']} bytes, {counts['retransmitted']} retransmitted bytes dropped")
        return 0

    records = load_trace(args.capture)
    if args.command == "summary":
        print(format_summary(build_operations(segment_operations(records))))
        return 0
    if args.command == "calibrate":
        print(json.dumps(calibrate(records), indent=2, default=str))
        return 0
    findings = check_assumptions(records)
    width = max(len(f.title) for f in findings)
    for f in findings:
        print(f"{f.id} {f.status:<12} {f.title:<{width}}  {f.observed}")
    if args.json:
        Path(args.json).write_text(json.dumps([f.__dict__ for f in findings], indent=2, default=str), encoding="utf-8")
    return 1 if any(f.status == FAIL for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
