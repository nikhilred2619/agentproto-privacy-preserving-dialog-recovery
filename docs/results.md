# Results

All numbers below come from one run of `agentproto-recovery all --out results`
on 2026-10-11 (Python 3.13.16, SQLite 3.45.1, cryptography 50.0.1, Linux
x86_64 container, 2 vCPU). Full tables: `results/summary.md` (generated);
raw data: `results/*.json`, `results/*.csv`.

## Test suite

`pytest`: **181 passed** in about 11 s on Python 3.13. Repeated four more
times on 3.13 (including twice from a fresh clone in a new virtualenv) and
once each on Python 3.11 and 3.12, all with the same result. No flaky tests
were observed. Test counts per file are visible with `pytest -v`.

## E1 Scenario matrix

134 cells (22 scenarios × 6 modes + 2 negative-control cells), 3 repeats each.

* Every cell returned the same outcome on all 3 repeats
  (`cells_with_nondeterministic_outcome: {}`).
* Incorrect associations outside the negative control: **0**.
  Negative control (T06, unsafe guess): **1** — the gateway resumed the
  sibling dialog with a near-identical task.
* Recoverable modes (`global-*`, `scoped-store`, both ticket modes) returned
  `OK` for T03, T04, T14, T14b, T15, T16 and the expected explicit failure
  code for T08-T13 and T19-T20.
* `scoped-endpoint-noticket` returned `MAPPING_NOT_FOUND` for every scenario
  that involved the replacement gateway.
* `global-endpoint` was the only mode that recovered after all gateway-side
  state was lost (T05, T06, T16b), because the identifier itself is the
  recovery material.
* T07: in both global modes the second tenant's establishment with the same
  identifier string was refused (`INVALID_STATE`); in scoped modes both
  tenants' dialogs were established and resumed correctly.
* T12b: rolling back a validly tagged store row passed the integrity check and
  was rejected as `STALE_REFERENCE`.
* T14 / T14b: the external tool executed exactly once in every recoverable
  mode; an unknown outcome came back as `OUTCOME_UNKNOWN` and was settled by
  reconciliation without re-execution.
* T18: in shared-store modes exactly one of two racing gateways succeeded
  (`CONCURRENT_RECOVERY|OK`); with independent gateways both succeeded with
  the same epoch and B advanced once.

## E2 Seeded fault schedule

3 seeds × 90 dialogs = 270 attempts per mode.

| mode | recovered | explicit failures | incorrect assoc. | safety violations | recovery rate where permitted |
|---|---|---|---|---|---|
| global-store | 59 | 211 | 0 | 0 | 0.557 |
| global-endpoint | 106 | 164 | 0 | 0 | 1.000 |
| scoped-store | 59 | 211 | 0 | 0 | 0.557 |
| scoped-endpoint-noticket | 0 | 270 | 0 | 0 | 0.000 |
| scoped-endpoint-ticket | 79 | 191 | 0 | 0 | 0.745 |
| scoped-endpoint-medticket | 79 | 191 | 0 | 0 | 0.745 |
| scoped-store + unsafe guess | 75 | 195 | **23** | **23** | 0.708 |

Reading this table: "recovery rate where permitted" counts the faults that
should not by themselves forbid recovery (none, mapping_deleted,
mapping_tampered, ticket_lost). Store modes lose mapping_deleted and
mapping_tampered; ticket modes lose ticket_lost; `global-endpoint` loses none
of them because its replacement gateway ignores the store and A never had a
ticket to lose. The point of the table is the zero columns: across 1,620
attempts in six modes no attempt resumed the wrong dialog or resumed a dialog
that was revoked, expired, terminated, stale, or presented by another
principal. The unsafe-guess control shows what happens without that
discipline.

## E3 Privacy (T17)

50 dialogs per mode, each resumed once through G2.

| mode | domains holding a common value (max) | A~B identifier join | GW1~GW2 join | distinct A-B shared values / dialog | gateway links both sides | order-based A~B join |
|---|---|---|---|---|---|---|
| global-store | 3 of 3 | 1.0 | n/a | 1 | 1.0 | 1.0 |
| global-endpoint | 4 of 4 | 1.0 | 1.0 | 1 | 1.0 | 1.0 |
| scoped-store | 2 of 3 | 0.0 | n/a | 0 | 1.0 | 1.0 |
| scoped-endpoint-noticket | 3 of 4 | 0.0 | 1.0 | 0 | GW1 1.0, GW2 0.0 | 1.0 |
| scoped-endpoint-ticket | 4 of 4 | 1.0 | 1.0 | 2 | 1.0 | 1.0 |
| scoped-endpoint-medticket | 3 of 4 | 0.0 | 1.0 | 0 | 1.0 | 1.0 |

What this shows:

* Scoped identifiers remove the identifier-equality join between the two
  endpoints only when the recovery material never reaches both endpoints
  (`scoped-store`, `scoped-endpoint-medticket`).
* A ticket sealed by B and held by A is opaque to A but still a value both
  endpoints hold. Colluding endpoints join on it exactly as they would on a
  global id. The difference is lifetime: it changed on resumption (2 distinct
  shared values per dialog) where a global id stays one value.
* Every gateway that forwards a dialog can link both sides. Independent
  gateway operators that hand a dialog over can join their logs in every
  configuration that recovers.
* With sequential establishment, event order alone links A and B in 100% of
  dialogs in every mode. Opaque identifiers do not make dialogs unlinkable.

## E4 Performance

5 runs × 200 sequential recoveries + 200 concurrent recoveries (4 threads)
per mode, after a discarded warm-up. Median across runs of per-run statistics.

| mode | establish med (ms) | lookup med (ms) | recovery mean / med / p95 (ms) | concurrent recoveries/s | ratio vs baseline (recovery median) |
|---|---|---|---|---|---|
| global-store | 2.53 | 0.038 | 2.72 / 2.55 / 3.88 | 485 | baseline |
| global-endpoint | 2.32 | 0.066 | 1.95 / 1.82 / 3.39 | 720 | baseline |
| scoped-store | 2.40 | 0.054 | 2.51 / 2.30 / 4.11 | 569 | 0.90 vs global-store |
| scoped-endpoint-ticket | 2.47 | 0.043 | 2.40 / 2.13 / 4.08 | 733 | 1.17 vs global-endpoint |
| scoped-endpoint-medticket | 2.41 | 0.037 | 2.47 / 2.15 / 4.05 | 775 | 1.18 vs global-endpoint |

Store size after 400 dialogs: 400 rows in every mode; 98-131 KB depending on
mode (`benchmark_summary.json`). Endpoint modes' G1 rows are smaller because
`last_result` is empty until a resumption happens through that gateway.

Interpretation, kept deliberately narrow:

* No incorrect associations and no non-OK recoveries in any benchmark run.
* The scoped-vs-global ratios (0.90 and 1.17-1.18) are smaller than the
  run-to-run spread of the same mode (e.g. `global-endpoint` per-run medians
  ranged 1.67-2.95 ms). On this machine the measurement cannot distinguish
  the cost of scoped identifiers from noise.
* The visible structural difference is between store-based and
  endpoint-based recovery: store-based recovery does two extra durable writes
  (claim and finalize), which matches its higher median and lower concurrent
  throughput. This was not isolated by a separate ablation.
* Absolute latencies are dominated by SQLite commits with
  `synchronous=FULL` on this container's filesystem and say nothing about a
  networked deployment.

## Incomplete or not attempted

* No real network transport (HTTP/QUIC); no multi-process or multi-host runs.
* No ablation without the epoch field (F5 in the gap analysis is an
  observation, not a measured failure).
* Order-based linkage measured only for a sequential workload.
* No protocol translation (B5-3) and no multi-hop chains beyond A → G → B.
