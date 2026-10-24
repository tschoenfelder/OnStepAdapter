# OnStepAdapter 0.7.0 SDK

This release is an in-process INDI client, not a serial-port owner. It uses
the unchanged `LX200 OnStep` device in the Raspberry's local `indiserver`.
Python 3.13 or newer is required; there are no runtime Python dependencies.
Install `onstep_adapter-0.7.0-py3-none-any.whl` with `python3 -m pip install`.

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
No serial or raw LX200 fallback ships in the 0.7.0 wheel.

The transport treats the background INDI reader as part of connection health.
Any unexpected exception or return marks the session failed, wakes property
waiters, makes `is_open` false and rejects further writes. `wait_property()`
raises `ConnectionError` with the original reader exception as `__cause__` for
diagnostics. A long-running application should recover with an explicit
`close()` followed by `connect()`; do not retry commands on the failed session.
This closes only the application's TCP socket and never sends
`CONNECTION.DISCONNECT`, so the shared INDI device and other clients remain
undisturbed.

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

The INDI number-property `Ok` state acknowledges command acceptance only.
It does not prove that the hardware pulse has elapsed. The adapter waits the
nominal duration of each chunk and then requires a newer compact OnStep status
without `G` (`pulse guide active`) before issuing the next chunk or reporting
completion. A missing status transition or a `G` flag that does not clear is
a failed pulse and requests emergency stop.

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
the adapter requests emergency stop. The 0.5.0 implementation passed a
supervised Terrans OnStep V4 hardware test on October 6, 2026: reciprocal
3000 ms east/west pulses completed with tracking preserved and no final `G`
flag. Drift-corrected FITS measurements were 1.36 and 1.38 pixels in opposite
directions, matching the 1.33-pixel high-declination prediction. This validates
the protocol path, not a universal pulse calibration for other optical rigs.

### Calibrated Angular Guide Corrections

Version 0.6.0 and later can convert an application-measured correction in arcseconds to
the same timed-guide operation:

```python
from onstep_adapter import IndiGuideCalibration, OnStepClient

calibration = IndiGuideCalibration(
    ra_east_arcsec_per_s=1.48,
    ra_west_arcsec_per_s=1.51,
    dec_north_arcsec_per_s=2.03,
    dec_south_arcsec_per_s=1.96,
)

with OnStepClient(config=config, guide_calibration=calibration) as client:
    east = client.mount.guide_correction_ra(+1.5)
    south = client.mount.guide_correction_dec(-2.0)
```

Calibration is supplied and owned by the calling application. It can instead
be passed per call or replaced at runtime with
`mount.set_guide_calibration()`. Nothing is persisted by the adapter. Positive
RA maps to east, negative RA to west, positive DEC to north and negative DEC
to south on either pier side. The selected direction-specific rate converts
arcseconds to milliseconds; results report both values and embed the complete
`IndiGuidePulseResult` evidence.

Missing, non-finite or non-positive rates, zero/non-finite corrections and
calculated durations outside 20-5000 ms are rejected before transmission.
The existing tracking, HOME/PARK, slew, status, authority, meridian and limit
checks remain unchanged because correction execution delegates directly to
`guide_pulse()`. The calling guider must acquire a fresh frame afterward and
decide whether another correction is necessary.

## Terrestrial Operating Mode

Issue #27 adds a mode boundary above the older coordinate-axis helpers. The
older `move_dec_axis_deg()` is an absolute sky-target operation and does not
represent a physical pole-crossing jog. The terrestrial API instead uses
`TELESCOPE_MOTION_NS` with a finite feedback-controlled stop.

Configure one shared state file for all Python applications on the Raspberry:

```toml
[terrestrial]
state_file = "/home/astro/.config/onstep_adapter/mode_state.json"
step_deg = 10.0
max_step_deg = 10.0
horizon_margin_deg = 2.0
brake_lead_deg = 0.5
stop_hold_seconds = 3.0
slew_rate_element = "SLEW_MAX"
```

The caller must explicitly execute `park()`, verify `confirmed`, call
`set_mode()`, then explicitly `unpark()`. This is required in both directions,
including when the mount was already parked. PARK waits for agreement between
the raw OnStep `P` status and standard INDI `TELESCOPE_PARK`, plus an idle hold.
An early standard PARK switch or matching coordinates are not completion.

The authorization is local to the client that requested PARK, is consumed
once, expires after 30 seconds, and is not restored after restart. The mode
itself is persisted with a generation under an advisory file lock and atomic
replace. This is the strongest shared arbitration available without modifying
the INDI driver. It does not coordinate clients on another host that do not
share the state file; such deployments must not use terrestrial mode.

Toward the northern horizon maps to `MOTION_NORTH` on the near pole branch and
`MOTION_SOUTH` after the estimated 12-hour RA branch change; outward movement
reverses that mapping. A residual of at most 4 hours is near, at least 8 hours
is far, and the ambiguous interval is refused. The estimate corrects the PARK
RA reference for sidereal drift but is not independent Axis-2 telemetry.
Motion uses the configured INDI slew-rate element, stops early by the braking
allowance, and does not complete until coordinates are stable for the hold
period. PARK/HOME/mode changes are blocked while moving or stopping; emergency
stop remains available. Horizon clipping uses the configured movement floor
plus margin. No exact high-speed endpoint or encoder accuracy is claimed.

After mocked validation, the deferred supervised hardware acceptance command is:

```bash
python3 -m onstep_adapter.tools.indi_terrestrial_smoke \
  --config ~/.config/onstep_adapter/config.toml
```

It requires confirmation before explicit PARK, mode selection and UNPARK.
At these three confirmation prompts, only an empty ENTER proceeds. Commands
such as `w`, `s`, or `p` are rejected without transmission until the later
`terrestrial>` command prompt is visible. The initial PARK request is
intentionally repeated when OnStep is already parked: it must not move the
mount and exists to obtain fresh, reconciled PARK evidence owned by this
client before changing the shared mode.
Each `w` or `s` command is one bounded step. `x`, ESC and Ctrl-C request a
verified stop and never start automatic PARK/HOME cleanup. `p` explicitly
parks; only then may `a` consume that fresh authorization to return to
astronomical mode. This physical UAT remains outstanding.

## RA And DEC Axis-Angle Test

`mount.move_ra_axis_deg(angle)` and `mount.move_dec_axis_deg(angle)` issue a
finite INDI coordinate target, not an indefinite manual-motion switch. Positive
RA increases hour angle (westward); positive DEC moves northward. Both require
fresh unparked status, tracking off, no slew/fault/limit, a stable pier side,
and a request from 30 arcseconds through 10 degrees. HOME and time/site authority are not
prerequisites for this terrestrial/manual operation. DEC targets are limited
by `[movement].min_dec_deg` and `max_dec_deg`; the example uses the inclusive
range -80 through +80 degrees. `[movement].min_alt_deg` is also inclusive and
defaults to -2 degrees. When an authoritative HA sample is available, the
adapter computes the projected target altitude and refuses a lower endpoint
before sending any INDI command. Without astronomical time authority, a
HOME-neutral terrestrial move retains the DEC gate but cannot infer altitude
from HOME's diagnostic coordinates. Each move monitors the path, disables
tracking after arrival and confirms fresh stationary status. Logical
coordinates are not independent encoder measurements; visually verify each stage. Compatibility
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
