# OnStep Adapter Readiness Plan

This is the working gate for shipping the direct OnStep mount/focuser adapter into the main SmartTScope controller. It tests the adapter contract directly. The SmartTScope web API is a later integration gate, not the primary proof that the adapter is safe.

## Goal

The OnStep adapter is ready when it can:

- connect to OnStep and decode compact status reliably,
- keep time, location, and limit trust explicit,
- block normal astronomical motion outside safe boundaries,
- unpark for recovery without a blind home slew,
- immediately disable tracking after recovery unpark when OnStep resumes it,
- allow only bounded watched recovery pulses outside normal observation limits,
- return to OnStep parked state and record one mirror-safe park-pose confirmation per trusted session,
- keep coordinate-goto recovery quarantined until a separate deterministic mechanical-axis strategy exists.

## Mechanical-First Safety Model

HOME and PARK are mechanical state, not sky-coordinate state. They do not depend
on OnStep date/time or site location. Invalid/missing datetime or location blocks
astronomical authority only: tracking, goto, normal guide, target validation, and
meridian logic.

The adapter reports two readiness layers:

- `mechanical_ready`: OnStep status is usable, HOME/PARK authority is known, and
  HOME-relative axis limits are configured.
- `astronomical_ready`: `mechanical_ready` plus trusted app/Raspberry time,
  OnStep clock/site agreement, and sky-coordinate safety validation.

HOME is the mechanical origin: `axis1_deg=0`, `axis2_deg=0`. PARK is a
mirror-safe storage pose calibrated from HOME. The calibration flow confirms HOME,
uses bounded adapter movement to reach PARK, then stores PARK both locally in the
rig calibration file and in OnStep with Set-Park (`:hQ#`). Later `:hP#` is
asynchronous when accepted; completion is proven only by polling `:GU#` for `P`.
If OnStep emits a one-byte `0`, the adapter treats that as command rejection
instead of letting it pollute the next status read.

Mechanical crash-prevention limits are HOME-relative axis limits and are
independent of datetime/location. Astronomical HA/RA/DEC/Alt/Az limits require
trusted datetime/location.

Storage PARK is allowed to be at or near 90 degrees physical tube altitude.
This must not weaken normal observing limits: `max_alt_deg` still gates
astronomical tracking/goto, but it must not block mechanical HOME/PARK
transitions or final parking.

All normal PARK transitions must be routed through HOME to protect cable routing:

- Unpark request: `PARK -> HOME -> unparked stopped`.
- Park request: `current mechanical pose -> HOME -> PARK`.

Until the moving HOME path is field-proven, the adapter must not hide this behind
RA/DEC or target-goto logic. The implementation gate for automatic HOME routing
is a watched adapter test that proves the HOME leg and PARK leg from live OnStep
status, with ESC emergency stop active. PARK/HOME motion is mechanical and must
not fail only because astronomical `max_alt_deg` would be exceeded by the
mirror-safe storage pose.

The first HOME-routing proof is `watched_adapter_home_route`: it prepares the
mount unparked/not-tracking if needed, sends OnStep Find/Home (`:hC#`), and polls
`:GU#` until the decoded `at_home` flag appears. This proves only the HOME leg.
The separate `watched_adapter_home_to_park_route` test must prove HOME -> PARK
by sending `:hP#` after a settle delay and polling `:GU#` until `P`/parked is
reported. A field run on 2026-05-25 showed that sending PARK immediately after
the first `at_home=true` can return `0`; with a 15 s settle delay, HOME -> PARK
was status-confirmed. Normal HOME-routed park must therefore wait for `at_home`
and a settle interval before `:hP#`.

During both HOME and HOME-to-PARK mechanical travel, compact `:GU#` status can
report `unparked`/not-slewing for many polls and only intermittently report
`slewing`, even while the mount is visibly moving. For mechanical HOME/PARK
routes, the adapter must not interpret `N`/not-slewing as proof that physical
motion has stopped. Completion is proven only by the target mechanical status:
`at_home=true` for HOME and `P`/parked for PARK.

The implementation now exposes explicit routed mechanical methods:

- `unpark_to_home_stop_tracking()` for PARK -> HOME/not-tracking.
- `park_via_home()` for current pose -> HOME -> settled PARK.

These methods are the candidate app-facing HOME/PARK path after the routed TDD
cycle passes on the rig.

## Current Field Evidence

Recorded Raspberry runs have proven:

- Non-moving contract suite passes with `--trust-system-time --allow-broad-onstep-limits`.
- Time/location readiness can be made ready through the adapter.
- Broad firmware limits are explicit and only accepted when the rig policy flag is set.
- Current HA boundary violation is reported and blocks normal motion.
- Parked recovery pulse is blocked.
- Recovery unpark leaves OnStep unparked and disables tracking.
- 250 ms N/S/E/W recovery pulses are accepted and bounded.
- Repeated DEC recovery pulses move DEC one way and then back by the same amount.
- HOME route reaches OnStep `at_home=true`; status may remain `unparked` and
  only intermittently show `slewing` while motion is ongoing.
- HOME-to-PARK route succeeds when PARK is sent after a 15 s settle at HOME, and
  final parked state is confirmed by later `:GU#` `P`.
- Routed adapter method `watched_adapter_routed_home_park_cycle` passes:
  PARK -> HOME/not-tracking -> settled PARK, without relying on the intermittent
  slewing flag.
- Finalization parks the mount.
- Operator confirmed mirror-safe upward park pose.

## Adapter TDD Gates

Run these on the Raspberry without the SmartTScope server.

### 1. Non-Moving Contract Gate

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite contract,time-location,gps,limits,guards \
  --trust-system-time \
  --allow-broad-onstep-limits
```

Expected:

- no `FAIL`,
- `boundary_validation_contract` may be `BLOCKED_EXPECTED`,
- `gps_sync_onstep_contract` may be skipped when GPS has no fix,
- summary verdict says the non-moving adapter contract gate is satisfied.

### 2. Watched DEC Recovery Gate

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite watched_adapter_park_unpark_status_cycle,watched_adapter_unpark,watched_adapter_pulse_n,watched_adapter_pulse_s \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --pulse-ms 250 \
  --pulse-repeat 10 \
  --pulse-gap-s 0.2 \
  --final-park-timeout-s 45 \
  --final-park-poll-s 2
```

Expected:

- park/unpark cycle starts parked and trusts only repeated OnStep status polling,
- immediate `:hR#` and `:hP#` fixed one-byte `0`/`1` replies are treated as command acceptance/rejection only,
- fire-and-forget unpark completion is confirmed by status as unparked or tracking,
- if unpark resumes tracking, tracking is disabled and confirmed by status,
- asynchronous park completion is confirmed by status as parked,
- unpark returns `unparked`, not tracking,
- N pulse changes DEC in one direction,
- S pulse returns DEC in the opposite direction,
- no continuous motion,
- final park reaches `parked`,
- mirror-safe park pose is confirmed if not already confirmed in the trusted session.

### 3. Optional RA Pulse Gate

Run only with line of sight and if the operator wants evidence for RA pulse behavior. It is not required for the first controller-integration gate because the current rig is outside HA limits and DEC recovery has proven bounded pulse behavior.

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite watched_adapter_unpark,watched_adapter_pulse_e,watched_adapter_pulse_w \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --pulse-ms 250 \
  --pulse-repeat 10 \
  --pulse-gap-s 0.2 \
  --final-park-timeout-s 45 \
  --final-park-poll-s 2
```

Expected:

- adapter disables tracking before pulses if needed,
- pulse commands remain bounded,
- final state is parked.

## Automated Unit Gate

Run locally or on the Raspberry:

```bash
pytest tests/unit/adapters/onstep tests/unit/api/test_mount.py tests/unit/api/test_gps.py tests/unit/api/test_emergency.py
```

Required coverage:

- `:GVP#`, compact `:GU#`, `:hR#`, `:Te#`, `:Td#`, and no-reply `:Mg...#` behavior,
- `:hP#` fixed `0`/`1` acceptance behavior and asynchronous completion by later `:GU#` `P`,
- `:hC#` HOME routing as a no-reply command with completion only by later `:GU#` `H`,
- direction movement/stop/rate commands as no-reply writes,
- time/location/limit readiness diagnostics,
- parked guard and unparked recovery pulse behavior,
- mechanical `home_reference_confirmed`, `park_pose_confirmed`, and `position_authority`,
- coordinate recovery offset remains out of the accepted readiness path,
- emergency stop remains available.

## Final Operation-First Release Gate

Validation is intentionally limited to these five commands. If any command
fails, stop and fix before continuing; do not add extra field tests unless one
of these five is explicitly replaced.

1. Non-moving protocol and firmware audit:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite onstep_raw_read_probe,onstep_firmware_safety_audit \
  --trust-system-time \
  --allow-broad-onstep-limits
```

2. Non-moving adapter readiness audit:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite contract,time-location,gps,limits,guards \
  --trust-system-time \
  --allow-broad-onstep-limits
```

3. Watched mechanical PARK/HOME/PARK route:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite watched_adapter_routed_home_park_cycle \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --home-route-timeout-s 60 \
  --home-route-poll-s 2 \
  --final-park-timeout-s 60 \
  --final-park-poll-s 2
```

4. Watched adapter tracking guard:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite watched_adapter_tracking_guard_watch \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --autonomous-watch-s 120 \
  --autonomous-watch-poll-s 2 \
  --home-route-timeout-s 60 \
  --home-route-poll-s 2 \
  --final-park-timeout-s 60 \
  --final-park-poll-s 2
```

This command is valid only when the mount is already tracking a real non-HOME
astronomical target. It must not route PARK -> HOME and then enable tracking to
manufacture a guard test, because HOME is a mechanical reference and RA/HA at
HOME is not a meaningful observing-target safety verdict. If the mount is HOME,
parked, or simply not tracking, the command should exit quickly with
`BLOCKED_EXPECTED`.

The guard output must include `ha`, `ha_state`, `flip_boundary`, and
`west_stop`. This is the release evidence that meridian flip recommendation was
checked and not silently missed. `ha_state=flip_recommended` must produce
`tracking_action=request_meridian_flip` without sending a stop. `ha_state` of
`east_stop_required` or `west_stop_required` must produce
`tracking_action=stop_tracking` and send tracking disable while the adapter is
alive.

If this watched command is run from mechanical HOME, the HA/meridian part is
not applicable. HOME is not an observing target, and RA/HA at the pole must not
be treated as a crash-risk tracking verdict. In that case the command exits
quickly with evidence that HOME was detected, not a full watch duration.

5. OnStep west-HA firmware safeguard proof:

First run the non-moving preflight:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite onstep_bad_adapter_fault_preflight \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --fault-start-before-limit-deg 1.0 \
  --fault-target-dec-deg 20 \
  --firmware-overrun-grace-deg 1.0
```

The first check compares Raspberry and OnStep civil date/time. If they do not
match within the configured threshold, the preflight stops before target
calculation and instructs the operator to run `sync_config_time_location`.
It then compares OnStep latitude/longitude with `[observer]` from
`~/.SmartTScope/config.toml`; altitude is reported as config-only because this
OnStep protocol has no altitude readback. After civil time and site match, it
compares sidereal time and prints the independently computed RA/DEC/HA/Alt/Az
target, OnStep sidereal time and pier side, the flip/west-stop/backstop
boundaries, and expected tracking time. It commands no motion. Do not proceed
if either time check, the site check, or start-target validation fails.

Then run the watched physical proof:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --gps-port /dev/ttyUSB_GPS0 \
  --suite watched_onstep_bad_adapter_fault_injection \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --fault-start-before-limit-deg 1.0 \
  --fault-target-dec-deg 20 \
  --firmware-overrun-grace-deg 1.0 \
  --autonomous-watch-s 600 \
  --autonomous-watch-poll-s 2 \
  --home-route-timeout-s 60 \
  --home-route-poll-s 2 \
  --final-park-timeout-s 60 \
  --final-park-poll-s 2
```

The command routes PARK -> HOME, computes a safe target 1 degree before the west
HA stop from live `:GS#`, prints the complete path, and requires a second empty
ENTER to arm the raw goto. It verifies the target position, confirms that the
adapter recommends a meridian flip without stopping, then deliberately ignores
the SmartTScope west stop. PASS requires OnStep itself to stop/report a
firmware safeguard before the raw backstop 1 degree beyond the stop. If the raw
backstop is needed, the result is `SAFETY_NOT_PROVEN`. If the boundary is not
reached, the target is missed, independent HA cannot be computed, or cleanup
cannot park, the test fails.

Release interpretation:

- OnStep firmware is the primary safety layer for Raspberry/app failure.
- The adapter watchdog is a supervised backup only.
- Horizon/overhead readback is partial firmware protection.
- HA/meridian and HOME-relative axis protection must be configured and proven
  before `unattended_tracking_allowed=true`.
- If those firmware protections are not proven, the adapter must report
  `onstep_firmware_protection.status=partial` and
  `unattended_tracking_allowed=false`.

## Non-Goals For This Gate

- Do not use coordinate-goto recovery to prove mechanical recovery.
- Do not treat RA/DEC/HA/ALT/AZ as proof of physical park/home pose.
- Do not require the SmartTScope webserver for adapter readiness.
- Do not silently apply GPS data to config or OnStep.
- Do not enable automatic meridian flips or PEC management as part of this adapter gate.

## Ready For Controller Integration When

- The non-moving contract gate is green.
- The watched DEC recovery gate is green and recorded.
- Unit tests for OnStep/GPS/emergency pass.
- Coordinate recovery offset is still blocked by default.
- The latest `summary.md` verdict says the selected gate is satisfied, and the combined evidence covers both non-moving and watched recovery gates.
