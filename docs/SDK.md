# OnStep Adapter SDK

`onstep-adapter` provides one serial owner with separate mount and focuser
interfaces. Do not open the same OnStep serial port from a second process or
adapter instance.

## Install

```bash
python -m pip install onstep_adapter-0.3.0-py3-none-any.whl
```

Python 3.13 or newer and `pyserial>=3.5` are required.

## Shared Client

```python
from onstep_adapter import OnStepClient, OnStepSafetyConfig

safety = OnStepSafetyConfig(
    observer_lat=50.336,
    observer_lon=8.533,
    min_alt_deg=-5,
    max_alt_deg=88,
    ha_east_limit_h=-5.5,
    ha_west_limit_h=5 / 15,
    require_home_confirmation=True,
)

with OnStepClient("/dev/ttyUSB_ONSTEP0", safety_config=safety) as client:
    print(client.mount.get_state())
    print(client.focuser.status())
```

`client.mount` and `client.focuser` share one locked serial bus. `close()` is
idempotent. Mount `:Q#` and focuser `:FQ#` emergency stops bypass the normal
command lock.

## Focuser

```python
status = client.focuser.status()
if status.available:
    result = client.focuser.move_absolute(status.position + 100)
    print(result.accepted, result.onstep_reply)

client.focuser.stop()
```

The adapter enforces configured position limits before transmission.
OnStep rejection raises `OnStepSafetyError` with a structured
`SafetyViolation`. On the validated OnStepX 10.19d rig, `:FT#` can report
stopped while an absolute move is visibly progressing; callers that need
completion proof should also poll `:FG#` until the position is stable.

## Meridian Operation

HOME and PARK are mechanical operations. Astronomical target and tracking
safety use time, location, RA/DEC, HA, ALT/AZ, and pier side.

The application workflow:

- before `0 degrees` HA: tracking allowed;
- `0` to below `+2 degrees`: post-meridian tracking allowed;
- at `+2 degrees`: notify the application and stop starting exposures;
- finish the current exposure only when it fits the reported safety budget;
- the application explicitly requests the controlled opposite-pier flip;
- at `+5 degrees` inclusive: tracking must stop if the handoff was missed.

OnStep firmware remains the final protection layer if the host application
fails.

## PARK Records

`set_park_position_from_current(confirmed_safe=True)` sends documented OnStep
Set-Park `:hQ#` only after verifying trusted HOME authority, stationary and
non-tracking state, no limit/fault, and a writable persistent calibration
destination. `allow_at_home=False` prevents accidentally replacing PARK with
HOME.

OnStep has no documented stored-PARK readback command.
`get_stored_park_position()` therefore returns the SDK capture made when it
last successfully sent `:hQ#`, including source, trust, and invalidation
fields. It does not claim independent controller verification.

## Bounded Axis Corrections

- `move_ra_timed()` and `move_dec_timed()` support safe manual nudges.
- `move_ra()` and `move_dec()` accept signed on-image arcseconds and require
  `OnStepMotionCalibration`.
- Guide mode uses `:RG#` and native `:Mg...#` pulse guiding.
- Center mode uses `:RC#`, directional movement, and a guaranteed matching
  direction stop.
- Tracking remains active and guide rate is restored after centering.
- Calls are serialized and bounded; no indefinite public start/stop API exists.
- Angular movement is estimated and must be verified from a new camera frame.

## Supervised Motion Validation

Small guide and center corrections:

```bash
python -m onstep_adapter.tools.axis_motion_smoke \
  --port /dev/ttyUSB_ONSTEP0 \
  --observer-lat 50.336 \
  --observer-lon 8.533 \
  --observer-alt-m 304 \
  --confirm-time-location-sync
```

Visually observable independent coordinate movement:

```bash
python -m onstep_adapter.tools.coordinate_axis_smoke \
  --port /dev/ttyUSB_ONSTEP0 \
  --observer-lat 50.336 \
  --observer-lon 8.533 \
  --observer-alt-m 304 \
  --confirm-time-location-sync
```

The second command moves RA `+1 h`, DEC `+10 degrees`, RA `-1 h`, and DEC
`-10 degrees`, then requires final confirmation before HOME-to-PARK. Both
commands passed on the physically tested Terrans OnStep V4.

## Cleanup

Always use the context manager or call `client.close()`. Applications that
also own a GPS daemon connection should close that service before closing the
shared OnStep client during shutdown and explicit disconnect.
