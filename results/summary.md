# Generated result tables

Source: `results/*.json`. Regenerate with `python experiments/render_results.py`.

## Environment

```json
{
  "agentproto_recovery": "0.2.0",
  "args": {
    "bench_dialogs": 400,
    "command": "all",
    "fault_dialogs": 90,
    "out": "results",
    "privacy_dialogs": 50,
    "repeats": 3,
    "runs": 5,
    "seeds": [
      1,
      2,
      3
    ],
    "threads": 4
  },
  "cpu_count": 2,
  "cryptography": "50.0.1",
  "machine": "x86_64",
  "platform": "Linux-6.18.44-fc-v114-x86_64-with-glibc2.39",
  "python": "3.13.16",
  "sqlite": "3.45.1",
  "timestamp_utc": "2026-10-11T10:07:13Z",
  "wall_seconds": 89.93
}
```

## Scenario matrix (code returned to Agent A)

✗WRONG = resumed a dialog other than the ground-truth one.

| scenario | global-store | global-endpoint | scoped-store | scoped-endpoint-noticket | scoped-endpoint-ticket | scoped-endpoint-medticket | scoped-store+UNSAFE_GUESS |
|---|---|---|---|---|---|---|---|
| T01 | OK | OK | OK | OK | OK | OK |  |
| T02 | OK | OK | OK | OK | OK | OK |  |
| T03 | OK | OK | OK | OK | OK | OK |  |
| T04 | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T05 | MAPPING_NOT_FOUND | OK | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND |
| T06 | MAPPING_NOT_FOUND | OK | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | OK ✗WRONG |
| T07 | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T08 | STALE_REFERENCE | STALE_REFERENCE | STALE_REFERENCE | MAPPING_NOT_FOUND | STALE_REFERENCE | STALE_REFERENCE |  |
| T09 | AUTHZ_EXPIRED | AUTHZ_EXPIRED | AUTHZ_EXPIRED | MAPPING_NOT_FOUND | AUTHZ_EXPIRED | AUTHZ_EXPIRED |  |
| T10 | AUTHZ_REVOKED | AUTHZ_REVOKED | AUTHZ_REVOKED | MAPPING_NOT_FOUND | AUTHZ_REVOKED | AUTHZ_REVOKED |  |
| T11 | PRINCIPAL_MISMATCH | PRINCIPAL_MISMATCH | PRINCIPAL_MISMATCH | MAPPING_NOT_FOUND | PRINCIPAL_MISMATCH | PRINCIPAL_MISMATCH |  |
| T12 | MAPPING_INTEGRITY_FAILURE | DIALOG_NOT_FOUND | MAPPING_INTEGRITY_FAILURE | NOT_APPLICABLE | TICKET_INVALID | TICKET_INVALID |  |
| T12b | STALE_REFERENCE | NOT_APPLICABLE | STALE_REFERENCE | NOT_APPLICABLE | NOT_APPLICABLE | NOT_APPLICABLE |  |
| T13 | AUTHZ_UNAVAILABLE | AUTHZ_UNAVAILABLE | AUTHZ_UNAVAILABLE | MAPPING_NOT_FOUND | AUTHZ_UNAVAILABLE | AUTHZ_UNAVAILABLE |  |
| T14 | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T14b | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T15 | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T16 | OK | OK | OK | MAPPING_NOT_FOUND | OK | OK |  |
| T16b | MAPPING_NOT_FOUND | OK | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND | MAPPING_NOT_FOUND |  |
| T18 | CONCURRENT_RECOVERY|OK | OK|OK | CONCURRENT_RECOVERY|OK | MAPPING_NOT_FOUND|MAPPING_NOT_FOUND | OK|OK | OK|OK |  |
| T19 | DIALOG_TERMINATED | DIALOG_TERMINATED | DIALOG_TERMINATED | MAPPING_NOT_FOUND | DIALOG_TERMINATED | DIALOG_TERMINATED |  |
| T20 | STORE_UNAVAILABLE | STORE_UNAVAILABLE | STORE_UNAVAILABLE | STORE_UNAVAILABLE | STORE_UNAVAILABLE | STORE_UNAVAILABLE |  |
| T21 | OK | OK | OK | MAPPING_NOT_FOUND | TICKET_MISMATCH | TICKET_MISMATCH |  |
| T22 | IN_PROGRESS | IN_PROGRESS | IN_PROGRESS | IN_PROGRESS | IN_PROGRESS | IN_PROGRESS |  |
| T23 | SEQUENCE_CONFLICT | SEQUENCE_CONFLICT | SEQUENCE_CONFLICT | SEQUENCE_CONFLICT | SEQUENCE_CONFLICT | SEQUENCE_CONFLICT |  |

Scenario summary:

```json
{
  "cells": 152,
  "cells_with_nondeterministic_outcome": {},
  "incorrect_associations": 1,
  "incorrect_associations_excluding_negative_control": 0,
  "repeats_per_cell": 3
}
```

## Seeded fault schedule

Config: `{"dialogs_per_run": 90, "faults": ["none", "mapping_deleted", "mapping_tampered", "ticket_lost", "foreign_ticket", "revoked", "expired", "terminated", "stale_replay", "wrong_principal"], "must_fail": ["expired", "revoked", "stale_replay", "terminated", "wrong_principal"], "seeds": [1, 2, 3]}`

| mode | attempts | recovered | explicit failures | incorrect assoc. | safety violations | recovery rate (faults not in must_fail) |
|---|---|---|---|---|---|---|
| global-store | 270 | 70 | 200 | 0 | 0 | 0.6195 |
| global-endpoint | 270 | 113 | 157 | 0 | 0 | 1.0 |
| scoped-store | 270 | 70 | 200 | 0 | 0 | 0.6195 |
| scoped-endpoint-noticket | 270 | 0 | 270 | 0 | 0 | 0.0 |
| scoped-endpoint-ticket | 270 | 67 | 203 | 0 | 0 | 0.5929 |
| scoped-endpoint-medticket | 270 | 67 | 203 | 0 | 0 | 0.5929 |
| scoped-store+UNSAFE_GUESS | 270 | 78 | 192 | 13 | 13 | 0.6903 |

## Privacy / linkability (T17)

50 dialogs per mode, each resumed once through G2.

| mode | domains | max domains sharing one value | A~B id join | GW1~GW2 id join | distinct A-B shared values / dialog | gateway links up+down | order-based A~B join |
|---|---|---|---|---|---|---|---|
| global-store | A, B, GW1 | 3 | 1.0 | n/a | 1 | {"GW1": 1.0} | 1.0 |
| global-endpoint | A, B, GW1, GW2 | 4 | 1.0 | 1.0 | 1 | {"GW1": 1.0, "GW2": 1.0} | 1.0 |
| scoped-store | A, B, GW1 | 2 | 0.0 | n/a | 0 | {"GW1": 1.0} | 1.0 |
| scoped-endpoint-noticket | A, B, GW1, GW2 | 3 | 0.0 | 1.0 | 0 | {"GW1": 1.0, "GW2": 0.0} | 1.0 |
| scoped-endpoint-ticket | A, B, GW1, GW2 | 4 | 1.0 | 1.0 | 2 | {"GW1": 1.0, "GW2": 1.0} | 1.0 |
| scoped-endpoint-medticket | A, B, GW1, GW2 | 3 | 0.0 | 1.0 | 0 | {"GW1": 1.0, "GW2": 1.0} | 1.0 |

## Performance (median across runs of per-run statistic)

| mode | runs | recoveries/run | establish med ms | lookup med ms | lookup p95 ms | recovery mean ms | recovery med ms | recovery p95 ms | concurrent recoveries/s | store rows | store bytes | baseline | recovery ratio vs baseline | incorrect assoc. | non-OK |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| global-store | 5 | 200 | 2.4154 | 0.0378 | 0.0855 | 2.765 | 2.4596 | 4.5798 | 538.95 | 400 | 126976 |  |  | 0 | 0 |
| global-endpoint | 5 | 200 | 2.2634 | 0.0374 | 0.1066 | 2.3369 | 2.0613 | 3.5241 | 793.96 | 400 | 98304 |  |  | 0 | 0 |
| scoped-store | 5 | 200 | 2.4953 | 0.036 | 0.0666 | 2.3966 | 2.1407 | 3.7026 | 539.6 | 400 | 126976 | global-store | 0.87 | 0 | 0 |
| scoped-endpoint-ticket | 5 | 200 | 2.536 | 0.0523 | 0.1175 | 2.4002 | 2.3659 | 3.2285 | 680.46 | 400 | 98304 | global-endpoint | 1.148 | 0 | 0 |
| scoped-endpoint-medticket | 5 | 200 | 2.4432 | 0.0364 | 0.0809 | 2.7338 | 2.3832 | 3.6977 | 709.8 | 400 | 98304 | global-endpoint | 1.156 | 0 | 0 |

Per-run recovery medians (ms), to show run-to-run spread:

- global-store: [2.6548, 2.4596, 2.5632, 2.3602, 2.1587]
- global-endpoint: [2.0613, 2.8091, 2.1709, 1.6417, 2.035]
- scoped-store: [2.1407, 2.2856, 2.9799, 1.8224, 1.7733]
- scoped-endpoint-ticket: [2.3072, 2.5196, 3.6137, 2.3659, 1.9114]
- scoped-endpoint-medticket: [2.3832, 2.7685, 2.8307, 2.1974, 2.091]
