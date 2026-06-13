# OnStep Adapter SDK 0.1.0

## Release Scope

- Distribution: `onstep-adapter`
- Import: `onstep_adapter`
- Python: 3.13+
- Runtime dependency: `pyserial>=3.5`
- Public interfaces: `OnStepClient`, `OnStepMount`, `OnStepFocuser`,
  `OnStepSafetyConfig`, structured connection/move/status results, and
  structured safety errors.

## Validation Evidence

Mocked/local:

- application handoff finishes the active frame, flips, reacquires, and resumes;
- inclusive `+5 deg` hard stop blocks another exposure;
- mount/focuser commands serialize on one bus;
- emergency focuser stop bypasses the busy command lock;
- repeated client close is safe.

Supervised hardware:

- `watched_adapter_normal_meridian_flip`, run 2026-06-10 19:43 local:
  notification at `1.998 deg`, 5-second frame completion, west-to-east pier
  transition, exact target reacquisition, tracking resumed, final PARK.
- `watched_onstep_focuser_smoke`, run 2026-06-10 20:18 local:
  reversible 100-step move, interrupted 500-step move, exact cleanup, mount
  remained PARKED at every checkpoint.

Wheel validation:

- clean isolated installation succeeded;
- public version and distribution metadata both report `0.1.0`;
- fake serial shared-client connection, focuser status, absolute move, stop,
  and idempotent close passed;
- wheel contains 18 entries and excludes SmartTScope application, API, camera,
  workflow, Astropy, and FastAPI modules.

Final artifact:

- `dist/onstep_adapter-0.1.0-py3-none-any.whl`
- size: `39224` bytes
- SHA-256: `bb5f17573a3d760c9c3a6d48b6a8ffc7fdf8601f06779c1b4d31ba2a1e802a49`

Build command:

```bash
python -m pip wheel . --no-deps --no-build-isolation --wheel-dir dist
```

Python 3.13+ is required for installation. A Python 3.12 build host may build
the pure-Python artifact with `--ignore-requires-python`, but the resulting
wheel retains `Requires-Python: >=3.13`.

## Hardware Commands

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite watched_adapter_normal_meridian_flip \
  --include-watched --trust-system-time --allow-broad-onstep-limits \
  --goto-timeout-s 120 --meridian-watch-timeout-s 600 \
  --meridian-watch-poll-s 1 --meridian-flip-timeout-s 120 \
  --home-route-timeout-s 120 --home-route-poll-s 2 \
  --final-park-timeout-s 120 --final-park-poll-s 2
```

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite watched_onstep_focuser_smoke \
  --include-watched --trust-system-time --allow-broad-onstep-limits \
  --focuser-smoke-delta 100 --focuser-stop-delta 500 \
  --focuser-move-timeout-s 30 --focuser-poll-s 0.25
```
