"""Operator-supervised terrestrial-mode acceptance runner."""

from __future__ import annotations

import argparse

from onstep_adapter import IndiOperatingMode, OnStepClient, load_indi_config


def _prompt(message: str) -> str:
    return input(message).strip().lower()


def _run(client, step_deg: float | None) -> int:
    print("This test can move the mount. Maintain line of sight and cable clearance.")
    print("ESC or Ctrl-C sends STOP and starts no automatic PARK/HOME motion.")
    if _prompt("ENTER requests explicit PARK; ESC aborts> ") == "\x1b":
        client.mount.stop()
        return 2
    parked = client.mount.park()
    print(f"PARK result: {parked}")
    if not parked.confirmed:
        return 1
    if _prompt("ENTER consumes PARK authorization and selects TERRESTRIAL> ") == "\x1b":
        client.mount.stop()
        return 2
    print(client.mount.set_mode(IndiOperatingMode.TERRESTRIAL))
    if _prompt("ENTER explicitly UNPARKS with tracking OFF> ") == "\x1b":
        client.mount.stop()
        return 2
    unparked = client.mount.unpark()
    print(f"UNPARK result: {unparked}")
    if not unparked.unparked_confirmed:
        return 1

    print("w=toward north horizon, s=away, x=STOP, p=PARK, a=ASTRONOMICAL, q=quit")
    while True:
        command = _prompt("terrestrial> ")
        if command in {"\x1b", "x", "q"}:
            print(client.mount.stop())
            return 2 if command == "\x1b" else 0
        if command == "w":
            print(client.mount.move_toward_north_horizon(step_deg))
        elif command == "s":
            print(client.mount.move_away_from_north_horizon(step_deg))
        elif command == "p":
            print(client.mount.park())
        elif command == "a":
            print(client.mount.set_mode(IndiOperatingMode.ASTRONOMICAL))
            return 0
        else:
            print("Unknown command; no motion sent.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Supervised finite terrestrial movement through local INDI"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--step-deg", type=float)
    args = parser.parse_args()
    config = load_indi_config(args.config)

    with OnStepClient(config=config, auto_supervise=False) as client:
        try:
            return _run(client, args.step_deg)
        except KeyboardInterrupt:
            print(client.mount.stop())
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
