# OnStepAdapter 0.3.4

Version `0.3.4` closes GitHub issue #7 / REQ-ST-010.

## Rate-Selectable Timed Moves

`move_ra_timed()` and `move_dec_timed()` now accept an optional per-call
`rate_preset`:

```python
client.mount.move_ra_timed("east", 500, mode="manual", rate_preset=4)
client.mount.move_dec_timed("south", 500, mode="manual", rate_preset=6)
```

When supplied, `rate_preset` must be an integer from `0` through `9`. The
adapter sends `:R0#` through `:R9#` instead of the mode default rate command.
Without a preset, existing behavior remains unchanged:

- `mode="guide"` selects `:RG#`;
- `mode="center"` and `mode="manual"` select `:RC#`.

The preset is scoped to one bounded move. The adapter still sends the matching
directional stop and restores guide rate afterward.

## Safety

Manual jog gating from `0.3.3` is unchanged: manual timed moves require
tracking off, may run at confirmed mechanical HOME, skip projected RA/DEC target
validation, and still honor mechanical safety blockers, OnStep fault/limit
state, duration bounds, and the motion lock.
