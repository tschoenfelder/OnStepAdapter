"""Supervised, parked-only INDI focuser round trip; no mount commands."""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys

from onstep_adapter.indi_config import load_indi_config
from onstep_adapter.indi_focuser import IndiFocuser
from onstep_adapter.indi_gu import decode_gu
from onstep_adapter.indi_transport import IndiTransport


def require_parked(transport: IndiTransport, device: str) -> None:
    connection = transport.wait_property(device, "CONNECTION", timeout=5)
    park = transport.wait_property(device, "TELESCOPE_PARK", timeout=5)
    tracking = transport.wait_property(device, "TELESCOPE_TRACK_STATE", timeout=5)
    if (connection.values.get("CONNECT") != "On" or
            park.values.get("PARK") != "On" or
            tracking.values.get("TRACK_OFF") != "On"):
        raise RuntimeError("Mount connection, PARK, or tracking-off preflight failed")
    status = transport.wait_property(device, "OnStep Status", timeout=5)
    revision = status.revision
    for sample in range(2):
        status = transport.wait_property(
            device, "OnStep Status", timeout=5, after_revision=revision
        )
        revision = status.revision
        raw = status.values.get(":GU# return", "")
        flags = decode_gu(raw)
        if (status.state.lower() == "alert" or not flags["parked"] or
                flags["slewing"] or flags["tracking"] or flags["at_limit"] or
                flags["park_failed"]):
            raise RuntimeError(f"Mount is not safely parked: {raw!r}")
        print(f"PARK check {sample + 1}: {raw}", flush=True)


def run_roundtrip(
    transport: IndiTransport, *, device: str, expected_start: int,
    delta: int, configured_max: int, config,
) -> None:
    require_parked(transport, device)
    focuser = IndiFocuser(transport, config)
    before = focuser.get_status()
    if before.position != expected_start or not before.move_ready:
        raise RuntimeError(f"Focuser preflight failed: {before}")
    target = expected_start + delta
    if target > min(configured_max, before.driver_maximum):
        raise RuntimeError("Requested target exceeds confirmed focuser ceiling")
    print(
        f"Focuser {expected_start} -> {target} -> {expected_start}; "
        f"INDI max={before.driver_maximum}, configured max={configured_max}",
        flush=True,
    )
    restored = False
    try:
        outward = focuser.move_absolute(target, timeout=30)
        print(f"Outward: {outward}", flush=True)
        if not outward.reached:
            raise RuntimeError(f"Outward move not confirmed: {outward.error}")
        require_parked(transport, device)
        inward = focuser.move_absolute(expected_start, timeout=30)
        print(f"Return: {inward}", flush=True)
        if not inward.reached:
            raise RuntimeError(f"Return move not confirmed: {inward.error}")
        require_parked(transport, device)
        restored = focuser.get_status().position == expected_start
        if not restored:
            raise RuntimeError("Final focuser position is not the starting position")
        print("PASS: bounded INDI focuser round trip; mount remained parked", flush=True)
    finally:
        if not restored:
            try:
                print(f"Focuser abort confirmed={focuser.stop(timeout=5)}", flush=True)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
                print(f"Focuser abort unconfirmed: {exc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7624)
    parser.add_argument("--expected-start", type=int, required=True)
    parser.add_argument("--delta", type=int, default=1000)
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args(argv)
    if not args.arm or args.delta <= 0 or args.delta > 1000:
        parser.error("Explicit --arm and a positive delta no greater than 1000 are required")
    config = replace(load_indi_config(args.config), host=args.host, port=args.port)
    if config.focuser_max_position is None:
        parser.error("A configured focuser maximum is required")
    transport = IndiTransport(host=config.host, port=config.port)
    try:
        transport.connect(timeout=5)
        run_roundtrip(
            transport, device=config.device, expected_start=args.expected_start,
            delta=args.delta, configured_max=config.focuser_max_position,
            config=config,
        )
    except (ConnectionError, RuntimeError, TimeoutError, ValueError, KeyboardInterrupt) as exc:
        print(f"FAIL: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
