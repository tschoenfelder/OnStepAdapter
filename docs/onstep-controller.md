# OnStep Controller Notes

SmartTScope owns the OnStep mount/focuser configuration. At startup it builds an `OnStepSafetyConfig` from `config.toml` and passes that object into the OnStep mount and focuser adapters. The adapters then read OnStep firmware limits over LX200 and apply the stricter combined policy.

## Safety Sources

The effective movement policy combines:

- OnStep firmware horizon and overhead readback via `:Gh#` and `:Go#`.
- SmartTScope `[mount_limits]`: altitude, hour-angle, and declination bounds.
- SmartTScope horizon file from `[session].horizon_dat`.
- Runtime state from OnStep `:GU#`, current RA/Dec, and the persisted state file.
- OnStep clock readback via `:GC#` and `:GL#`, compared with Raspberry local time.

OnStep should keep its own horizon near the physical firmware limit. SmartTScope horizon files may be higher for trees, buildings, and imaging quality. The adapter validates against the higher minimum altitude.

## Mechanical HOME/PARK Authority

HOME and PARK are mechanical references and are independent of OnStep date/time
and site location. A bad OnStep clock blocks astronomical operations such as
tracking, goto, target validation, and meridian logic, but it does not by itself
block mechanical HOME/PARK work such as confirming HOME, storing PARK, parking,
or unpark-stop-tracking.

The adapter uses HOME as the mechanical origin (`axis1_deg=0`, `axis2_deg=0`).
PARK is a calibrated mirror-safe pose reached from HOME by the operator, stored
locally in the rig calibration file, and written to OnStep with Set-Park
(`:hQ#`). HOME-relative mechanical axis limits are the crash-prevention layer;
RA/DEC/HA/Alt/Az are astronomical diagnostics until time and location are
trusted.

The mirror-safe PARK pose may be at or near 90 degrees physical tube altitude.
That is allowed for storage and must not relax the normal astronomical observing
ceiling. In other words, `max_alt_deg` can still block tracking/goto near zenith,
but it must not by itself block mechanical HOME/PARK transitions.

Cable safety requires PARK transitions to pass through HOME:

- Unpark means move from PARK to HOME, then leave the mount unparked with
  tracking disabled.
- Park means move from the current mechanical pose to HOME, then to PARK.

This is a mechanical requirement, not an RA/DEC target requirement. The adapter
must not prove these transitions through sky coordinates. Automatic HOME routing
requires a watched adapter test for the HOME leg before it becomes the default
implementation path.

The watched HOME-routing proof uses OnStep Find/Home (`:hC#`) and polls compact
status until `at_home` is decoded from `:GU#`. It is separate from RA/DEC target
validation and remains supervised with emergency stop active. HOME -> PARK is a
second watched proof, `watched_adapter_home_to_park_route`; it sends `:hP#` only
after `at_home` is already reported, waits for the configured HOME settle period,
and trusts PARK only after a later `:GU#` reports parked. A successful HOME route
is not by itself proof that PARK routing works.

The adapter exposes this proven routing as explicit mechanical methods before it
is made the default for application-level `park()`/`unpark()`:

- `unpark_to_home_stop_tracking()`: recovery unpark, disable tracking if OnStep resumes it, send Find/Home, then poll until `at_home`.
- `park_via_home()`: route to HOME if needed, wait `home_park_settle_s`, send PARK, then poll until parked.

For HOME/PARK mechanical travel, `:GU#` can report `N`/not-slewing and the
adapter state can appear `unparked` during visible motion, with `S`/slewing only
appearing intermittently. Do not use `not_slewing` as a stop/completion proof for
these routes; use `at_home` or `parked`.

## Limit Calibration UI

The Stage 1 mount card shows the current OnStep limits and the decoded OnStep status. In advanced mode it also exposes OnStep limit calibration:

- `Horizon`: writes `:ShsDD#`.
- `Overhead`: writes `:SoDD#`.

Each write requires browser confirmation and the API confirms the command by reading the OnStep limits back.

## Focuser Max Calibration

The focuser card exposes `Set Max` in advanced mode. This stores a per-setup calibration file:

```text
~/.SmartTScope/onstep_focuser_calibration.json
```

The focuser minimum remains zero. The learned maximum is loaded on adapter connect and clamps future focuser moves.

Mount and focuser traffic use one `OnStepClient` and one locked serial bus.
Opening the OnStep USB serial device a second time is unsupported. The public
Python interfaces are `client.mount` and `client.focuser`; the HTTP interface
remains `/api/focuser`.

Validated field behavior on 2026-06-10:

- a 100-step absolute move reached its target and returned exactly;
- `:FQ#` stopped a 500-step move before its target;
- the focuser returned to its exact starting position after the stop test;
- `:GU#` remained PARKED before, during, and after all focuser operations;
- `:FT#` remained `S` while `:FG#` showed position progression, so completion
  and stop verification use both motion status and stable position.

## Mechanical Authority And Counterweight State

RA/DEC and OnStep axis coordinates are logical controller positions, not
independent mechanical sensor readings. SmartTScope establishes session
mechanical authority by routing `PARK -> HOME` and observing live `:GU#` HOME
status. Continuing after that successful route accepts HOME for the session.

Before normal goto, tracking, guide, or slew commands, the adapter reads one
fresh safety snapshot containing `:GU#`, `:Gm#`, logical RA/DEC and axis
coordinates when available, HA/meridian distance, motion state, and OnStep
limit/fault state. Missing or inconsistent inputs refuse normal motion.

The public safety contract reports `counterweight_state`,
`counterweight_up`, `operational_limit_margin_deg`, `limit_warning`,
`capture_pause_required`, `tracking_stop_required`, and `motion_refused`.
The adapter reports warning state only; SmartTScope owns exposure decisions.
At or beyond the inclusive hard limit, tracking is stopped and normal motion
is refused. Emergency stop and explicit HOME/PARK recovery remain available.

## Application-Controlled Meridian Flip

The imaging application owns exposure completion; the adapter owns mount
safety. The runtime supervisor reports one stable meridian session:

- before HA `0 deg`: normal tracking;
- HA `0..+2 deg`: post-meridian tracking remains allowed;
- at `+2 deg`: emit `meridian_flip_required` and reject new exposures;
- allow the active exposure to finish only if its remaining duration fits the
  reported budget;
- execute the opposite-pier flip only after the application submits the current
  `meridian_session_id`;
- at `+5 deg` inclusive: stop tracking if the application handoff was missed.

The supervised field gate on 2026-06-10 notified at HA `1.998 deg`, finished a
5-second frame, changed pier side from west to east, reacquired the target
exactly, resumed tracking, and returned through HOME to PARK.

## Parked Recovery

Parked is considered physically safe, but not automatically safe for normal motion. A full park-to-home slew is disabled. Recovery from park requires:

- user confirmation,
- Stop immediately available,
- explicit recovery unpark,
- then slow watched pulses only if needed.

Emergency Stop always bypasses safety locks.

## Confirmed Adapter Wire Behavior

OnStep commands use the LX200-style `:CC...#` frame. The documented maximum
command length is 40 characters: two frame characters (`:#`), two command-code
characters, and up to 36 parameter characters. Carriage returns and line feeds
may be sent but are ignored by OnStep. Replies are command-specific: many
commands return fixed `0`/`1` success bytes, many readbacks return `#`
terminated text, and movement/stop/rate commands often return nothing. Do not
guess a reply shape from a neighboring command.

The field rig uses compact `:GU#` status strings, not pipe-delimited status. Examples:

| Raw `:GU#` | Decoded state |
|---|---|
| `nNPEW260` | parked, not tracking |
| `NpeEW260` | not parked, tracking |
| `nNpeEW260` | not parked, not tracking |

Detection rule:

- `P` means parked.
- `p` means not parked.
- Lowercase `n` means not tracking; its absence means tracking.
- Uppercase `N` means no goto is active; its absence means a goto is active.
- `H` means at HOME.
- `F` means park failed.
- `I` means park in progress.
- `R` means PEC recorded.
- `G` means guiding in progress.
- `S` means GPS PPS synced.
- `E` means GEM mount.
- `T` means pier side east; `W` means pier side west.

Wrong approaches:

- Do not require pipe-delimited status such as `n|T|0|0`.
- Do not treat `T` as tracking.
- Do not treat `N` as tracking or as a generic motion flag.
- Do not treat compact `E`, `T`, or `W` as software limit flags.

Park/unpark behavior:

| Adapter method | Command | Confirmed behavior |
|---|---|---|
| `get_state()` | `:GU#` | Hash-terminated status read; compact status parser. |
| `unpark()` | `:hR#` | Restore parked telescope to operation; fixed one-byte `0`/`1` acceptance reply; local serial timeout override is 5 s. `:hU#` is rejected and must not be used. |
| `park()` | `:hP#` | Move to park position; fixed one-byte `0`/`1` acceptance reply. `1` means command accepted, not completed. Trust final parked state only after later `:GU#` reports `P`. |
| `set_park()` | `:hQ#` | Set current pose as park position; fixed one-byte `0`/`1` reply. |
| `return_home_mechanical()` | `:hC#` | Move to HOME/CWD; no reply. Completion must be polled through `:GU#` `H`. |
| `set_home()` | `:hF#` | Set HOME/CWD; no reply. Dangerous calibration action; not part of normal readiness tests. |

PARK and HOME are mechanical operations for this rig, but OnStep can still apply
its own mathematical motion limits while executing a park-like route. In
particular, the overhead limit (`:SoDD#` / `:Go#`) can block a park move if the
stored park pose or the computed path to it exceeds the configured overhead
altitude. Example: if park points the tube to 90 deg altitude but the overhead
limit is 85 deg, OnStep may reject or stop the park move. For a mirror-safe
upward park pose, either the overhead limit must permit that pose, commonly up
to 90 deg when mechanically safe, or the stored park pose must be lower. This is
OnStep firmware behavior, separate from SmartTScope's own RA/DEC/HA validator.
SmartTScope can also define AZ-conditioned overhead corridors, for example a
narrow corridor around north/south where ALT 90 is allowed for storage geometry.
Outside those corridors the normal overhead limit applies, and OnStep `:Go#`
still remains the firmware-level cap.

Operational safety is split into three authorities:

- Mechanical state: live `:GU#` HOME/PARK/tracking/limit/park-failed flags.
- Astronomical target safety: RA/DEC target validation using time, site, HA,
  DEC, horizon, overhead, and optional AZ overhead corridors.
- Tracking runtime safety: current position while tracking; the adapter reports
  `tracking_stop_required` and can disable tracking before the configured stop
  boundary while OnStep remains the last-resort firmware layer.

Firmware protection policy:

- OnStep firmware is the primary crash-prevention layer because it continues
  running if the Raspberry/app/adapter fails.
- The adapter watchdog is only a secondary supervised backup while the
  Raspberry is alive.
- Horizon/overhead readback (`:Gh#`/`:Go#`) proves only partial firmware
  protection. It does not prove HA/meridian or HOME-relative axis protection.
- Until OnStep firmware HA/axis/meridian protection is configured and proven,
  `unattended_tracking_allowed=false`.
- In that partial state, `supervised_tracking_allowed` may still be true when
  time/location/limits are ready and the adapter watchdog is active.

Meridian policy for this GEM rig:

- Tracking is allowed before the meridian.
- Tracking is allowed on the meridian.
- Tracking is allowed after the meridian until the configured west HA stop
  boundary, for example `+0.333h` = `+5 deg`.
- The calling app should start or prepare the meridian flip workflow while the
  mount is still inside that post-meridian safe range.
- `GET /api/mount/meridian_status` is the reconnect-safe application contract.
  `/ws/mount-events` emits transition events, including
  `meridian_flip_required` and `tracking_hard_stop`.
- One stable `meridian_session_id` identifies the tracked target across the
  notification and flip. `POST /api/mount/meridian_flip` requires that ID and
  rejects stale or premature requests.
- At the `+2 deg` recommendation the calling app stops starting new exposures.
  It may finish the active exposure only when its remaining duration is no
  greater than `max_safe_exposure_seconds`; otherwise it must abort the frame.
  The budget reserves 120 seconds for the flip plus 30 seconds safety margin.
- The status payload also exposes `seconds_to_flip_boundary`,
  `seconds_to_hard_stop`, `latest_safe_flip_start_seconds`,
  `frame_finish_deadline_utc`, `new_exposure_allowed`, and
  `finish_current_exposure_allowed`. The adapter does not control the camera.
- The adapter hard stop is the configured west HA boundary itself. At or beyond
  that boundary, `tracking_stop_required=true` and the adapter watchdog must
  disable tracking if still alive.
- Therefore `meridian_flip_recommended` and `tracking_stop_required` are not
  the same state. Flip recommendation is an early operational warning; tracking
  stop is the final safety boundary.
- The runtime safety payload must expose the HA decision evidence:
  `ha_hours`, `ha_limit_state`, `ha_east_limit_h`, `ha_west_limit_h`,
  `meridian_flip_boundary_h`, and `meridian_margin_deg`. The expected states
  are `pre_meridian_allowed`, `post_meridian_allowed`, `flip_recommended`,
  `postflip_tracking_allowed`, `east_stop_required`, and
  `west_stop_required`.
- `ha_limit_state=flip_recommended` means the caller must start/continue the
  meridian flip workflow, but the adapter must not stop tracking yet.
  `ha_limit_state=west_stop_required` means the flip window was exhausted and
  tracking must be stopped immediately.
- If OnStep reports `at_home`, HA/meridian validation is diagnostic only.
  HOME is a mechanical reference, not an astronomical observing target, and
  RA is not a distinct target coordinate at the pole. Therefore HOME must not
  produce `tracking_stop_required` from RA/HA validation.
- For the configured `+5 deg` west stop and `3 deg` margin, the flip request is
  emitted at `HA=+2.00 deg` (`+0.133 h`). Boundary comparisons are inclusive:
  exactly `HA=+5.00 deg` (`+0.333 h`) requires tracking to stop.
- With OnStep preferred pier side set to BEST, the initial goto may select
  either `E` or `W`. SmartTScope records the selected side when tracking starts.
  The flip recommendation is emitted at the configured HA boundary until a
  controlled flip changes `:Gm#` to the opposite side and reacquires the target.
- SmartTScope performs a controlled flip by disabling tracking, issuing
  OnStepX `:MN#` (goto current RA/Dec on the opposite pier side), and polling
  until `:Gm#` changes to the opposite of the session's initial side. Because
  sky RA advances while tracking is disabled during the long flip, SmartTScope
  resumes tracking as soon as the side change settles. Since `:MNe#`/`:MNw#`
  also target the current physical position, SmartTScope instead temporarily
  selects the confirmed side with `:SX96,E/W#`, restores the original RA/DEC,
  starts a normal `:MS#` goto, and immediately restores the prior preferred
  pier policy (normally BEST) with verified readback. Reacquisition must then
  be within `0.02 h` RA and `0.25 deg` DEC.
  The caller starts this within 10 seconds of the recommendation and completion
  is limited to 120 seconds.
- The post-flip firmware proof starts on the opposite pier side confirmed by the
  controlled BEST-side flip at `HA=+4.00 deg`. OnStep must stop or report a
  limit/fault at `+5.00 deg`; two consecutive
  non-tracking polls also count as evidence. An independent raw `:Td#`/`:Q#`
  backstop fires at `+5.25 deg`. If it fires, firmware safety is not proven and
  unattended tracking remains disabled.
- Do not physically test Raspberry loss before the flip on reported pier west. Stock
  OnStep falls back to Axis 1 maximum on that branch, not the required
  `+5 deg` boundary.

Movement/rate behavior:

| Operation | Command | Behavior |
|---|---|---|
| Normal guide east/west/north/south | `:Me#`, `:Mw#`, `:Mn#`, `:Ms#` | No reply; moves at current rate until stopped. |
| Direction stop | `:Qe#`, `:Qw#`, `:Qn#`, `:Qs#` | No reply; stops that direction. |
| Global stop | `:Q#` | No reply; stop telescope motion. |
| Pulse guide | `:Mgdnnnn#` | No reply; `d=n,s,e,w`, duration 20..16399 ms. |
| Rate guide/center/move/slew | `:RG#`, `:RC#`, `:RM#`, `:RS#` | No reply. |
| Rate n | `:Rn#` | No reply; `R0=0.25x`, `R1=0.5x`, `R2/RG=1x`, `R3=2x`, `R4/RC=4x`, `R5/RM=8x`, `R6=16x`, `R7/RS=24x`, `R8=40x`, `R9=60x` for the documented branch. |
| Distance bars | `:D#` | Read-only slew indicator; not a reliable mechanical completion proof for HOME/PARK on this rig. |
| Pier side | `:Gm#` | Read-only `N#`, `E#`, or `W#`. |
| Preferred pier policy | `:GX96#` | `E`, `W`, `B` (BEST), or `A` (automatic). |
| Opposite-side current-position goto | `:MN#` | One-byte `0..9`; `0` means accepted. |

Timeout handling rule:

- A command timeout is not final state evidence.
- If later `:GU#` contains `P`, SmartTScope must treat the live mount state as parked.
- If later `:GU#` contains `p`, SmartTScope must treat the live mount state as unparked.
- The timeout remains diagnostic evidence about command acknowledgement/serial latency, but live status wins for state.

Serial bus rule:

- Use hash-terminated reads for readback commands such as `:GU#`, `:GR#`, `:GD#`, `:GZ#`, `:GA#`, `:GC#`, `:GL#`, `:GS#`, `:Gt#`, `:Gg#`, `:Gm#`, `:D#`, `:GVP#`, `:GVD#`, `:GVT#`, and `:GVN#`.
- Use fixed one-byte reads with per-command timeout overrides for `:Sr#`, `:Sd#`, `:MS#`, `:hR#`, `:hP#`, `:hQ#`, `:Te#`, `:Td#`, and other documented one-byte commands. `:MS#` returns a one-byte error code where `0` means the slew was accepted.
- Use no-reply writes for `:hC#`, `:hF#`, `:M*#`, `:Q*#`, `:R*#`, `:Mg...#`, and emergency stop paths.
- Do not lengthen the global serial timeout to handle `:hR#`; long global timeouts block the shared bus.
- Keep the per-command timeout override local to the command that needs it.
- Treat the `:Td#` reply as command acceptance only. A tracking stop succeeds
  only when later `:GU#` no longer reports tracking. SmartTScope sends
  immediate `:Q#`, consumes the `:Td#` reply, retries once if necessary, and
  polls live status.
- ESC or Ctrl-C during fault injection performs the same status-verified stop.
  It must not start automatic HOME/PARK motion after an emergency interruption;
  recovery routing is a separate watched action.

OnStepX native limit readback:

| Command | Meaning | Units |
|---|---|---|
| `:GXE9#` | East past-meridian limit | minutes of hour angle |
| `:GXEA#` | West past-meridian limit | minutes of hour angle |
| `:GXEe#` / `:GXEw#` | Axis 1 minimum / maximum | degrees |
| `:GXEB#` | Axis 1 maximum | hours |
| `:GXEC#` / `:GXED#` | Axis 2 minimum / maximum | degrees |
| `:GXEG#` | SmartTScope dual-pier positive-HA stop capability | `1` enabled, `0` disabled; custom extension |

These signed limits are pier-side dependent and must not be compared directly
with one unsigned sky-HA stop:

- on pier east, OnStep checks `HA < -pastMeridianE`;
- on pier west, OnStep checks `HA > pastMeridianW`;
- other pier-side/HA combinations can instead reach Axis 1 bounds.

The June 9 fault-injection path was positive HA on pier east. It therefore did
not exercise the pier-west meridian branch; with Axis 1 limits at `-180/+180`
degrees, no firmware stop was expected before the one-degree raw backstop.

`:GX42#` and `:GX43#` report raw instrument coordinates. They must not be
compared directly with `:GXEe#`/`:GXEw#` and `:GXEC#`/`:GXED#`. OnStepX first
applies its GEM `instrumentToMount` transform. In the northern hemisphere, when
raw Axis 2 is greater than 90 degrees, the mount is on the west pier side and:

```text
mount_axis1 = instrument_axis1 - 180 degrees
mount_axis2 = 180 degrees - instrument_axis2
```

For the June 9 parked readback, raw `(175.2667, 128.0333)` therefore means
mount `(-4.7333, 51.9667)` on pier west, which is inside the configured
`[-180,+180]` and `[-90,+90]` mount-axis limits. This is a coordinate
diagnostic only. PARK and HOME are mechanical terminal states, so their
coordinates do not prove or disprove tracking-limit protection.

Operational limit evaluation is state-aware:

- `parked` or `at_home`: limit applicability is `not_applicable`; trust the
  mechanical status and the completed HOME/PARK route;
- stationary `unparked`: no active motion-limit branch is selected;
- `slewing` or `tracking`: use live pier side and HA to select the applicable
  OnStep meridian or Axis 1 branch;
- OnStep `at_limit` or `park_failed`: always a hard operational fault.

The intended meridian operation path is evaluated as three explicit segments:

1. reported pier-west tracking from the meridian to the configured flip request;
2. app-controlled pier-west to pier-east flip handoff;
3. reported pier-east tracking from the flip completion to the hard west stop.

The path is unattended-safe only if firmware protection covers both tracking
segments and the configured flip boundary precedes the hard stop. The
field-proven application path starts on pier WEST and flips to pier EAST.
Stock OnStepX applies `pastMeridianW` to the first segment, while the second
segment falls back to Axis-1 maximum. At `+180` degrees that fallback does not
implement SmartTScope's desired `+5` degree hard stop. Stock OnStepX validates
`AXIS1_LIMIT_MAX` only in the range `90..360` degrees, so it cannot be
configured as a `+5` degree stop.

The pre-flip pier-WEST boundary can be aligned with the SmartTScope `+5`
degree policy using `:SXEA,20#` (20 minutes of hour angle), after explicit
operator approval and readback verification. The dual-pier firmware extension
is required to apply the same threshold after the flip on pier EAST.

SmartTScope exposes this as `sync_onstep_west_meridian_policy`. It refuses to
write unless the explicit confirmation flag is present and live `:GU#` status
reports PARKED. It then requires the `:SXEA#` success reply and matching
`:GXEA#` readback. This action sends no movement or tracking command.
Compatibility uses half of OnStep's one-minute HA wire resolution as tolerance:
`20` minutes (`0.333333 h`) therefore matches a configured decimal
`ha_west_limit_h = 0.333`.

Consequently, unattended post-flip tracking is not proven by configuration
readback alone. Meeting the requirement needs one of:

- a physical OnStep Axis 1 limit input placed at the verified safe boundary;
- a verified custom OnStep firmware safeguard for this pier-side/HA path; or
- a staged physical proof that stock OnStep autonomously stops at Axis-1
  maximum and that every pose through that endpoint is mechanically safe.

The stock proof does not redefine SmartTScope's imaging policy. SmartTScope
still requests the flip at `+2 degrees` and stops at `+5 degrees` while alive.
The Axis-1 maximum, normally `180 degrees`, is only the final Raspberry-loss
fallback. Its proof record is bound to firmware identity, all axis-limit
readbacks, west-meridian readback, and observer latitude/longitude.

OnStep automatic meridian flip state is readable with `:GX95#` and is also
present as compact `:GU#` flag `a` when enabled. Source audit of OnStepX 10.19d
shows the actual positive-HA branches:

- pier WEST uses `pastMeridianW`;
- pier EAST falls back to Axis-1 maximum.

For the field-proven SmartTScope W-to-E flip, the stock gap is therefore after
the flip, not before it. `:SXEA,20#` protects the pre-flip pier-WEST segment at
`+5 degrees`, but stock firmware does not apply that threshold to post-flip
pier EAST.

SmartTScope 0.2 adds an optional OnStepX patch in
`firmware/onstepx-smarttscope-dual-pier-stop.patch`. When compiled with
`SMARTTSCOPE_DUAL_PIER_WEST_HA_STOP ON`, it applies `pastMeridianW` as a hard
positive-HA tracking stop on both pier sides. Capability readback is `:GXEG#`:
`1` means enabled, `0` means compiled but disabled, and an empty/unknown reply
means the extension is absent.

Unattended tracking is enabled only after either the dual-pier `+5 degree`
stops are physically proven or the stock pier-EAST Axis-1 fallback is
physically proven safe. Flashing firmware, changing a relevant limit, or
changing the observer location invalidates the stored proof automatically.

OnStep UTC offset convention:

- `:SGsHH#` stores the number of hours added to OnStep local time to obtain UTC.
- This sign is the inverse of the common local `UTC+N` notation. For example,
  Berlin summer time (`UTC+02`) must be sent to OnStep as `:SG-02#`.
- Time/location synchronization must write date, local time, UTC offset,
  latitude, and longitude together. A matching local clock alone does not prove
  correct sidereal time.
- Before astronomical motion, compare OnStep `:GS#` with SmartTScope-computed
  LST. A significant mismatch blocks motion even if `:GC#` and `:GL#` match.

West-HA firmware safeguard fault injection:

- First compare Raspberry local date/time with OnStep `:GC#` and `:GL#`.
  If the difference exceeds the configured clock threshold, stop before target
  calculation or any movement. Run `sync_config_time_location` to align OnStep,
  then restart the preflight.
- Next compare OnStep `:Gt#` and `:Gg#` with `[observer].lat` and
  `[observer].lon` loaded from `~/.SmartTScope/config.toml`. OnStep/LX200
  longitude is west-positive, while SmartTScope configuration is east-positive.
  Therefore Usingen `+8.533 deg` east must read back as approximately
  `-008*32`; `+008*32` represents west longitude and is a mismatch.
- `[observer].alt_m` remains the SmartTScope altitude reference. This OnStep
  command set has no site-altitude readback, so altitude cannot be independently
  compared with the controller.
- Only after civil time matches, compare `:GS#` with SmartTScope-computed LST.
- Compute the test target from OnStep sidereal time `:GS#`; do not use adapter-derived LST.
- Start 1 degree before the configured west HA stop, at the configured test DEC.
- Print RA/DEC/HA/Alt/Az, pier side, flip boundary, west stop, raw backstop, and expected timing before motion.
- Require an explicit empty ENTER arm after printing the path. ESC aborts and returns HOME -> PARK.
- Confirm goto completion from repeated `:GU#`, `:D#`, `:GR#`, and `:GD#` reads. A command acknowledgement is not completion.
- Independently monitor `:GS# - :GR#`. The adapter safety result is diagnostic comparison only and must recommend a flip without stopping at the safe start point.
- Deliberately ignore the SmartTScope west stop to simulate adapter/Raspberry failure. PASS requires OnStep itself to stop tracking, report `at_limit`, park, or fault after that boundary and before the raw backstop.
- If OnStep does not safeguard by 1 degree beyond the west stop, send raw `:Td#` and `:Q#` and report `SAFETY_NOT_PROVEN`.
- Console progress reports estimated seconds to the west stop and raw backstop.
  Continuous polling between those boundaries is expected, not a hang.

Detailed follow-up planning for park/home action policy, SmartTScope-controlled meridian handling, slew-rate gating, and PEC staging lives in:

```text
docs/onstep-park-home-meridian-pec-plan.md
```

## Hardware Validation Commands

Read state and decoded `:GU#` samples:

```bash
python -m smart_telescope.tools.onstep_safety_probe \
  --port /dev/ttyUSB_ONSTEP0 \
  --status-samples 5 \
  --status-interval 1
```

Validate OnStep limit write/readback, then restore the original values:

```bash
python -m smart_telescope.tools.onstep_safety_probe \
  --port /dev/ttyUSB_ONSTEP0 \
  --set-horizon-deg 0 \
  --set-overhead-deg 88 \
  --restore-limits \
  --i-understand-this-writes-onstep-limits
```

Validate the SmartTScope API target grid without moving the mount:

```bash
python -m smart_telescope.tools.onstep_validate_grid \
  --api-base http://localhost:8000 \
  --ra-start 10.5 --ra-stop 12.5 --ra-step 0.25 \
  --dec-start -10 --dec-stop 70 --dec-step 10 \
  --margin-deg 0.25
```

Watch current safety while parked, unparked, or tracking:

```bash
python -m smart_telescope.tools.onstep_validate_grid \
  --api-base http://localhost:8000 \
  --watch-current \
  --watch-count 10 \
  --watch-interval 2 \
  --margin-deg 0.25
```

## Status Sampling

The adapter decodes official `:GU#` flags and keeps unknown/extended flags visible. Useful states to sample during hardware validation:

- parked
- unparked
- tracking
- slewing
- guide pulse active
- park in progress
- park failed, if safely reproducible
- OnStep limit hit, if safely reproducible

Keep the raw status string with any report because OnStepX and OnStep V4 may expose extra compact flags.

## Clock Validation And Sync

The safety snapshot includes `system_clock` and `onstep_clock`.

SmartTScope first checks whether the Raspberry clock is sane by comparing the current local time with the SmartTScope application file timestamp. This protects a Raspberry Pi without RTC after power loss, where GPS/NTP may not have corrected time yet. If the Raspberry time is not sane, normal mount motion is blocked and SmartTScope will not copy that time into OnStep.

If the Raspberry time is sane, SmartTScope reads OnStep local date/time via `:GC#` and `:GL#`. A warning and safety lock are raised when the difference is larger than `[onstep_safety].clock_warning_threshold_s` (default 120 seconds). This catches fresh OnStep reboots such as `01/01/-12`.

Check remotely:

```bash
curl http://localhost:8000/api/mount/safety
```

Expected when clocks match:

```json
"onstep_clock": {
  "available": true,
  "warning": false,
  "delta_s": 0.0
}
```

If the Raspberry time and SmartTScope location have been checked, sync OnStep from the Raspberry:

```bash
curl -X POST http://localhost:8000/api/mount/clock/sync_onstep \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"reason":"Raspberry time and SmartTScope location checked correct"}'
```

Expected after a successful sync:

- `system_clock.valid` is `true`
- `onstep_clock.warning` is `false`
- `safety_locked` is `false`, unless another independent safety lock remains

If `system_clock.valid` is false, fix Raspberry time first and do not move the mount. If `onstep_clock.warning` is true, do not trust HA/park/home interpretation until OnStep has been synced or otherwise corrected.

## GPS Advisory Site/Time

GPS is advisory and never applied automatically. A GPS fix may arrive minutes after startup or not at all in the parked position. SmartTScope keeps using the configured observer site until the user explicitly applies a GPS fix.

Check GPS status:

```bash
curl http://localhost:8000/api/gps/status?force=true
```

The status compares the GPS fix with the active SmartTScope runtime site/time. Defaults:

- time warning: more than 2 seconds
- location warning: more than 100 meters
- altitude warning: more than 50 meters

Apply the current GPS fix to SmartTScope runtime and OnStep, without writing `config.toml`:

```bash
curl -X POST http://localhost:8000/api/gps/apply \
  -H 'Content-Type: application/json' \
  -d '{"confirmed_by_user":true,"sync_onstep":true,"reason":"GPS fix confirmed for this observing site"}'
```

After this, SmartTScope's active runtime observer state and OnStep site/time are intended to be identical. The config file remains the normal default for the next startup.
