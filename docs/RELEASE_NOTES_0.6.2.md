# OnStepAdapter 0.6.2

This patch release makes finite RA/DEC movement target bounds explicit runtime
configuration.

## Added

- A `[movement]` TOML section with inclusive `min_alt_deg`, `min_dec_deg` and
  `max_dec_deg` target limits.
- Default movement limits of altitude `>= -2 degrees` and DEC
  `-80..+80 degrees`.
- Pre-command projected-altitude validation whenever an authoritative hour
  angle is available.

Existing configuration files remain compatible and receive the same defaults.
HOME-neutral terrestrial movement without astronomical time authority retains
the configured DEC gate because HOME coordinates are diagnostic rather than
astronomical truth.

## Validation

- Exact inclusive altitude and DEC boundaries, rejected out-of-range targets,
  configuration validation and legacy defaults are covered by unit tests.
- The full suite passes with 219 tests and 64 subtests.
