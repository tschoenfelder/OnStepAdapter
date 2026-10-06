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
identification, image measurement, pulse calibration and convergence. The
feature has mocked INDI protocol and safety coverage. Initial hardware use
must remain supervised until a physical guide-pulse acceptance run succeeds.
