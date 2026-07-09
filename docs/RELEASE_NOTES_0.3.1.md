# OnStepAdapter 0.3.1

## Packaging Fix

Version `0.3.0` shipped a regular top-level `smart_telescope` package inside
the wheel. That collided with existing SmartTScope deployments because Python
does not merge regular packages across `sys.path`; whichever
`smart_telescope` package resolved first owned the entire namespace.

Version `0.3.1` fixes this by making the standalone SDK importable entirely
through `onstep_adapter`:

```python
from onstep_adapter import OnStepClient, OnStepSafetyConfig
from onstep_adapter.ports.mount import MountPosition, MountState
```

The wheel no longer contains any `smart_telescope/*` files.

## Compatibility Note

Applications should import this SDK from `onstep_adapter`. SmartTScope should
keep using its own internal `smart_telescope` modules. Installing
`onstep-adapter` beside SmartTScope no longer shadows SmartTScope's package.

## Validation

- Full unit suite passes after the namespace move.
- Wheel inspection verifies that no `smart_telescope/` paths are present.
- Public imports continue to expose `OnStepClient`, `OnStepMount`,
  `OnStepFocuser`, `OnStepSafetyConfig`, and the structured result types from
  `onstep_adapter`.
