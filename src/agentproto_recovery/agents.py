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
        """Simulated process restart: in-flight work of the old incarnation is gone."""
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

    def invoke(self, ctx: AuthContext, ref: str, seq: int, payload: str) -> OpResult:
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
    state TEXT NOT NULL, result TEXT, PRIMARY KEY (dialog_id, seq));
"""

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
        r = self._row(dialog_id)
        if seq is None:
            seq = r["next_seq"]
            with self.db.tx() as c:
                c.execute("INSERT INTO ops VALUES (?,?,?,?,NULL)", (dialog_id, seq, payload, "SENT"))
                c.execute("UPDATE dialogs SET next_seq=? WHERE dialog_id=?", (seq + 1, dialog_id))
        res = gw.invoke(self.ctx, dialog_id, seq, payload)
        if res.ok:
            new_state = "COMMITTED"
        elif res.code in (Code.DOWNSTREAM_UNAVAILABLE, Code.GATEWAY_DOWN):
            new_state = "UNKNOWN"  # cannot tell whether B executed it
            with self.db.tx() as c:
                c.execute("UPDATE dialogs SET state=? WHERE dialog_id=?",
                          (State.INTERRUPTED.value, dialog_id))
        elif res.code is Code.OUTCOME_UNKNOWN:
            new_state = "NEEDS_RECONCILIATION"
        elif res.code is Code.OPERATION_ABORTED:
            new_state = "UNKNOWN"  # fenced, never executed: safe to resend same seq
        else:
            new_state = "FAILED"
        with self.db.tx() as c:
            c.execute("UPDATE ops SET state=?, result=? WHERE dialog_id=? AND seq=?",
                      (new_state, res.result, dialog_id, seq))
        return res

    def unknown_ops(self, dialog_id: str) -> list[tuple[int, str]]:
        rows = self.db.query("SELECT seq, payload FROM ops WHERE dialog_id=? AND state='UNKNOWN'",
                             (dialog_id,))
        return [(r["seq"], r["payload"]) for r in rows]

    def settle_unknown_ops(self, gw: Gateway, dialog_id: str) -> list[OpResult]:
        """After resumption, resend ops whose outcome was unknown using the
        SAME sequence number; B deduplicates on (dialog, seq)."""
        return [self.invoke(gw, dialog_id, p, seq=s) for s, p in self.unknown_ops(dialog_id)]

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

