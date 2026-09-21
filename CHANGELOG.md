# Changelog

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
