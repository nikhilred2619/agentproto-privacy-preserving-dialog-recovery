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

**v0.2.2 note.** The scenario matrix, fault schedule and privacy analysis
were re-run on v0.2.2 and compared with the committed files: all 152 cells,
all 1,890 fault rows and the privacy output were identical
(`results/v0.2.2_rerun_check.txt`). They remain valid for v0.2.2. The
benchmark was not re-run because it never calls `invoke` or `resolve_op`.

## Agent A process restart (v0.2.1 → v0.2.2)

Scenario suggested by N. Templeman in the AgentProto discussion after he
reviewed v0.2.1. He has not reviewed v0.2.2; the implementation, the
pre-publication technical audit below and all test results are this
project's own. The scenario: an operation is persisted as
SENT, Agent A's process is killed before the acknowledgement is recorded, a
new Agent A process opens the same ledger, and the original downstream
execution, which outlives the old process, finishes late.

**Method.** `tests/restart_harness.py` starts real OS processes with a fresh
interpreter: a downstream server (gateways, Agent B, external tool,
authorization), Agent A process P1, and restart processes P2 (and P3). P1 is
killed with SIGKILL (return code -9 asserted) while its request is held at
one of two event gates: inside the tool call after B recorded PENDING
("tool"), or before B received it ("pre_b"). No sleeps; every wait is on an
event with a 60 s failure bound. Agent A's ledger, Agent B's ledger and the
tool's execution table are read directly (read-only SQLite) in every test.

**Root cause in v0.2.1.** A SENT row carried no indication of which process
wrote it, so a row left by a dead process looked like an in-flight row of the
live one. No recovery path looked at SENT (`unknown_ops` selects UNKNOWN
only), so the row was stranded, and `_reserve` treated SENT as retryable, so
a retry dispatched a new attempt while the original was still executing.
Agent A had no authoritative way to learn the outcome, and requests carried
no attempt number, so B could not refuse a late copy of an attempt.

**Baseline failure.** `tests/test_process_restart_regression.py` against the
unmodified v0.2.1 code (commit `a940ace`): **8 failed, exit 1**
(`results/process_restart_before_fix.txt`), in all four modes tested:
* "does not resend an in-doubt operation": P2 sent a new `invoke` to the
  gateway (`SENT row was treated as permission to resend`);
* "reaches authoritative outcome": after the original execution committed at
  B, P2's ledger still had the operation as `SENT`.

**After the fix.** Same file: **8 passed**. Acceptance tests
`tests/test_process_restart.py`: **66 passed**. Together: 74 passed, exit 0
(`results/process_restart_after_fix.txt`).

| Test | What is asserted (all modes listed) |
|---|---|
| A, B (6 modes) | P1 killed (-9) with the row SENT, owner = P1's incarnation; in P2 (different pid and incarnation) the row has the same dialog id, seq, payload and attempt, now IN_DOUBT; dialog row incl. `next_seq` unchanged; B row PENDING with the payload's SHA-256; no execution; P2 sent nothing |
| C (6) | first reconcile: UNRESOLVED / IN_PROGRESS, row IN_DOUBT; original completes late (its OK reply goes to nobody); second reconcile: COMMITTED with `done:pay-10`, attempt still 1; exactly 1 execution, key `…:1:1`; P2 sent no invoke |
| D (6) | request held before B: reconcile → NOT_EXECUTED, B row NOT_EXECUTED, fence through attempt 1; late original → `OPERATION_ABORTED`, 0 executions; `retry_op` → COMMITTED as attempt 2 with the same dialog, seq, payload; 1 execution; `next_seq` still 2 |
| D2 (6) | simulated Agent B restart (new incarnation id inside the same downstream process; not an OS-process restart) while its old executor thread is inside the tool call: reconcile fences at the tool → NOT_EXECUTED; the old executor is refused; retry executes once under key `…:1:2` |
| E (6) | two `resume()` calls with an in-doubt operation: A ops, B ops, executions and `next_seq` unchanged; P2's only calls were `resume`; same after resolution |
| F (6) | `seq=1, "pay-1000"` → local SEQUENCE_CONFLICT with no call of any kind, both while in doubt and after commit; same payload after commit → `deduplicated` from the ledger |
| G (6) | a NOT_EXECUTED answer for attempt 1 is held in P2 until a retry has COMMITTED as attempt 2; applying it changes nothing (`applied: false`); the original attempt arriving last is answered from B's ledger (`OK`, `deduplicated`); 1 execution |
| H (6) | after COMMITTED: two more reconciles and a second restart (P3) change no row in A, B or the tool table; P3 sends nothing for an identical retry |
| H2 (6) | P2 killed while still unresolved; P3 finds the row IN_DOUBT and resolves it; 1 execution |
| I (6) | B's store unavailable → UNRESOLVED (`DOWNSTREAM_UNAVAILABLE`); gateway down → UNRESOLVED (`GATEWAY_DOWN`); retry refused locally; `retry_op` refused (`INVALID_STATE`); row IN_DOUBT; 0 executions, no invoke; after recovery → COMMITTED, 1 execution |
| restart + failover (5) | carrying gateway G1 also fails; P2 resumes and reconciles through G2: UNRESOLVED, then COMMITTED; 1 execution |
| unsupported (1) | `scoped-endpoint-noticket` with G1 gone: resume via G2 `MAPPING_NOT_FOUND`; reconcile UNRESOLVED (`MAPPING_NOT_FOUND`); row stays IN_DOUBT although the original executed (1 execution); nothing resent |

Correction made while writing the tests: my first draft of G expected the
late original attempt to receive `OPERATION_ABORTED`. B checks "already
committed with this payload" before the fence, so it answers from its ledger
(`OK`, `deduplicated`) and executes nothing. The code was left as is and the
expectation corrected; the no-second-execution and unchanged-row assertions
were already there and still hold.

**What remains unresolved by design.** If no gateway that holds the dialog's
mapping can reach Agent B, the operation stays IN_DOUBT indefinitely; the
agent reports it and does nothing else. This is the intended fail-closed
behaviour, not a success.

What this does and does not show. The restart tests use real, separate OS
processes on one host: Agent A is SIGKILLed and a new Agent A process reopens
the same SQLite files, while a separate downstream process (gateways, Agent B,
external tool) keeps running. They do not cover multi-host operation, real
network protocols (the processes talk over a local Unix socket with Python's
`multiprocessing.connection`), clock skew, cross-operator authentication, or
interoperability with any AgentProto specification; none exists yet and none
is claimed.

## v0.2.2 pre-publication technical audit

A pre-publication technical audit of the first v0.2.2 draft raised three concerns. All
three were confirmed. For the first two, `tests/test_review_v022.py` was
written and run against the draft first: **44 failed, 28 passed, exit 1**
(`results/v0.2.2_review_before_fix.txt`; the 28 passing cases are
preserved-behaviour and policy checks that are expected to pass both before
and after). After the corrections: **72 passed, exit 0**
(`results/v0.2.2_review_after_fix.txt`).

**1. Local resend policy.** Real defect. `_reserve()` sent every
non-COMMITTED, non-IN_DOUBT row through one "attempt+1 and dispatch" branch,
so:
* `invoke(seq=...)` retried a NOT_EXECUTED operation, bypassing `retry_op()`
  (1a, 4 modes);
* `retry_op()` checked the state in one read and reserved in a separate
  transaction; with both callers past the check, two retries dispatched
  (1b: "2 dispatches for one retry", 4 modes);
* a CONFLICT row was resent although B's payload mismatch is permanent
  (1c, 4 modes).
None of these could execute an operation twice (B deduplicates and honours
fences); they broke the documented rule and caused needless dispatches.
Correction: `_reserve()` refuses NOT_EXECUTED unless called by `retry_op()`,
which now performs its state check inside the reserving transaction, and
refuses CONFLICT locally. UNKNOWN, NEEDS_RECONCILIATION, FAILED and SENT of
the live process are still resent, because each is safe under B's
(dialog, seq, digest) deduplication and fences; 1d and 1e pin that down.
The policy table is in `docs/architecture.md`.

**2. Authorization of `resolve`.** Real defect. `AgentB.resolve` checked only
principal and tenant binding: a principal whose grant was revoked or expired,
or with the authorization service down, could still fence attempts and abort
an operation (2a, 2b: 24 cases); and B did not reject an unauthenticated
context passed to it directly (2e). Correction: `resolve` now rejects
unauthenticated callers itself, and splits reading from aborting. Reading an
existing outcome needs no grant (2c, 2d); any write that aborts or fences
requires a current `invoke` grant, checked just before the write, which then
re-validates the observed state in its own transaction. A different principal
holding a valid grant and the correct dialog reference is rejected with
`PRINCIPAL_MISMATCH` and writes nothing (2f). An authorized fence still works
and still stops a late copy (2g). Consequence, documented: without authority
an in-doubt operation stays unresolved, and an attempt B had started may
still complete, once (2b asserts exactly 1 execution).

**3. Agent B restart wording.** Real documentation defect. `restart_b`
only gives the existing AgentB object a new incarnation id inside the
downstream process; the harness comment, the D2 test name and docstring, and
the D2 row above described it as a B process restart. Corrected (D2 is now
`test_d2_zombie_executor_after_simulated_b_restart_is_fenced_at_the_tool`),
and the simulation is listed as assumption M11 in `docs/threat_model.md`. No
assertion of any Agent A process-restart test changed.

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

v0.2.2 (after the pre-publication technical audit): `pytest`: **379 passed**
(233 from v0.2.1, 8 restart regression, 66 restart acceptance, 72 audit cases
in `test_review_v022.py`).

Verification runs on 2026-10-12, all with the same result:

* `pytest -q`: 379 passed, exit 0 (4 consecutive runs on Python 3.13).
* From a clean copy in new virtualenvs: 379 passed on Python 3.11, 3.12 and
  3.13; on each, `test_review_v022.py` 72 passed, `test_process_restart.py`
  66 passed and `test_process_restart_regression.py` 8 passed.
* The audit, process-restart, ledger-integrity, audit-regression and
  concurrency files run 10 more times together: green every time.
* Scenario matrix, fault schedule and privacy outputs re-run: identical to
  the committed files (`results/v0.2.2_rerun_check.txt`).

The first v0.2.2 draft, before the audit: 307 passed.

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
* No real network transport and no multi-host runs. Multi-process runs
  exist only for the Agent A restart tests (one host, Unix socket).
* Fencing depends on the external system offering an atomic
  check-and-fence; the prototype's tool was written to offer it.
* No ablation without the epoch field; order-based linkage measured only for
  a sequential workload; no protocol translation (B5-3).
