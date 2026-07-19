# OnStepAdapter 0.3.3

Version `0.3.3` closes GitHub issue #5 / REQ-ST-009.

## Manual Timed Jog Mode

`move_ra_timed()` and `move_dec_timed()` now accept:

```python
client.mount.move_ra_timed("east", 250, mode="manual")
client.mount.move_dec_timed("north", 250, mode="manual")
```

Manual mode is for deliberate non-astronomical terrestrial jog controls with
tracking off. It may run at confirmed mechanical HOME and skips projected
RA/DEC target validation because HOME RA/DEC readback is not a trustworthy sky
target context.

The mode remains bounded and safety-gated:

- normal motion preflight still runs;
- `motion_refused` still blocks the jog;
- OnStep fault/limit, park-failed, untrusted mechanical authority, hard
  counterweight limit, duration limits, and the motion lock are still enforced;
- tracking must already be off;
- a matching directional stop is always sent and guide rate is restored.

`mode="manual"` is intentionally rejected by angular `move_ra()` and
`move_dec()` because those APIs describe sky offsets and require astronomical
context.
