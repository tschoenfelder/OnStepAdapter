# OnStep Adapter SDK

`onstep-adapter` provides one serial owner with separate mount and focuser
interfaces.

The supported OnStep ownership model is **exclusive serial ownership by
OnStepAdapter**. A consuming application must not open the OnStep controller
through raw serial, raw LX200 TCP/socket, INDI `LX200 OnStep`, or another
adapter path. All OnStep-owned capabilities used by the application must route
through `OnStepClient.mount` and `OnStepClient.focuser`: RA/DEC correction,
stop/abort, PARK/unpark, tracking, status, and OnStep focuser control.

INDI can continue to serve unrelated hardware such as cameras, filter wheels,
or non-OnStep accessories. If `indiserver` previously owned the OnStep serial
port through `indi_lx200_OnStep`, remove that OnStep device from the INDI
server before starting an application that uses OnStepAdapter.

## Install

```bash
python -m pip install onstep_adapter-0.3.5-py3-none-any.whl
```

The wheel owns only the `onstep_adapter` namespace. It intentionally does not
ship `smart_telescope/*` modules, which avoids import collisions with a real
SmartTScope installation.

Python 3.13 or newer and `pyserial>=3.5` are required.

## Unrequested Tracking

OnStep may resume sidereal tracking after unpark even when the host application
did not request tracking. The SDK treats that as motion without caller
authority. `OnStepMount` records successful explicit `enable_tracking()` calls;
`stop()`, `park()`, `unpark()`, and verified disable clear that authority. If a
status poll sees tracking while no authority is active, the adapter calls the
verified tracking-disable path and reports the resulting non-tracking state.

`unpark()` also guarantees this postcondition directly: a successful unpark
must leave the mount unparked and not tracking.

## Location Readback Helpers

OnStep/LX200 site readback uses arcminute precision. Compare configured and
controller sites using:

```python
from onstep_adapter import haversine_distance_m, round_lx200_site_degrees

configured_lat = 50.336
configured_lon = 8.533
readback_lat = round_lx200_site_degrees(configured_lat)
readback_lon = round_lx200_site_degrees(configured_lon)
distance_m = haversine_distance_m(configured_lat, configured_lon, readback_lat, readback_lon)
```

This prevents false mismatches immediately after a successful site sync.

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
- `mode="manual"` on timed RA/DEC motion is for deliberate non-astronomical
  terrestrial jogs with tracking off. It may run at confirmed mechanical HOME,
  skips RA/DEC target projection, and still honors mechanical safety blockers,
  OnStep fault/limit status, duration bounds, and the motion lock.
- Timed RA/DEC motion accepts `rate_preset=0..9` to send `:R0#` through
  `:R9#` instead of the mode default rate. The selected preset is scoped to
  that one bounded move, and guide rate is restored afterward.
- `move_ra()` and `move_dec()` accept signed on-image arcseconds and require a
  direction-specific `OnStepMotionCalibration` rate for the requested
  mode/axis/direction.
- `set_motion_calibration()` and `get_motion_calibration()` allow applications
  to install measured rates after the client has already been constructed.
- Partial calibration records are allowed, so bootstrap workflows can install
  only the rates they have measured.
- `mode="manual"` is rejected by angular `move_ra()` and `move_dec()` because
  those APIs describe sky offsets that require astronomical context.
- Guide mode uses `:RG#` and native `:Mg...#` pulse guiding.
- Center mode uses `:RC#`, directional movement, and a guaranteed matching
  direction stop.
- Tracking remains active and guide rate is restored after centering.
- Calls are serialized and bounded; no indefinite public start/stop API exists.
- Angular movement is estimated and must be verified from a new camera frame.

Bootstrap flow for a camera-measured mount:

```python
# 1. Use timed motion while uncalibrated.
client.mount.move_ra_timed("east", 500, mode="center", rate_preset=4)

# 2. Measure the achieved sky/image displacement in the consuming application.
measured_rate = measured_arcsec / 0.5

# 3. Install just the measured direction, or a full calibration if available.
client.mount.set_motion_calibration(
    OnStepMotionCalibration(center_ra_east_arcsec_per_s=measured_rate)
)

# 4. Arcsecond moves are now available for that calibrated direction.
client.mount.move_ra(+3.0, mode="center")
```

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
