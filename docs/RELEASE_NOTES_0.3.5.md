# OnStepAdapter 0.3.5

Version `0.3.5` closes GitHub issues #9 and #10.

## Ownership Model

OnStepAdapter now documents one supported transport/ownership model:

- `OnStepClient` owns the physical OnStep serial port exclusively.
- Applications must route OnStep mount movement, stop/abort, PARK/unpark,
  tracking, state/position, and OnStep focuser operations through
  `OnStepClient.mount` and `OnStepClient.focuser`.
- Applications must not open raw serial, raw LX200 socket/TCP, INDI
  `LX200 OnStep`, or any other direct OnStep controller path in parallel.
- INDI remains valid for unrelated devices such as cameras, external filter
  wheels, or non-OnStep accessories.

This is a documented ownership contract, not a new INDI-backed transport.
Existing direct serial behavior remains the supported adapter transport.

## Runtime Motion Calibration

`OnStepMount` now exposes:

```python
mount.get_motion_calibration()
mount.set_motion_calibration(calibration_or_none)
```

`OnStepMotionCalibration` accepts partial records. This lets a consuming
application bootstrap from timed moves, measure actual image displacement, and
install only the measured direction/mode rates:

```python
mount.move_ra_timed("east", 500, mode="center", rate_preset=4)
mount.set_motion_calibration(
    OnStepMotionCalibration(center_ra_east_arcsec_per_s=measured_rate)
)
mount.move_ra(+3.0, mode="center")
```

Angular `move_ra()` and `move_dec()` still reject a request until the exact
mode, axis, and direction rate they need is present, finite, and positive.

## Validation

- Runtime calibration unit tests cover rejection before calibration, success
  after `set_motion_calibration()`, duration changes after rate updates,
  invalid-rate rejection, and partial-calibration direction refusal.
- Existing serial client, axis-motion, release-contract, and packaging tests
  remain in force.
