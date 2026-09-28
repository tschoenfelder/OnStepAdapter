# OnStep Adapter Requirements

## Target Architecture: Local INDI

The accepted [INDI architecture decision](INDI_ARCHITECTURE.md) governs the
0.4.1 wheel. Its public client and packaged modules use INDI only. The
direct-serial 0.3.5 model below is retained as historical requirements, not
as a fallback available in 0.4.0. HOME-dependent mount motion, astronomical
goto/tracking, axis jog, guiding and PARK-record writes are outstanding and
fail closed or are absent from the 0.4.0 public API.

- The adapter shall use the unchanged local INDI OnStep driver exclusively,
  without direct serial or raw-controller fallback.
- It shall run inside each Python application; no persistent service is added.
- Other INDI clients may remain connected and send commands without eviction.
- While connected, supervision shall request and verify stops for observed
  unsafe motion regardless of the initiating client.
- Close shall release only local resources, leaving the shared device/server
  and other clients connected. Supervision ends with the client lifetime.
- No GPS time authority is built into the planned INDI adapter. Raspberry
  system time is the operator-selected source. Before its own astronomical
  motion, each controlling application shall ask its user to approve
  time/location synchronization or explicitly accept an existing session
  baseline; it shall not push either value automatically on connection.
- User-approved INDI Sync may be issued while another client is tracking or
  slewing, after warning that a mid-motion time/site change can alter pointing
  and other clients' safety calculations. It shall not automatically stop the
  mount. An Ok acknowledgement establishes this client's accepted sync
  authority, not independent OnStep clock readback; a failed sync does not.
- Cached `TIME_UTC` alone shall not establish a new client's time authority.
  Without an accepted baseline, refuse that client's dependent motion, not
  read-only observation, emergency stop, or another client's existing track.
- Time and location are shared across INDI clients. Observed external changes
  shall invalidate the adapter's safety baseline; changes not published by
  the driver may be undetectable, and this limitation shall be exposed.
  Exposure control belongs to the calling application.
- A newly connected client lacking its own time/site baseline shall not stop
  another client's existing tracking for that reason alone. It shall refuse
  only its own dependent commands until ready; intervention in existing motion
  requires affirmative, fresh evidence of danger.
- Meridian flip and stop thresholds, allowance and reserve shall be loaded
  from per-installation runtime configuration and checked against live
  firmware guards. The +1/+1.75-degree proposal is not a hard-coded constant.
- A calling application may provide a jog UI and request signed, bounded
  RA-axis or DEC-axis rotations (for example +1 or -5 degrees). The adapter
  shall not own the UI or interpret those values as on-sky RA or extra HA
  allowance. Axis-angle operations shall be named distinctly from existing
  on-sky arcsecond corrections. It shall preflight path and endpoint, supervise,
  and verify a stop on completion, cancellation, timeout, disconnect or an
  unsafe state. A client press-and-hold UI must not rely on indefinite motion.
- Manual/local angular RA/DEC movement shall be HOME-neutral and shall not
  require trusted astronomical time. It is permitted at HOME or safely
  unparked away from HOME when tracking is off, fresh state is available,
  no fault/limit/slew is active, and the bounded endpoint is accepted. It
  shall never force a HOME transition. This implements GitHub issue #12.
- INDI tracking enable shall default to strict astronomical authority and a
  safe live meridian state. An explicit per-client `controller_managed` policy
  may delegate time/site, HOME, coordinate and meridian authority while still
  requiring fresh OnStep status and enforcing PARK, slew, fault, firmware
  limit and inclusive hard-stop blockers. Delegated gaps shall be warnings.
  A newly connected client shall not alter an already-running track solely
  because it lacks local authority. A real `TRACK_ON` transition shall require
  two fresh tracking reports. Accepted but unconfirmed or unsafe activation
  shall request emergency stop.
  Local angular movement shall support every documented issue #14 calibration
  seed (minimum 110 arcseconds), use scaled verification tolerances, and reject
  unsupported smaller requests explicitly without clamping or rounding.
- Missing mandatory INDI safety capabilities shall refuse dependent motion,
  rather than weakening safety checks or bypassing the server.
- Each 0.4.0 feature shall update user-facing installation, API and safety
  documentation alongside implementation and tests. Release requires the
  documented examples to run against the built wheel and must distinguish
  implemented behavior from planned behavior.
- The planned INDI client shall default `safe_meridian_flip_via_home` to true,
  enable and verify the installed driver's HOME-route switch on connection,
  recheck it before flips and after reconnect, and refuse flips when unverified.
  It shall not disable the shared switch on close. Setting the option false
  shall suppress automatic activation without permitting an unsafe fallback.
- PARK shall be a direct move to the stored controller PARK position from any
  fresh, stationary, non-tracking and fault-free unparked state. It shall not
  require or implicitly route through HOME. PARK shall be idempotent when
  recent raw OnStep status already proves the requested state.

## Released 0.3.5 Connection Model

- The adapter shall own exactly one serial connection per controller.
- Mount and focuser commands shall be serialized on that shared connection.
- Closing the client shall be idempotent.
- Mount `:Q#` and focuser `:FQ#` emergency stops shall remain available.
- The supported ownership model shall be exclusive OnStepAdapter ownership of
  the physical OnStep serial port.
- Consuming applications shall route all OnStep mount, tracking, PARK/unpark,
  status, stop, and OnStep focuser operations through OnStepAdapter.
- Consuming applications shall not open raw serial, raw LX200 socket, INDI
  `LX200 OnStep`, or another direct OnStep controller connection in parallel.
- INDI may remain in use for unrelated non-OnStep hardware.

## Mechanical Safety Authority

- The adapter shall not infer mechanical safety from RA/DEC alone.
- Before normal astronomical motion it shall obtain fresh status, pier side,
  hour angle, meridian distance, tracking state, slew state, HOME/PARK state,
  OnStep limit state, and logical Axis-1 position when readable.
- Controller positions are logical positions, not independent encoder or
  physical-sensor evidence.
- A successful status-confirmed PARK to HOME route establishes HOME authority
  for the session.
- Missing, stale, inconsistent, or untrusted safety inputs shall refuse normal
  astronomical motion.
- HOME/PARK and emergency/recovery commands use separate mechanical authority.

## Meridian And Counterweight Policy

- Counterweight and limit state shall be exposed explicitly.
- The warning boundary shall be reported to the calling application; exposure
  start, completion, or abortion remains an application decision.
- The hard boundary is inclusive.
- At or beyond the hard boundary, tracking shall be disabled and verified.
- Goto, guide, tracking, and ordinary slew requests that maintain or worsen a
  hard-limit violation shall be refused.
- Explicit recovery motion toward safety and emergency stop remain available.

## Focuser

- Availability, position, maximum position, and movement state shall be
  readable.
- Absolute moves shall enforce configured limits before transmission.
- The reply from `:FS<n>#` shall be consumed and validated.
- Rejected moves shall raise a structured safety error.
- `:FQ#` shall remain an immediate write-only stop.

## Firmware Safeguard

- The adapter shall report OnStep horizon and overhead limits when readable.
- OnStep limit and park-failure states are hard operational faults.
- Unattended operation may be allowed only when the configured firmware
  fallback has a valid proof for the current controller and rig.

## PARK Record Authority

- Setting PARK shall require caller confirmation, trusted HOME authority,
  stationary and non-tracking state, no OnStep limit/fault, and writable local
  persistence.
- Setting PARK at HOME shall be refused unless the caller explicitly passes
  `allow_at_home=True`.
- The adapter shall capture current RA/DEC, logical axes, pier side, firmware
  identity, and HOME authority before sending `:hQ#`.
- The adapter shall report controller-updated/local-persistence-failed as a
  partial outcome and shall not invite a blind retry.
- Because OnStep exposes no documented stored-PARK readback, the local PARK
  record shall state that controller matching is unverifiable.

## RA/DEC Corrections

- RA and DEC corrections shall be independent, serialized, bounded, and
  automatically stopped.
- Positive on-image RA means east; positive DEC means north.
- Guide correction requires tracking and uses native pulse guiding.
- Center correction may overlay tracking and shall preserve its prior state.
- Angular corrections require direction-specific application-supplied
  calibration and shall be marked as requiring image verification.
- Direction-specific calibration may be supplied or updated after construction
  through a thread-safe public API.
- Partial calibration shall be allowed, but an angular correction shall be
  refused until its exact mode, axis, and direction rate is present and valid.
- Fresh motion safety preflight and inclusive hard-limit enforcement apply to
  guide and center corrections.
