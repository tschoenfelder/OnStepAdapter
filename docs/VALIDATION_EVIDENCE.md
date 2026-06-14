# Validation Evidence For 0.3.0

## Meridian Workflow

The application-controlled meridian workflow was physically exercised on June
10, 2026. The adapter notified the application near `+2 degrees`, allowed the
simulated active frame to complete, changed pier side, reacquired the target,
resumed tracking, and returned through HOME to PARK.

## Focuser

The shared-bus focuser smoke test passed on June 10, 2026:

- mount remained PARKED at every checkpoint;
- a 100-step move completed;
- the focuser returned exactly to its starting position;
- a longer move was interrupted with `:FQ#`;
- final exact return succeeded;
- mount communication remained operational.

## Mechanical Safety Authority

The counterweight warning approach passed on June 13, 2026:

- PARK to HOME established trusted HOME authority;
- a target before the warning boundary remained allowed;
- pier side, hour angle, counterweight state, margin, warning, and refusal
  state were reported;
- final HOME to PARK succeeded.

## Stock OnStep Firmware Fallback

The staged Axis-1 proof passed on June 13, 2026:

- operator-confirmed checkpoints: `60`, `120`, `150`, `170`, `178.5 degrees`;
- tracking was off and verified at each checkpoint;
- natural tracking was armed at Axis-1 `178.514 degrees`;
- OnStep autonomously stopped at Axis-1 `180.00013 degrees`;
- two stationary non-tracking polls confirmed the firmware stop;
- the independent raw-stop backstop at `180.25 degrees` was not needed;
- endpoint and complete path were physically confirmed safe;
- final HOME to PARK succeeded.

The final unattended audit reported:

```text
physical proof: valid=True mode=axis1_fallback reasons=[]
operational stop=4.995deg
firmware fallback=axis1_max at 180.0deg
proven=True physically_safe=True
PASS: unattended tracking firmware safeguard is proven
```

## Independent RA And DEC Motion

The `0.3.0` motion APIs were physically exercised on June 14, 2026.

The bounded correction smoke test:

- routed PARK to HOME and acquired a safe target;
- executed center-rate RA east/west and DEC north/south corrections;
- executed guide-rate RA east and DEC north pulses;
- sent the matching directional stop after every correction;
- preserved tracking throughout;
- returned through HOME to live PARK.

The larger coordinate-path test then proved visually observable independent
coordinate movement:

```text
initial:       RA=12.528611h DEC=20deg
RA increase:   RA=13.528611h DEC=20deg
DEC increase:  RA=13.528611h DEC=30deg
RA decrease:   RA=12.528611h DEC=30deg
DEC decrease:  RA=12.528611h DEC=20deg
```

Every leg stabilized at the requested target within tolerance. The final
target matched the initial target, and operator confirmation authorized the
final HOME-to-PARK route. The command ended with:

```text
PASS: independent RA/DEC coordinate path completed and final state PARKED.
```

Before movement, both commands verified Raspberry/OnStep civil time, observer
location, and sidereal time while PARKED.

## Interpretation

The stock Axis-1 fallback is much later than the normal operational stop. It
is accepted only as final crash prevention for the exact validated rig. Normal
operation still requests the meridian flip near `+2 degrees` and stops around
`+5 degrees` while the host application is alive.
