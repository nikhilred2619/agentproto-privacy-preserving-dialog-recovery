# Generated result tables

Source: `results/*.json`. Regenerate with `python experiments/render_results.py`.

## Environment

```json
{
  "agentproto_recovery": "0.1.0",
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
  "timestamp_utc": "2026-10-11T09:37:43Z",
  "wall_seconds": 88.48
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

Scenario summary:

```json
{
  "cells": 134,
  "cells_with_nondeterministic_outcome": {},
  "incorrect_associations": 1,
  "incorrect_associations_excluding_negative_control": 0,
  "repeats_per_cell": 3
}
```

## Seeded fault schedule

Config: `{"dialogs_per_run": 90, "faults": ["none", "mapping_deleted", "mapping_tampered", "ticket_lost", "revoked", "expired", "terminated", "stale_replay", "wrong_principal"], "must_fail": ["expired", "revoked", "stale_replay", "terminated", "wrong_principal"], "seeds": [1, 2, 3]}`

| mode | attempts | recovered | explicit failures | incorrect assoc. | safety violations | recovery rate (faults not in must_fail) |
|---|---|---|---|---|---|---|
| global-store | 270 | 59 | 211 | 0 | 0 | 0.5566 |
| global-endpoint | 270 | 106 | 164 | 0 | 0 | 1.0 |
| scoped-store | 270 | 59 | 211 | 0 | 0 | 0.5566 |
| scoped-endpoint-noticket | 270 | 0 | 270 | 0 | 0 | 0.0 |
| scoped-endpoint-ticket | 270 | 79 | 191 | 0 | 0 | 0.7453 |
| scoped-endpoint-medticket | 270 | 79 | 191 | 0 | 0 | 0.7453 |
| scoped-store+UNSAFE_GUESS | 270 | 75 | 195 | 23 | 23 | 0.7075 |

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
| global-store | 5 | 200 | 2.5296 | 0.0375 | 0.1007 | 2.7172 | 2.5481 | 3.8839 | 485.14 | 400 | 131072 |  |  | 0 | 0 |
| global-endpoint | 5 | 200 | 2.3152 | 0.0663 | 0.117 | 1.9483 | 1.8245 | 3.389 | 719.66 | 400 | 98304 |  |  | 0 | 0 |
| scoped-store | 5 | 200 | 2.4022 | 0.0542 | 0.1004 | 2.5078 | 2.3014 | 4.1098 | 568.92 | 400 | 126976 | global-store | 0.903 | 0 | 0 |
| scoped-endpoint-ticket | 5 | 200 | 2.4677 | 0.0428 | 0.084 | 2.4017 | 2.1281 | 4.0815 | 732.7 | 400 | 98304 | global-endpoint | 1.166 | 0 | 0 |
| scoped-endpoint-medticket | 5 | 200 | 2.4102 | 0.0371 | 0.0707 | 2.4685 | 2.1538 | 4.0539 | 774.8 | 400 | 98304 | global-endpoint | 1.18 | 0 | 0 |

Per-run recovery medians (ms), to show run-to-run spread:

- global-store: [2.5722, 2.325, 2.5481, 2.3755, 2.6377]
- global-endpoint: [1.8245, 2.5817, 2.9531, 1.6745, 1.6873]
- scoped-store: [2.4266, 2.1376, 2.3014, 2.1086, 2.3237]
- scoped-endpoint-ticket: [2.0181, 2.5926, 2.1408, 2.1281, 2.0175]
- scoped-endpoint-medticket: [2.1538, 2.7217, 2.2852, 1.7718, 2.0833]
