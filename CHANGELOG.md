# Changelog

## 0.7.18 - Unreleased

- A DEC move never runs past the DEC bound. Field capture fd84c75f: from PARK at pier side
  east, NORTH raised DEC (51.8 -> 71.9 deg); the next +20 deg step was projected as
  "positive lowers DEC" (51.9 deg), started, was stopped by the in-motion check at 80.0 deg
  and coasted on to 88.3 deg. Now:
  - the pre-check uses the direction the last DEC move at this pier side measured
    (`IndiAxisMover.projected_dec_targets`); until one has (after connecting or HOME), BOTH
    directions must stay inside the bound;
  - a DEC pass stops so that its coast ends `BOUND_MARGIN_DEG` (0.2 deg) inside the bound,
    and the move then fails with "DEC move stopped at ... would have passed the configured
    DEC bound".

## 0.7.17 - Unreleased

- Finite single-axis moves (`move_dec_axis_deg` / `move_ra_axis_deg`) land on target in
  one pass. Field capture 2eb2a5cb: a 10 deg DEC move swung back and forth (77.7, 58.0,
  63.6 deg) and gave up after 8 passes, because the real lx200_OnStep driver publishes
  no OnStep Status while a motion switch is On (the live stop was skipped on every such
  sample), the stop allowed ~0.5 deg for braking where the rig needs ~10 deg at full
  speed, and "stopped" was taken from :GU#, which never flags manual motion (a pass was
  measured while the axis still coasted). Now:
  - a pass stops from the live position, predicted between readings, as soon as the
    remaining angle equals the distance the axis still covers (reaction time plus
    braking `v^2 / 2a`); `a` is learned from each pass's coast;
  - stale OnStep Status alone no longer interrupts a move while coordinates are live
    (`IndiAxisMover.check_in_motion`); a pass with no fresh reading for 2.5 s stops;
  - "at rest" is two agreeing fresh coordinate readings plus a fresh status with
    tracking off;
  - angles from 0.5 deg use the fastest rate; the probe pass, pass-growth limit and
    timer-based pass lengths are gone.
- `MAX_AXIS_MOVE_DEG` is 45 (was 15): a 20 deg step is one move (operator request
  2026-11-18: 1/5/20 deg RA/Dec steps); the DEC and altitude bounds still apply.
- Mock: `SimConfig.rig_2eb2a5cb()` models the rig as that capture recorded it (start
  0.39 s after On, 0.54 deg/s^2 up to 3.27 deg/s, braking 0.565 deg/s^2, no status
  while a switch is On, :GU# never "slewing" in manual motion, 0.26 s / 1.5 s reading
  cadence, 0.1 s reading lag); tests check it reproduces two recorded passes.

## 0.7.16 - Unreleased

- `connect()` connects an OnStep INDI device that is not connected instead of
  refusing with "OnStep INDI device is not connected". Field diag 4f73ce56: after
  an indiserver restart the driver ran but was not connected (INDI drivers do not
  connect by themselves). It sends CONNECT, waits up to `DEVICE_CONNECT_TIMEOUT_S`
  (20 s) for CONNECT=On/Ok, logs a WARNING before and after, and raises a
  `ConnectionError` naming the reason on an Alert or timeout. Never moves the mount.
  The mock gains `SimConfig.start_device_connected`.

## 0.7.15 - Unreleased

- Coarse single-axis steps no longer overshoot on an accelerating mount. Field
  incident 56b54f01: a 0.5 s first pass at the fastest rate covered only 0.14 deg
  while the mount accelerated; the drive took that as the rate, planned an 8 s
  second pass and moved about 15 deg instead of 10. Now every pass stops early from
  the live position once the remaining angle is within `LIVE_STOP_LEAD_S` (0.6 s) of
  travel (but never before 3/4 of the pass);
  a pass may last at most `PASS_GROWTH_LIMIT` (3) times the previous one; a
  first pass at an unmeasured fast rate (8/9) lasts at least 1 s; `MAX_PASSES` is
  8. The mock gains `SimConfig.manual_accel_deg_per_s2` (acceleration and braking).

## 0.7.14 - Unreleased

- Finite single-axis moves may use `TELESCOPE_SLEW_RATE` 8 and 9 (OnStepX: half of
  and the full configured maximum slew) for coarse steps. Operator request: RA/Dec
  steps of 1, 5 and 20 degrees; at 48x a 20 degree step took about 100 s. These
  rates have no nominal value, so a pass at one that has not been measured yet
  plans with a deliberately high assumption (6 and 12 deg/s) and undershoots;
  later passes use the measured rate; on a controller whose measured rates run
  faster than nominal, the assumption scales up too. `MIN_PASS_S` drops from
  1.0 s to 0.75 s.

## 0.7.13 - Unreleased

- A short gap in `OnStep Status` right after motion commands is a wait, not a
  fault (`IndiAxisMover.check_allowing_status_gap`, up to `STATUS_GAP_GRACE_MS`
  = 8 s). Field incident e515713a: after a stop the real lx200_OnStep driver
  published no status for 2.6-3.1 s while it worked through the stop and
  TRACK_OFF; the 3 s freshness limit fired during the stop confirmation and an
  emergency ABORT was sent although nothing was wrong. Applies to the single-axis
  drive (in motion and while confirming the stop) and to the jog's stop
  confirmation, whose deadline grows from 5 s to 15 s. Every other blocker, and
  a longer gap, still stops at once.

## 0.7.12 - Unreleased

- `reconnect_device()` no longer reports a driver that reconnects by itself as a
  failure. Field incident 469ebcee: after the DISCONNECT the real driver went to
  DISCONNECT and back to CONNECT within the same second, fresh OnStep status
  returned, but the 0.25 s poll for the DISCONNECT state missed it and the call
  returned "the driver did not disconnect" after 20 s. Any newer `CONNECTION`
  vector now counts as the driver's reaction, CONNECT is sent only if the driver
  is not already connected, and fresh `OnStep Status` decides success. The mock's
  `lose_serial(driver_reconnects=True)` reproduces it.

## 0.7.11 - Unreleased

- Re-arm both INDI manual-motion vectors with an explicit all-Off update before
  every direction command. This fixes repeated same-direction RA or DEC jogs
  being accepted by INDI without starting a new movement.
- Log bounded RA start, cumulative progress, STOP request, completion and
  timeout position in the persistent monitor log.
- Decode the final OnStep `:GU#` general-error digit. Axis-1 motion now reports
  an OnStep under-pole stop (`6`) immediately instead of waiting for the
  overall JOG timeout.
- Wait for a published all-Off manual-motion vector before selecting the next
  direction, and wait for the selected direction to become On.
- Log every fresh compact-status sample during controller HOME and PARK so the
  requested two-stage route can be distinguished from motion performed inside
  OnStep's HOME sensor search.

## 0.7.10 - Unreleased

- Replace terrestrial RA sky-target goto with fixed-direction INDI Axis-1
  motion. `A` commands east and `D` commands west; short signed RA segments,
  DEC drift, altitude, and verified STOP bound the move.
- Refuse and stop immediately if Axis-1 feedback runs opposite to the requested
  direction instead of retrying or changing direction automatically.

## 0.7.9

- Report the current DEC, projected DEC target, and configured DEC bounds when
  refusing a finite terrestrial target jog.
- Restore bounded terrestrial Axis-2 paths toward or away from the northern
  horizon. Travel is accumulated from consecutive short sky arcs so the pole's
  12-hour RA transition and logical DEC reversal do not undercount motion.
- Apply `movement.min_alt_deg` to this Axis-2 path without treating logical
  DEC `+/-80` as a mechanical pole-crossing limit.

## 0.7.8

- Accept unchanged but present logical RA/DEC when raw and standard INDI PARK
  evidence is reconciled, stationary, fault-free, and explicitly confirmed.
  The LX200 driver need not republish coordinates merely because PARK completed.
- Continue to reject missing/non-finite coordinates, motion, tracking, limits,
  or conflicting PARK evidence before terrestrial UNPARK authorization.

## 0.7.7 - Unreleased

- Restore healthy terrestrial `1`, `5`, and `10` degree RA/DEC JOG through
  OnStep's finite coordinate-target operation after a retained PARK -> UNPARK
  session; verify same-pier arrival and tracking-off completion.
- Keep `0.5`, `1`, and `2` second manual-axis commands exclusive to explicit
  OnStep fault/limit recovery. A failed normal jog is never retried as recovery.
- Continue to reject the removed continuous RA/DEC-displacement estimator;
  logical coordinates are not independent mechanical-axis encoder feedback.

## 0.7.6 - Unreleased

- Add explicit time-bounded DEC recovery nudges with verified STOP before and
  after motion, including while OnStep reports a limit or fault.
- Disable normal terrestrial DEC-by-degree motion after physical testing showed
  celestial DEC could remain near 45 degrees across roughly 135 degrees of
  mechanical Axis-2 travel at a pole transition.
- Recovery remains an operator-observed directional action and makes no
  angular-distance claim.
- Keep healthy `UNPARKED` terrestrial sessions in normal timed-JOG mode.
  Recovery is selected only for a freshly reported OnStep fault or limit and
  is never entered merely because the mount is unparked.

## 0.7.4 - Unreleased

- Keep ordinary terrestrial RA-by-degree movement fail-closed because the
  tested INDI feedback cannot safely bound mechanical Axis-1 travel.
- Add explicit, operator-observed RA recovery nudges bounded to 0.1 through
  2 seconds by default, with verified STOP before and after every request.
- Permit only that recovery primitive while OnStep reports a limit or fault;
  PARK, HOME, tracking, guiding, slewing and conflicting status remain blocked.
- Do not require a PARK reference for recovery, so it remains available after
  an application restart while PARK itself is refused by the controller.

## 0.7.0 - Unreleased

- Added shared first-class astronomical/terrestrial mode arbitration.
- Added one-shot explicit-PARK authorization for every mode transition.
- Reconciled raw OnStep and standard INDI PARK completion with an idle hold.
- Added finite pole-aware northern-horizon terrestrial steps, horizon clipping,
  early STOP and stable-settle verification.
- Blocked terrestrial tracking/guiding and blocked PARK/HOME/mode changes while
  terrestrial motion is active or stopping.
## 0.6.14 and current-main integration

- 0.6.14: `mount.reconnect_device()` / `client.reconnect_device()` reopen the
  INDI driver's link to the controller -- `CONNECTION` DISCONNECT, then CONNECT,
  as Ekos' Disconnect/Connect does -- and wait for a fresh `OnStep Status`;
  returns `IndiReconnectResult(recovered, elapsed_s, error)`. For a driver that
  lost its serial port while indiserver kept running (field incident 908a26b9:
  a USB over-current reset re-enumerated the adapter; no OnStep Status, the
  position stuck in "Alert", unpark refused). Never moves the mount; refused
  while a move or jog is active. The mock gains `lose_serial()` for this case.

- 0.6.13: **finite axis moves no longer use a goto.** `move_dec_axis_deg()` /
  `move_ra_axis_deg()` drive only the requested axis with OnStep's single-axis
  motion (`TELESCOPE_MOTION_NS` / `TELESCOPE_MOTION_WE`) at a `TELESCOPE_SLEW_RATE`
  entry (index 3-7, 2x-48x sidereal), for the time the angle needs, then measure
  the result from fresh readings after a confirmed stop and close the gap in up
  to 4 passes (a pass that overshot is followed by one in the opposite direction
  of the same axis). The other axis gets no command at all: a goto to "current RA,
  new DEC" still turned the RA motor, because OnStep takes RA only to the whole
  second (real capture: 12-20 arcsec of HA per DEC move, 0.6.12 reduced but could
  not remove it). Rates are learned per axis and rate across moves; a first pass at
  an unmeasured rate aims for 60% of the angle. Landing tolerance: DEC
  max(10", 3%), RA max(20", 3%) (RA reads only to 1 s of time).
  `IndiAxisMoveResult.measured_deg` is now the measured angle. The positive
  directions are OnStep's MOTION_NORTH / MOTION_WEST jogs (as in 0.6.11); the sky
  sign of a pass is measured, not assumed. Moves are slower than a goto (48x =
  12 arcmin/s; ~5 s per degree plus about 4 s per pass to confirm the stop).
  `IndiAxisMover(single_axis=False)` keeps the former goto for comparison.
  The mock now models NORTH as a mechanical direction (it lowers DEC at pier side
  east and when leaving the pole).

- 0.6.12: a finite axis move (`move_dec_axis_deg` / `move_ra_axis_deg`) now
  holds the mechanical hour angle of the axis it does not move. With tracking
  off the published RA grows at the sidereal rate, and the goto target used
  the last RA reading unchanged, so every DEC move also drove the RA axis back
  by that reading's age (real capture: 0.4-1.5 s, 12-20 arcsec of HA per DEC
  move). The target RA is now advanced by the sidereal drift since the
  reading (its `coordinates_age_ms` plus the time to the send). What remains
  is OnStep's own 1 s RA resolution.
- 0.6.12: TRACK_OFF is sent as soon as the slew is seen, not only after
  arrival: OnStep can start sidereal tracking by itself at the end of a goto
  (seen on the first goto after unpark). The post-arrival TRACK_OFF and its
  two-sample confirmation are unchanged.

- Merge the 0.6.7 to 0.6.11 releases (tagged on a side branch) into `main`:
  finite axis moves may be up to 15 degrees; HA missing from INDI is derived
  from the UTC clock and the configured longitude; finite-move targets and
  arrival use the sky separation from the target RA/DEC; continuous Axis 2
  through the north pole and transient INDI pier display conflicts are
  handled.
- **Breaking:** a positive DEC-axis angle now follows OnStep's mechanical
  jog direction, which lowers the published sky DEC (`move_dec_axis_deg(+x)`
  targets DEC - x). The DEC-first move from HOME (DEC +90) toward the bound
  is therefore a positive DEC move.
- Add hold-to-move jogs: `mount.jog_ra(direction)` / `mount.jog_dec(direction)`
  (and `client.jog()`) start OnStep manual motion and return a `JogHandle`
  at once. The axis moves while the caller calls `renew()` at least every
  `lease_s` (0.2-2 s, default 0.5 s) and stops on `release()`, a missed
  renewal, `max_duration_s` (default 60 s, at most 120 s) or the
  `[movement]` DEC/altitude bound. Every ending switches the motion off,
  turns tracking off and confirms two fresh stationary readings; a failure
  after motion started sends the emergency stop. `rate_index` picks an entry
  of the controller's `TELESCOPE_SLEW_RATE` list. RA jogs follow the
  DEC-first rule, and a DEC jog that moved DEC defines RA. The lease is
  watched in this process: if the whole process dies mid-jog, OnStep keeps
  moving until something else stops it.
- **Breaking:** the first axis move after connecting, after `go_home()` or
  `route_park_to_home()`, and whenever a fresh reading reports HOME must be
  DEC. At HOME the mount points at the pole, where RA has a value but no
  meaning, so `move_ra_axis_deg()` and `move_ra()` raise
  `RaAxisUndefinedError` (a `RuntimeError`) before anything is sent until a
  DEC move has completed and been verified. `mount.ra_axis_defined` reports
  the state.
- A DEC move that starts outside the `[movement]` DEC bound (HOME is
  DEC +90) may now run when it only brings DEC closer to the bound, so the
  DEC-first move from HOME no longer needs a full 10 degree step.
- Ship `onstep_mock`, `onstep_adapter.capture` and the `indi_capture`,
  `trace_compare` and `trace_consolidate` tools in the wheel, so applications
  can run against the mock or record traffic from an installed release. The
  wheel built from `main` also lacked `indi_motion` and `indi_trace` and
  failed on `import onstep_adapter`; a release contract test now checks that
  every module a shipped module imports is shipped too.
- Add `onstep_adapter.capture` and `python3 -m onstep_adapter.tools.indi_capture`
  to record the traffic between an INDI client, indiserver and OnStepX
  passively: from a `tcpdump` recording, or through a transparent TCP
  pass-through that forwards bytes unchanged and never sends anything itself.
  Captures use the trace format and are checked against sixteen documented
  assumptions (A01-A16). SET HOME, SET PARK and SYNC writes and the matching
  LX200 commands are flagged.
- Add `python3 -m onstep_mock.validate` to replay a capture against the mock
  and report where the mock differs from the recorded mount, with measured
  `SimConfig` values. Recordings in `tests/acceptance/recordings/` are
  acceptance tests. See `docs/CAPTURE.md`.
- Store logs by day: a capture or trace path containing `{date}` writes one
  file per UTC day that each session appends to; a session crossing midnight
  continues in the next file under the same `session_id`. Every session
  record now carries a `session_id`. `trace_compare`, `indi_capture` and
  `onstep_mock.validate` accept several files and handle each session.
- Add `python3 -m onstep_adapter.tools.trace_consolidate`, which writes a
  condensed copy of a log where each run of repeated entries is one entry
  with `repeat`, `t_end` and `duration_s` (`--text` for a readable listing).

- **Breaking:** `park()`, `go_home()`, `move_ra_axis_deg()`,
  `move_dec_axis_deg()`, `move_ra()` and `move_dec()` no longer block. They
  return a `MotionHandle` at once with `progress()`, `wait()` and `stop()`;
  `wait()` returns the result the call used to return. `mount.stop()` and the
  meridian supervisor end a running handle's move, only one move runs at a
  time, and `close()` stops a move still running. Callers that want the old
  behavior use `mount.go_home().wait()`.

- Add an optional transaction trace (JSON Lines) of every INDI write, every
  received property update with decoded `:GU#` status, and every public mount
  call with its result or error. Enable it with `[trace] path` in the runtime
  configuration or `OnStepClient(trace=...)`. Tracing is off by default and
  can never interrupt a command.
- Add `python3 -m onstep_adapter.tools.trace_compare` to summarize a trace or
  compare an observed run with an expected one, operation by operation.
- Add `onstep_mock`, a virtual OnStepX mount behind a local INDI server, for
  testing applications against the unchanged adapter. Slews take real time
  (with a speed factor), STOP works at any moment, UNPARK auto-tracks like
  OnStepX, HOME and PARK are fixed, and SET HOME, SET PARK and SYNC are
  rejected and logged. Tests inspect `server.mount` and `server.writes`.

## 0.6.6 - 2026-10-20

- Track terrestrial DEC jogs through the north celestial pole with a continuous
  Axis-2 coordinate derived from DEC and OnStep's authoritative pier side.
- Shift RA by 12 hours exactly once when the coordinate representation changes.
- Continue subsequent west-side Axis-2 jogs without repeating that RA shift.

## 0.6.5 - 2026-10-20

- Tolerate only the transient secondary INDI pier-side conflict produced by a
  terrestrial DEC coordinate fold.
- Continue to require the authoritative OnStep `:GU#` pier side to remain
  unchanged throughout the move and require both sources to agree at rest.

## 0.6.4 - 2026-10-20

- Treat terrestrial DEC jogs as continuous mechanical Axis-2 motion.
- Fold targets through the celestial poles into legal RA/DEC coordinates.
- Apply the altitude floor, rather than astronomical DEC bounds, to terrestrial jogs.

## 0.6.3 - 2026-10-20

- Raised the maximum finite RA/DEC axis jog from 10 to 15 degrees.
- Kept projected altitude and configured DEC target bounds mandatory for the
  larger request.
- Added regression coverage for the inclusive 15-degree boundary and rejection
  above it.
- Preserve a freshly received property acknowledgement when the INDI peer closes
  immediately after sending it, while still failing promptly if no newer
  property was received.

## 0.6.2 - 2026-10-20

- Move finite-axis target altitude and declination bounds into the runtime
  `[movement]` configuration.
- Permit endpoints down to an inclusive altitude of -2 degrees while retaining
  inclusive DEC bounds of -80 through +80 degrees by default.

## 0.6.1 - 2026-10-20

- Detect every unexpected INDI reader-thread exception or return and report the
  transport closed instead of leaving applications on a stale socket.
- Wake property waiters immediately, preserve the original reader failure as
  the `ConnectionError` cause, and reject writes on the failed session.
- Support explicit `close()` plus `connect()` recovery without sending device
  `CONNECTION.DISCONNECT` or disturbing other INDI clients. Resolves #23.

## 0.6.0 - 2026-10-14

- Add caller-calibrated RA/DEC corrections in arcseconds while sidereal
  tracking remains active. Positive RA guides east, negative RA west,
  positive DEC north and negative DEC south.
- Route every angular correction through the physically validated 0.5.0 timed
  guide-pulse path, retaining chunking, fresh safety checks, `G`-flag hardware
  completion and tracking preservation. Resolves #21.
- Reject missing/invalid calibration and pulse durations outside 20-5000 ms
  before any INDI command is sent.
- Make persistent INDI switch writes idempotent when a current `Ok` vector
  already reports the requested state.
- Let `unpark()` complete without an unnecessary `TRACK_OFF` write/status wait
  when its fresh post-UNPARK OnStep status is already stationary and
  non-tracking. Resolves #17.

## 0.5.0 - 2026-10-06

- Add bounded tracking-preserving north, south, east and west guide pulses
  through the standard INDI timed-guide properties.
- Split longer requests into 500 ms chunks with fresh safety checks between
  chunks.
- Treat INDI `Ok` as command acceptance only; pace every chunk for its nominal
  duration and require a newer OnStep status with the guide-active `G` flag
  cleared before continuing or returning success.
- Add strict and controller-managed guide authority behavior, structured pulse
  results, emergency-stop handling and compatibility `guide()` support.
- Physically validate reciprocal 3000 ms east/west pulses on the Terrans
  OnStep V4 while tracking. FITS measurements agreed with the expected
  high-declination displacement and the final RA returned to its start value.

## 0.4.1 - 2026-09-29

- Allow direct PARK from any fresh, stationary, non-tracking, fault-free
  unparked state; HOME is no longer an artificial prerequisite.
- Make repeated PARK idempotent when recent raw OnStep status already proves
  the stored PARK position is active.
- Add explicit `strict` and `controller_managed` tracking authority policies.
  Strict remains the default; delegated authority is reported as warnings.
- Preserve fresh transition confirmation and all hard fault, PARK, slew,
  firmware-limit, and inclusive operational hard-stop gates. Resolves #16.

## 0.4.0 - 2026-09-22

- Replaced exclusive serial ownership with an in-process client of the local
  INDI `LX200 OnStep` driver.
- Added INDI status, time/site synchronization, meridian supervision,
  emergency stop, focuser movement, and bounded RA/DEC axis movement.
- Kept the unchanged INDI driver and other clients connected.

## 0.3.5 - 2026-09-21

- Document exclusive OnStepAdapter serial ownership as the supported transport
  model for all OnStep mount, tracking, PARK/unpark, status, stop, and focuser
  access.
- Add thread-safe runtime `set_motion_calibration()` /
  `get_motion_calibration()` on `OnStepMount`.
- Allow partial `OnStepMotionCalibration` records so applications can bootstrap
  with timed moves, measure actual displacement, and then install calibrated
  angular rates.
- Add tests proving angular moves are rejected before calibration, accepted
  after runtime calibration, updated rates affect duration, and invalid rates
  are refused.

## 0.3.4 - 2026-07-21

- Add per-call `rate_preset=0..9` for timed RA/DEC moves.
- Send `:R0#` through `:R9#` when a preset is supplied, while retaining
  bounded directional stops and guide-rate restoration.
- Record the selected rate preset in `AxisMotionResult`.
- Document rate-selectable manual jog usage.

## 0.3.3 - 2026-07-19

- Add `mode="manual"` for timed RA/DEC jogs used by deliberate
  non-astronomical terrestrial controls.
- Manual timed jogs may run at confirmed mechanical HOME with tracking off and
  skip RA/DEC endpoint validation while retaining mechanical safety blockers.
- Reject `mode="manual"` on angular sky-offset APIs.
- Document manual jog semantics for consuming applications.

## 0.3.2 - 2026-07-11

- Actively disable unrequested OnStep tracking observed after unpark or status
  polling when the adapter has no active caller tracking request.
- Make `unpark()` guarantee an unparked, non-tracking postcondition.
- Add public location helpers for great-circle distance and LX200 arcminute
  site-coordinate rounding.
- Document tracking authority and realistic OnStep site readback comparison.

## 0.3.1 - 2026-07-10

- Fixed standalone wheel packaging so it ships only the `onstep_adapter`
  namespace.
- Removed the top-level `smart_telescope` package from the distribution to
  avoid collisions with SmartTScope installations.
- Updated tests and documentation to import SDK internals from
  `onstep_adapter.*`.

## 0.3.0 - 2026-06-14

- Added provenance-bearing local PARK records and safe `:hQ#` transactions.
- Removed undocumented `:GpA#` and `:GpD#` PARK readback probes.
- Added bounded independent RA and DEC guide/centering corrections.
- Added direction-specific application-supplied motion calibration.
- Added a supervised PARK-to-target axis-motion hardware smoke command.
- Added a supervised coordinate path proving independent RA `+1 h/-1 h` and
  DEC `+10/-10 degrees` GoTo movement with final HOME-to-PARK recovery.
- Physically validated both correction modes on the Terrans OnStep V4.

## 0.2.0 - 2026-06-13

- Added P0 mechanical-safety authority and fresh motion preflight.
- Added application-controlled meridian notification, flip, and inclusive hard stop.
- Added physically proven stock OnStep Axis-1 firmware fallback support.
- Added one shared serial client exposing mount and focuser interfaces.
- Added structured safety, status, connection, and focuser results.
- Added focused mock, protocol, safety, firmware-proof, and shared-bus tests.
- Validated with a Terrans OnStep V4 running OnStep 10.19d without changing
  `Config.h`.
