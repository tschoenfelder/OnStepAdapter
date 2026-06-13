# Hardware Compatibility

## Physically Tested Device

OnStepAdapter `0.2.0` was physically tested with:

- controller: Terrans OnStep V4;
- firmware product: `On-Step`;
- firmware version: `10.19d`;
- firmware date: February 29, 2024;
- connection: USB serial;
- mount type: German equatorial mount;
- shared OnStep focuser: present;
- preferred pier policy: BEST;
- OnStep automatic meridian flip: disabled during safeguard certification.

The device was tested with its existing firmware build. No modification to
OnStep `Config.h` was required.

## Proven Behaviors

- status-confirmed PARK to HOME to PARK routing;
- HOME authority establishment from live OnStep HOME status;
- shared mount and focuser traffic over one serial connection;
- bounded focuser move, exact return, immediate `:FQ#` stop, and cleanup;
- application-controlled meridian recommendation and flip workflow;
- SmartTScope operational tracking stop around `+5 degrees`;
- stock OnStep Axis-1 maximum stop at approximately `180 degrees`;
- autonomous OnStep stop before the independent `180.25 degree` backstop.

## Important Scope

The stock Axis-1 fallback proof is specific to the tested physical rig. Its
proof record is tied to firmware identity, axis-limit readbacks, meridian
configuration, observer location, and operator-confirmed physical clearance.
Changing those inputs invalidates that proof.

Other OnStep and OnStepX controllers may work because the adapter uses the
documented LX200-compatible command protocol, but they are not represented as
physically certified by this release.

