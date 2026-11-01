# Recording Real OnStepX Traffic To Validate The Mock

`onstep_adapter.capture` records what an INDI client, `indiserver` with
`indi_lx200_OnStep`, and OnStepX say to each other. The recording is checked
against the behavior that `ONSTEPX_LX200_INDI_BEHAVIOR.md`, `docs/MOCK.md`
and `onstep_mock` assume, and it is replayed against the mock to find every
place where the mock answers differently from the real mount. A recording
that is dropped into `tests/acceptance/recordings/` becomes an acceptance
test.

Capture is passive. Nothing in this package sends a command to the mount:
the pcap path only reads packets, and the proxy only forwards bytes that one
peer sent to the other. SET PARK, SET HOME and SYNC are never sent; if a
client sends one during a capture, it is flagged (`A01`, `A02`) and the
replay skips it.

## 1. Record

Logs are stored by day: an output path containing `{date}` (the default,
`capture-{date}.jsonl`) gets the UTC day, so every capture of a day is
appended to one file, each as its own session. A session that runs past
midnight continues in the next day's file under the same session id; give
both files to a command and it is joined again. The adapter's own trace
(`[trace] path = ".../trace-{date}.jsonl"`) is stored the same way.

Run the capture on the Raspberry that runs `indiserver`, then drive the
mount the way you normally do (your application or the smoke tools). Start
the capture **before** the client connects, so the property definitions are
recorded too.

### Option A: tcpdump (recommended, nothing in the data path)

```bash
sudo tcpdump -i lo -s 0 -U -w ~/onstep-$(date -u +%Y%m%dT%H%M%SZ).pcap 'tcp port 7624'
# ... run the session, then Ctrl+C
python3 -m onstep_adapter.tools.indi_capture from-pcap ~/onstep-*.pcap \
    -o ~/onstep-captures/capture-{date}.jsonl
```

If OnStepX is reached over its TCP command port (WiFi or Ethernet, port
9999) rather than USB serial, record that link as well, so set-park,
set-home and sync LX200 commands from the driver would be seen:

```bash
sudo tcpdump -i any -s 0 -U -w onstep.pcap 'tcp port 7624 or tcp port 9999'
python3 -m onstep_adapter.tools.indi_capture from-pcap onstep.pcap -o capture.jsonl --lx200-port 9999
```

A USB serial link is not recorded; the INDI side is enough for every check
except `A02`.

### Option B: pass-through proxy

```bash
python3 -m onstep_adapter.tools.indi_capture proxy --listen 127.0.0.1:7625 \
    --upstream 127.0.0.1:7624 -o ~/onstep-captures/capture-{date}.jsonl \
    --label "field test"
```

Point the client at port 7625 instead of 7624 (for the adapter, `port = 7625`
in the runtime configuration). Bytes are forwarded unchanged and at once;
the recording is made afterwards from a copy, so it adds no latency to STOP.
The proxy is still in the data path: if it is stopped, the client loses its
connection. Keep the usual physical stop at hand, as in every hardware test.

`--protocol lx200` records the driver's TCP link to OnStepX the same way
(point the driver's network connection at the proxy).

### What to do during the session

Every check reports `NOT_OBSERVED` for a situation the session did not
contain, so any session is useful. To cover all checks in one run, starting
PARKED:

1. UNPARK (`A03`, `A15`).
2. Stay stationary with tracking OFF for at least 30 s (`A11`, `A12`).
3. A finite axis move of at least 2 degrees, away from the pole, to arrival
   (`A07`, `A08`, `A13`).
4. A finite axis move stopped with STOP while it slews (`A04`, `A05`, `A06`).
5. GO HOME (`A09`), then PARK (`A10`).

## 2. Check

```bash
python3 -m onstep_adapter.tools.indi_capture check capture.jsonl
python3 -m onstep_adapter.tools.indi_capture summary capture.jsonl     # operations
python3 -m onstep_adapter.tools.indi_capture calibrate capture.jsonl   # SimConfig values
```

Each command takes one or more files (for example two consecutive days) and
handles every session in them separately. `check` needs no mock. Exit status
1 means the recorded mount contradicted an assumption.

| ID | Assumption | Source |
| --- | --- | --- |
| A01 | No SET HOME, SET PARK or SYNC INDI write | BEHAVIOR 4.1, 4.12 |
| A02 | No `:hQ#`, `:hF#`, `:CS#`, `:CM#` on the LX200 link | BEHAVIOR 1 |
| A03 | UNPARK starts tracking until TRACK_OFF | BEHAVIOR 4.9, MOCK |
| A04 | ABORT while moving: stationary `:GU#` after 1.40-1.50 s | BEHAVIOR 4.8, `abort_settle_s` |
| A05 | ABORT acknowledged within 0.2 s | BEHAVIOR 4.8 |
| A06 | ABORT leaves both manual-motion vectors all Off | BEHAVIOR 4.8 |
| A07 | Goto: coordinates Busy during the slew, then Idle (spec) or Ok (mock) | BEHAVIOR 4.2 |
| A08 | Goto arrival starts tracking; status can lead coordinates | BEHAVIOR 5 |
| A09 | GO HOME ends at DEC +90, RA = LST + 6 h, with `H` | BEHAVIOR 2.3, 4.11 |
| A10 | PARK ends PARKED with `PARK=On` at the stored pose | BEHAVIOR 2.4, 4.10 |
| A11 | Stationary with tracking OFF: RA advances at the sidereal rate | BEHAVIOR 2.2 |
| A12 | OnStep Status republish period (mock 0.25 s) | `status_period_s` |
| A13 | Goto slew speed (mock 4 deg/s) | `slew_deg_per_s` |
| A14 | Manual motion needs an OFF re-arm; a repeated On starts nothing | BEHAVIOR 4.6 |
| A15 | TRACK_OFF stops tracking and moves nothing | BEHAVIOR 4.7 |
| A16 | `:GU#` letters are the ones the mock models | BEHAVIOR 6 |

## 3. Validate the mock

```bash
python3 -m onstep_mock.validate capture.jsonl --report report.md
```

The client writes of the capture are replayed, in order and with their
recorded spacing, against a fresh mock that starts at the recorded site,
pose and park state. Before a write that the real mount received while
stationary, the replay waits until the mock is stationary too. The mock's
answers are recorded through the same proxy and checked the same way. The
report lists:

- every assumption where the real mount and the mock disagree, including
  measured values (abort settle, slew speed, final coordinate state, ...);
- an operation-by-operation diff (`trace_compare`) of the `:GU#` motion
  states and coordinate states;
- INDI properties whose definition differs, or that only the mock publishes;
- writes the mock rejected but the real driver accepted;
- the measured `SimConfig` values.

`--calibrated` configures the mock with the measured values first, which
separates behavioral differences from differences in speed and timing.
`--speed 5` replays five times faster (times are scaled back before they are
compared; latencies that do not scale are then left out).

## 4. Consolidate a log for reading

```bash
python3 -m onstep_adapter.tools.trace_consolidate capture-2026-11-02.jsonl --text
```

A stationary mount repeats the same status several times a second. The
consolidated log (`capture-2026-11-02.consolidated.jsonl`, plus a readable
`.consolidated.txt` with `--text`) writes each run of identical consecutive
entries of one property as its first entry with `repeat`, `t_end`, `utc_end`
and `duration_s`, for example:

```text
21:40:44.911-21:40:45.657     0.75 s x4     c1 recv    OnStep Status [Ok] :GU# return=nNpEW060 Error=None
```

Entries of other properties in between do not end a run; a client write, a
public adapter call or a new session ends every run. `--tolerance 0.001`
also merges entries whose numbers drift (coordinates with tracking OFF) and
keeps the last values as `values_end`. The raw log stays untouched: `check`,
`validate` and `trace_compare` refuse a consolidated log, because update
rates and timings are what they measure. Works on adapter traces too.

## 5. Turn a recording into an acceptance test

Copy the capture into `tests/acceptance/recordings/` with a descriptive name.
`tests/acceptance/test_recorded_behavior.py` replays every recording there
and fails with the report when the mock differs. Then decide per difference:

- the spec text was wrong: correct `ONSTEPX_LX200_INDI_BEHAVIOR.md` and
  `docs/MOCK.md`;
- the mock was wrong: change `onstep_mock` (often only a `SimConfig`
  default, see `calibrate`) until the test passes;
- the recorded mount misbehaved (`FAIL` on the real side): keep the recording
  out of the suite and investigate the mount.

The repository holds one seed recording made from the mock itself
(`mock-seed-unpark-axis-abort-home-park.jsonl`, regenerated with
`tests/acceptance/record_mock_seed.py`). It proves the pipeline end to end; it
says nothing about the real mount until hardware recordings are added.
