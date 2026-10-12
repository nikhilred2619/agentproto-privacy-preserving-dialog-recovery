# Threat model

## Assets

* Correct dialog association (the right B-side dialog is resumed for the
  right A-side dialog).
* Authorization decisions (who may resume or invoke).
* Side-effect integrity (no duplicate execution).
* Cross-domain unlinkability of dialog identifiers, to the extent the design
  claims it.

## Actors

| Actor | Capability assumed |
|---|---|
| Honest-but-curious Agent A or Agent B | Follows the protocol, retains every value it sends or receives, may share its logs with the other endpoint (collusion) |
| Honest-but-curious gateway operator | Same, for everything that passes through its gateways |
| Two gateway operators (endpoint modes) | May compare their logs |
| Misconfigured or compromised gateway | Presents wrong mappings, replays old records, guesses dialogs |
| Storage attacker | Can modify, delete or roll back rows in a gateway store file, but does not hold the operator's HMAC key |
| Other principal in the same tenant | Holds its own valid grant; tries to resume someone else's dialog |
| Other tenant | Uses identical local identifier strings |
| Network | Drops messages (failure injector); does not read or modify them |

## Mock trust assumptions

These are simplifications of the prototype. Each would need a real mechanism
in a deployment.

* **M1 Authenticated context.** `AuthContext(principal, tenant)` is assumed to
  come from an authenticated transport (e.g. mTLS or a verified token). Nothing
  in the prototype authenticates anyone; `authenticated=False` exists only to
  test that the flag is checked.
* **M2 Gateway relays the principal to B.** Agent B trusts the principal the
  gateway asserts. A compromised gateway could therefore assert any principal
  to B. A real design would forward verifiable credentials (e.g. a token
  bound to the principal) so that B can verify them itself.
* **M3 Shared authorization service.** Both the gateway and B call one mock
  service and trust its answer. Grants have an expiry and a revoked flag.
  The service being unreachable is treated as deny (fail closed).
* **M4 Key custody.** The HMAC key of a gateway operator is held only by that
  operator's gateways; B's ticket key only by B; the mediator-federation key
  only by cooperating gateway operators. Keys are generated per run and never
  written to disk or logs.
* **M5 Confidential hops.** Each hop is assumed to be encrypted, so a passive
  network observer is out of scope. Without that, every identifier on the
  wire is visible to the path.
* **M7 Fenceable tool.** The external system offers `fence(op_key)`: an
  atomic "did this key execute? if not, refuse it from now on". Reconciliation
  relies on it. A tool with neither fencing nor idempotency keys leaves
  unknown outcomes for a human.
* **M8 Execution lease.** The lease (30 s) is an estimate of how long a tool
  call can take. Too short a lease aborts slow calls (they are fenced, never
  duplicated); it does not break safety, because of M7.
* **M9 One live Agent A process per ledger.** Restart recovery treats a SENT
  row stamped by another incarnation as orphaned. Nothing enforces this; the
  prototype's tests wait for the old process to exit (SIGKILL + wait) before
  starting the new one. Two live processes on one ledger would mark each
  other's in-flight rows IN_DOUBT (no duplicate execution, but spurious
  unresolved rows).
* **M11 Agent B restarts are simulated.** `AgentB.restart()` (and the
  harness's `restart_b`) gives the same object, in the same OS process, a new
  incarnation id; threads of the old incarnation keep running. This models an
  executor that outlives a B restart. Only Agent A restarts are tested as
  real OS-process restarts.
* **M10 Agent B's ledger is authoritative and durable.** "No record at B"
  is taken to mean "never reached B". If B loses its database, resolve would
  wrongly answer NOT_EXECUTED for an operation that executed.
* **M6 One clock.** Leases and grant expiry use a single `ManualClock`. Clock
  skew between gateways is not modelled.

## Threats, mitigations, evidence

| Threat | Mitigation in prototype | Test(s) | Residual risk |
|---|---|---|---|
| Correlation-reference replay | Epoch must equal stored/B epoch; tickets carry epoch; repeat of the most recent transition returns the recorded result without change | T08, T15, `test_tickets_rotate_and_old_ticket_is_stale` | The one-step-behind replay returns the current ticket to the same authenticated principal (deliberate, see below) |
| Rollback of stored mapping | Epoch check at gateway and at B | T12b | Gateway alone cannot detect a rollback if A's local state is rolled back too; B's epoch is the backstop |
| Identifier guessing | 128-bit CSPRNG identifiers; lookups require tenant + principal binding | T11, T12 (`global-endpoint`: altered id → `DIALOG_NOT_FOUND`) | Not brute-force tested |
| Cross-tenant confusion | Store keyed by `(tenant, upstream_id)`; B checks tenant | T07 | In global modes B keys dialogs by the global id alone, so a second tenant presenting the same id is refused at establishment (`INVALID_STATE`). This is a squatting/denial surface if ids are initiator-chosen |
| Unauthorized resumption | Principal binding + separate authz at gateway and B | T09, T10, T11, T13 | Depends on M1/M2 |
| Tampered mapping | HMAC-SHA256 tag over every column | T12, `test_corrupted_row_garbage` | An attacker with the HMAC key can forge rows (compromised operator) |
| Tampered ticket | AES-GCM authentication | T12 (ticket modes) | Same, for key holders |
| Ticket substitution (authentic ticket of another dialog, same principal) | Ticket bound to the upstream dialog: dA inside mediator tickets; `upstream_binding(dA)` inside B tickets, checked by B against the value the gateway derives from the presented dA | T21, fault `foreign_ticket` | **Present in v0.1.0** (wrong dialog resumed and wrong mapping persisted). A compromised gateway can still mis-route, because it chooses which dA to derive the binding from |
| Reconciliation racing a live tool call | Lease + incarnation; fencing at the tool | T22, `test_late_tool_call_after_reconcile_is_fenced_not_duplicated` | **Present in v0.1.0** (duplicate execution). Relies on M7 |
| Sequence number reused for a different operation | Payload digest per `(dialog, seq)` at B; since v0.2.1 also a local payload check at A before dispatch | T23, `test_operation_ledger_integrity.py` | **Present in v0.1.0** (silently deduplicated) |
| Client ledger corruption by rejected, late or concurrent responses | Attempt-numbered transactional updates; COMMITTED never matched by an update | `test_operation_ledger_integrity.py` (Tests A, E, F, G) | **Present in v0.2.0** (COMMITTED overwritten with FAILED/UNKNOWN and NULL result) |
| Agent A process dies between dispatch and recording the ack | SENT rows carry the writer's incarnation; restart marks them IN_DOUBT; no resend; outcome from `AgentB.resolve`; NOT_EXECUTED only after fencing; explicit `retry_op` with a higher attempt | `test_process_restart_regression.py`, `test_process_restart.py` | **Present in v0.2.1** (row stranded as SENT; resent on retry). Relies on M7, M9, M10. An operation stays unresolved for as long as no gateway with the mapping can reach B |
| Abort/fence of an operation by a caller without current authority | `AgentB.resolve`: authenticated caller and principal binding always; a current `invoke` grant for any write (NOT_EXECUTED for an unseen operation, raising a fence, fencing at the tool); re-validates state before writing; reading an existing outcome needs no grant | `test_review_v022.py` 2a-2g | **Present in the first v0.2.2 draft** (resolve fenced without any authorization check; B did not check `authenticated`). Gateway checks authentication and principal binding only; B decides authorization. Deliberate: outcome discovery stays available to the bound principal when its grant is revoked, expired or unavailable |
| Local resend of a reconciled or conflicting operation | `_reserve`: NOT_EXECUTED only via `retry_op` (state checked in the reserving transaction); CONFLICT never resent | `test_review_v022.py` 1a-1c | **Present in the first v0.2.2 draft** (bypass of `retry_op`; non-atomic `retry_op` check let two concurrent retries dispatch; CONFLICT resent). None of these could execute twice, because B deduplicates; they violated the documented rule and caused needless dispatches |
| Late copy of a request whose attempt was declared NOT_EXECUTED | Per-operation attempt fence at B (`op_fences`); tool-level fence for attempts B had started | `test_process_restart.py` D, D2, G | A request without an attempt number (pre-v0.2.2 client) is refused once a fence exists |
| Compromised gateway guesses dialogs | Not mitigable by the gateway itself; B rejects principal/tenant/epoch mismatches | Negative control (T06 `+UNSAFE_GUESS`, fault schedule: 13 incorrect associations in v0.2.0) | A compromised gateway that guesses *within the same principal* and with a correct epoch can mis-associate. B cannot tell. See gap analysis |
| Identifier leakage in logs | Per-domain keyed redaction (`Redactor`) | `test_logs_contain_no_raw_identifiers`, `test_redaction_is_domain_keyed` | Only the prototype's own events are checked |
| Metadata linkability | Scoped identifiers; rotating tickets; ticket binding derived from dA so B does not learn dA | T17 (`privacy.json`), binding checks in `test_privacy.py` | Gateways always link both sides; event order links A and B in 100% of sequential dialogs in every mode; B-sealed tickets are a shared A-B value; operators of G1 and G2 can join on dA/dB/tickets |

## Integrity vs. binding vs. freshness vs. authorization

The prototype keeps these apart and tests each separately (four
properties since v0.2.0):

* *Integrity*: HMAC tag on store rows; AEAD on tickets. Detects modification.
* *Binding*: which dialog a valid ticket belongs to. An authentic, fresh
  ticket of another dialog is still the wrong ticket (T21). Checked
  separately from integrity since v0.2.0.
* *Freshness*: epoch / version comparisons. Detects old-but-valid material.
  T12b restores a validly tagged older row: integrity passes, freshness fails.
* *Authorization validity*: `AuthorizationService.check`, called on every
  resumption and invocation, independent of whether the mapping is valid.
  `test_valid_mapping_is_not_authorization` has a perfect mapping and no grant.

## Deliberate design choice: idempotent retry of the latest transition

If B receives a resumption for epoch *e* while already at *e+1*, and its
`resume_log` has the *e → e+1* entry, it returns that recorded result. Without
this, a retry through a different gateway after a lost response (T16) would
strand the dialog. Consequence: a principal holding the epoch-*e* ticket can
obtain the epoch-*e+1* ticket. Since that principal is the one the dialog is
bound to and must also pass authorization, this was judged acceptable for
the prototype. Two or more epochs behind is always `STALE_REFERENCE`.

## Out of scope

Traffic analysis beyond the order-based join, denial of service, side
channels, malicious Agent B (B is authoritative for its own state), key
management, and any formal privacy guarantee. No differential privacy or
anonymity property is claimed.
