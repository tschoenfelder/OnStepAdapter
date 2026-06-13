# OnStep Adapter SDK 0.2.0 Release Gate

## Protection Model

SmartTScope normally requests a meridian flip at `+2 degrees` and stops
tracking at `+5 degrees`. Those are the operational imaging limits while the
Raspberry is alive.

Stock OnStepX uses a different final fallback after the field-proven W-to-E
flip: pier EAST positive-HA tracking continues to Axis-1 maximum. With the
current configuration that is `180 degrees`, approximately HA `+12 h`.
This fallback may qualify for unattended operation only after OnStep's stop and
the complete physical path have been proven safe on the exact mount.

The optional SmartTScope dual-pier firmware extension remains supported. Either
of these proof modes can satisfy the release gate:

- dual-pier firmware stop proven at `+5 degrees`; or
- stock Axis-1 maximum stop proven and explicitly confirmed physically safe.

## Mock And Non-Moving Gates

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite mocked_onstep_axis1_fallback,onstep_axis1_fallback_preflight \
  --trust-system-time \
  --allow-broad-onstep-limits
```

The mock must prove both the autonomous-stop and raw-backstop classifications.
The live preflight sends no movement. It requires PARKED state, matching
Raspberry/OnStep time and location, readable firmware and Axis-1 limits, no
active OnStep fault, and automatic firmware flip disabled.

It prints the predicted pier-EAST checkpoints at DEC `+85 degrees`, including
RA, HA, ALT, and AZ.

## Staged Stock-Firmware Proof

Run only with continuous line of sight:

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite watched_onstep_axis1_fallback_stop \
  --include-watched \
  --trust-system-time \
  --allow-broad-onstep-limits \
  --axis1-fallback-checkpoints-deg 60,120,150,170,178.5 \
  --axis1-fallback-dec-deg 85 \
  --axis1-fallback-backstop-deg 0.25 \
  --axis1-fallback-watch-timeout-s 480 \
  --meridian-watch-poll-s 1 \
  --goto-timeout-s 120 \
  --home-route-timeout-s 120 \
  --home-route-poll-s 2 \
  --final-park-timeout-s 120 \
  --final-park-poll-s 2
```

The path is:

1. PARK to HOME.
   After `H` first appears, require three consecutive HOME-idle samples:
   `:GU#` reports HOME/not-slewing and `:D#` reports no active goto. This avoids
   racing OnStep's HOME completion with the first checkpoint goto.
2. Force pier EAST.
3. Move to Axis-1 `60`, `120`, `150`, `170`, and `178.5 degrees`.
4. Require ENTER before every slew and a physical-clearance confirmation after
   every arrival. Tracking is disabled and verified at each checkpoint before
   the confirmation prompt, so the mount holds position while the operator
   inspects it. Raw `:Sr#`, `:Sd#`, and `:MS#` replies are printed before
   polling. A rejected goto or a goto that remains stationary at HOME aborts
   quickly and performs a verified HOME-to-PARK cleanup. `:MS#` replies use
   the OnStepX mapping; in particular reply `5` means an existing goto is
   still active, not the legacy `not aligned` interpretation.
5. After confirming the `178.5 degree` pose, require a separate final ENTER to
   arm natural tracking. The command prints the estimated wait to both the
   firmware limit and raw backstop before accepting this confirmation. This
   certification phase deliberately uses the watched raw `:Te#` path, bypassing
   SmartTScope's `+5 degree` operational guard, and requires live `:GU#`
   tracking confirmation before the watch begins.
6. Observe OnStep stopping just beyond the configured Axis-1 maximum.
7. If tracking reaches `180.25 degrees`, send raw `:Td#` and `:Q#` and record
   `SAFETY_NOT_PROVEN`.
8. After a successful proof, require explicit endpoint safety confirmation,
   then route HOME to PARK.

ESC always sends an immediate stop and suppresses every later HOME/PARK
movement. `Ctrl-C` follows the same emergency-stop path. Prompt input and the
live ESC watcher are never active concurrently, so an ENTER cannot be consumed
by the watcher. The preferred pier policy is restored to BEST without
commanding motion.

PASS requires pier EAST throughout, autonomous OnStep evidence before the
backstop, operator confirmation of all poses, and final live `:GU# P`.

## Proof And Final Audit

The proof record is tied to firmware product/version/date, all four axis-limit
readbacks, west-meridian readback, and observer latitude/longitude. A change to
any of these invalidates the proof.

```bash
python -m smart_telescope.tools.onstep_adapter_tdd \
  --port /dev/ttyUSB_ONSTEP0 \
  --suite onstep_unattended_release_audit \
  --trust-system-time \
  --allow-broad-onstep-limits
```

After a valid proof the audit reports:

- `operational_stop_deg=5`;
- `firmware_fallback_type=axis1_max`;
- `firmware_fallback_deg=180`;
- `firmware_fallback_proven=true`;
- `firmware_fallback_physically_safe=true`;
- `unattended_tracking_allowed=true`.

The stock fallback is not equivalent to a `+5 degree` stop. It is accepted only
as last-resort crash prevention because its much later endpoint was physically
verified safe on this exact rig.
