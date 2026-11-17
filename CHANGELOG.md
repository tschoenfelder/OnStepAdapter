# Changelog

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
