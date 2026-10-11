# Results

Version 0.2.0. All numbers below come from one run of
`agentproto-recovery all --out results` on 2026-10-11 (Python 3.13.16,
SQLite 3.45.1, cryptography 50.0.1, Linux x86_64 container, 2 vCPU, wall
time 90 s). Full tables: `results/summary.md` (generated); raw data:
`results/*.json`, `results/*.csv`.

Version 0.1.0 results were replaced, not merged. Section "Audit repair"
lists what changed and why.

**v0.2.1 note.** The patch release changes only Agent A's local operation
ledger. The scenario matrix, fault schedule and privacy analysis were re-run
on v0.2.1 and compared with the committed v0.2.0 files: every cell and row was
identical (`results/v0.2.1_rerun_check.txt`), so the result files above and
below were left unchanged and remain valid for v0.2.1. The benchmark was not
re-run because it never calls the changed code path.

## Ledger integrity repair (v0.2.0 → v0.2.1)

A further audit found that Agent A's local operation ledger could lose a
committed result. Steps: seq 1 "pay-10" commits; a later call reuses seq 1
with "pay-1000"; Agent B correctly answers `SEQUENCE_CONFLICT`; Agent A then
rewrote its COMMITTED row to FAILED with a NULL result.

**Root cause** (`AgentA.invoke`, v0.2.0): with an explicit `seq` the method
did not read the local row at all; it forwarded the request and then ran an
unconditional `UPDATE ops SET state=?, result=? WHERE dialog_id=? AND seq=?`
with whatever the response implied. Nothing compared payloads, nothing
protected COMMITTED, and nothing distinguished the response of an older
attempt from the latest one. With an explicit seq and no local row, the
final UPDATE matched nothing, so the operation was dispatched without any
local record.

**Reproduction on v0.2.0:** `tests/test_operation_ledger_integrity.py` run
against the unmodified code: **26 failed, 10 passed**
(`results/ledger_integrity_before_fix.txt`). Failing: A (committed row
overwritten), B (conflict dispatched to the gateway), E (unsettled row
overwritten), F (concurrent submissions: no local row or a corrupted one),
G (late error overwrote COMMITTED). Passing on v0.2.0 as expected: C and D
(identical retries), and 2 of the 8 Test F cases, whose outcome on v0.2.0
depended on thread timing.

**Repair** (`src/agentproto_recovery/agents.py`):

* `_reserve()` runs in one `BEGIN IMMEDIATE` transaction before any dispatch.
  An existing row with a different payload returns `SEQUENCE_CONFLICT`
  without contacting the gateway and without writing. A COMMITTED row with
  the same payload is answered from the ledger (`deduplicated`). Any other
  row with the same payload gets `attempt + 1` and is dispatched again.
* `_record()` applies the response in a transaction. Success from any
  attempt may set COMMITTED, because Agent B keeps exactly one result per
  `(dialog, seq)`. A non-success is applied only to the row's current
  attempt. No update ever matches a COMMITTED row (`AND state!='COMMITTED'`).
* Explicit seq with no local row ("adoption"): the row is inserted with that
  payload in the same transaction that checked for it, and `next_seq` is
  raised past it. Agent B's payload digest still rejects a payload that
  differs from what B executed; Agent A then records CONFLICT.
* New column `ops.attempt`; new local state `CONFLICT`. Agent B's payload
  digest, the ticket binding and operation fencing from v0.2.0 are unchanged.

**After the repair:** the same file plus 8 defence-in-depth cases: **44
passed** (`results/ledger_integrity_after_fix.txt`). No existing test was
modified.

**Why an immutable committed result matters.** The committed row is Agent
A's only local evidence that a side effect happened and what it produced.
Recovery logic reads it to decide what not to resend; operators and audits
read it to learn what the agent did. If a rejected or late response can
rewrite it, the agent can believe a completed payment failed and issue a new
one under a fresh sequence number (Agent B would correctly accept that as a
different operation), or lose the result that a later step depends on. Agent
B's deduplication cannot protect against that, because from B's side the new
request is legitimate. Keeping COMMITTED immutable is what makes "retry until
you know" safe on the client side.

## Audit repair (v0.1.0 → v0.2.0)

An independent audit reported three defects in v0.1.0. The audit report and
its own regression tests were not available to me; the regression tests in
`tests/test_audit_regressions.py` were written from the defect descriptions
in the repair request. Each was run against the v0.1.0 code **before** any
fix and failed (`results/audit_regressions_before_fix.txt`, 6 of 6
failing), then run again after the fix and passed
(`results/audit_regressions_after_fix.txt`, 6 of 6 passing).

| # | Defect in v0.1.0 | Reproduced on v0.1.0 | Fix in v0.2.0 | Evidence after fix |
|---|---|---|---|---|
| 1 | In both ticket modes a valid ticket belonging to dialog 2 was accepted when resuming dialog 1 (same principal, same epoch). B resumed dialog 2, and G2 then **persisted** the mapping dialog 1 → dialog 2's B-side dialog. | Yes, both modes (`OK`, wrong B dialog, wrong mapping stored) | Tickets are bound to the upstream dialog. Mediator tickets carry the upstream id (gateways already see it). B-sealed tickets carry `upstream_binding(dA)` = HMAC keyed with dA, so B never learns dA; the resuming gateway recomputes it from the dA presented and B rejects a mismatch with `TICKET_MISMATCH`. | T21 in the matrix; `test_ticket_of_other_dialog_is_rejected`; fault `foreign_ticket`: 0 incorrect associations |
| 2 | `reconcile()` deleted a `PENDING` operation when the tool had no record yet, even while the tool call was still running. The running call then completed and a retry with the same seq executed it again. | Yes (`NOT_EXECUTED` while in flight; 2 executions after the late call) | `PENDING` rows record attempt number, executor incarnation and an execution lease. Reconcile returns `IN_PROGRESS` while the lease is live in the current incarnation. Otherwise it calls `fence(op_key)` on the tool, which atomically reports an execution that happened or makes sure a delayed call is refused. A retry starts a new attempt with a new key. | T22; `test_reconcile_while_tool_call_in_flight_does_not_clear_pending`, `test_late_tool_call_after_reconcile_is_fenced_not_duplicated`: exactly 1 execution |
| 3 | A repeated seq with a **different** payload was answered as a duplicate (`deduplicated`, or `OUTCOME_UNKNOWN` for a pending one). | Yes | B stores a SHA-256 digest of the payload with each operation and returns `SEQUENCE_CONFLICT` on a mismatch, without executing. | T23; `test_duplicate_seq_with_different_payload_is_rejected` (committed and pending cases) |

What the v0.1.0 documentation got wrong as a result:

* It said no configuration attached a resumption to the wrong dialog across
  1,620 fault-injected attempts. That was true for the faults tested, but the
  fault set did not include presenting another dialog's ticket. The ticket
  modes were not safe against it. v0.2.0 adds that fault (`foreign_ticket`).
* It listed invariant I6 (no repeated side effect) as demonstrated. It was
  demonstrated only for the crash cases tested, not for reconciliation racing
  a live call, and dedup ignored the payload.

Changes to existing tests: one test's **setup** changed because the `ops`
table gained columns (`test_reconcile_when_tool_never_ran` inserts a row with
the new columns); its assertions are unchanged. The other 180 v0.1.0 tests
are unchanged and pass. Two test-side mistakes in my first draft of the
regression tests were corrected before the final run (a clock step that also
expired the authorization grant, and an import left unused); neither affects
what is asserted.

## Test suite

v0.2.1: `pytest`: **233 passed** (181 from v0.1.0, 8 added in v0.2.0,
44 in `test_operation_ledger_integrity.py`).

v0.2.1 verification runs on 2026-10-11, all with the same result:

* `pytest -q`: 233 passed. `pytest tests/test_operation_ledger_integrity.py
  -v`: 44 passed. `pytest tests/test_audit_regressions.py -v`: 6 passed.
* Gateway-failover, authorization, replay, side-effect, concurrency and
  privacy files together: 133 passed.
* 5 consecutive full runs on Python 3.13: 233 passed each time.
* From a clean copy in new virtualenvs: 233 passed on Python 3.11, 3.12 and
  3.13 (ledger file 44 passed and audit file 6 passed on each).
* The threaded files (`test_operation_ledger_integrity.py`,
  `test_audit_regressions.py`, `test_concurrency.py`) run 20 more times
  together: green every time. No flaky test observed.

v0.2.0 (for reference): 189 passed, including on Python 3.11 and 3.12.

## E1 Scenario matrix

152 cells (25 scenarios × 6 modes + 2 negative-control cells), 3 repeats each.

* Every cell returned the same outcome on all 3 repeats.
* Incorrect associations outside the negative control: **0**. Negative
  control (T06, unsafe guess): **1**.
* T21 (foreign ticket): `TICKET_MISMATCH` in both ticket modes; in store and
  global-endpoint modes the presented ticket is not used and the correct
  dialog is resumed (`OK`, correct association); noticket fails as before.
* T22: reconciliation during the tool call returns `IN_PROGRESS`; after the
  lease expires it returns `NOT_EXECUTED`, the stalled call is refused at the
  tool (`OPERATION_ABORTED`), the retry succeeds, 1 execution in total. Same
  in every mode (this logic is local to Domain B).
* T23: `SEQUENCE_CONFLICT` in every mode, 1 execution; the genuine retry is
  still deduplicated.
* T01-T20: same codes as v0.1.0 in every cell.

## E2 Seeded fault schedule

3 seeds × 90 dialogs = 270 attempts per mode. The fault list gained
`foreign_ticket`, so the per-seed draws differ from v0.1.0 and totals are not
comparable line by line.

| mode | recovered | explicit failures | incorrect assoc. | safety violations | recovery rate where permitted |
|---|---|---|---|---|---|
| global-store | 70 | 200 | 0 | 0 | 0.620 |
| global-endpoint | 113 | 157 | 0 | 0 | 1.000 |
| scoped-store | 70 | 200 | 0 | 0 | 0.620 |
| scoped-endpoint-noticket | 0 | 270 | 0 | 0 | 0.000 |
| scoped-endpoint-ticket | 67 | 203 | 0 | 0 | 0.593 |
| scoped-endpoint-medticket | 67 | 203 | 0 | 0 | 0.593 |
| scoped-store + unsafe guess | 78 | 192 | **13** | **13** | 0.690 |

`foreign_ticket` (24 attempts per mode): store and global-endpoint modes 24 OK
(ticket ignored, correct dialog); ticket modes 21 `TICKET_MISMATCH` and 3
`MAPPING_NOT_FOUND` (that user had no other dialog, so no ticket was
presented). "Recovery rate where permitted" counts faults not in `MUST_FAIL`;
ticket modes now lose `foreign_ticket` and `ticket_lost` there by design.

Across 1,620 attempts in six modes, no attempt resumed the wrong dialog, and
none resumed a dialog that was revoked, expired, terminated, stale, or
presented by another principal — **for the faults in this list**. The list
is not exhaustive; finding 1 is an example of a fault that was missing.

## E3 Privacy (T17)

50 dialogs per mode, each resumed once through G2. Unchanged from v0.1.0 in
every metric.

| mode | domains holding a common value (max) | A~B identifier join | GW1~GW2 join | distinct A-B shared values / dialog | gateway links both sides | order-based A~B join |
|---|---|---|---|---|---|---|
| global-store | 3 of 3 | 1.0 | n/a | 1 | 1.0 | 1.0 |
| global-endpoint | 4 of 4 | 1.0 | 1.0 | 1 | 1.0 | 1.0 |
| scoped-store | 2 of 3 | 0.0 | n/a | 0 | 1.0 | 1.0 |
| scoped-endpoint-noticket | 3 of 4 | 0.0 | 1.0 | 0 | GW1 1.0, GW2 0.0 | 1.0 |
| scoped-endpoint-ticket | 4 of 4 | 1.0 | 1.0 | 2 | 1.0 | 1.0 |
| scoped-endpoint-medticket | 3 of 4 | 0.0 | 1.0 | 0 | 1.0 | 1.0 |

Effect of the ticket binding on linkability:

* `scoped-endpoint-ticket`: the binding value is now included in the
  analysis. It is observed by GW1, GW2 and B only, never sent to A
  (`test_ticket_binding_reaches_only_gateways_and_b`). Every pair that sees it
  could already join on dB or the ticket bytes, so no pairwise join fraction
  changed. It is a deterministic function of dA, so anyone holding dA can
  compute it; A could therefore recognise it if B showed it to A. A and B can
  already join on the ticket in this mode, so this adds no new A~B linkage,
  but it is one more A-derived value inside Domain B.
* `scoped-endpoint-medticket`: the ticket now contains dA, encrypted under
  the mediator federation key. Only gateways can open it, and they already see
  dA. A~B join stays 0.0 (`test_binding_not_used_outside_b_ticket_mode`).
* `scoped-store`: unaffected; no binding value is created.

## E4 Performance

5 runs × 200 sequential recoveries + 200 concurrent recoveries (4 threads)
per mode, after a discarded warm-up. Median across runs of per-run statistics.

| mode | establish med (ms) | lookup med (ms) | recovery mean / med / p95 (ms) | concurrent recoveries/s | ratio vs baseline (recovery median) |
|---|---|---|---|---|---|
| global-store | 2.42 | 0.038 | 2.77 / 2.46 / 4.58 | 539 | baseline |
| global-endpoint | 2.26 | 0.037 | 2.34 / 2.06 / 3.52 | 794 | baseline |
| scoped-store | 2.50 | 0.036 | 2.40 / 2.14 / 3.70 | 540 | 0.87 vs global-store |
| scoped-endpoint-ticket | 2.54 | 0.052 | 2.40 / 2.37 / 3.23 | 680 | 1.15 vs global-endpoint |
| scoped-endpoint-medticket | 2.44 | 0.036 | 2.73 / 2.38 / 3.70 | 710 | 1.16 vs global-endpoint |

Interpretation, unchanged in substance from v0.1.0:

* No incorrect associations and no non-OK recoveries in any benchmark run.
* The ratios are inside the run-to-run spread of a single mode (per-run
  recovery medians for `scoped-endpoint-ticket` ranged 1.91-3.61 ms). The
  cost of scoped identifiers, and of the new binding check (one HMAC per
  ticket resumption), cannot be distinguished from noise on this machine.
* Store-based recovery does two extra durable writes (claim, finalize); that
  remains the visible structural difference. Not isolated by ablation.
* The benchmark does not exercise operations, so it measures nothing about
  the new payload digest or fencing.

## Incomplete or not attempted

* The audit's own report and tests were not available; the regression tests
  are reconstructions from the defect descriptions.
* No real network transport, multi-process, or multi-host runs.
* Fencing depends on the external system offering an atomic
  check-and-fence; the prototype's tool was written to offer it.
* No ablation without the epoch field; order-based linkage measured only for
  a sequential workload; no protocol translation (B5-3).
