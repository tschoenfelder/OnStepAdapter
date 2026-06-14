# Changelog

## 0.3.0 - 2026-06-14

- Added provenance-bearing local PARK records and safe `:hQ#` transactions.
- Removed undocumented `:GpA#` and `:GpD#` PARK readback probes.
- Added bounded independent RA and DEC guide/centering corrections.
- Added direction-specific application-supplied motion calibration.
- Added a supervised PARK-to-target axis-motion hardware smoke command.
- Added a supervised coordinate path proving independent RA `+1 h/-1 h` and
  DEC `+10/-10 degrees` GoTo movement with final HOME-to-PARK recovery.
- Physically validated both correction modes on the Terrans OnStep V4.

## 0.2.0 - 2026-06-13

- Added P0 mechanical-safety authority and fresh motion preflight.
- Added application-controlled meridian notification, flip, and inclusive hard stop.
- Added physically proven stock OnStep Axis-1 firmware fallback support.
- Added one shared serial client exposing mount and focuser interfaces.
- Added structured safety, status, connection, and focuser results.
- Added focused mock, protocol, safety, firmware-proof, and shared-bus tests.
- Validated with a Terrans OnStep V4 running OnStep 10.19d without changing
  `Config.h`.
