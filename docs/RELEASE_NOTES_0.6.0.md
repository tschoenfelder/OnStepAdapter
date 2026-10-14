# OnStepAdapter 0.6.0

## Added

- Caller-calibrated RA and DEC guide corrections expressed in arcseconds while
  sidereal tracking remains active.
- Public `IndiGuideCalibration` and `IndiGuideCorrectionResult` types.
- `guide_correction_ra()` and `guide_correction_dec()` mount methods, plus
  runtime and per-call calibration selection.

Angular corrections are converted to 20-5000 ms pulses and execute through
the physically validated 0.5.0 timed-guide controller. All status, authority,
meridian, fault/limit, chunking, tracking-preservation and OnStep `G`-flag
completion checks remain in force. The application still owns calibration,
fresh-frame measurement and convergence.

## Fixed

- Persistent switch writes now return immediately when the current `Ok` INDI
  vector already carries the requested state.
- `unpark()` no longer sends `TRACK_OFF` or waits for a redundant status echo
  when its fresh post-UNPARK OnStep status already confirms stationary,
  non-tracking operation.

These changes resolve #21 and #17. The existing #19 timed-pulse behavior and
its physical acceptance evidence remain unchanged and fully covered.
