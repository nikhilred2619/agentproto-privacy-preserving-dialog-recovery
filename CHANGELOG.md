# Changelog

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
