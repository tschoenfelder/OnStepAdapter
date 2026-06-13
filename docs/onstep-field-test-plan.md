# OnStep Field Test Plan

This plan is for the real Raspberry Pi / OnStep rig. Run no-motion checks remotely if needed. Run any movement test only from a laptop with direct line of sight to the telescope and immediate access to Stop.

## 0. Preconditions

- SmartTScope server is running on the Raspberry Pi.
- UI is reachable at `http://rasppiserver3.fritz.box:8000`.
- OnStep mount is connected and reported in the UI.
- Emergency Stop is visible in the UI, or this command is ready in a terminal:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/emergency_stop
```

## 1. Startup And Config

Run:

```bash
./scripts/codex_start.sh --no-install
```

Expected:

- No Python traceback.
- Config line shows `/home/astro/.SmartTScope/config.toml`, or the UI readiness confirms config is green.
- Server starts on `http://0.0.0.0:8000`.

Check:

```bash
curl http://localhost:8000/api/status
```

Expected:

- Mount, focuser, solver are not red.
- GPS may warn/no-fix; this must not block observing by itself.

## 2. OnStep Safety And Clock

Run:

```bash
curl http://localhost:8000/api/mount/safety
```

Expected if OnStep was freshly powered:

- `system_clock.valid` is `true`.
- `onstep_clock.warning` may be `true`.
- If OnStep date is `01/01/-12`, `safety_locked` is `true` with `reason:"onstep_clock_invalid"`.

If OnStep clock is invalid and Raspberry time/location are correct, sync OnStep:

```bash
curl -X POST http://localhost:8000/api/mount/clock/sync_onstep \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"reason":"Current Raspberry time and SmartTScope config location confirmed correct"}'
```

Expected:

- `ok:true`.
- `date_reply:"1"`.
- `time_reply:"1"`.
- `onstep_clock.warning:false`.
- `safety_locked:false`.

Recheck:

```bash
curl http://localhost:8000/api/mount/safety
```

Expected:

- `safety_locked:false`.
- `observer.lat/lon/alt_m` match the intended current SmartTScope site.

## 3. GPS Advisory Check

Run:

```bash
curl 'http://localhost:8000/api/gps/status?force=true'
```

Expected without fix:

- `configured:true`.
- `fix.has_fix:false`.
- `apply_available:false`.
- This must not block mount progress.

Expected with fix:

- `fix.has_fix:true`.
- `fix.lat`, `fix.lon`, `fix.alt_m`, `fix.time_utc` are present.
- `deltas.location_m`, `deltas.time_s`, `deltas.altitude_m` are shown.
- No automatic sync happens.

Only if the GPS fix represents the real observing site, apply it:

```bash
curl -X POST http://localhost:8000/api/gps/apply \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"sync_onstep":true,"reason":"GPS fix confirmed for this observing site"}'
```

Expected:

- `ok:true`.
- `observer.source:"gps"`.
- OnStep site/time are synced.
- `config.toml` is not changed.

## 4. Parked Safety Semantics

Run:

```bash
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
curl 'http://localhost:8000/api/mount/park_home_state?margin_deg=0.25'
```

Expected while parked:

- `state:"parked"`.
- `physical_safe:true`.
- `tracking_safe:false`.
- `normal_motion_safe:false`.
- `requires_explicit_recovery_confirmation:true`.
- `status_reason:"parked_safe_but_motion_requires_explicit_confirmation"`.
- `park_home_state.mode:"parked_safe"`.
- `park_home_state.time_location.ready:true`.
- `park_home_state.allowed_actions` contains `recovery_unpark`.
- `park_home_state.blocked_actions.slew_to_home:"park_to_home_full_slew_disabled"`.
- `park_home_state.blocked_actions.park_to_home:"full_park_to_home_slew_disabled_use_unpark_then_slow_pulses"`.
- `park_home_state.home_reference.confirm_physical_home_allowed:true`.
- `park_home_state.home_reference.slew_to_home_available:false`.

## 5. Blocked Motion Checks

Run:

```bash
curl -i -X POST http://localhost:8000/api/mount/track
```

Expected:

- HTTP `409`.
- Message: `Tracking from parked requires explicit recovery unpark first`.

Run:

```bash
curl -i -X POST http://localhost:8000/api/mount/recovery/park_to_home \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"test disabled full park to home"}'
```

Expected:

- HTTP `409`.
- `reason:"full_park_to_home_slew_disabled_use_unpark_then_slow_pulses"`.

Run:

```bash
curl -i -X POST http://localhost:8000/api/mount/recovery/slow_pulse \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"test pulse while parked should fail","direction":"n","duration_ms":500}'
```

Expected:

- HTTP `409`.
- `reason:"unpark_before_recovery_pulse"`.

## 6. Confirm Current Parked State

This can be run remotely, but do not issue movement commands unless you are at the laptop with line of sight.

Run:

```bash
curl http://localhost:8000/api/mount/status
```

Expected parked baseline:

- `state:"parked"`.
- `safety.last_onstep_status:"nNPEW260"` or another status with uppercase `P`.
- `decoded_onstep_status.parked:true`.
- `decoded_onstep_status.tracking:false`.
- `safety.safety_locked:false`.
- `onstep_clock.warning:false`.

Observed known-good baseline on 2026-05-23:

- Parked raw status: `nNPEW260`.
- OnStep clock delta: below 2 seconds after sync.

## 7. Visible Movement: Watched Recovery Unpark

Run this only from the laptop with direct line of sight.

Before starting:

- Confirm telescope clearance visually.
- Keep the UI Stop button visible.
- Keep this terminal command ready:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/emergency_stop
```

Start watched unpark:

```bash
curl --max-time 6 -i -X POST http://localhost:8000/api/mount/recovery/unpark \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"visible watched unpark from parked position"}'
```

Expected:

- HTTP `200`.
- Response contains `accepted:true`.
- Response may contain `confirmed:false`; this is OK. Confirm by polling status.
- No SmartTScope-commanded full slew to celestial home.
- Mount leaves parked state.
- If motion looks wrong, use Stop immediately.

After unpark:

```bash
curl http://localhost:8000/api/mount/status
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected:

- `state:"tracking"`.
- `safety.last_onstep_status:"NpeEW260"` or another not-parked tracking status.
- `decoded_onstep_status.parked:false`.
- `decoded_onstep_status.not_parked:true`.
- `decoded_onstep_status.tracking:true`.
- Safety remains clear unless the current position violates configured limits.

Observed known-good result on 2026-05-23:

- Unpark command returned HTTP `200` in under 6 seconds.
- Status changed from `nNPEW260` to `NpeEW260`.
- SmartTScope decoded this as `state:"tracking"`.

## 8. Disable Tracking After Unpark

This can be run from the laptop immediately after watched unpark.

Run:

```bash
curl -i -X POST http://localhost:8000/api/mount/disable_tracking
curl http://localhost:8000/api/mount/status
```

Expected:

- Disable command returns HTTP `200` and `{"ok":true}`.
- `state:"unparked"`.
- `safety.last_onstep_status:"nNpeEW260"` or another not-parked/non-tracking status.
- `decoded_onstep_status.parked:false`.
- `decoded_onstep_status.not_parked:true`.
- `decoded_onstep_status.tracking:false`.

Observed known-good result on 2026-05-23:

- Before disable: `NpeEW260`, decoded as tracking.
- After disable: `nNpeEW260`, decoded as unparked and not tracking.

Stop condition:

- If tracking remains true after HTTP `200`, do not proceed with pulse tests. Capture `/api/mount/status` and stop testing.

## 9. Visible Movement: Recovery Pulse

Run only after successful watched unpark and only with line of sight.

Check current safety first:

```bash
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected before pulse:

- `parked:false`.
- `recovery_motion_allowed:true`.
- `physical_safe:false` is possible during recovery if normal validation is blocked, for example `hour_angle_east`.
- `normal_motion_safe:false` and `tracking_safe:false` mean normal tracking/goto remain blocked; they do not block explicit watched recovery pulses.
- No `safety_locked` condition in `/api/mount/status`.

Send one very short Dec pulse:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/mount/recovery/slow_pulse \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"visible watched first recovery pulse test","direction":"n","duration_ms":250}'
```

Expected:

- HTTP `200`.
- One tiny movement only, or no obvious movement if the guide rate is too low to see clearly.
- No continuous motion.
- If direction is unsafe, use Stop immediately.

Emergency stop command:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/emergency_stop
```

After the pulse:

```bash
curl http://localhost:8000/api/mount/status
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected after pulse:

- Mount remains not parked.
- `decoded_onstep_status.slewing:false`.
- `decoded_onstep_status.tracking:false` if tracking was disabled before the pulse.
- Safety lock remains clear.

Observed known-good Dec pulse result on 2026-05-24:

- Starting state was `state:"unparked"`, raw `nNpeEW260`, `tracking:false`.
- `direction:"n"`, `duration_ms:250` returned HTTP `200`.
- Emergency stop returned HTTP `200`, `mount_stopped:true`.
- `direction:"s"`, `duration_ms:250` returned HTTP `200`.
- Status after pulses remained `state:"unparked"`, `tracking:false`, `slewing:false`, `safety_locked:false`.
- Dec moved slightly up after `n` and back down after `s`, confirming pulse effect.
- A timeout observed before this result was fixed by treating `:Mg...#` as a no-reply OnStep command.

Stop condition:

- If movement continues after the pulse duration, immediately call `/api/emergency_stop`.
- If the pulse direction moves toward a collision risk, immediately call `/api/emergency_stop`.
- If any pulse returns HTTP `409`, record `detail.reason` and do not bypass it.

## 10. Visible Movement: RA-Axis Recovery Pulse

Run only after the Dec-axis `n`/`s` pulse test succeeded, with line of sight and stop ready. RA-axis recovery matters most when current safety reports `hour_angle_east` or `hour_angle_west`.

Before starting, check the live mount state:

```bash
curl http://localhost:8000/api/mount/status
```

Use only these live top-level fields for the decision:

- `state`
- `safety.last_onstep_status`
- `safety.decoded_onstep_status.parked`
- `safety.decoded_onstep_status.tracking`

Do not use `safety.persisted_state_at_startup` for movement decisions. It is only the saved snapshot from application startup and can be stale after later park/unpark operations.

If live state is parked:

- `state:"parked"`.
- `safety.last_onstep_status:"nNPEW260"` or another status with uppercase `P`.
- `safety.decoded_onstep_status.parked:true`.

Then unpark first:

```bash
curl --max-time 6 -i -X POST http://localhost:8000/api/mount/recovery/unpark \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"visible watched unpark before RA recovery pulse"}'
```

Expected:

- HTTP `200`.
- `accepted:true`.

Then confirm/force tracking disabled:

```bash
curl -i -X POST http://localhost:8000/api/mount/disable_tracking
curl http://localhost:8000/api/mount/status
```

Expected:

- `state:"unparked"`.
- `safety.last_onstep_status:"nNpeEW260"` or another not-parked/non-tracking status.
- `safety.decoded_onstep_status.parked:false`.
- `decoded_onstep_status.tracking:false`.
- `decoded_onstep_status.slewing:false`.

Stop condition:

- If status still says `state:"parked"`, do not run RA pulses. Repeat watched unpark or stop testing.
- If status says `tracking:true`, disable tracking again before pulsing.

Observed parked-state guard on 2026-05-24:

- Calling `disable_tracking` while already parked returned HTTP `200`, but live `/api/mount/status` still reported `state:"parked"` and raw `nNPEW260`.
- The following RA pulse correctly returned HTTP `409` with `reason:"unpark_before_recovery_pulse"`.
- This confirms the live parked guard works; unpark is required before RA pulse testing.

Run the first RA pulse with the shortest watched duration:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/mount/recovery/slow_pulse \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"visible watched first RA recovery pulse test","direction":"e","duration_ms":250}'
```

Expected:

- HTTP `200`.
- One tiny RA-axis movement only.
- No continuous movement.
- Use emergency stop immediately if the motion goes toward collision risk.


Emergency stop command:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/emergency_stop
```

After the pulse:

```bash
curl http://localhost:8000/api/mount/status
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected:

- Mount remains `state:"unparked"`.
- `decoded_onstep_status.tracking:false`.
- `decoded_onstep_status.slewing:false`.
- Safety lock remains clear.
- HA changes slightly. If the pulse moved HA farther outside the limit, stop and use the opposite direction only if visually safe.

If the first RA direction was visibly safe, test the opposite direction:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/mount/recovery/slow_pulse \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"visible watched opposite RA recovery pulse test","direction":"w","duration_ms":250}'
```

Expected:

- HTTP `200`.
- Tiny opposite RA-axis movement.
- No continuous motion.

Stop condition:

- If either RA pulse returns no response within 3 seconds, call `/api/emergency_stop`, capture `/api/mount/status`, and stop testing.
- If either RA pulse causes continuous motion or visually unsafe movement, call `/api/emergency_stop` and stop testing.
- If current safety changes to normal motion safe, do not automatically enable tracking; first capture status and decide intentionally.

## 11. Tracking Enable Check

Run only when current safety reports normal motion safe.

Check:

```bash
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Proceed only if:

- `parked:false`.
- `normal_motion_safe:true`.
- `tracking_safe:true`.

Enable tracking:

```bash
curl -i -X POST http://localhost:8000/api/mount/track
```

Expected:

- HTTP `200`.
- `/api/mount/status` eventually reports `state:"tracking"`.
- Raw status likely returns to a tracking form such as `NpeEW260`.
- `decoded_onstep_status.tracking:true`.

If blocked:

- Keep it blocked.
- Record `detail.reason`.
- Do not bypass with recovery pulses unless visually safe and intentional.

Disable tracking again before parking if you want a stable non-tracking check:

```bash
curl -i -X POST http://localhost:8000/api/mount/disable_tracking
curl http://localhost:8000/api/mount/status
```

Expected:

- `state:"unparked"`.
- `decoded_onstep_status.tracking:false`.

## 12. Observation Boundary Checks

These checks verify behavior at the normal observation boundaries. They can mostly be run remotely because they use validation endpoints and do not move the mount. Any test that enables tracking or sends a pulse must be done from the laptop with line of sight.

### 12.1 Boundary Configuration Snapshot

Run:

```bash
curl http://localhost:8000/api/mount/safety
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
curl 'http://localhost:8000/api/mount/meridian_status?margin_deg=0.25'
```

Expected:

- OnStep limits are visible in `onstep_limits`.
- SmartTScope limits are visible in `configured_limits`.
- Effective altitude limits are visible in `effective_limits`.
- Horizon file status is visible as `horizon_profile_loaded`.
- If parked, `physical_safe:true`, `normal_motion_safe:false`, `tracking_safe:false`.
- If outside HA limit, `validation.violation.reason` is `hour_angle_east` or `hour_angle_west`.
- `meridian_status.auto_flip_enabled:false`.
- `meridian_status.smarttscope_flip_available:true` for the OnStep adapter.
- `meridian_status.time_location.ready:true`.
- `meridian_status.ha_hours`, `ha_deg`, `time_to_meridian_h`, and `time_to_west_limit_h` are present when RA is known.
- `meridian_status.pier_side` reports authoritative `:Gm#` readback (`east` or `west`).

## Outstanding Meridian Physical Tests

Run these separately and stop after any failure.

Normal controlled flip:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite watched_adapter_normal_meridian_flip \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --goto-timeout-s 120 \
  --home-route-timeout-s 120 \
  --final-park-timeout-s 120
```

Expected: OnStep BEST may select either pier side at `HA=+1 deg`. The harness
records it, observes the flip recommendation at `+2.00 deg` within `0.05 deg`,
starts the flip within 10 seconds, requires `:Gm#` to change to the opposite
side within 120 seconds, verifies target reacquisition, resumes tracking without
another recommendation, and cleans up to live `:GU# P`.

Post-flip firmware safeguard:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite watched_onstep_postflip_firmware_stop \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --goto-timeout-s 120 \
  --home-route-timeout-s 120 \
  --final-park-timeout-s 120
```

Expected: controlled flip establishes the side opposite OnStep BEST's initial
selection, proof tracking remains on that verified side at `HA=+4 deg`, and
OnStep autonomously stops or reports a limit at `+5 deg`.
Two consecutive non-tracking polls count as proof. The independent raw backstop
is `+5.25 deg`; reaching it produces `SAFETY_NOT_PROVEN`.

### 12.2 Target Validation: Known Safe Target

Use a target expected to be inside all configured limits:

```bash
curl 'http://localhost:8000/api/mount/validate_target?ra=12.0&dec=20.0'
```

Expected:

- `allowed:true`, if the current sidereal time still places the target inside limits.
- `context.alt_deg` is above `effective_min_alt_deg`.
- `context.alt_deg` is below `effective_max_alt_deg`.
- HA is between `ha_east_limit_h` and `ha_west_limit_h`.
- `violation:null`.

If this target is not safe at the current time, choose another target from the grid tool output that reports `ok yes`.

### 12.3 Target Validation: Horizon / Low Altitude Block

Use the grid tool to find a target below the effective horizon:

```bash
python -m smart_telescope.tools.onstep_validate_grid \
  --api-base http://localhost:8000 \
  --ra-start 0 --ra-stop 24 --ra-step 1 \
  --dec-start -30 --dec-stop 30 --dec-step 15
```

Expected:

- At least some rows are blocked with a low-altitude or horizon reason such as `below_horizon`.
- For blocked rows, `alt` is below `min` or below the horizon-file value.
- A blocked target must not be usable for normal goto/tracking.

Direct endpoint check for one blocked row:

```bash
curl 'http://localhost:8000/api/mount/validate_target?ra=<blocked_ra>&dec=<blocked_dec>'
```

Expected:

- `allowed:false`.
- `violation.reason` identifies the altitude or horizon boundary.
- `violation.severity:"blocked"`.

### 12.4 Target Validation: Overhead Limit Block

Use the grid tool to find a target near zenith or above `effective_max_alt_deg`:

```bash
python -m smart_telescope.tools.onstep_validate_grid \
  --api-base http://localhost:8000 \
  --ra-start 0 --ra-stop 24 --ra-step 0.5 \
  --dec-start 40 --dec-stop 80 --dec-step 5
```

Expected:

- Targets above the configured overhead boundary are blocked with an overhead/altitude reason.
- For blocked rows, `alt` is above `max`.
- Normal goto/tracking must remain blocked.

### 12.5 Target Validation: Hour-Angle Boundary Block

Current known recovery state on 2026-05-24 showed:

- HA around `-7.86 h`.
- `ha_east_limit_h:-5.5`.
- `current_safety.validation.violation.reason:"hour_angle_east"`.

Run:

```bash
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
curl 'http://localhost:8000/api/mount/park_home_state?margin_deg=0.25'
curl 'http://localhost:8000/api/mount/meridian_status?margin_deg=0.25'
```

Expected when outside the HA boundary:

- `normal_motion_safe:false`.
- `tracking_safe:false`.
- `validation.allowed:false`.
- `validation.violation.axis:"ra"`.
- `validation.violation.reason:"hour_angle_east"` or `hour_angle_west`.
- `recovery_motion_allowed:true` only if live state is unparked and fresh.
- If live state is parked, `recovery_motion_allowed:false`.
- `park_home_state.mode:"unparked_recovery"` if live state is unparked and fresh.
- `park_home_state.time_location.ready:true`.
- `park_home_state.allowed_actions` contains `recovery_pulse`.
- `park_home_state.blocked_actions.track:"outside_normal_motion_limits"`.
- `meridian_status.boundary_state` is `outside_east_limit`, `outside_west_limit`, or `west_limit_approaching`.
- `meridian_status.stop_tracking_required:true` for outside/approaching west boundary states.

If time/location are not ready:

- `park_home_state.time_location.ready:false`.
- `park_home_state.time_location.reason` explains the issue, for example `onstep_clock_not_synced`.
- Normal motion and recovery pulses remain blocked.
- `meridian_status.stop_tracking_required:true`.

### 12.6 Normal Motion Must Be Blocked Outside Boundaries

Run only while `current_safety` reports a boundary violation.

```bash
curl -i -X POST http://localhost:8000/api/mount/track
```

Expected:

- HTTP `409`.
- Error explains tracking is blocked by safety or recovery state.
- Status after the command still reports no tracking.

Do not issue a real goto to a blocked target unless the endpoint is known to be dry-run/validation only. The normal acceptance condition is that `validate_target` blocks it before motion.

### 12.7 Recovery Motion At Boundary

Run only from the laptop with line of sight. This verifies that normal motion remains blocked while explicit watched recovery is still available.

Preconditions:

- `/api/mount/current_safety` reports a boundary violation such as `hour_angle_east`.
- Live status is `state:"unparked"`.
- `decoded_onstep_status.tracking:false`.
- `recovery_motion_allowed:true`.

Run one short recovery pulse:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/mount/recovery/slow_pulse \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"stop_available":true,"reason":"boundary recovery pulse test","direction":"e","duration_ms":250}'
```

Expected:

- HTTP `200`.
- No continuous movement.
- Emergency stop remains available.
- Normal motion can still remain blocked until the mount is actually back inside boundaries.

Emergency stop:

```bash
curl --max-time 3 -i -X POST http://localhost:8000/api/emergency_stop
```

### 12.8 Boundary Exit Confirmation

After one or more watched recovery pulses, run:

```bash
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected before the mount is back inside limits:

- `normal_motion_safe:false`.
- `tracking_safe:false`.
- Boundary violation remains visible.

Expected after the mount is back inside limits:

- `validation.allowed:true`.
- `normal_motion_safe:true`, if unparked and state is fresh.
- `tracking_safe:true`, if unparked and state is fresh.
- Only then may normal tracking be tested intentionally.

## 13. End State

Return to park only when visually safe:

```bash
curl -i -X POST http://localhost:8000/api/mount/park
```

Expected:

- Mount parks.
- `/api/mount/status` reports `state:"parked"`.
- `current_safety` returns `physical_safe:true`, `tracking_safe:false`.
- Raw status likely returns to parked form such as `nNPEW260`.

Final confirmation:

```bash
curl http://localhost:8000/api/mount/status
curl 'http://localhost:8000/api/mount/current_safety?margin_deg=0.25'
```

Expected:

- `state:"parked"`.
- `decoded_onstep_status.parked:true`.
- `decoded_onstep_status.tracking:false`.
- `physical_safe:true`.

