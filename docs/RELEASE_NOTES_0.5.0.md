# OnStepAdapter 0.5.0

## Added

- Tracking-preserving astronomical guide pulses through standard INDI timed
  guide properties.
- North, south, east and west directions with bounded 20-5000 ms durations.
- Safety rechecks between 500 ms chunks for longer requests.
- Hardware-completion verification using the OnStep compact-status `G` flag;
  INDI `Ok` is treated only as command acceptance.
- Structured guide results and a boolean compatibility wrapper.
- Strict and controller-managed astronomical authority behavior consistent
  with tracking enable.

## Safety Boundary

Guide pulses require fresh unparked tracking state and no active slew, HOME,
fault or OnStep limit. The inclusive operational hard stop and firmware limit
always refuse guiding. A flip recommendation is returned as a warning while
the pulse remains inside the hard stop. Failures after a command is issued
request emergency stop.

The adapter supplies only the timed pulse. The calling application owns star
identification, image measurement, pulse calibration and convergence.

## Physical Validation

Supervised validation passed on October 6, 2026 with the Terrans OnStep V4,
the unchanged `indi_lx200_OnStep` 1.25 driver and active sidereal tracking:

- reciprocal 3000 ms east and west requests each completed as six sequential
  500 ms chunks;
- every request returned with tracking preserved and compact status
  `NpEW260`, proving that the guide-active `G` flag had cleared;
- three 1.5-second FITS exposures measured the east and west pulse components
  as opposite vectors of 1.36 and 1.38 pixels after linear tracking drift was
  removed;
- at 3.323 arcseconds per pixel and declination +84.35 degrees, those vectors
  correspond to 4.50 and 4.59 arcseconds, compared with the theoretical
  4.43 arcseconds for a 3000 ms 1x sidereal RA pulse;
- FITS RA changed by +0.01218 degrees after east and -0.01221 degrees after
  west, returning within 0.00003 degrees of the initial value.

This proves command execution, completion detection, direction reversal and
tracking preservation on the tested rig. Applications must still calibrate
their own image-scale response and close the guiding loop from fresh frames.
