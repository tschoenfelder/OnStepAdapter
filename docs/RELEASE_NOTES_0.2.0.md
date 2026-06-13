# OnStepAdapter 0.2.0

OnStepAdapter 0.2.0 is the first public standalone SDK release of the tested
SmartTScope OnStep mount and focuser adapter.

## Highlights

- One locked serial connection shared by mount and focuser.
- Importable public API through `onstep_adapter`.
- Mechanical HOME/PARK authority separated from astronomical target safety.
- Application-controlled meridian workflow:
  - notification at the configured warning boundary;
  - explicit flip request by the calling application;
  - inclusive tracking stop and unsafe-motion refusal at the hard boundary.
- OnStep status, pier side, hour angle, tracking, slew, HOME/PARK, and firmware
  limit state included in motion decisions.
- Stock Axis-1 firmware fallback proof persisted against the tested rig.

## Tested Hardware

The release was physically tested with a **Terrans OnStep V4 device** running
OnStep firmware `10.19d` dated February 29, 2024.

The device retained its existing firmware configuration. **No change to
`Config.h` was required.**

Physical validation included:

- PARK to HOME and HOME to PARK routing;
- application-controlled meridian handoff;
- inclusive operational hard-stop behavior;
- stock Axis-1 firmware stopping at approximately `180.00013 deg`;
- shared-bus focuser move, exact return, immediate stop, and mount coexistence;
- final live parked-state confirmation.

The firmware fallback proof is rig-specific. Revalidate it after firmware,
mount geometry, observer location, Axis-1 limit, clutch reference, or relevant
configuration changes.

## Installation

```bash
python -m pip install \
  https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.2.0/onstep_adapter-0.2.0-py3-none-any.whl
```

```python
import onstep_adapter

print(onstep_adapter.__version__)
```

Requires Python 3.13 or newer and `pyserial>=3.5`.

