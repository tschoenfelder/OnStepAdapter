# OnStepAdapter

`onstep-adapter` is a Python 3.13+ SDK for controlling an OnStep telescope
mount and OnStep focuser through one shared serial connection.

The distribution is named `onstep-adapter`; applications import it as
`onstep_adapter`.

## Install

Download `onstep_adapter-0.3.0-py3-none-any.whl` from the
[v0.3.0 GitHub release](https://github.com/tschoenfelder/OnStepAdapter/releases/tag/v0.3.0),
then install it:

```bash
python -m pip install ./onstep_adapter-0.3.0-py3-none-any.whl
```

Or install directly from the release URL:

```bash
python -m pip install \
  https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.3.0/onstep_adapter-0.3.0-py3-none-any.whl
```

Verify the import:

```bash
python -c "import onstep_adapter; print(onstep_adapter.__version__)"
```

Runtime requirement: `pyserial>=3.5`.

## Shared Mount And Focuser Connection

```python
from onstep_adapter import OnStepClient, OnStepMotionCalibration, OnStepSafetyConfig

safety = OnStepSafetyConfig(
    observer_lat=50.336,
    observer_lon=8.533,
    min_alt_deg=-5,
    max_alt_deg=90,
    ha_east_limit_h=-5.5,
    ha_west_limit_h=5 / 15,
    require_home_confirmation=True,
)

motion = OnStepMotionCalibration(
    guide_ra_east_arcsec_per_s=7.5,
    guide_ra_west_arcsec_per_s=7.5,
    guide_dec_north_arcsec_per_s=7.5,
    guide_dec_south_arcsec_per_s=7.5,
    center_ra_east_arcsec_per_s=60.0,
    center_ra_west_arcsec_per_s=60.0,
    center_dec_north_arcsec_per_s=60.0,
    center_dec_south_arcsec_per_s=60.0,
)

with OnStepClient(
    "/dev/ttyUSB_ONSTEP0",
    safety_config=safety,
    motion_calibration=motion,
) as client:
    print(client.mount.get_state())
    print(client.mount.safety_snapshot())

    focuser = client.focuser.status()
    if focuser.available:
        client.focuser.move_absolute(focuser.position + 100)
```

`client.mount` and `client.focuser` serialize access through one locked serial
bus. Do not open the same OnStep serial port from another process or adapter
instance.

## PARK Position Record

OnStep documents commands to set, move to, and restore PARK, but no command to
read the stored PARK pose. The SDK records the current RA/DEC, logical axes,
pier side, firmware identity, and HOME authority when it successfully sends
`:hQ#`:

```python
result = client.mount.set_park_position_from_current(
    confirmed_safe=True,
    allow_at_home=False,
)
record = client.mount.get_stored_park_position()
```

The calling application owns the user-confirmation UI. The record explicitly
states `controller_readback_supported=False` and
`controller_match="unverifiable"`. A configured writable
`mechanical_calibration_file` is required before changing PARK.

## RA And DEC Corrections

Manual applications can issue bounded timed nudges:

```python
client.mount.move_ra_timed("east", 250, mode="center")
client.mount.move_dec_timed("north", 100, mode="guide")
```

Plate-solving applications can request estimated on-image corrections:

```python
client.mount.move_ra(+8.2, mode="center")
client.mount.move_dec(-3.5, mode="center")
```

Angular corrections require direction-specific calibration and always return
`verification_required=True`. A new guide frame or plate solve must measure
the result and close the loop.

## Safety Model

- PARK and HOME are mechanical operations and are not rejected from RA/DEC
  target validation.
- Normal goto, guide, slew, and tracking require fresh OnStep status, pier
  side, time/location, hour angle, motion state, limit state, and established
  HOME authority.
- The application is notified at the configured meridian warning boundary.
- Tracking is stopped and further unsafe motion is refused at the inclusive
  hard boundary.
- Emergency stop and explicitly classified recovery motion remain available.
- OnStep firmware is the final safeguard if the host computer fails.

Read [Requirements](docs/REQUIREMENTS.md) and
[Controller and protocol guide](docs/onstep-controller.md) before commanding
real hardware.

## Tested Hardware

OnStepAdapter `0.3.0` was physically tested with a
**Terrans OnStep V4 device**
running OnStep `10.19d` dated February 29, 2024. The device was used with its
existing firmware configuration; **no change to `Config.h` was required**.

The validation covered HOME/PARK routing, application-controlled meridian
handoff, the stock Axis-1 firmware stop, shared-bus focuser movement and stop,
small guide/center corrections, independent RA/DEC coordinate movement, and
final parking. See [Hardware compatibility](docs/HARDWARE_COMPATIBILITY.md),
[validation evidence](docs/VALIDATION_EVIDENCE.md), and
[the 0.3.0 release notes](docs/RELEASE_NOTES_0.3.0.md).
It validates civil time, observer location, and sidereal time while the mount
is still PARKED. A mismatch commands no movement. After verifying the
Raspberry clock and observer coordinates, the explicit
`--confirm-time-location-sync` option authorizes synchronization to OnStep.

## Development

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m build
```

The wheel is pure Python and preserves the historical
`smart_telescope.adapters.onstep` imports for SmartTScope compatibility.

## License

MIT. See [LICENSE](LICENSE).
