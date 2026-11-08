# Changelog

## Unreleased

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
