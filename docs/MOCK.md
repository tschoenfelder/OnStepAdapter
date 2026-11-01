# OnStepX Mock For Application Tests

`onstep_mock` is a virtual OnStepX German equatorial mount behind a small
local INDI server. It sits **below** the adapter: the unchanged
`onstep_adapter.OnStepClient` connects to it over TCP exactly as it connects
to the Raspberry's indiserver, so every adapter layer (status decoding, STOP,
HOME/PARK routing, axis moves, meridian supervisor) runs for real. Only the
standard library is used.

```python
from onstep_adapter import OnStepClient
from onstep_mock import SimConfig, SimulatedOnStepServer
from onstep_mock.testing import runtime_config, wait_for_live_status

with SimulatedOnStepServer(SimConfig(speed=20)) as sim:
    with OnStepClient(config=runtime_config(sim)) as client:
        wait_for_live_status(client)   # adapter needs a fresh status first
        client.mount.unpark()
        client.mount.go_home().wait()  # moves return a MotionHandle at once
        mount = sim.mount              # MountSnapshot of the internal state
        assert mount.at_home and mount.dec_deg == 90.0
        assert not mount.tracking
        sim.assert_no_forbidden()
```

In pytest, add `pytest_plugins = ["onstep_mock.pytest_plugin"]` to the
top-level `conftest.py` and use the `onstep_sim` and `onstep_client`
fixtures. Override `onstep_sim_config` to change the mount's behavior.

## What It Reproduces

| Behavior | Mock |
| --- | --- |
| Slews take time | Each axis moves at `slew_deg_per_s` (default 4) times `speed`; axes finish independently |
| STOP at any moment | A device thread moves the mount; `TELESCOPE_ABORT_MOTION` is handled on the next 20 ms tick, stationary status follows after `abort_settle_s` (1.45 s) / `speed` |
| UNPARK auto-tracking | Tracking starts on UNPARK; `mount.tracking_on_seconds` shows how fast the adapter stopped it |
| Arrival tracking | A finite RA/DEC target starts tracking at arrival; status is published before coordinates |
| Fixed HOME | `TELESCOPE_HOME.GO` slews to DEC +90, RA = LST + 6 h, with the `H` flag |
| Fixed PARK | `TELESCOPE_PARK.PARK` slews to the one stored `park_pose` |
| Non-tracking drift | Pose stored as hour angle: tracking OFF keeps ALT/AZ fixed and RA advancing |
| Manual motion | `TELESCOPE_MOTION_NS/WE` at the selected `TELESCOPE_SLEW_RATE`; a repeated On without OFF re-arm starts nothing |
| Guide pulses | `TELESCOPE_TIMED_GUIDE_NS/WE` set the `G` flag for the pulse duration |
| Forbidden writes | `TELESCOPE_HOME.SET`, `ON_COORD_SET.SYNC` and any `TELESCOPE_PARK_OPTION` write get INDI `Alert`, change nothing and are listed in `sim.forbidden` |

Status, coordinates and pier side are republished every 0.25 s and on every
state change, so the adapter's freshness checks pass. Meridian flips, pole
crossings and acceleration curves are not modelled.

## Validated Against Recordings

The behavior above is checked against recordings of the real mount:
`python3 -m onstep_mock.validate capture.jsonl` replays a capture's client
writes against the mock and lists every difference, and every capture in
`tests/acceptance/recordings/` is an acceptance test. See
[CAPTURE.md](CAPTURE.md) for how to record and what is checked (A01-A16).

## Inspecting And Steering

- `sim.mount`: frozen `MountSnapshot` with UTC, LST, HA, DEC, RA, ALT/AZ,
  pier side, parked, at_home, tracking, `tracking_on_seconds`, motion
  (`IDLE`, `SLEWING`, `STOPPING`, `MANUAL`), guiding, slew rate, fault, the
  published `:GU#` string and the fixed HOME/PARK poses.
- `sim.writes`: every INDI write received, with values, acceptance and the
  rejection reason; `sim.forbidden` is the SET/SYNC subset.
- `sim.wait_until(predicate, timeout)`: block the test, never the device.
- `sim.place(ha_h=..., dec_deg=..., pier_side=..., parked=..., tracking=...)`:
  test setup of the internal truth; not an INDI command and not a SYNC. Give
  a connected client one status period (about 0.3 s) to see the new pose.
- `sim.inject_fault("LIMIT" | "ERROR")`, `sim.clear_fault()`,
  `sim.reject_next(property)`, `sim.freeze_status()`,
  `sim.drop_connections()`.

## Stopping A Move From The Test

The adapter's `park()`, `go_home()` and axis moves return a `MotionHandle`
at once, so a test can watch a slow slew and stop it from the same thread:

```python
with SimulatedOnStepServer(SimConfig(speed=1.0)) as sim:
    ...
    move = client.mount.go_home()
    sim.wait_until(lambda m: m.motion == "SLEWING", timeout=3)
    assert move.progress().state == "running"
    stopped = move.stop()
    assert stopped.stopped_confirmed and sim.mount.motion == "IDLE"
    with pytest.raises(MotionStopped):
        move.wait()
```

The mock itself needed no change for this: its device thread already moves
the mount independently of the client, and an ABORT is applied on the next
20 ms tick.
