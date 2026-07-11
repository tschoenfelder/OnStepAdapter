# OnStepAdapter 0.3.2

Version `0.3.2` closes GitHub issue #3.

## Safety

Some OnStep firmware resumes sidereal tracking immediately after `:hR#`
unpark. The adapter now tracks whether the caller explicitly requested
tracking. If live `:GU#` status reports tracking while no such request is
active, the adapter sends the verified disable sequence and only then reports
the resulting state.

`unpark()` now guarantees that a successful return leaves the mount unparked
and not tracking. `enable_tracking()` marks tracking as caller-authorized;
`stop()`, `park()`, `unpark()`, and verified disable clear that authorization.

## Location Helpers

The public API now exports:

```python
from onstep_adapter import haversine_distance_m, round_lx200_site_degrees
```

Use these when comparing full-precision configured observer coordinates with
OnStep/LX200 site register readback. OnStep stores site coordinates at
arcminute precision, so exact float comparison is the wrong test after a sync.

## Packaging

This release keeps the `0.3.1` namespace fix: the wheel ships only the
`onstep_adapter` package and no top-level `smart_telescope` package.
