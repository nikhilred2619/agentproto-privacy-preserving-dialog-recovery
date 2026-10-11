# Experiment methodology

## What is compared

Six configurations (`evaluation.MODES`), defined by three choices:

* identifier scheme: one global id end to end, or a separate id per domain;
* whether the replacement gateway reads the failed gateway's store (same
  operator) or has only its own empty store (different operator);
* what, if anything, Agent A presents besides its own id: nothing, a ticket
  sealed by Agent B, or a ticket sealed by cooperating gateways.

`scoped-endpoint-noticket` is included as the configuration expected to fail;
`scoped-store` with `unsafe_guess_on_missing=True` is a negative control that
deliberately guesses.

All configurations run the same agent, gateway, store and authorization
code; only the configuration flags differ. Functional behaviour is therefore
equivalent except where the configuration removes recovery information.

## Ground truth (oracle)

`Deployment.establish` records which Agent B dialog was created for each
Agent A dialog. After every successful resumption the harness compares the
dialog B actually resumed (`AgentB.oracle_resumed`) with that record.
A mismatch is an *incorrect association*. The negative control shows the
oracle detects errors (T06: `OK ✗WRONG`; fault schedule: 23).

Oracle data is instrumentation. No component reads it.

## E1 Scenario matrix (`agentproto-recovery scenarios`)

25 scripted scenarios (T01-T20 except T17, plus T12b, T14b, T16b, and
the audit scenarios T21 foreign ticket, T22 reconciliation during a live tool
call, T23 reused sequence number with a different payload) × 6 modes, plus 2 negative-control cells, each repeated 3 times in fresh
temporary directories. The same functions are used by pytest
(`evaluation.SCENARIOS`). Output: `scenario_matrix.csv`,
`scenario_summary.json`. Determinism check: for each cell the set of distinct
`(code, correct_association, extra)` tuples across repeats must have size 1.

T17 (privacy) is E3. T18 (concurrency) and T22 (blocked tool call) use real
threads. T22 blocks the tool by wrapping `ExternalTool.execute` with an event
wait (test instrumentation only).

## E2 Seeded fault schedule (`agentproto-recovery faults`)

For each seed in {1, 2, 3} and each mode: 90 dialogs are established through
G1 by 5 users with repetitive task text (`job-0` … `job-6`). Each dialog gets
one fault drawn with `random.Random(seed)` from: none, mapping_deleted,
mapping_tampered (pointed at another dialog's B id), ticket_lost,
foreign_ticket (added in v0.2.0: A presents a valid ticket of another dialog
of the same user; ticket is `None` in modes without tickets), revoked,
expired, terminated, stale_replay, wrong_principal. Revoked/expired dialogs
get their own principal so the fault stays local. G1 is crashed, the clock
advanced 120 s, faults applied, then every dialog is resumed through G2.

Recorded per attempt: code, recovered, correct association, safety violation.
A *safety violation* is a success with a wrong association, or any success
for a fault in `MUST_FAIL` = {revoked, expired, terminated, stale_replay,
wrong_principal}. "Recovery rate where permitted" is successes over attempts
whose fault is not in `MUST_FAIL`; it is expected to be below 1.0 when the
fault destroyed the recovery material (e.g. mapping_deleted in store modes).

The schedule is deterministic given the seed. Identifier *values* are not
(they come from the OS CSPRNG on purpose), and no result depends on them.

## E3 Privacy (`agentproto-recovery privacy`)

Workload: 50 dialogs established sequentially through G1, one operation each,
G1 crashed, all resumed through G2, one more operation each.

Observer model: each administrative domain (A, B, GW1, GW2) is
honest-but-curious and retains every identifier value it sends or receives
(`ObservationLog`). Hops are assumed encrypted, so there is no network
observer. The harness then computes:

1. `max_domains_sharing_one_value`: for each dialog, the largest number of
   domains holding any single value of that dialog; maximum over dialogs.
2. `identifier_join_fraction_by_domain_pair`: fraction of dialogs for which
   two domains hold at least one identical value of that dialog (they can join
   their logs by equality).
3. Endpoint view: the A~B entry of (2).
4. `gateway_can_link_upstream_downstream`: fraction of dialogs where a gateway
   operator observed both the A-side and the B-side identifier.
5. One concrete linkage attack without identifiers:
   `order_based_A_B_join_accuracy_sequential_workload` pairs the k-th dialog
   first seen at A with the k-th dialog first seen at B.

Also reported: `mean_distinct_A_B_shared_values_per_dialog` (how many different
values A and B have in common per dialog over one resumption).

Limits: high-entropy identifier equality and one order-based attack only.
No timing, size, or content analysis. The sequential workload is the easiest
case for the order attack; with concurrent establishment it would be lower.
That was not measured.

## E4 Performance (`agentproto-recovery benchmark`)

Per run and mode, in a fresh temporary directory:

* establish 400 dialogs through G1, timing each (`time.perf_counter`);
* crash G1; time `store.get` for all 400 (mapping lookup);
* resume the first 200 sequentially through G2, timing each end to end
  (Agent A → G2 → B → G2 → A, including all SQLite commits);
* resume the remaining 200 concurrently with 4 threads, each with its own
  replacement gateway, distinct dialogs per thread; throughput = 200 / wall
  time;
* record G1 store row count and file size after a WAL checkpoint.

One discarded warm-up pass per mode, then 5 runs with modes interleaved.
Summary values are the median across runs of each per-run statistic.
`scoped-endpoint-noticket` is excluded (it cannot recover).

Overhead is reported against a like-for-like baseline: `scoped-store` vs
`global-store`, and the ticket modes vs `global-endpoint`.

The machine is a 2-vCPU Linux container (see `environment_all.json`). SQLite
uses `synchronous=FULL`, so latencies are dominated by commit/fsync cost of
the underlying filesystem. Absolute numbers will differ elsewhere; only the
comparison within one run set is meaningful, and even that has noticeable
run-to-run spread on this machine (up to 1.8× between the fastest and slowest
per-run median of the same mode) (per-run medians are listed in
`results/summary.md`).

## Correctness vs performance

Correctness is asserted by pytest (`pytest`) and E1/E2. E4 only counts
non-OK and incorrect associations as a sanity check; it asserts nothing.

## Reproducing

```
pip install -e ".[test]"
pytest                                    # correctness
agentproto-recovery all --out results     # E1-E4, ~90 s on the reference machine
python experiments/render_results.py      # results/summary.md
```
