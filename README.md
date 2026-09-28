# OnStepAdapter 0.4.1

Python 3.13+ access to an OnStep mount and focuser through an existing local
INDI server and its unchanged `LX200 OnStep` driver. The package is
`onstep-adapter`; the import is `onstep_adapter`. Version 0.4.1 does not open
the controller's serial port, run a daemon, or disconnect other INDI clients.

This is a deliberately restricted INDI release. It provides shared status,
explicit time/site synchronization, meridian supervision during the client's
lifetime, an emergency mount stop, and bounded absolute focuser movement.
General astronomical goto/tracking and HOME/PARK motion remain disabled by
default. HOME-neutral local axis movements are separately available when
unparked, stationary, tracking off and fault-free; this gate has not yet
passed physically.
Do not install 0.4.0 expecting the direct-serial 0.3.5 API.

## Install

Install the local wheel on the Raspberry (Python 3.13 or newer):

```bash
python3 -m pip install ./onstep_adapter-0.4.1-py3-none-any.whl
python3 -c 'import onstep_adapter; print(onstep_adapter.__version__)'
```

The wheel has no runtime Python dependencies. `indiserver` must already run
the OnStep LX200 driver and own `/dev/ttyUSB_ONSTEP0`; applications must not
open that serial device or a raw LX200 socket in parallel. The package does
not modify or replace the INDI driver. It was checked against the Terrans
OnStep V4 with the installed `indi_lx200_OnStep` driver, without changing
OnStep `Config.h`.

Copy [config.indi.example.toml](config.indi.example.toml) to an application-
owned location and review the observer, meridian, and focuser limits. The
example deliberately keeps `home_motion_enabled=false` and a conservative
focuser software ceiling of 50000, even though INDI reports `FOCUS_MAX=100000`.

## Use

```python
from onstep_adapter import OnStepClient, load_indi_config

config = load_indi_config("/home/astro/.config/onstep_adapter/config.toml")
with OnStepClient(config=config) as client:
    print(client.mount.get_status())
    print(client.mount.meridian_status())
    print(client.focuser.get_status())
```

`OnStepClient.connect()` enables and verifies the driver's
`SAFE_MERIDIAN_FLIP` HOME-route switch by default. Closing the client stops
only its own supervisor and INDI socket; it leaves the shared server, driver,
switch, and other clients alone. A newly connected client without a local
time/site baseline observes passively and must not stop another client's
tracking merely because its own baseline is missing.

The `TIME_UTC` INDI property may be a cached snapshot, not the controller's
live clock. No GPS clock authority is built into this SDK. An application
must ask its user before calling `client.sync_time_location(user_approved=True)`.
That writes Raspberry time and configured location to the shared INDI device;
it can change pointing or invalidate another client's assumptions, including
while tracking. An INDI `Ok` confirms acceptance by the driver, not an
independent controller-clock readback. No time/site write occurs on connect.

The adapter reports a flip request at the configured HA warning boundary and
requests a verified stop at the inclusive operational hard boundary when it
has fresh, authoritative evidence. The example policy is +1.0 degree and
+1.75 degrees, below the current firmware guard readback of East 12 and West
8 minutes. An unverified or conflicting status refuses this client's normal
motion; it does not justify interfering with another client's existing track.
An INDI client cannot prevent a different client from restarting tracking
after a stop. OnStep firmware remains the final cross-client safeguard.

## Focuser

When the mount is confirmed parked, the 0.4.0 focuser supports bounded
absolute movement and abort through the same INDI connection:

```python
status = client.focuser.get_status()
if status.move_ready:
    result = client.focuser.move_absolute(status.position + 100)
    if not result.reached:
        raise RuntimeError(result.error)
```

The operator-authorized live test on 2026-09-22 moved 15145 to 16145 and
back through INDI, confirmed both endpoints, and confirmed PARKED before and
after each leg. It did not certify the entire physical focuser travel.

## Mount Capability Boundary

`client.mount.get_status()`, `meridian_status()`, and `stop()` are available.
`unpark()` leaves PARKED without a HOME slew. `park()` moves directly to the
stored PARK position and does not require or silently route through HOME. A
repeated `park()` is idempotent when fresh raw OnStep status already proves
the requested state. These mechanical operations remain disabled by
the example configuration until their supervised acceptance is complete.
General `goto()` remains unavailable.

`enable_tracking()` uses INDI `TRACK_ON` and requires two fresh tracking
reports after a real transition. The default
`tracking_authority_policy="strict"` requires this client's accepted
time/site and HOME authority plus a safe meridian phase. Applications that
deliberately delegate those authorities to the controller or another INDI
client may explicitly select `controller_managed`; missing local authorities
then appear in `IndiTrackingResult.warnings` while fresh OnStep status,
stationary/unparked state, faults, limits, and the inclusive hard stop remain
enforced. A newly connected client never stops or reissues an existing track
merely because its own authority is absent. Finite
`move_ra_axis_deg()` / `move_dec_axis_deg()` take degrees. The compatibility
`move_ra(..., mode="manual")` / `move_dec(..., mode="manual")` methods take
arcseconds. Both use finite INDI targets.
They support 30 arcseconds through 10 degrees and require fresh unparked,
stationary, non-tracking and fault-free state,
but neither HOME nor clock authority. Their 1/5/10-degree mock cases pass;
the supervised mount test has **not** passed. Requests below 30″ are refused,
never rounded. Guide, PARK-record and application-controlled flip
commands from 0.3.5 have not been ported; no serial fallback is provided.

The installed driver appears to publish OnStep's `H` HOME flag in raw status,
but this has not been physically checked through INDI on this rig. Local
axis-angle movement does not depend on HOME. The final operator-approved
HOME -> PARK cleanup will separately validate `H` and remains outstanding.

The supervised [axis-angle procedure](docs/SDK.md#ra-and-dec-axis-angle-test)
tests paired +1/-1, +5/-5 and +10/-10 degree movements on both axes before
0.4.0 is considered complete. It requires line of sight.

See the [SDK guide](docs/SDK.md), [requirements](docs/REQUIREMENTS.md),
[INDI architecture](docs/INDI_ARCHITECTURE.md), and
[hardware inventory](docs/INDI_RASPBERRY_INVENTORY.md). The previous
[0.3.5 release](https://github.com/tschoenfelder/OnStepAdapter/releases/tag/v0.3.5)
documents the historical exclusive-serial API and must not be used alongside
the OnStep INDI driver.
