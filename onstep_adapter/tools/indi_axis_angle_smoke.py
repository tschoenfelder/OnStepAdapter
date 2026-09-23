"""Supervised INDI 1/5/10-degree RA and DEC axis-movement acceptance test."""

from __future__ import annotations

import argparse
from dataclasses import replace
import time

from onstep_adapter import OnStepClient, load_indi_config


class OperatorAbort(Exception):
    pass


def confirm(message: str) -> None:
    response = input(f"{message} ENTER=continue, ESC or f=stop> ").strip().lower()
    if response in {"\x1b", "f"}:
        raise OperatorAbort("Operator stopped the test")
    if response:
        raise OperatorAbort("No stage armed: expected ENTER only")


def wait_live(client: OnStepClient, timeout_s: float = 10.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = client.observe_mount()
        if last.status_live and last.coordinates_live:
            return last
        time.sleep(0.25)
    raise TimeoutError(f"Live OnStep status/coordinates unavailable: {last}")


def stop_tracking(client: OnStepClient) -> None:
    transport = client._transport
    transport.set_switch(client.device, "TELESCOPE_TRACK_STATE", "TRACK_OFF")
    deadline = time.monotonic() + 8.0
    stable = 0
    last_revision = -1
    while stable < 2:
        if time.monotonic() >= deadline:
            raise TimeoutError("Tracking-off was not confirmed by fresh status")
        time.sleep(0.25)
        status = client.observe_mount()
        if not status.status_live or status.at_limit:
            raise RuntimeError("Status stale or limit active while disabling tracking")
        if status.status_revision <= last_revision:
            continue
        last_revision = status.status_revision
        stable = stable + 1 if not status.tracking and not status.slewing else 0


def run(config_path: str, *, goto_timeout_s: float, sync_time_location: bool = False) -> int:
    config = replace(load_indi_config(config_path), home_motion_enabled=True)
    print("INDI axis-angle acceptance: PARK -> UNPARK -> paired axis moves -> HOME -> PARK")
    print("Requires direct line of sight. No raw serial connection is opened.")
    print("UNPARK does not move to HOME. Local movement is HOME-neutral and needs no clock authority.")
    print("ESC or f at a prompt, or Ctrl-C, sends stop and suppresses later motion.")
    client = OnStepClient(config=config)
    moved = False
    try:
        client.connect()
        initial = wait_live(client)
        if not initial.parked or initial.slewing or initial.tracking or initial.at_limit:
            raise RuntimeError("The acceptance test must start PARKED, stationary and fault-free")
        print(f"Initial OnStep status={initial.raw_status}")
        if sync_time_location:
            confirm("Approve optional Raspberry time and observer-site push through INDI.")
            synced = client.sync_time_location(user_approved=True)
            if not synced.time_accepted or not synced.location_accepted:
                raise RuntimeError(f"Time/site sync was not accepted: {synced.error}")
        confirm("Arm UNPARK only. This must not move the mount to HOME.")
        moved = True
        unparked = client.unpark()
        print(f"UNPARK result: {unparked}")
        if not unparked.unparked_confirmed:
            raise RuntimeError("UNPARK was not confirmed")
        initial_unparked = wait_live(client)
        if (
            initial_unparked.parked or initial_unparked.tracking or
            initial_unparked.slewing or initial_unparked.at_limit
        ):
            raise RuntimeError("UNPARK did not leave a stationary, non-tracking state")
        confirm("Confirm the unchanged UNPARK pose is physically clear for paired moves.")
        for axis in ("ra", "dec"):
            for magnitude in (1.0, 5.0, 10.0):
                for angle in (magnitude, -magnitude):
                    current = wait_live(client)
                    print(
                        f"NEXT {axis.upper()} {angle:+.1f}deg: "
                        f"RA={current.ra_hours:.6f}h DEC={current.dec_deg:.3f} "
                        f"pier={current.pier_side} status={current.raw_status}"
                    )
                    confirm(f"Arm finite {axis.upper()} {angle:+.1f}deg move.")
                    result = client.mount.move_ra_axis_deg(angle) if axis == "ra" else client.mount.move_dec_axis_deg(angle)
                    print(f"RESULT: {result}")
                    if not result.stop_confirmed:
                        raise RuntimeError("Axis move did not confirm a stop")
                    confirm("Confirm this stage's physical clearance.")
        confirm("All twelve moves passed. Arm explicit HOME -> PARK cleanup.")
        home = client.go_home()
        print(f"HOME return: {home}")
        if not home.confirmed:
            raise RuntimeError("Return HOME was not confirmed")
        parked = client.park()
        print(f"PARK result: {parked}")
        if not parked.confirmed or not wait_live(client).parked:
            raise RuntimeError("Final PARK was not confirmed by live status")
        print("PASS: twelve finite RA/DEC moves and final PARK confirmed")
        return 0
    except (OperatorAbort, KeyboardInterrupt, Exception) as exc:
        print(f"STOPPED: {exc}; no further HOME/PARK motion will be started")
        if moved:
            try:
                print(f"Emergency stop: {client.emergency_stop()}")
            except Exception as stop_error:
                print(f"STOP NOT CONFIRMED: {stop_error}")
        return 1
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Local INDI adapter TOML")
    parser.add_argument("--goto-timeout-s", type=float, default=120.0)
    parser.add_argument("--confirm-home-uat", action="store_true")
    parser.add_argument("--sync-time-location", action="store_true")
    args = parser.parse_args()
    if not args.confirm_home_uat:
        parser.error("--confirm-home-uat is required because live INDI H arrival is not yet certified")
    return run(
        args.config, goto_timeout_s=args.goto_timeout_s,
        sync_time_location=args.sync_time_location,
    )


if __name__ == "__main__":
    raise SystemExit(main())
