# OnStepAdapter 0.3.0

## New APIs

- Provenance-bearing local PARK records:
  - `get_stored_park_position()`
  - `set_park_position_from_current()`
- Bounded independent axis corrections:
  - `move_ra_timed()` / `move_dec_timed()`
  - `move_ra()` / `move_dec()`
- Public records:
  - `StoredParkPosition`
  - `SetParkPositionResult`
  - `OnStepMotionCalibration`
  - `AxisMotionResult`

## PARK Limitation

OnStep documents `:hQ#`, `:hP#`, and `:hR#`, but no command that reads the
stored PARK pose. Version 0.3.0 therefore removes the undocumented
`:GpA#`/`:GpD#` probe and returns the SDK record captured when `:hQ#` was sent.
The result explicitly says that controller matching is unverifiable.

The SDK never presents a local persistence failure as an ordinary rejected
command after OnStep has accepted `:hQ#`.

## Axis-Correction Validation

Mock validation covers command direction, duration, guide/center rate
selection, tracking overlay, guaranteed stop, cancellation, concurrency,
calibration conversion, projected endpoint safety, and inclusive hard-limit
interruption.

The supervised bounded-correction hardware gate passed on June 14, 2026:

```bash
python -m onstep_adapter.tools.axis_motion_smoke \
  --port /dev/ttyUSB_ONSTEP0 \
  --observer-lat 50.336 \
  --observer-lon 8.533 \
  --observer-alt-m 304
```

The command starts PARKED, routes through HOME, acquires a safe target, runs
short RA/DEC corrections in both directions, and returns through HOME to PARK.
Before any movement, it checks Raspberry/OnStep civil time, observer location,
and sidereal time while the mount remains PARKED. A mismatch blocks motion.
After verifying the Raspberry clock and supplied coordinates, add
`--confirm-time-location-sync` to explicitly synchronize them to OnStep before
the test proceeds.

An additional supervised coordinate-path test makes larger, visually
observable independent axis changes and returns to its initial target:

```bash
python -m onstep_adapter.tools.coordinate_axis_smoke \
  --port /dev/ttyUSB_ONSTEP0 \
  --observer-lat 50.336 \
  --observer-lon 8.533 \
  --observer-alt-m 304 \
  --confirm-time-location-sync
```

Its default path is RA `+1 h`, DEC `+10 deg`, RA `-1 h`, DEC `-10 deg`.
Each leg requires confirmation, and a final confirmation authorizes the
HOME-to-PARK route.
It never writes the PARK position. ESC sends an immediate stop and suppresses
all later automatic movement.

This coordinate-path gate also passed on June 14, 2026. Every leg reached the
requested logical RA/DEC within the test tolerance, the final leg recovered
the original target, and the mount returned through HOME to a live PARKED
state.

The final local release suite contains 84 passing tests.

## Hardware Baseline

The adapter remains targeted at the physically validated Terrans OnStep V4
running OnStep `10.19d`. No `Config.h` modification is required.
