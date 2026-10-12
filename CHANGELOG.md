# Changelog

## 0.2.2 — 2026-10-12

Agent A process-restart recovery. Scenario suggested by N. Templeman in the
AgentProto discussion after he reviewed 0.2.1 (he has not reviewed 0.2.2):
persist an operation as SENT, restart Agent A in a new OS
process from its durable ledger before the acknowledgement is recorded, and
let the original downstream execution finish late.

### Fixed

- **A SENT row left by a dead Agent A process was stranded or resent.** On
  v0.2.1 nothing distinguished it from an in-flight row of the live process;
  `settle_unknown_ops` only looked at UNKNOWN, so the row stayed SENT forever
  (even after the original execution committed), and `invoke(seq=s, same
  payload)` treated it as retryable and dispatched a new attempt. Reproduced
  with real processes: 8 of 8 regression cases failed on v0.2.1
  (`results/process_restart_before_fix.txt`).
- Repair:
  - Agent A rows carry `owner` (the writing process's incarnation id).
    `recover_inflight()` marks SENT rows of other incarnations `IN_DOUBT`
    without touching dialog id, seq, payload, attempt or `next_seq`.
  - `IN_DOUBT` is never resent: `invoke` returns `OUTCOME_UNKNOWN` locally.
  - `reconcile_ops()` asks Agent B through `Gateway.resolve_op` →
    `AgentB.resolve`, bound to the payload digest: COMMITTED (with result),
    NOT_EXECUTED, IN_PROGRESS or CONFLICT. It submits nothing. Anything short
    of an authoritative answer leaves the row unresolved.
  - B answers NOT_EXECUTED only after fencing every Agent A attempt up to the
    one asked about (`op_fences`), and, for an attempt B had started, at the
    tool (`ExternalTool.fence`). Agent A now sends its attempt number with
    each invocation, so a late copy of a fenced attempt is refused.
  - `retry_op()` is the only way to retry, only from NOT_EXECUTED, with the
    original seq and payload and a higher attempt number.
  - `settle_unknown_ops()` now reconciles IN_DOUBT rows (never resends them)
    before resending UNKNOWN rows as before.
  - Answers are applied with the same rules as v0.2.1: COMMITTED is never
    overwritten; a NOT_EXECUTED/CONFLICT answer only applies to the attempt it
    was asked about.

### Added

- `tests/restart_harness.py`: multi-process harness (downstream server
  process, Agent A worker processes, event-based gates, read-only SQLite
  inspection).
- `tests/test_process_restart_regression.py` (8 cases, v0.2.1-compatible API).
- `tests/test_process_restart.py` (66 cases): scenarios A-I across all six
  modes, combined restart + gateway failover (5 modes), and the unsupported
  `scoped-endpoint-noticket` failover variant, which is asserted to stay
  unresolved.
- `AgentA.recover_inflight`, `unresolved_ops`, `reconcile_ops`, `retry_op`;
  `AgentB.resolve`; `Gateway.resolve_op`; A states `IN_DOUBT` and
  `NOT_EXECUTED`; schema: `ops.owner` (A, migrated in place on open), table
  `op_fences` (B).

### Changed

- `Gateway.invoke` and `AgentB.invoke` take an optional attempt number.
  One existing test wrapper (`test_g_late_failure_cannot_overwrite_commit`
  in `test_operation_ledger_integrity.py`) replaces `gateway.invoke` with a
  four-argument function; its signature was extended to accept and forward
  `attempt`. Its assertions are unchanged.

### Unchanged

- Experimental results: scenario matrix (152 cells), fault schedule (1,890
  rows) and privacy outputs re-run on 0.2.2 and identical to the committed
  files (`results/v0.2.2_rerun_check.txt`). Benchmark not re-run; it does not
  call the changed code.
- All 233 tests of 0.2.1 still pass.

### Pre-publication technical audit corrections

This project's own pre-publication technical audit of the first 0.2.2 draft
found three issues. `tests/test_review_v022.py`
(72 cases) failed 44 times on the draft and passes on the corrected code.

- **`invoke(seq=...)` could retry a NOT_EXECUTED operation, bypassing
  `retry_op()`, and `retry_op()`'s state check was not atomic** (two
  concurrent retries both dispatched). `_reserve()` now refuses NOT_EXECUTED
  except from `retry_op()`, which checks the state in the reserving
  transaction. CONFLICT rows are no longer resent (B's payload mismatch is
  permanent). UNKNOWN, NEEDS_RECONCILIATION, FAILED and SENT of the live
  process are still resent; each is safe under B's deduplication and fences.
  No case could execute twice before the fix.
- **`AgentB.resolve` could fence or abort without authorization.** It now
  rejects unauthenticated callers itself; reading an existing outcome needs
  only authentication and principal binding; every write that aborts or
  fences needs a current `invoke` grant and re-validates state before
  writing. Revoked, expired or unavailable authorization leaves the caller
  unresolved and writes nothing.
- **Agent B restart was described as a process restart.** It is simulated in
  the same process (new incarnation id). Harness comment, D2 test name and
  docstring, and docs corrected; added as assumption M11.
- Full suite: 379 passed on Python 3.11, 3.12 and 3.13. Experiment outputs
  re-run and identical.

### Scope

Tested: real OS process restart of Agent A on one host, a downstream process that
outlives Agent A, local Unix-socket IPC. Not tested or claimed: multi-host
operation, real network protocols, cross-operator authentication, official
AgentProto interoperability. The external tool's atomic fence (or honoured
idempotency key) remains an explicit assumption (M7).

## 0.2.1 — 2026-10-11

Focused patch: Agent A's local operation ledger.

### Fixed

- **A COMMITTED operation could be overwritten.** `AgentA.invoke()` with an
  explicit `seq` forwarded the request without reading the local row, then
  applied an unconditional `UPDATE` with the response. A retry reusing seq 1
  with a different payload (rejected by B as `SEQUENCE_CONFLICT`) turned the
  COMMITTED row into FAILED with a NULL result; a late error from an older
  attempt turned it into UNKNOWN; concurrent conflicting submissions could
  leave no row or a corrupted one. Reproduced on 0.2.0: 26 of 36 new test
  cases failed (`results/ledger_integrity_before_fix.txt`).
- Repair: `_reserve()` checks the local row in a transaction before dispatch
  (different payload → local `SEQUENCE_CONFLICT`, no gateway call, no write;
  COMMITTED + same payload → answered from the ledger; otherwise attempt + 1).
  `_record()` commits on success from any attempt, applies a non-success only
  to the current attempt, and never updates a COMMITTED row.

### Added

- `tests/test_operation_ledger_integrity.py`: Tests A-G across global-store,
  scoped-store, scoped-endpoint-ticket and scoped-endpoint-medticket (Test H),
  plus two cases that keep Agent B's payload-digest check covered now that
  Agent A rejects conflicts first. 44 cases, all passing.
- `AgentA.op()` accessor; `ops.attempt` column; local state `CONFLICT`.
- Documented policy for an explicit seq with no local row (adopt it and move
  `next_seq` past it).

### Unchanged

- Agent B payload digest, ticket binding, operation fencing (v0.2.0).
- Experimental results: scenario matrix, fault schedule and privacy outputs
  re-run on 0.2.1 and identical to the committed files
  (`results/v0.2.1_rerun_check.txt`). Benchmark not re-run; it does not call
  the changed code.
- No existing test was modified. Full suite: 233 passed on Python 3.11, 3.12
  and 3.13.

## 0.2.0 — 2026-10-11

Repairs for three defects reported by an independent audit of 0.1.0. The
audit report and its regression tests were not available in this session; the
regression tests in `tests/test_audit_regressions.py` were written from the
defect descriptions. All 6 test cases failed on 0.1.0 and pass on 0.2.0
(`results/audit_regressions_before_fix.txt`, `..._after_fix.txt`).

### Fixed

- **Foreign ticket resumed the wrong dialog** (`scoped-endpoint-ticket`,
  `scoped-endpoint-medticket`). An authentic ticket for dialog 2 presented
  with dialog 1's identifier resumed dialog 2 and made the replacement gateway
  store dialog 1 → dialog 2's B-side dialog. Tickets are now bound to the
  upstream dialog. Mediator tickets carry the upstream id. B-sealed tickets
  carry `upstream_binding(dA)` (HMAC keyed with dA); the gateway recomputes it
  from the presented dA and B compares. New code `TICKET_MISMATCH`.
- **Reconciliation raced a live tool call.** `AgentB.reconcile` deleted a
  `PENDING` operation when the tool had no record yet, even while the call was
  running; the call then completed and a retry executed again. Operations now
  record attempt, executor incarnation and an execution lease. Reconcile
  returns `IN_PROGRESS` while the lease is live; afterwards it calls
  `ExternalTool.fence`, which atomically reports a past execution or refuses
  the key in future. A fenced call returns `OPERATION_ABORTED`; a retry runs a
  new attempt under a new key.
- **Reused sequence number with a different payload** was answered as a
  duplicate. B stores a SHA-256 payload digest per `(dialog, seq)` and returns
  `SEQUENCE_CONFLICT`.

### Added

- Scenarios T21 (foreign ticket), T22 (reconcile during live call), T23
  (sequence conflict) in the scenario matrix; fault `foreign_ticket` in the
  fault schedule.
- Two privacy tests showing the binding value reaches only gateways and B and
  is not used outside the B-ticket mode.
- `ExternalTool.fence`, `AgentB.restart` (simulated restart: new incarnation).

### Changed

- `ops` table schema (Agent B): `payload_digest`, `attempt`, `incarnation`,
  `lease_until`. `dialogs` gains `binding`. The setup of one 0.1.0 test
  (`test_reconcile_when_tool_never_ran`) was updated for the schema; its
  assertions are unchanged.
- Documentation: results, gap analysis (new finding F10, F6 rewritten),
  threat model, architecture invariants, IETF note.

### Corrected statements

- GitHub PR #72 (tireddy2/agentic-ai-ucreq2) was **merged on 2026-10-09**
  (merge commit `f9920d8`), not open on 2026-10-11. It *added* B3-13; an
  interim commit used different wording; the merged text matches -03.
  Verified from the repository's git history.
- 0.1.0 stated that no configuration resumed the wrong dialog across 1,620
  attempts. That held only for the faults tested; the ticket modes were
  vulnerable to a fault the schedule did not include.

## 0.1.0 — 2026-10-11

Initial prototype.
