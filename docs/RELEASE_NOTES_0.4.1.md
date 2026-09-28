# OnStepAdapter 0.4.1

This maintenance release resolves GitHub issue #16.

## Mechanical State

- `park()` directly requests the stored PARK position. HOME is not required.
- Repeated PARK calls succeed idempotently when recent raw OnStep status
  already proves the requested state.
- Actual PARK transitions still require fresh raw status.

## Tracking Authority

- Strict tracking authority remains the default.
- `tracking_authority_policy="controller_managed"` explicitly allows an
  application to delegate time/site and HOME authority. Missing local
  authorities are returned as warnings; PARK, slew, fault, limit, and hard
  stop blockers remain enforced.
- Connecting an unsynchronized client does not disturb existing tracking.

No direct serial fallback or persistent service is introduced.
