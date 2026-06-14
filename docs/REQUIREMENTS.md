# OnStep Adapter Requirements

## Connection

- The adapter shall own exactly one serial connection per controller.
- Mount and focuser commands shall be serialized on that shared connection.
- Closing the client shall be idempotent.
- Mount `:Q#` and focuser `:FQ#` emergency stops shall remain available.

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
- Fresh motion safety preflight and inclusive hard-limit enforcement apply to
  guide and center corrections.
