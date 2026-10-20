# OnStepAdapter 0.6.1

This patch release fixes stale INDI sessions reported in issue #23.

## Fixed

- Every unexpected background INDI reader exception or return now marks the
  transport failed and wakes all property waiters.
- `is_open` now includes reader-thread health instead of relying only on socket
  and close flags.
- Property waits and writes fail promptly after reader loss. The original
  exception is retained as the `ConnectionError` cause for diagnostics.
- Applications can recover with `close()` followed by `connect()` on the same
  client transport.
- Recovery closes only the application's socket. It does not send an INDI
  device `CONNECTION.DISCONNECT`, so the mount and other clients remain active.

## Validation

- Regression coverage forces an unexpected reader exception, verifies prompt
  waiter failure and confirms a clean reconnect to a second INDI session.
- The full suite passes with 215 tests and 64 subtests.
