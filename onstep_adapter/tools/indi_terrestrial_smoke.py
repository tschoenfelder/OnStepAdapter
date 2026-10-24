"""Operator-supervised terrestrial-mode acceptance runner."""

from __future__ import annotations

import argparse

from onstep_adapter import IndiOperatingMode, OnStepClient, load_indi_config


def _prompt(message: str) -> str:
    return input(message).strip().lower()


def _confirm(message: str) -> bool:
    while True:
        answer = _prompt(message)
        if answer == "":
            return True
        if answer in {"\x1b", "esc"}:
            return False
        print("Only ENTER confirms. Type ESC then ENTER to abort; no command was sent.")


def _run(client, step_deg: float | None) -> int:
    print("This test can move the mount. Maintain line of sight and cable clearance.")
    print("ESC or Ctrl-C sends STOP and starts no automatic PARK/HOME motion.")
    print(
        "Purpose: verify an explicit PARK -> TERRESTRIAL -> UNPARK transition, "
        "then operator-requested finite north-horizon steps through INDI."
    )
    initial = client.mount.get_status()
    print(
        f"Initial state: parked={initial.parked} tracking={initial.tracking} "
        f"slewing={initial.slewing} raw={initial.raw_status!r}."
    )
    print(
        "PARK verification is repeated even when already parked. In that case no "
        "PARK command is sent and the mount must not move; two fresh raw-P reports "
        "provide this client's reconciled evidence for the mode change."
    )
    if not _confirm("Press ENTER to request/confirm PARK; type ESC then ENTER to abort> "):
        client.mount.stop()
        return 2
    parked = client.mount.park()
    print(f"PARK result: {parked}")
    if not parked.confirmed:
        return 1
    if not _confirm("Press ENTER to select TERRESTRIAL; type ESC then ENTER to abort> "):
        client.mount.stop()
        return 2
    print(client.mount.set_mode(IndiOperatingMode.TERRESTRIAL))
    if not _confirm("Press ENTER to UNPARK with tracking OFF; type ESC then ENTER to abort> "):
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
