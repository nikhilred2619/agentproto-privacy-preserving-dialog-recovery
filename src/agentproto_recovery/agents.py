"""Agent A (Domain A, initiator) and Agent B (Domain B, delegated receiver).

Each agent owns a private SQLite file. Agent B is authoritative for its
own dialog lifecycle and epoch and enforces principal binding, termination,
freshness and authorization independently of any gateway.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .authorization import AuthorizationService, Decision
from .diagnostics import EventEmitter, ObservationLog
from .errors import AuthzUnavailable, Code, InjectedCrash, RecoveryError
from .failure_injection import FailureInjector, ManualClock
from .lifecycle import State
from .messages import AuthContext, OpResult, ResumeResult
from .persistence import Database
from .security import Redactor, TicketInvalid, TicketSealer, new_identifier, new_key

if TYPE_CHECKING:  # pragma: no cover
    from .gateways import Gateway

AUTHZ_CODES = {
    Decision.DENIED: Code.AUTHZ_DENIED,
    Decision.EXPIRED: Code.AUTHZ_EXPIRED,
    Decision.REVOKED: Code.AUTHZ_REVOKED,
}


def require_authz(authz: AuthorizationService, ctx: AuthContext, action: str) -> None:
    try:
        d = authz.check(ctx.principal, ctx.tenant, action)
    except AuthzUnavailable as e:
        raise RecoveryError(Code.AUTHZ_UNAVAILABLE) from e
    if d is not Decision.ALLOW:
        raise RecoveryError(AUTHZ_CODES[d], action)


# --------------------------------------------------------------------------
# Agent B
# --------------------------------------------------------------------------

B_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
    dialog_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, principal TEXT NOT NULL,
    state TEXT NOT NULL, epoch INTEGER NOT NULL, task TEXT NOT NULL,
    binding TEXT);
CREATE TABLE IF NOT EXISTS ops (
    dialog_id TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
    result TEXT, payload_digest TEXT NOT NULL, attempt INTEGER NOT NULL,
    incarnation TEXT NOT NULL, lease_until REAL NOT NULL,
    PRIMARY KEY (dialog_id, seq));
CREATE TABLE IF NOT EXISTS resume_log (
    dialog_id TEXT NOT NULL, from_epoch INTEGER NOT NULL, new_epoch INTEGER NOT NULL,
    ticket TEXT, mediator TEXT, PRIMARY KEY (dialog_id, from_epoch));
CREATE TABLE IF NOT EXISTS op_fences (
    dialog_id TEXT NOT NULL, seq INTEGER NOT NULL, fenced_through INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, seq));
"""

TOOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    n INTEGER PRIMARY KEY AUTOINCREMENT, op_key TEXT NOT NULL, payload TEXT);
CREATE TABLE IF NOT EXISTS fences (op_key TEXT PRIMARY KEY);
"""


class ToolFenced(Exception):
    """The external system refused an operation key that was fenced."""


class ExternalTool:
    """A downstream system with a real side effect. It is deliberately NOT
    idempotent: every execute() appends a row, so duplicates are visible.

    ASSUMPTION (documented in docs/threat_model.md, M7): the system offers
    `fence(op_key)`, an atomic "tell me if this key executed; if not, refuse
    it from now on". Without such a primitive (or an idempotency key honoured
    by the tool) an unknown outcome cannot be resolved safely by software and
    the operation stays OUTCOME_UNKNOWN for a human to resolve.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def execute(self, op_key: str, payload: str) -> str:
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM fences WHERE op_key=?", (op_key,)).fetchone():
                raise ToolFenced(op_key)
            c.execute("INSERT INTO executions (op_key, payload) VALUES (?,?)", (op_key, payload))
        return f"done:{payload}"

    def lookup(self, op_key: str) -> str | None:
        rows = self.db.query("SELECT payload FROM executions WHERE op_key=?", (op_key,))
        return f"done:{rows[0]['payload']}" if rows else None

    def fence(self, op_key: str) -> str | None:
        """Atomically: return the result if `op_key` executed, otherwise make
        sure it never will."""
        with self.db.tx() as c:
            row = c.execute("SELECT payload FROM executions WHERE op_key=?", (op_key,)).fetchone()
            if row is not None:
                return f"done:{row['payload']}"
            c.execute("INSERT OR IGNORE INTO fences VALUES (?)", (op_key,))
            return None

    def executions(self, op_key: str | None = None) -> int:
        if op_key is None:
            return self.db.query("SELECT COUNT(*) AS n FROM executions")[0]["n"]
        return self.db.query("SELECT COUNT(*) AS n FROM executions WHERE op_key=?",
                             (op_key,))[0]["n"]


def payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class BDialog:
    dialog_id: str
    tenant: str
    principal: str
    state: str
    epoch: int
    binding: str | None = None


class AgentB:
    DOMAIN = "B"

    def __init__(self, workdir: Path, authz: AuthorizationService, injector: FailureInjector,
                 obs: ObservationLog, use_tickets: bool, name: str = "agent-b",
                 clock: ManualClock | None = None, exec_lease_seconds: float = 30.0) -> None:
        self.name = name
        self.authz = authz
        self.injector = injector
        self.obs = obs
        self.use_tickets = use_tickets
        self.clock = clock or ManualClock()
        self.exec_lease_seconds = exec_lease_seconds
        # Changes when the (simulated) process restarts; a PENDING operation
        # started by an earlier incarnation cannot still be running here.
        self.incarnation = new_identifier("inc")
        self.db = Database(Path(workdir) / "domain_b.sqlite", "b", injector, B_SCHEMA)
        self.tool = ExternalTool(Database(Path(workdir) / "domain_b_tool.sqlite", "b_tool",
                                          injector, TOOL_SCHEMA))
        self.sealer = TicketSealer(new_key(), aad_context=name)
        self.events = EventEmitter("agent_b", self.DOMAIN, Redactor(new_key()))
        # Evaluation oracle only (never sent to other domains).
        self.oracle_resumed: list[str] = []
        self.oracle_established: list[str] = []

    def restart(self) -> None:
        """Simulated restart: gives this AgentB object a new incarnation id, as
        a restarted process would have. It does not start a new OS process
        and does not stop threads of the old incarnation; a call already
        inside the tool keeps running (which is how the tests model an
        executor that outlives a B restart)."""
        self.incarnation = new_identifier("inc")

    # -- helpers -----------------------------------------------------------
    def _row(self, dialog_id: str) -> BDialog | None:
        rows = self.db.query("SELECT * FROM dialogs WHERE dialog_id=?", (dialog_id,))
        if not rows:
            return None
        r = rows[0]
        return BDialog(r["dialog_id"], r["tenant"], r["principal"], r["state"], r["epoch"],
                       r["binding"])

    def _seal(self, d: BDialog, epoch: int) -> str | None:
        if not self.use_tickets:
            return None
        t = self.sealer.seal({"d": d.dialog_id, "t": d.tenant, "p": d.principal, "e": epoch,
                              "u": d.binding})
        self.obs.saw(self.DOMAIN, "ticket", t)  # a domain observes what it emits
        return t

    def _resolve(self, ctx: AuthContext, ref: str | None, ticket: str | None,
                 binding: str | None = None) -> tuple[BDialog, int | None]:
        ticket_epoch = None
        if ref is None:
            if ticket is None:
                raise RecoveryError(Code.MAPPING_NOT_FOUND, "no reference")
            self.obs.saw(self.DOMAIN, "ticket", ticket)
            self.obs.saw(self.DOMAIN, "binding", binding)
            try:
                tp = self.sealer.open(ticket)
            except TicketInvalid as e:
                raise RecoveryError(Code.TICKET_INVALID, str(e)) from e
            # The ticket is authentic; it must also be the ticket of the
            # upstream dialog being resumed, not of some other dialog.
            if not tp.get("u") or not binding or not hmac.compare_digest(tp["u"], binding):
                raise RecoveryError(Code.TICKET_MISMATCH, "ticket bound to another dialog")
            ref, ticket_epoch = tp["d"], tp["e"]
            if tp["t"] != ctx.tenant:
                raise RecoveryError(Code.TENANT_MISMATCH, "ticket")
            if tp["p"] != ctx.principal:
                raise RecoveryError(Code.PRINCIPAL_MISMATCH, "ticket")
        self.obs.saw(self.DOMAIN, "dialog_id", ref)
        d = self._row(ref)
        if d is None:
            raise RecoveryError(Code.DIALOG_NOT_FOUND)
        if d.tenant != ctx.tenant:
            raise RecoveryError(Code.TENANT_MISMATCH)
        if d.principal != ctx.principal:
            raise RecoveryError(Code.PRINCIPAL_MISMATCH)
        if ticket_epoch is not None and d.binding != binding:
            raise RecoveryError(Code.TICKET_MISMATCH, "stored binding differs")
        return d, ticket_epoch

    # -- protocol operations -------------------------------------------------
    def establish(self, ctx: AuthContext, task: str, global_id: str | None = None,
                  binding: str | None = None) -> tuple[str, str | None]:
        require_authz(self.authz, ctx, "delegate")
        dialog_id = global_id or new_identifier("db")
        self.obs.saw(self.DOMAIN, "dialog_id", dialog_id)
        self.obs.saw(self.DOMAIN, "binding", binding)
        try:
            with self.db.tx() as c:
                c.execute("INSERT INTO dialogs VALUES (?,?,?,?,?,?,?)",
                          (dialog_id, ctx.tenant, ctx.principal, State.ACTIVE.value, 0, task,
                           binding))
        except sqlite3.IntegrityError as e:
            # Only reachable with caller-supplied (global) identifiers.
            raise RecoveryError(Code.INVALID_STATE, "dialog identifier already in use") from e
        self.oracle_established.append(dialog_id)
        d = BDialog(dialog_id, ctx.tenant, ctx.principal, State.ACTIVE.value, 0, binding)
        self.events.emit("dialog_established", ids={"dialog": dialog_id})
        return dialog_id, self._seal(d, 0)

    def resume(self, ctx: AuthContext, expected_epoch: int, ref: str | None = None,
               ticket: str | None = None, mediator: str = "",
               binding: str | None = None) -> tuple[str, ResumeResult]:
        d, ticket_epoch = self._resolve(ctx, ref, ticket, binding)
        if d.state == State.TERMINATED.value:
            raise RecoveryError(Code.DIALOG_TERMINATED)
        require_authz(self.authz, ctx, "resume")
        if ticket_epoch is not None and ticket_epoch != expected_epoch:
            raise RecoveryError(Code.STALE_REFERENCE, "ticket epoch")
        with self.db.tx() as c:
            cur = c.execute("SELECT epoch, state FROM dialogs WHERE dialog_id=?",
                            (d.dialog_id,)).fetchone()
            if cur["state"] == State.TERMINATED.value:
                raise RecoveryError(Code.DIALOG_TERMINATED)
            if cur["epoch"] == expected_epoch:
                new_epoch = expected_epoch + 1
                tkt = self._seal(d, new_epoch)
                c.execute("UPDATE dialogs SET epoch=?, state=? WHERE dialog_id=?",
                          (new_epoch, State.ACTIVE.value, d.dialog_id))
                c.execute("INSERT INTO resume_log VALUES (?,?,?,?,?)",
                          (d.dialog_id, expected_epoch, new_epoch, tkt, mediator))
                replay = False
            elif cur["epoch"] == expected_epoch + 1:
                # Idempotent retry of the most recent transition only.
                row = c.execute("SELECT new_epoch, ticket FROM resume_log WHERE dialog_id=? "
                                "AND from_epoch=?", (d.dialog_id, expected_epoch)).fetchone()
                if row is None:
                    raise RecoveryError(Code.STALE_REFERENCE)
                new_epoch, tkt, replay = row["new_epoch"], row["ticket"], True
            else:
                raise RecoveryError(Code.STALE_REFERENCE,
                                    f"expected {expected_epoch}, at {cur['epoch']}")
        self.oracle_resumed.append(d.dialog_id)
        self.events.emit("dialog_resumed", ids={"dialog": d.dialog_id},
                         epoch=new_epoch, idempotent_replay=replay)
        return d.dialog_id, ResumeResult(Code.OK, new_epoch, tkt)

    def invoke(self, ctx: AuthContext, ref: str, seq: int, payload: str,
               client_attempt: int | None = None) -> OpResult:
        """`client_attempt` is Agent A's attempt number for this (dialog, seq).
        Once reconciliation has fenced attempts up to k (see resolve()), a
        request carrying attempt <= k, or no attempt at all, is refused
        without executing. It is how a late-arriving copy of an attempt that
        reconciliation declared NOT_EXECUTED is kept from executing after all."""
        d, _ = self._resolve(ctx, ref, None)
        if d.state == State.TERMINATED.value:
            raise RecoveryError(Code.DIALOG_TERMINATED)
        require_authz(self.authz, ctx, "invoke")
        digest = payload_digest(payload)
        now = self.clock.now()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM ops WHERE dialog_id=? AND seq=?",
                            (d.dialog_id, seq)).fetchone()
            if row is not None:
                if not hmac.compare_digest(row["payload_digest"], digest):
                    return OpResult(Code.SEQUENCE_CONFLICT, row["state"], None,
                                    "sequence number already used for a different operation")
                if row["state"] == "COMMITTED":
                    return OpResult(Code.OK, "COMMITTED", row["result"], "deduplicated")
            fence = c.execute("SELECT fenced_through FROM op_fences WHERE dialog_id=? AND seq=?",
                              (d.dialog_id, seq)).fetchone()
            if fence is not None and (client_attempt is None
                                      or client_attempt <= fence["fenced_through"]):
                return OpResult(Code.OPERATION_ABORTED, "NOT_EXECUTED", None,
                                f"attempt fenced by reconciliation (through "
                                f"{fence['fenced_through']}); retry with a newer attempt")
            if row is not None:
                if row["state"] == "PENDING":
                    live = (row["incarnation"] == self.incarnation and row["lease_until"] > now)
                    return OpResult(Code.OUTCOME_UNKNOWN, "PENDING", None,
                                    "in progress" if live else
                                    "prior attempt did not record an outcome; reconcile")
                # NOT_EXECUTED: the previous attempt was fenced; start a new one
                attempt = row["attempt"] + 1
                c.execute("UPDATE ops SET state='PENDING', attempt=?, incarnation=?, "
                          "lease_until=? WHERE dialog_id=? AND seq=?",
                          (attempt, self.incarnation, now + self.exec_lease_seconds,
                           d.dialog_id, seq))
            else:
                attempt = 1
                # Write-ahead intent, committed BEFORE the side effect.
                c.execute("INSERT INTO ops VALUES (?,?,?,?,?,?,?,?)",
                          (d.dialog_id, seq, "PENDING", None, digest, attempt,
                           self.incarnation, now + self.exec_lease_seconds))
        op_key = f"{d.dialog_id}:{seq}:{attempt}"
        try:
            result = self.tool.execute(op_key, payload)
        except ToolFenced:
            return OpResult(Code.OPERATION_ABORTED, "NOT_EXECUTED", None,
                            "attempt was fenced by reconciliation; retry with the same seq")
        try:
            self.injector.check("b.after_tool_before_commit")
        except InjectedCrash:
            self.restart()
            raise
        with self.db.tx() as c:
            c.execute("UPDATE ops SET state='COMMITTED', result=? WHERE dialog_id=? AND seq=? "
                      "AND attempt=? AND state='PENDING'", (result, d.dialog_id, seq, attempt))
        self.injector.check("b.after_commit_before_ack")
        return OpResult(Code.OK, "COMMITTED", result)

    def op_status(self, ctx: AuthContext, ref: str, seq: int) -> OpResult:
        d, _ = self._resolve(ctx, ref, None)
        rows = self.db.query("SELECT state, result FROM ops WHERE dialog_id=? AND seq=?",
                             (d.dialog_id, seq))
        if not rows:
            return OpResult(Code.OK, "NOT_FOUND")
        r = rows[0]
        if r["state"] == "COMMITTED":
            return OpResult(Code.OK, "COMMITTED", r["result"])
        if r["state"] == "NOT_EXECUTED":
            return OpResult(Code.OK, "NOT_EXECUTED")
        return OpResult(Code.OUTCOME_UNKNOWN, "PENDING")

    def _fence(self, c, dialog_id: str, seq: int, client_attempt: int) -> None:
        c.execute("INSERT INTO op_fences VALUES (?,?,?) ON CONFLICT(dialog_id, seq) DO UPDATE "
                  "SET fenced_through=MAX(fenced_through, excluded.fenced_through)",
                  (dialog_id, seq, client_attempt))

    def resolve(self, ctx: AuthContext, ref: str, seq: int, payload: str,
                client_attempt: int) -> OpResult:
        """Authoritative answer for one logical operation, asked by an Agent A
        that does not know what happened (e.g. after its process restarted).

        The answer is bound to the operation's payload digest, not just
        (dialog, seq). It never executes anything. Possible answers:

        * COMMITTED (code OK, with result): the operation executed exactly once.
        * NOT_EXECUTED (code OK): it has not executed and, for every Agent A
          attempt <= client_attempt, never will: those attempts are fenced
          here and, if B had already started one, at the external tool.
        * IN_PROGRESS (code OUTCOME_UNKNOWN): B's executor may still be running
          (live lease in this incarnation). Nothing is decided; ask again.
        * CONFLICT (code SEQUENCE_CONFLICT): B holds a different payload.

        Authorization policy (v0.2.2 pre-publication technical audit):
        * Every call needs an authenticated caller bound to the dialog's
          principal and tenant. A correlation reference alone is never enough.
        * Reading an outcome that already exists (COMMITTED, CONFLICT,
          IN_PROGRESS, NOT_EXECUTED already fenced through this attempt) needs
          no further authorization: it is not a new action, and blocking it
          would leave the caller unable to learn what happened.
        * Anything that aborts an attempt (recording NOT_EXECUTED for an
          operation B never saw, raising a fence, fencing a started attempt
          at the tool) is a new action and needs a current "invoke" grant.
          Revoked, expired or unavailable authorization means nothing is
          written and the caller stays unresolved.
        * Allowed in any dialog state, including TERMINATED.

        Relies on assumption M7 (the external tool can fence a key atomically).
        """
        if not ctx.authenticated:
            raise RecoveryError(Code.UNAUTHENTICATED)
        d, _ = self._resolve(ctx, ref, None)
        digest = payload_digest(payload)
        for _ in range(3):
            answer, action = self._resolve_observe(d.dialog_id, seq, digest, client_attempt)
            if answer is not None:
                return answer
            if action != "settle_executed":
                require_authz(self.authz, ctx, "invoke")
            answer = self._resolve_act(d.dialog_id, seq, digest, client_attempt, action)
            if answer is not None:
                return answer
            # state changed between observing and acting; observe again
        return OpResult(Code.OUTCOME_UNKNOWN, "IN_PROGRESS", None,
                        "state changed concurrently; ask again")

    def _resolve_observe(self, dialog_id: str, seq: int, digest: str, client_attempt: int
                         ) -> tuple[OpResult | None, str | None]:
        """Read-only. Returns (answer, None) or (None, action needed)."""
        rows = self.db.query("SELECT * FROM ops WHERE dialog_id=? AND seq=?", (dialog_id, seq))
        if not rows:
            return None, "fence_unseen"
        row = rows[0]
        if not hmac.compare_digest(row["payload_digest"], digest):
            return OpResult(Code.SEQUENCE_CONFLICT, "CONFLICT", None,
                            "B holds a different payload for this sequence number"), None
        if row["state"] == "COMMITTED":
            return OpResult(Code.OK, "COMMITTED", row["result"]), None
        if row["state"] == "NOT_EXECUTED":
            f = self.db.query("SELECT fenced_through FROM op_fences WHERE dialog_id=? AND seq=?",
                              (dialog_id, seq))
            if f and f[0]["fenced_through"] >= client_attempt:
                return OpResult(Code.OK, "NOT_EXECUTED", None, "fenced"), None
            return None, "raise_fence"
        if row["incarnation"] == self.incarnation and row["lease_until"] > self.clock.now():
            return OpResult(Code.OUTCOME_UNKNOWN, "IN_PROGRESS", None,
                            "executor may still be running"), None
        # PENDING from an earlier incarnation or with an expired lease. If the
        # tool already executed it, settling it only records a fact.
        if self.tool.lookup(f"{dialog_id}:{seq}:{row['attempt']}") is not None:
            return None, "settle_executed"
        return None, "fence_started"

    def _resolve_act(self, dialog_id: str, seq: int, digest: str, client_attempt: int,
                     action: str | None) -> OpResult | None:
        """Re-validates the observed state inside a transaction before writing;
        returns None if it changed (the caller observes again)."""
        if action == "fence_unseen":
            with self.db.tx() as c:
                if c.execute("SELECT 1 FROM ops WHERE dialog_id=? AND seq=?",
                             (dialog_id, seq)).fetchone():
                    return None
                # Never reached B. Record that it did not execute and fence the
                # attempts the caller knows about, so a request still in flight
                # (between Agent A and B) is refused when it arrives.
                c.execute("INSERT INTO ops VALUES (?,?,?,?,?,?,?,?)",
                          (dialog_id, seq, "NOT_EXECUTED", None, digest, 0, self.incarnation, 0.0))
                self._fence(c, dialog_id, seq, client_attempt)
            return OpResult(Code.OK, "NOT_EXECUTED", None, "no record at B; fenced")
        if action == "raise_fence":
            with self.db.tx() as c:
                row = c.execute("SELECT state, payload_digest FROM ops WHERE dialog_id=? AND seq=?",
                                (dialog_id, seq)).fetchone()
                if row is None or row["state"] != "NOT_EXECUTED" or \
                        not hmac.compare_digest(row["payload_digest"], digest):
                    return None
                self._fence(c, dialog_id, seq, client_attempt)
            return OpResult(Code.OK, "NOT_EXECUTED", None, "fenced")
        # settle_executed / fence_started: settle at the tool (atomic fence),
        # never by re-executing.
        verdict = self.reconcile(dialog_id, seq)
        with self.db.tx() as c:
            row = c.execute("SELECT state, result FROM ops WHERE dialog_id=? AND seq=?",
                            (dialog_id, seq)).fetchone()
            if row["state"] == "COMMITTED":
                return OpResult(Code.OK, "COMMITTED", row["result"])
            if row["state"] == "NOT_EXECUTED" and action == "fence_started":
                self._fence(c, dialog_id, seq, client_attempt)
                return OpResult(Code.OK, "NOT_EXECUTED", None, "fenced at tool")
        return OpResult(Code.OUTCOME_UNKNOWN, "IN_PROGRESS", None, f"reconcile: {verdict}")

    def reconcile(self, dialog_id: str, seq: int) -> str:
        """Local operator action in Domain B: settle a PENDING operation by
        asking the external system, never by re-executing.

        Returns IN_PROGRESS while the attempt may still be running in this
        incarnation (lease not expired). Otherwise it fences the attempt's key
        at the tool, which atomically reports an execution that already
        happened or guarantees that a delayed call will be refused."""
        rows = self.db.query("SELECT * FROM ops WHERE dialog_id=? AND seq=?", (dialog_id, seq))
        if not rows:
            return "NO_RECORD"
        row = rows[0]
        if row["state"] != "PENDING":
            return row["state"]
        if row["incarnation"] == self.incarnation and row["lease_until"] > self.clock.now():
            return "IN_PROGRESS"
        attempt = row["attempt"]
        result = self.tool.fence(f"{dialog_id}:{seq}:{attempt}")
        new_state = "COMMITTED" if result is not None else "NOT_EXECUTED"
        with self.db.tx() as c:
            c.execute("UPDATE ops SET state=?, result=? WHERE dialog_id=? AND seq=? "
                      "AND attempt=? AND state='PENDING'",
                      (new_state, result, dialog_id, seq, attempt))
        return new_state

    def terminate(self, ctx: AuthContext, ref: str) -> None:
        d, _ = self._resolve(ctx, ref, None)
        with self.db.tx() as c:
            c.execute("UPDATE dialogs SET state=? WHERE dialog_id=?",
                      (State.TERMINATED.value, d.dialog_id))

    def dialog(self, dialog_id: str) -> BDialog | None:
        return self._row(dialog_id)


# --------------------------------------------------------------------------
# Agent A
# --------------------------------------------------------------------------

A_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogs (
    dialog_id TEXT PRIMARY KEY, state TEXT NOT NULL, epoch INTEGER NOT NULL,
    ticket TEXT, next_seq INTEGER NOT NULL, pending_resume_id TEXT);
CREATE TABLE IF NOT EXISTS ops (
    dialog_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
    state TEXT NOT NULL, result TEXT, attempt INTEGER NOT NULL DEFAULT 1,
    owner TEXT, PRIMARY KEY (dialog_id, seq));
"""

# Agent A's local operation ledger (see docs/architecture.md, "Agent A
# operation ledger"). One row per (dialog, seq). The payload of a row never
# changes after insertion. COMMITTED is terminal: its state, payload and
# result never change. Every other state can be retried with the SAME payload.
#
#   (none) --reserve--> SENT (owner = this process's incarnation)
#   SENT (this process) / UNKNOWN / NEEDS_RECONCILIATION / FAILED
#          --invoke(seq) retry (same payload, attempt+1)--> SENT
#   NOT_EXECUTED --retry_op() only (atomic state check, attempt+1)--> SENT
#   CONFLICT --anything--> CONFLICT (B holds another payload; never resent)
#   SENT   --OK (from any attempt)----------------------------> COMMITTED
#   SENT   --non-OK response of the CURRENT attempt----------> UNKNOWN |
#                                   NEEDS_RECONCILIATION | FAILED | CONFLICT
#   SENT owned by another (dead) incarnation --recover-------> IN_DOUBT    (v0.2.2)
#   IN_DOUBT / UNKNOWN / NEEDS_RECONCILIATION --reconcile, B says:
#          COMMITTED -> COMMITTED; NOT_EXECUTED (fenced) -> NOT_EXECUTED;
#          CONFLICT -> CONFLICT; in progress / unreachable -> unchanged
#   IN_DOUBT --invoke/retry--> refused locally (OUTCOME_UNKNOWN), nothing sent
#   COMMITTED --anything-------------------------------------> COMMITTED
OP_STATES = ("SENT", "IN_DOUBT", "UNKNOWN", "NEEDS_RECONCILIATION", "FAILED", "CONFLICT",
             "NOT_EXECUTED", "COMMITTED")
UNRESOLVED_STATES = ("IN_DOUBT", "UNKNOWN", "NEEDS_RECONCILIATION")

RETRYABLE = {Code.GATEWAY_DOWN, Code.DOWNSTREAM_UNAVAILABLE, Code.STORE_UNAVAILABLE,
             Code.AUTHZ_UNAVAILABLE, Code.CONCURRENT_RECOVERY}


class AgentA:
    DOMAIN = "A"

    def __init__(self, workdir: Path, obs: ObservationLog, id_scheme: str,
                 principal: str = "alice@tenant-x", tenant: str = "tenant-x",
                 name: str = "agent-a") -> None:
        self.name = name
        self.ctx = AuthContext(principal, tenant)
        self.obs = obs
        self.id_scheme = id_scheme
        self.db = Database(Path(workdir) / f"domain_a_{name}.sqlite", f"a_{name}", None, A_SCHEMA)
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(ops)")}
        if "owner" not in cols:  # ledger written by v0.2.1 or earlier
            self.db.conn().execute("ALTER TABLE ops ADD COLUMN owner TEXT")
        # Identifies this Agent A process. A SENT row stamped with any other
        # value was written by an incarnation that no longer exists (single
        # live Agent A process per ledger file: assumption M9).
        self.incarnation = new_identifier("ainc")
        self.events = EventEmitter("agent_a", self.DOMAIN, Redactor(new_key()))

    def _row(self, dialog_id: str):
        rows = self.db.query("SELECT * FROM dialogs WHERE dialog_id=?", (dialog_id,))
        return rows[0] if rows else None

    def state(self, dialog_id: str) -> tuple[str, int, str | None]:
        r = self._row(dialog_id)
        return r["state"], r["epoch"], r["ticket"]

    def establish(self, gw: Gateway, task: str, dialog_id: str | None = None
                  ) -> tuple[Code, str]:
        prefix = "dlg" if self.id_scheme == "global" else "da"
        dialog_id = dialog_id or new_identifier(prefix)
        with self.db.tx() as c:
            c.execute("INSERT INTO dialogs VALUES (?,?,?,?,?,NULL)",
                      (dialog_id, State.ESTABLISHING.value, 0, None, 1))
        self.obs.saw(self.DOMAIN, "dialog_id", dialog_id)
        code, ticket = gw.establish(self.ctx, dialog_id, task)
        if code is Code.OK:
            self.obs.saw(self.DOMAIN, "ticket", ticket)
            with self.db.tx() as c:
                c.execute("UPDATE dialogs SET state=?, ticket=? WHERE dialog_id=?",
                          (State.ACTIVE.value, ticket, dialog_id))
        return code, dialog_id

    def invoke(self, gw: Gateway, dialog_id: str, payload: str,
               seq: int | None = None) -> OpResult:
        """Send one operation. `seq=None` allocates the next number; an
        explicit `seq` is a retry of an operation in the local ledger.

        Policy for an explicit seq with no local row ("adoption"): the row is
        inserted with this payload in the same transaction that checks for it,
        and `next_seq` is raised past it so automatic allocation never reuses
        it. This covers an Agent A that lost its ledger and retries what it
        believes it sent; Agent B's payload digest still rejects a mismatch.
        """
        return self._dispatch(gw, dialog_id, self._reserve(dialog_id, payload, seq))

    def _dispatch(self, gw: Gateway, dialog_id: str,
                  reserved: tuple[int, int, str] | OpResult) -> OpResult:
        if isinstance(reserved, OpResult):
            return reserved  # answered from the local ledger; nothing dispatched
        seq, attempt, payload = reserved
        res = gw.invoke(self.ctx, dialog_id, seq, payload, attempt=attempt)
        self._record(dialog_id, seq, attempt, res)
        return res

    def _reserve(self, dialog_id: str, payload: str | None, seq: int | None,
                 retry_not_executed: bool = False) -> tuple[int, int, str] | OpResult:
        """One transaction decides whether this request may be dispatched.

        Resend policy for an existing row with the same payload (v0.2.2):
          COMMITTED            answered from the ledger, never resent
          IN_DOUBT / SENT of a dead process   refused (reconcile first)
          CONFLICT             refused: B holds another payload for this seq,
                               which is permanent, so a resend cannot succeed
          NOT_EXECUTED         only via retry_op() (retry_not_executed=True)
          UNKNOWN              resent: transport failure seen by this process;
                               B deduplicates on (dialog, seq, digest) and
                               refuses fenced attempts
          NEEDS_RECONCILIATION resent: B answered "PENDING"; a resend is
                               answered from B's ledger or refused, unless B
                               has since established NOT_EXECUTED, in which
                               case it executes once
          FAILED               resent: recorded only for rejections before B
                               writes or executes anything
          SENT of this process resent (concurrent in-process retry); B
                               deduplicates
        """
        with self.db.tx() as c:
            d = c.execute("SELECT next_seq FROM dialogs WHERE dialog_id=?",
                          (dialog_id,)).fetchone()
            if d is None:
                return OpResult(Code.DIALOG_NOT_FOUND)
            if seq is None:
                assert payload is not None and not retry_not_executed
                seq = d["next_seq"]
                c.execute("INSERT INTO ops (dialog_id, seq, payload, state, result, attempt, "
                          "owner) VALUES (?,?,?,'SENT',NULL,1,?)",
                          (dialog_id, seq, payload, self.incarnation))
                c.execute("UPDATE dialogs SET next_seq=? WHERE dialog_id=?", (seq + 1, dialog_id))
                return seq, 1, payload
            row = c.execute("SELECT payload, state, result, attempt, owner FROM ops "
                            "WHERE dialog_id=? AND seq=?", (dialog_id, seq)).fetchone()
            if retry_not_executed:
                # retry_op(): the state check and the reservation are one
                # transaction, so two concurrent retries cannot both dispatch.
                if row is None or row["state"] != "NOT_EXECUTED":
                    return OpResult(Code.INVALID_STATE, row["state"] if row else "", None,
                                    "retry_op requires a reconciled NOT_EXECUTED operation")
                payload = row["payload"]
            assert payload is not None
            if row is None:  # adoption, see docstring of invoke()
                c.execute("INSERT INTO ops (dialog_id, seq, payload, state, result, attempt, "
                          "owner) VALUES (?,?,?,'SENT',NULL,1,?)",
                          (dialog_id, seq, payload, self.incarnation))
                c.execute("UPDATE dialogs SET next_seq=MAX(next_seq, ?) WHERE dialog_id=?",
                          (seq + 1, dialog_id))
                return seq, 1, payload
            if not hmac.compare_digest(row["payload"].encode(), payload.encode()):
                # Checked before any dispatch; the existing row is not touched.
                return OpResult(Code.SEQUENCE_CONFLICT, row["state"], None,
                                "sequence number already recorded locally for a "
                                "different payload")
            if row["state"] == "COMMITTED":
                return OpResult(Code.OK, "COMMITTED", row["result"], "deduplicated")
            orphan = row["state"] == "SENT" and row["owner"] != self.incarnation
            if row["state"] == "IN_DOUBT" or orphan:
                # Sent by an Agent A process that no longer exists; its outcome
                # is unknown. Not a licence to resend: reconcile_ops() first.
                if orphan:
                    c.execute("UPDATE ops SET state='IN_DOUBT' WHERE dialog_id=? AND seq=? "
                              "AND state='SENT' AND attempt=?", (dialog_id, seq, row["attempt"]))
                return OpResult(Code.OUTCOME_UNKNOWN, "IN_DOUBT", None,
                                "outcome of an attempt sent before a restart is unknown; "
                                "reconcile before retrying")
            if row["state"] == "CONFLICT":
                return OpResult(Code.SEQUENCE_CONFLICT, "CONFLICT", None,
                                "Agent B holds a different payload for this sequence number; "
                                "not resent")
            if row["state"] == "NOT_EXECUTED" and not retry_not_executed:
                return OpResult(Code.INVALID_STATE, "NOT_EXECUTED", None,
                                "proven NOT_EXECUTED by reconciliation; retry only through "
                                "retry_op()")
            attempt = row["attempt"] + 1
            c.execute("UPDATE ops SET state='SENT', attempt=?, owner=? WHERE dialog_id=? "
                      "AND seq=? AND attempt=? AND state!='COMMITTED'",
                      (attempt, self.incarnation, dialog_id, seq, row["attempt"]))
            return seq, attempt, payload

    def _record(self, dialog_id: str, seq: int, attempt: int, res: OpResult) -> None:
        """Apply a response to the ledger. A success from any attempt may
        commit (Agent B holds one result per (dialog, seq)); a non-success is
        applied only if it belongs to the latest attempt; nothing replaces
        COMMITTED."""
        with self.db.tx() as c:
            if res.ok:
                c.execute("UPDATE ops SET state='COMMITTED', result=? WHERE dialog_id=? "
                          "AND seq=? AND state!='COMMITTED'", (res.result, dialog_id, seq))
                return
            if res.code in (Code.DOWNSTREAM_UNAVAILABLE, Code.GATEWAY_DOWN,
                            Code.OPERATION_ABORTED):
                new_state = "UNKNOWN"  # cannot tell (or fenced): resend same seq later
            elif res.code is Code.OUTCOME_UNKNOWN:
                new_state = "NEEDS_RECONCILIATION"
            elif res.code is Code.SEQUENCE_CONFLICT:
                new_state = "CONFLICT"  # B holds a different payload for this seq
            else:
                new_state = "FAILED"
            cur = c.execute("UPDATE ops SET state=? WHERE dialog_id=? AND seq=? AND attempt=? "
                            "AND state!='COMMITTED'", (new_state, dialog_id, seq, attempt))
            if cur.rowcount and res.code in (Code.DOWNSTREAM_UNAVAILABLE, Code.GATEWAY_DOWN):
                c.execute("UPDATE dialogs SET state=? WHERE dialog_id=?",
                          (State.INTERRUPTED.value, dialog_id))

    def op(self, dialog_id: str, seq: int) -> dict | None:
        rows = self.db.query("SELECT state, payload, result, attempt FROM ops "
                             "WHERE dialog_id=? AND seq=?", (dialog_id, seq))
        return dict(rows[0]) if rows else None

    # -- restart recovery (v0.2.2) ------------------------------------------
    def recover_inflight(self) -> list[dict]:
        """Run once when a restarted Agent A process opens its ledger, before
        new work. Marks every SENT row written by another incarnation IN_DOUBT
        (identity, payload, attempt and next_seq untouched) and returns them.
        Sends nothing."""
        with self.db.tx() as c:
            rows = c.execute("SELECT dialog_id, seq, payload, attempt FROM ops WHERE "
                             "state='SENT' AND (owner IS NULL OR owner != ?) "
                             "ORDER BY dialog_id, seq", (self.incarnation,)).fetchall()
            c.execute("UPDATE ops SET state='IN_DOUBT' WHERE state='SENT' "
                      "AND (owner IS NULL OR owner != ?)", (self.incarnation,))
        return [dict(r) for r in rows]

    def unresolved_ops(self, dialog_id: str | None = None) -> list[dict]:
        q = ("SELECT dialog_id, seq, payload, state, attempt FROM ops WHERE state IN "
             f"({','.join('?' * len(UNRESOLVED_STATES))})")
        args: tuple = UNRESOLVED_STATES
        if dialog_id is not None:
            q += " AND dialog_id=?"
            args = (*args, dialog_id)
        return [dict(r) for r in self.db.query(q + " ORDER BY dialog_id, seq", args)]

    def reconcile_ops(self, gw: Gateway, dialog_id: str,
                      states: tuple[str, ...] = UNRESOLVED_STATES) -> list[dict]:
        """Learn the authoritative outcome of every unresolved operation of a
        dialog from Agent B (gateway.resolve_op). Never submits an operation.
        An outcome that cannot be established leaves the row as it is and is
        reported as UNRESOLVED."""
        self.recover_inflight()
        q = (f"SELECT seq, payload, attempt FROM ops WHERE dialog_id=? AND state IN "
             f"({','.join('?' * len(states))}) ORDER BY seq")
        out = []
        for r in self.db.query(q, (dialog_id, *states)):
            res = gw.resolve_op(self.ctx, dialog_id, r["seq"], r["payload"], attempt=r["attempt"])
            out.append(self._apply_resolution(dialog_id, r["seq"], r["attempt"], res))
        return out

    def _apply_resolution(self, dialog_id: str, seq: int, attempt: int, res: OpResult) -> dict:
        """COMMITTED may be recorded from any attempt (B holds one result per
        operation). NOT_EXECUTED / CONFLICT are recorded only if the row is
        still unresolved at the attempt that was asked about, so a stale
        answer cannot override a newer attempt. COMMITTED is never changed."""
        if res.ok and res.status == "COMMITTED":
            outcome, sql, args = "COMMITTED", (
                "UPDATE ops SET state='COMMITTED', result=? WHERE dialog_id=? AND seq=? "
                "AND state!='COMMITTED'"), (res.result, dialog_id, seq)
        elif (res.ok and res.status == "NOT_EXECUTED") or res.code is Code.SEQUENCE_CONFLICT:
            outcome = "NOT_EXECUTED" if res.ok else "CONFLICT"
            sql = (f"UPDATE ops SET state=? WHERE dialog_id=? AND seq=? AND attempt=? AND state IN "
                   f"({','.join('?' * len(UNRESOLVED_STATES))})")
            args = (outcome, dialog_id, seq, attempt, *UNRESOLVED_STATES)
        else:
            return {"seq": seq, "outcome": "UNRESOLVED", "code": res.code.value,
                    "status": res.status, "detail": res.detail, "applied": False}
        with self.db.tx() as c:
            applied = c.execute(sql, args).rowcount == 1
            cur = c.execute("SELECT state, result FROM ops WHERE dialog_id=? AND seq=?",
                            (dialog_id, seq)).fetchone()
        return {"seq": seq, "outcome": outcome, "code": res.code.value, "status": res.status,
                "result": res.result, "detail": res.detail, "applied": applied,
                "ledger_state": cur["state"], "ledger_result": cur["result"]}

    def retry_op(self, gw: Gateway, dialog_id: str, seq: int) -> OpResult:
        """Explicit retry of an operation that reconciliation proved
        NOT_EXECUTED: same dialog, seq and stored payload, a new attempt
        number above the fenced ones. Refused in any other state."""
        return self._dispatch(gw, dialog_id,
                              self._reserve(dialog_id, None, seq, retry_not_executed=True))

    def unknown_ops(self, dialog_id: str) -> list[tuple[int, str]]:
        rows = self.db.query("SELECT seq, payload FROM ops WHERE dialog_id=? AND state='UNKNOWN'",
                             (dialog_id,))
        return [(r["seq"], r["payload"]) for r in rows]

    def settle_unknown_ops(self, gw: Gateway, dialog_id: str) -> list[OpResult]:
        """After resumption:

        1. (v0.2.2) operations left SENT by a dead Agent A process, or already
           IN_DOUBT, are reconciled with Agent B (reconcile_ops); they are
           never resent here, whatever the answer;
        2. ops whose response was lost in this process (UNKNOWN) are resent
           with the SAME sequence number and payload, as in v0.2.1; B
           deduplicates on (dialog, seq, payload digest) and honours fences.
        """
        out = [OpResult(Code(r["code"]), r["outcome"], r.get("result"), r.get("detail", ""))
               for r in self.reconcile_ops(gw, dialog_id, states=("IN_DOUBT",))]
        return out + [self.invoke(gw, dialog_id, p, seq=s)
                      for s, p in self.unknown_ops(dialog_id)]

    def resume(self, gw: Gateway, dialog_id: str) -> ResumeResult:
        r = self._row(dialog_id)
        if r is None:
            return ResumeResult(Code.DIALOG_NOT_FOUND)
        req_id = r["pending_resume_id"] or new_identifier("rq")
        with self.db.tx() as c:
            c.execute("UPDATE dialogs SET pending_resume_id=? WHERE dialog_id=?", (req_id, dialog_id))
        res = gw.resume(self.ctx, dialog_id, r["epoch"], req_id, r["ticket"])
        with self.db.tx() as c:
            if res.ok:
                self.obs.saw(self.DOMAIN, "ticket", res.ticket)
                c.execute("UPDATE dialogs SET state=?, epoch=?, ticket=COALESCE(?, ticket), "
                          "pending_resume_id=NULL WHERE dialog_id=?",
                          (State.ACTIVE.value, res.epoch, res.ticket, dialog_id))
            elif res.code not in RETRYABLE:
                c.execute("UPDATE dialogs SET pending_resume_id=NULL WHERE dialog_id=?", (dialog_id,))
        self.events.emit("resume_attempt", ids={"dialog": dialog_id}, code=res.code.value)
        return res

    def terminate(self, gw: Gateway, dialog_id: str) -> Code:
        code = gw.terminate(self.ctx, dialog_id)
        if code is Code.OK:
            with self.db.tx() as c:
                c.execute("UPDATE dialogs SET state=? WHERE dialog_id=?",
                          (State.TERMINATED.value, dialog_id))
        return code

    # test helper: simulate restoring Agent A's own state from an old backup
    def _force_local(self, dialog_id: str, epoch: int, ticket: str | None) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE dialogs SET epoch=?, ticket=?, pending_resume_id=NULL "
                      "WHERE dialog_id=?", (epoch, ticket, dialog_id))

