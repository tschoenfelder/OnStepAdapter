# OnStepAdapter 0.5.0 SDK

This release is an in-process INDI client, not a serial-port owner. It uses
the unchanged `LX200 OnStep` device in the Raspberry's local `indiserver`.
Python 3.13 or newer is required; there are no runtime Python dependencies.
Install `onstep_adapter-0.5.0-py3-none-any.whl` with `python3 -m pip install`.

## Connection And Cleanup

```python
from onstep_adapter import OnStepClient, load_indi_config

config = load_indi_config("/home/astro/.config/onstep_adapter/config.toml")
with OnStepClient(config=config) as client:
    mount = client.mount.get_status()
    focuser = client.focuser.get_status()
    print(mount, focuser)
```

`with OnStepClient(...)` connects automatically. Call `connect()` only when
not using the context manager. `close()`
stops the client's own meridian supervisor and closes its INDI socket; it
does not disconnect the shared device, stop the server, or evict other apps.
No serial or raw LX200 fallback ships in the 0.5.0 wheel.

`SAFE_MERIDIAN_FLIP` defaults to enabled. On connect, the client checks the
installed driver's switch, enables it if needed, and verifies the readback.
It does not restore the old shared switch state on close. Firmware meridian
guards are read and checked against the configured operational policy.

## Shared Time And Location

`TIME_UTC` can be a cached or echoed INDI value, not a live OnStep clock.
Reading it never establishes this client's time authority. There is no GPS
time source inside the adapter. Raspberry time and the configured observer
site may be pushed only after user approval:

```python
result = client.sync_time_location(user_approved=True)
if result.error or not (result.time_accepted and result.location_accepted):
    raise RuntimeError(result.error or "Time/site sync incomplete")
```

This is a shared-device write. Ask the user even if another application has
already synced. Warn that changing time or location during tracking or
slewing can change pointing and invalidate other clients' calculations. An
INDI `Ok` means the driver accepted the write, not that a fresh controller
clock readback was obtained. A fresh observed change made by another client
invalidates this client's local baseline; an unpublished change may not be
detectable. A new unsynced client remains read-only and must not stop an
existing track merely because it lacks its own baseline.

## Mount Status And Safety

`client.mount.get_status()` returns raw compact `:GU#` status, logical
RA/DEC, pier side, estimated HA, state, ages, authority and blockers.
Coordinates and axis values are controller logical data, not independent
physical sensor evidence. HOME/PARK are mechanical terminal states; their
diagnostic sky coordinates do not constitute meridian-limit violations.

`client.mount.meridian_status()` reports pre-meridian, post-meridian,
flip-required, post-flip, hard-stop, firmware-limit or unknown state. The
example config requests a flip at +1.0 degree and a hard stop at +1.75
degrees, below the current firmware guard readback. The background
supervisor runs only while the client is connected. It requests a verified
mount stop only on fresh, authoritative unsafe evidence and retries while
the hazard remains. This is reactive across clients: another INDI client
could restart tracking, so OnStep firmware remains the final safeguard.
The adapter reports exposure timing; the calling application owns capture
completion, abortion and jog UI.

`client.mount.stop()` requests INDI abort and tracking off, then waits for two
fresh non-moving status reports. The result reports whether each write and the
final stop were confirmed. Acknowledgement alone is not proof of a physical
stop.

`client.mount.unpark()` only leaves PARKED and disables resumed tracking; it
does not go to HOME. `park()` moves directly to the stored PARK position from
any fresh, stationary, non-tracking, fault-free unparked state; it does not
require HOME. A repeated PARK is idempotent when raw OnStep status already
proves the destination. These operations are disabled by default
along with `go_home()` and `route_park_to_home()` because the installed driver's raw HOME `H` status has
not yet been observed on this rig. This does not block HOME-neutral local
movement; the supervised runner checks `H` only during final HOME -> PARK.
`goto()` remains deliberately unavailable and there is no direct-serial escape
hatch. `enable_tracking()` always requires fresh, fault-free OnStep status,
an unparked stationary mount, and no hard limit. With the default
`tracking_authority_policy="strict"`, it also requires fresh coordinates,
accepted time/site, HOME authority, and an allowed meridian phase. With
`"controller_managed"`, those local authority gaps are returned as warnings
instead of blockers; the calling application deliberately accepts that the
controller or another client owns them. Both modes require two fresh tracking
reports after a transition and never disturb an already-running safe track.
If an accepted transition becomes unsafe or cannot be confirmed, emergency
stop is requested. PARK-record and application-controlled flip operations
from the former 0.3.5 API remain unavailable.

## Tracking-Preserving Guide Pulses

Astronomical guiding and guide-assisted reacquisition use INDI's timed-guide
properties while sidereal tracking remains active:

```python
result = client.mount.guide_pulse("north", 200)
if not result.pulse_completed:
    raise RuntimeError(result.error)
```

Accepted directions are `north`, `south`, `east`, `west` and `n/s/e/w`.
Durations are integer milliseconds from 20 through 5000. Requests longer
than 500 ms are split into bounded chunks so the adapter can obtain a fresh
status and meridian decision between chunks. The result reports requested
and completed chunks, tracking preservation, final raw status, meridian
phase, warnings and any error. `mount.guide()` remains as a boolean
compatibility wrapper.

The mount must be connected, unparked, away from HOME, tracking, stationary,
and free of OnStep faults or limits. The inclusive operational hard stop and
firmware limit always refuse a pulse. With strict authority, fresh
coordinates, pier side, time/site, HOME authority and an allowed meridian
phase are also required. With `tracking_authority_policy="controller_managed"`,
delegated authority gaps are warnings, but the live hardware blockers remain
mandatory. `flip_required` is permitted and returned as
`meridian_flip_required`; the calling application should finish its bounded
reacquisition promptly and perform its normal flip workflow.

These pulses do not promise a specific angular displacement. The calling
guider calibrates milliseconds per image displacement and closes the loop
with another frame. On command failure or lost safe tracking after a pulse,
the adapter requests emergency stop. This 0.5.0 path has mocked protocol and
safety coverage; its first physical pulse should remain supervised.

## RA And DEC Axis-Angle Test

`mount.move_ra_axis_deg(angle)` and `mount.move_dec_axis_deg(angle)` issue a
finite INDI coordinate target, not an indefinite manual-motion switch. Positive
RA increases hour angle (westward); positive DEC moves northward. Both require
fresh unparked status, tracking off, no slew/fault/limit, a stable pier side,
and a request from 30 arcseconds through 10 degrees. HOME and time/site authority are not
prerequisites for this terrestrial/manual operation. DEC targets are limited
to -80 through +80 degrees. Each move monitors the path, disables tracking
after arrival and confirms fresh stationary status. Logical coordinates are
not independent encoder measurements; visually verify each stage. Compatibility
aliases `move_ra(offset_arcsec, mode="manual")` and
`move_dec(offset_arcsec, mode="manual")` accept arcseconds and report both
`requested_arcsec` and `requested_deg`. The explicit axis methods take degrees.
The practical 30″ floor reflects logical RA feedback resolution. Smaller
requests fail clearly and are never rounded up. Issue #14's documented seeds
of 110″, 159″, 195″, 283″, 897″ and 1595″ are covered by mocked paths.

The runner stages PARK -> UNPARK without moving to HOME, then runs paired
+1/-1, +5/-5 and +10/-10 degrees on RA, then DEC. Every movement requires
ENTER and physical clearance confirmation. Final HOME -> PARK also requires
confirmation and is the separate live `H` check. ESC, `f` or Ctrl-C requests
emergency stop and suppresses later route commands. Add `--sync-time-location`
only when the user explicitly chooses that shared write.

```bash
python3 -m onstep_adapter.tools.indi_axis_angle_smoke \
  --config ~/.config/onstep_adapter/config.toml \
  --confirm-home-uat
```

This command **has not been run on the mount**. `--confirm-home-uat` opts into
the final-cleanup INDI `H` test; it does not assert a pass. Run
only with line of sight, clearance and an available emergency stop, with no
other mount controller issuing commands. The 0.4.0 movement gate remains open
until the full live sequence and final PARK are confirmed.

## Focuser

`client.focuser.get_status()` returns position, INDI driver maximum,
configured maximum, moving state and blockers. Set `[focuser].max_position`
in the installation TOML; no focuser move is accepted without it. The
effective ceiling is the lower of the driver readback and that configured
number. On this rig INDI repeatedly reports `FOCUS_MAX=100000`; the example
caps moves at 50000 because the full physical travel is not certified.
The maximum is a static INDI property and need not update periodically;
position must be fresh and non-conflicting.

```python
status = client.focuser.get_status()
if status.move_ready:
    result = client.focuser.move_absolute(status.position + 100, timeout=30)
    if not result.reached:
        raise RuntimeError(result.error)
```

`move_absolute()` waits for two fresh target-position confirmations and
requests `FOCUS_ABORT_MOTION` on a fault or timeout. `stop()` can issue abort
while a move is in progress and confirms a stable position. The abort path
does not need time/site or HOME authority. Do not infer a stopped motor from
the direction-switch values alone.

On 2026-09-22 a bounded live INDI test moved the focuser 15145 -> 16145 ->
15145. Fresh position updates confirmed both endpoints; two fresh `:GU#`
reports confirmed PARKED before and after each leg. An independent final
query again showed 15145 and PARKED. This was a remote protocol test, not
visual certification of full physical focuser travel.

## Release Scope

0.4.0 was built as a pure-Python wheel, installed in an isolated directory
on Raspberry Python 3.13, and imported without serial modules. The source
suite passed 172 tests plus 5 subtests. The read-only public client was
connected to the actual local INDI driver. The live HOME `H`, HOME/PARK
movement, meridian stop, and astronomical motion paths have **not** been
physically accepted through INDI. Their dependent public operations remain
disabled. Historical direct-serial behavior is documented by the 0.3.5 tag,
not by this wheel.
