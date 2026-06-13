# Hardware Test Log

Append one entry per hardware test run. Never delete entries — mark superseded
runs as VOID with a reason. This log is the evidence record for R7-004 and the
release go/no-go checklist.

**Required evidence items (R7-004):**

| ID    | Test |
|-------|------|
| E-001 | STOP during mount slew |
| E-002 | STOP during focuser move |
| E-003 | Shutdown during active motion (mount or focuser) |
| E-004 | Reconnect after power-cycle (mount + focuser) |
| E-005 | Full setup check (all steps pass) |
| E-006 | Full observing workflow (GoTo → solve → focus → stack → save) |

When all six evidence items have at least one **PASS** entry, R7-004 is satisfied.

---

## Log entries

<!--
Copy the template below for each new run. Fill in all fields.
Use evidence IDs from the table above. Multiple IDs if one session covers several.
-->

---

### Entry template

```
---
Date:          YYYY-MM-DD HH:MM (local)
Evidence IDs:  E-001, E-002, ...
Pi serial:     [from /proc/cpuinfo or raspi-config]
OS build:      Raspbian Trixie 64 / [version]
App commit:    [git rev-parse --short HEAD]
OnStep FW:     [from :GVP# response]
Camera model:  [from /api/cameras list]
---

Test: [one-line description, e.g. "STOP during GoTo slew to M42"]

Steps performed:
1. [...]
2. [...]

Observed behaviour:
- [what actually happened]

Result: PASS / FAIL / VOID ([reason if VOID])

App log extract (key lines):
```
[paste relevant log lines here]
```

Notes:
[anything worth remembering — temperature, seeing, cable issues, etc.]
---
```

---

<!-- Add entries below this line, newest first -->
