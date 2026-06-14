"""Supervised PARK->HOME->target axis-correction smoke test."""

from __future__ import annotations

import argparse
import os
import select
import sys
import time
from pathlib import Path

from onstep_adapter import OnStepClient, OnStepSafetyConfig
from smart_telescope.ports.mount import MountPosition, MountState


def _escape_requested() -> bool:
    if os.name == "nt":
        try:
            import msvcrt

            if msvcrt.kbhit():
                return msvcrt.getwch() == "\x1b"
        except ImportError:
            return False
        return False
    readable, _, _ = select.select([sys.stdin], [], [], 0.0)
    if not readable:
        return False
    return sys.stdin.read(1) == "\x1b"


def _confirm(prompt: str) -> bool:
    value = input(prompt).strip().lower()
    if value in {"\x1b", "esc"}:
        raise KeyboardInterrupt("ESC emergency stop")
    return value not in {"f", "s", "skip"}


def _ra_error_h(actual: float, target: float) -> float:
    return abs(((actual - target + 12.0) % 24.0) - 12.0)


def _wait_for_target(
    client: OnStepClient,
    target: MountPosition,
    *,
    timeout_s: float,
    poll_s: float,
) -> bool:
    deadline = time.monotonic() + timeout_s
    stable = 0
    while time.monotonic() < deadline:
        if _escape_requested():
            client.mount.stop()
            raise KeyboardInterrupt("ESC emergency stop")
        state = client.mount.get_state()
        position = client.mount.get_position()
        ra_error = _ra_error_h(position.ra, target.ra)
        dec_error = abs(position.dec - target.dec)
        print(
            f"goto: state={state.name.lower()} ra={position.ra:.6f} "
            f"dec={position.dec:.5f} ra_error={ra_error:.5f}h "
            f"dec_error={dec_error:.3f}deg"
        )
        if state != MountState.SLEWING and ra_error <= 0.02 and dec_error <= 0.25:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        time.sleep(max(0.1, poll_s))
    return False


def _axis_delta(result) -> tuple[float | None, float | None]:
    if result.after_ra is None or result.after_dec is None:
        return None, None
    return (
        ((result.after_ra - result.before_ra + 12.0) % 24.0) - 12.0,
        result.after_dec - result.before_dec,
    )


def _time_location_preflight(client: OnStepClient) -> dict[str, object]:
    clock = client.mount.read_onstep_clock()
    site = client.mount.read_onstep_site()
    sidereal = client.mount.read_onstep_sidereal_consistency()
    snapshot = client.mount.safety_snapshot()
    location = snapshot["location_readiness"]
    ready = bool(
        not clock.get("warning")
        and location.get("ready")
        and sidereal.get("ok")
    )
    return {
        "ready": ready,
        "clock": clock,
        "site": site,
        "sidereal": sidereal,
        "location": location,
    }


def _print_time_location_preflight(preflight: dict[str, object]) -> None:
    clock = preflight["clock"]
    site = preflight["site"]
    sidereal = preflight["sidereal"]
    location = preflight["location"]
    print("Pre-motion time/location gate (mount remains PARKED):")
    print(
        f"  civil time: system={clock.get('system_local')} "
        f"OnStep={clock.get('onstep_local')} delta={clock.get('delta_s')}s "
        f"ok={not bool(clock.get('warning'))}"
    )
    print(
        f"  observer: configured={location.get('active_observer')} "
        f"OnStep=({site.get('lat')}, {site.get('lon')}) "
        f"delta={location.get('deltas', {}).get('location_m')}m "
        f"ok={bool(location.get('ready'))}"
    )
    print(
        f"  sidereal: OnStep={sidereal.get('onstep_lst_h')}h "
        f"expected={sidereal.get('expected_lst_h')}h "
        f"delta={sidereal.get('delta_s')}s ok={bool(sidereal.get('ok'))}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervised bounded RA/DEC correction smoke; never writes PARK.",
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--observer-lat", type=float, required=True)
    parser.add_argument("--observer-lon", type=float, required=True)
    parser.add_argument("--observer-alt-m", type=float, default=0.0)
    parser.add_argument(
        "--confirm-time-location-sync",
        action="store_true",
        help=(
            "While PARKED, explicitly authorize copying the Raspberry civil time, "
            "UTC offset, and supplied observer latitude/longitude into OnStep when "
            "the pre-motion gate detects a mismatch."
        ),
    )
    parser.add_argument("--target-ha-deg", type=float, default=-15.0)
    parser.add_argument("--target-dec-deg", type=float, default=20.0)
    parser.add_argument("--center-duration-ms", type=int, default=100)
    parser.add_argument("--guide-duration-ms", type=int, default=100)
    parser.add_argument("--goto-timeout-s", type=float, default=120.0)
    parser.add_argument("--poll-s", type=float, default=1.0)
    parser.add_argument("--home-timeout-s", type=float, default=120.0)
    parser.add_argument("--park-timeout-s", type=float, default=120.0)
    parser.add_argument("--ha-east-limit-h", type=float, default=-5.5)
    parser.add_argument("--ha-west-limit-h", type=float, default=5.0 / 15.0)
    parser.add_argument("--min-alt-deg", type=float, default=-5.0)
    parser.add_argument("--max-alt-deg", type=float, default=90.0)
    parser.add_argument(
        "--state-file",
        default=str(Path.home() / ".OnStepAdapter" / "axis_motion_smoke_state.json"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not -90.0 <= args.target_dec_deg <= 90.0:
        raise SystemExit("--target-dec-deg must be within [-90, 90]")
    for name in ("center_duration_ms", "guide_duration_ms"):
        value = int(getattr(args, name))
        if value < 20 or value > 2000:
            raise SystemExit(f"--{name.replace('_', '-')} must be within [20, 2000] ms")

    config = OnStepSafetyConfig(
        observer_lat=args.observer_lat,
        observer_lon=args.observer_lon,
        observer_alt_m=args.observer_alt_m,
        min_alt_deg=args.min_alt_deg,
        max_alt_deg=args.max_alt_deg,
        ha_east_limit_h=args.ha_east_limit_h,
        ha_west_limit_h=args.ha_west_limit_h,
        require_home_confirmation=True,
        time_trust_source="user_confirmed",
        allow_broad_onstep_limits=True,
        state_file=args.state_file,
    )
    escaped = False
    cleanup_required = False
    with OnStepClient(args.port, safety_config=config) as client:
        try:
            state = client.mount.get_state()
            print(f"Initial state={state.name.lower()} status={client.mount.last_decoded_status}")
            if state != MountState.PARKED:
                print("FAIL: test requires a PARKED start.")
                return 2
            preflight = _time_location_preflight(client)
            _print_time_location_preflight(preflight)
            if not preflight["ready"]:
                if not args.confirm_time_location_sync:
                    print(
                        "BLOCKED before motion: OnStep time/location is not aligned. "
                        "Verify the Raspberry time and observer coordinates, then rerun "
                        "with --confirm-time-location-sync to authorize synchronization."
                    )
                    return 5
                print(
                    "Synchronizing OnStep time, UTC offset, latitude, and longitude "
                    "from the confirmed Raspberry/observer values."
                )
                sync = client.mount.sync_onstep_time_location(
                    lat=args.observer_lat,
                    lon=args.observer_lon,
                    alt_m=args.observer_alt_m,
                    confirmed_by_user=True,
                )
                print(f"Time/location synchronization result: {sync}")
                preflight = _time_location_preflight(client)
                _print_time_location_preflight(preflight)
                if not sync.get("ok") or not preflight["ready"]:
                    print(
                        "FAIL: OnStep time/location still fails readback after "
                        "synchronization. No movement was commanded."
                    )
                    return 5
            print(
                "Path: PARK -> HOME -> safe target -> bounded RA/DEC corrections "
                "-> HOME -> PARK.\n"
                "No PARK position is written. ESC sends immediate stop and suppresses "
                "all later automatic movement."
            )
            if not _confirm("ENTER arms PARK->HOME; ESC/skip aborts> "):
                return 3
            cleanup_required = True
            home = client.mount.unpark_to_home_stop_tracking(
                timeout_s=args.home_timeout_s,
                poll_s=args.poll_s,
            )
            if not home.get("ok"):
                print(f"FAIL: HOME route failed: {home}")
                return 4

            sidereal = preflight["sidereal"]
            lst_h = float(sidereal["onstep_lst_h"])
            target = MountPosition(
                ra=(lst_h - args.target_ha_deg / 15.0) % 24.0,
                dec=args.target_dec_deg,
            )
            validation = client.mount.validate_target(target.ra, target.dec, margin_deg=1.0)
            if not validation.get("allowed"):
                print(f"FAIL: computed target is unsafe: {validation}")
                return 6
            print(
                f"Target RA={target.ra:.6f}h DEC={target.dec:.3f}deg "
                f"HA={args.target_ha_deg:.2f}deg."
            )
            if not _confirm("ENTER arms target goto; ESC/skip aborts> "):
                return 3
            client.mount.goto(target.ra, target.dec)
            if not _wait_for_target(
                client,
                target,
                timeout_s=args.goto_timeout_s,
                poll_s=args.poll_s,
            ):
                print("FAIL: target was not acquired.")
                return 7
            if client.mount.get_state() != MountState.TRACKING:
                if not client.mount.enable_tracking():
                    print("FAIL: tracking could not be enabled.")
                    return 8

            corrections = (
                ("center RA east", client.mount.move_ra_timed, "east", args.center_duration_ms, "center"),
                ("center RA west", client.mount.move_ra_timed, "west", args.center_duration_ms, "center"),
                ("center DEC north", client.mount.move_dec_timed, "north", args.center_duration_ms, "center"),
                ("center DEC south", client.mount.move_dec_timed, "south", args.center_duration_ms, "center"),
                ("guide RA east", client.mount.move_ra_timed, "east", args.guide_duration_ms, "guide"),
                ("guide DEC north", client.mount.move_dec_timed, "north", args.guide_duration_ms, "guide"),
            )
            for label, method, direction, duration, mode in corrections:
                if not _confirm(
                    f"ENTER arms {label} for {duration} ms; ESC/skip aborts> "
                ):
                    return 3
                result = method(
                    direction,
                    duration,
                    mode=mode,
                    cancel_check=_escape_requested,
                )
                ra_delta, dec_delta = _axis_delta(result)
                print(
                    f"{label}: ok={result.ok} cancelled={result.cancelled} "
                    f"tracking={result.tracking_before}->{result.tracking_after} "
                    f"logical_delta_ra_h={ra_delta} logical_delta_dec_deg={dec_delta} "
                    f"commands={result.commands_sent}"
                )
                if not result.ok or result.tracking_after is not True:
                    print("FAIL: bounded correction or tracking preservation failed.")
                    return 9
                if not _confirm("ENTER confirms physical motion/stop was safe; f/ESC rejects> "):
                    print("FAIL: operator rejected the observed correction.")
                    return 10

            parked = client.mount.park_via_home(
                timeout_s=args.park_timeout_s,
                poll_s=args.poll_s,
            )
            cleanup_required = False
            if not parked.get("ok") or client.mount.get_state() != MountState.PARKED:
                print(f"FAIL: final HOME->PARK was not confirmed: {parked}")
                return 11
            print("PASS: bounded RA/DEC guide and center corrections; final state PARKED.")
            return 0
        except KeyboardInterrupt:
            escaped = True
            client.mount.stop()
            print("ESC emergency stop sent. No automatic HOME/PARK movement will follow.")
            return 130
        finally:
            if cleanup_required and not escaped:
                print("Finalization: routing HOME->PARK.")
                result = client.mount.park_via_home(
                    timeout_s=args.park_timeout_s,
                    poll_s=args.poll_s,
                )
                print(f"Finalization result: {result}")


if __name__ == "__main__":
    raise SystemExit(main())
