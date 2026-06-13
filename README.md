# OnStepAdapter

`onstep-adapter` is a Python 3.13+ SDK for controlling an OnStep telescope
mount and OnStep focuser through one shared serial connection.

The distribution is named `onstep-adapter`; applications import it as
`onstep_adapter`.

## Install

Download `onstep_adapter-0.2.0-py3-none-any.whl` from the
[v0.2.0 GitHub release](https://github.com/tschoenfelder/OnStepAdapter/releases/tag/v0.2.0),
then install it:

```bash
python -m pip install ./onstep_adapter-0.2.0-py3-none-any.whl
```

Or install directly from the release URL:

```bash
python -m pip install \
  https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.2.0/onstep_adapter-0.2.0-py3-none-any.whl
```

Verify the import:

```bash
python -c "import onstep_adapter; print(onstep_adapter.__version__)"
```

Runtime requirement: `pyserial>=3.5`.

## Shared Mount And Focuser Connection

```python
from onstep_adapter import OnStepClient, OnStepSafetyConfig

safety = OnStepSafetyConfig(
    observer_lat=50.336,
    observer_lon=8.533,
    min_alt_deg=-5,
    max_alt_deg=90,
    ha_east_limit_h=-5.5,
    ha_west_limit_h=5 / 15,
    require_home_confirmation=True,
)

with OnStepClient("/dev/ttyUSB_ONSTEP0", safety_config=safety) as client:
    print(client.mount.get_state())
    print(client.mount.safety_snapshot())

    focuser = client.focuser.status()
    if focuser.available:
        client.focuser.move_absolute(focuser.position + 100)
```

`client.mount` and `client.focuser` serialize access through one locked serial
bus. Do not open the same OnStep serial port from another process or adapter
instance.

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

Version `0.2.0` was physically tested with a **Terrans OnStep V4 device**
running OnStep `10.19d` dated February 29, 2024. The device was used with its
existing firmware configuration; **no change to `Config.h` was required**.

The validation covered HOME/PARK routing, application-controlled meridian
handoff, the stock Axis-1 firmware stop, shared-bus focuser movement and stop,
and final parking. See [Hardware compatibility](docs/HARDWARE_COMPATIBILITY.md)
and [validation evidence](docs/VALIDATION_EVIDENCE.md). Release details are in
[the 0.2.0 release notes](docs/RELEASE_NOTES_0.2.0.md).

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
