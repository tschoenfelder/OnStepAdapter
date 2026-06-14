"""Supervised PARK->HOME->target independent RA/DEC coordinate path."""

from __future__ import annotations

import argparse
from pathlib import Path

from onstep_adapter import OnStepClient, OnStepSafetyConfig
from smart_telescope.ports.mount import MountPosition, MountState

from .axis_motion_smoke import (
    _confirm,
    _print_time_location_preflight,
    _time_location_preflight,
    _wait_for_target,
)


def _coordinate_path(
    initial: MountPosition,
    *,
    ra_delta_h: float,
    dec_delta_deg: float,
) -> tuple[tuple[str, MountPosition], ...]:
    ra_changed = (initial.ra + ra_delta_h) % 24.0
    dec_changed = initial.dec + dec_delta_deg
    if not -90.0 <= dec_changed <= 90.0:
        raise ValueError("DEC offset leaves the physical [-90, 90] degree range")
    return (
        ("initial target", initial),
        ("increase RA", MountPosition(ra=ra_changed, dec=initial.dec)),
        ("increase DEC", MountPosition(ra=ra_changed, dec=dec_changed)),
        ("decrease RA", MountPosition(ra=initial.ra, dec=dec_changed)),
        ("decrease DEC", initial),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Supervised independent coordinate path: RA +1h, DEC +10deg, "
            "RA -1h, DEC -10deg; never writes PARK."
        ),
    )
    parser.add_argument("--port", required=True)
    parser.add_argument("--observer-lat", type=float, required=True)
    parser.add_argument("--observer-lon", type=float, required=True)
    parser.add_argument("--observer-alt-m", type=float, default=0.0)
    parser.add_argument("--confirm-time-location-sync", action="store_true")
    parser.add_argument("--target-ha-deg", type=float, default=-15.0)
    parser.add_argument("--target-dec-deg", type=float, default=20.0)
    parser.add_argument("--ra-delta-h", type=float, default=1.0)
    parser.add_argument("--dec-delta-deg", type=float, default=10.0)
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
        default=str(Path.home() / ".OnStepAdapter" / "coordinate_axis_smoke_state.json"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not -90.0 <= args.target_dec_deg <= 90.0:
        raise SystemExit("--target-dec-deg must be within [-90, 90]")
    if args.ra_delta_h <= 0.0:
        raise SystemExit("--ra-delta-h must be positive")
    if args.dec_delta_deg <= 0.0:
        raise SystemExit("--dec-delta-deg must be positive")

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
                        "BLOCKED before motion: verify Raspberry time and observer "
                        "coordinates, then add --confirm-time-location-sync."
                    )
                    return 3
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
                    print("FAIL: time/location readback still fails; no movement commanded.")
                    return 3

            lst_h = float(preflight["sidereal"]["onstep_lst_h"])
            initial = MountPosition(
                ra=(lst_h - args.target_ha_deg / 15.0) % 24.0,
                dec=args.target_dec_deg,
            )
            try:
                path = _coordinate_path(
                    initial,
                    ra_delta_h=args.ra_delta_h,
                    dec_delta_deg=args.dec_delta_deg,
                )
            except ValueError as exc:
                print(f"FAIL: {exc}")
                return 4

            print(
                "Path: PARK -> HOME -> initial target -> RA +"
                f"{args.ra_delta_h:.3f}h -> DEC +{args.dec_delta_deg:.3f}deg "
                f"-> RA -{args.ra_delta_h:.3f}h -> DEC -{args.dec_delta_deg:.3f}deg "
                "-> final confirmation -> HOME -> PARK."
            )
            print(
                "Each coordinate leg is a separate bounded GoTo. ESC sends immediate "
                "stop and suppresses all later automatic movement."
            )
            for index, (label, target) in enumerate(path):
                print(
                    f"  {index}: {label}: RA={target.ra:.6f}h DEC={target.dec:.3f}deg"
                )

            if not _confirm("ENTER arms PARK->HOME; ESC/skip aborts> "):
                return 5
            cleanup_required = True
            home = client.mount.unpark_to_home_stop_tracking(
                timeout_s=args.home_timeout_s,
                poll_s=args.poll_s,
            )
            if not home.get("ok"):
                print(f"FAIL: HOME route failed: {home}")
                return 6

            for index, (label, target) in enumerate(path):
                validation = client.mount.validate_target(
                    target.ra,
                    target.dec,
                    margin_deg=1.0,
                )
                if not validation.get("allowed"):
                    print(f"FAIL: {label} destination is unsafe: {validation}")
                    return 7
                if not _confirm(
                    f"ENTER arms {label} to RA={target.ra:.6f}h "
                    f"DEC={target.dec:.3f}deg; ESC/skip aborts> "
                ):
                    return 5
                client.mount.goto(target.ra, target.dec)
                if not _wait_for_target(
                    client,
                    target,
                    timeout_s=args.goto_timeout_s,
                    poll_s=args.poll_s,
                ):
                    print(f"FAIL: {label} was not acquired.")
                    return 8
                actual = client.mount.get_position()
                print(
                    f"REACHED {label}: RA={actual.ra:.6f}h DEC={actual.dec:.3f}deg"
                )
                if index < len(path) - 1:
                    if not _confirm(
                        "ENTER confirms this leg was physically safe; "
                        "f rejects, ESC stops> "
                    ):
                        print(f"FAIL: operator rejected {label}.")
                        return 9

            if not _confirm(
                "FINAL CONFIRMATION: ENTER authorizes HOME->PARK; "
                "f rejects, ESC stops without automatic movement> "
            ):
                print("FAIL: final path confirmation rejected.")
                return 10
            parked = client.mount.park_via_home(
                timeout_s=args.park_timeout_s,
                poll_s=args.poll_s,
            )
            cleanup_required = False
            if not parked.get("ok") or client.mount.get_state() != MountState.PARKED:
                print(f"FAIL: final HOME->PARK was not confirmed: {parked}")
                return 11
            print(
                "PASS: independent RA/DEC coordinate path completed and final state PARKED."
            )
            return 0
        except KeyboardInterrupt:
            escaped = True
            client.mount.stop()
            print("ESC emergency stop sent. No automatic HOME/PARK movement will follow.")
            return 130
        finally:
            if cleanup_required and not escaped:
                print("Finalization after failure: routing HOME->PARK.")
                result = client.mount.park_via_home(
                    timeout_s=args.park_timeout_s,
                    poll_s=args.poll_s,
                )
                print(f"Finalization result: {result}")


if __name__ == "__main__":
    raise SystemExit(main())
