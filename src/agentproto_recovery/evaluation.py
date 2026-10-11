"""Deployment builder, scripted scenarios (T01-T20), privacy analysis and
benchmark helpers. Used by both the pytest suite and the experiment CLI so
there is one definition of each scenario.

Evaluation oracle: the harness records, at establishment time, which Agent B
dialog was created for which Agent A dialog (`Deployment.truth`). After every
successful resumption it compares the dialog Agent B actually resumed with
that ground truth. This oracle is instrumentation; no component uses it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agents import AgentA, AgentB
from .authorization import AuthorizationService
from .correlation import CorrelationStore
from .diagnostics import ObservationLog
from .errors import Code
from .failure_injection import FailureInjector, ManualClock
from .gateways import Gateway, GatewayConfig
from .persistence import Database
from .security import RecordSigner, TicketInvalid, new_identifier, new_key, upstream_binding

ACTIONS = {"delegate", "invoke", "resume"}


@dataclass(frozen=True)
class Mode:
    name: str
    id_scheme: str          # global | scoped
    recovery_source: str    # store | endpoint
    shared_store: bool      # does G2 read G1's store?
    tickets: bool           # opaque ticket sealed by Agent B
    fed_tickets: bool = False  # opaque ticket sealed by cooperating gateways


MODES: dict[str, Mode] = {m.name: m for m in [
    Mode("global-store", "global", "store", True, False),
    Mode("global-endpoint", "global", "endpoint", False, False),
    Mode("scoped-store", "scoped", "store", True, False),
    Mode("scoped-endpoint-noticket", "scoped", "endpoint", False, False),
    Mode("scoped-endpoint-ticket", "scoped", "endpoint", False, True),
    Mode("scoped-endpoint-medticket", "scoped", "endpoint", False, False, True),
]}


class Deployment:
    """One isolated system instance in its own temporary directory."""

    def __init__(self, mode: Mode | str, workdir: Path | None = None, *,
                 unsafe_guess: bool = False, lease_seconds: float = 30.0) -> None:
        self.mode = MODES[mode] if isinstance(mode, str) else mode
        self._own_dir = workdir is None
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="agentproto-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.clock = ManualClock()
        self.injector = FailureInjector()
        self.obs = ObservationLog()
        self.authz = AuthorizationService(self.clock, self.injector)
        self.b = AgentB(self.workdir, self.authz, self.injector, self.obs, self.mode.tickets,
                        clock=self.clock)
        self.cfg = GatewayConfig(self.mode.id_scheme, self.mode.recovery_source, lease_seconds,
                                 unsafe_guess, self.mode.fed_tickets)
        self.federation_key = new_key()  # only used when mode.fed_tickets
        self._op_keys: dict[str, tuple[bytes, bytes]] = {}
        self.gateways: dict[str, Gateway] = {}
        self.truth: dict[str, str] = {}
        self.truth_t: dict[tuple[str, str], str] = {}  # (tenant, upstream id)
        self.agents: list[AgentA] = []
        self.g1 = self.add_gateway("g1", "GW1")
        self.g2 = self.add_gateway("g2", "GW1" if self.mode.shared_store else "GW2")

    def add_gateway(self, name: str, operator: str | None = None) -> Gateway:
        if operator is None:  # replacement gateways follow G2's operator
            operator = "GW1" if self.mode.shared_store else f"GW-{name}"
        if operator not in self._op_keys:
            self._op_keys[operator] = (new_key(), new_key())
        sign_key, log_key = self._op_keys[operator]
        db = Database(self.workdir / f"gwstore_{operator}.sqlite", f"gw_{operator}", self.injector)
        gw = Gateway(name, operator, self.cfg, CorrelationStore(db, RecordSigner(sign_key)),
                     self.authz, self.b, self.injector, self.obs, self.clock, log_key,
                     self.federation_key)
        self.gateways[name] = gw
        return gw

    def agent_a(self, principal: str = "alice", tenant: str = "tenant-x",
                ttl: float = 3600.0, grant: bool = True) -> AgentA:
        a = AgentA(self.workdir, self.obs, self.mode.id_scheme, principal, tenant,
                   name=f"{tenant}-{principal}")
        if grant:
            self.authz.grant(principal, tenant, ACTIONS, ttl)
        self.agents.append(a)
        return a

    def establish(self, a: AgentA, task: str = "task", gw: Gateway | None = None,
                  dialog_id: str | None = None) -> tuple[Code, str]:
        before = len(self.b.oracle_established)
        code, d_a = a.establish(gw or self.g1, task, dialog_id=dialog_id)
        if len(self.b.oracle_established) > before:
            self.truth[d_a] = self.b.oracle_established[-1]
            self.truth_t[(a.ctx.tenant, d_a)] = self.b.oracle_established[-1]
        return code, d_a

    def resumed_correctly(self, d_a: str) -> bool:
        return bool(self.b.oracle_resumed) and self.b.oracle_resumed[-1] == self.truth.get(d_a)

    def crash_g1(self) -> None:
        self.g1.alive = False

    def store_path(self, gw: Gateway) -> Path:
        return gw.store.db.path

    def raw_sql(self, gw: Gateway, sql: str, args: tuple = ()) -> None:
        c = sqlite3.connect(self.store_path(gw))
        c.execute(sql, args)
        c.commit()
        c.close()

    def close(self) -> None:
        for gw in self.gateways.values():
            gw.store.db.close()
        self.b.db.close()
        self.b.tool.db.close()
        for a in self.agents:
            a.db.close()
        if self._own_dir:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> Deployment:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

@dataclass
class Outcome:
    scenario: str
    mode: str
    code: str
    recovered: bool
    correct_association: bool | None   # None when nothing was resumed
    explicit_failure: bool
    extra: dict[str, Any] = field(default_factory=dict)


def _outcome(name: str, dep: Deployment, code: Code, d_a: str | None,
             **extra: Any) -> Outcome:
    ok = code is Code.OK
    return Outcome(name, dep.mode.name, code.value, ok,
                   dep.resumed_correctly(d_a) if ok and d_a else None, not ok, extra)


def s_t01(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    code, d_a = dep.establish(a, "summarize Q3 pipeline")
    return Outcome("T01", dep.mode.name, code.value, code is Code.OK, None, code is not Code.OK,
                   {"b_dialog_exists": dep.b.dialog(dep.truth.get(d_a, "")) is not None,
                    "a_state": a.state(d_a)[0]})


def s_t02(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    r1 = a.invoke(dep.g1, d_a, "step-1")
    r2 = a.invoke(dep.g1, d_a, "step-2")
    code = r2.code if r1.ok else r1.code
    return Outcome("T02", dep.mode.name, code.value, False, None, code is not Code.OK,
                   {"tool_executions": dep.b.tool.executions()})


def s_t03(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.injector.arm("gw.g1.forward_invoke", "drop")
    lost = a.invoke(dep.g1, d_a, "op-during-outage")
    res = a.resume(dep.g1, d_a)
    settled = a.settle_unknown_ops(dep.g1, d_a) if res.ok else []
    return _outcome("T03", dep, res.code, d_a, invoke_during_outage=lost.code.value,
                    settled=[s.status for s in settled],
                    tool_executions=dep.b.tool.executions())


def s_t04(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    a.invoke(dep.g1, d_a, "before-crash")
    dep.injector.arm("gw.g1.before_forward", "crash")
    lost = a.invoke(dep.g1, d_a, "during-crash")
    res = a.resume(dep.g2, d_a)
    settled = a.settle_unknown_ops(dep.g2, d_a) if res.ok else []
    after = a.invoke(dep.g2, d_a, "after-failover") if res.ok else None
    return _outcome("T04", dep, res.code, d_a, g1_alive=dep.g1.alive,
                    invoke_during_crash=lost.code.value,
                    settled=[s.status for s in settled],
                    invoke_after=after.code.value if after else None,
                    tool_executions=dep.b.tool.executions())


def s_t05(dep: Deployment) -> Outcome:
    """All gateway-side recovery state lost: G1 dead, its mapping row deleted,
    and Agent A has no ticket. Only A's own dialog identifier remains."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.raw_sql(dep.g1, "DELETE FROM correlation WHERE upstream_id=?", (d_a,))
    a._force_local(d_a, 0, None)
    res = a.resume(dep.g2, d_a)
    return _outcome("T05", dep, res.code, d_a)


def s_t06(dep: Deployment) -> Outcome:
    """Two concurrent dialogs, same principal, near-identical tasks. The
    mapping for the first is lost. Recovery must not land on the second."""
    a = dep.agent_a()
    _, d1 = dep.establish(a, "book flight AUS->SFO 2026-11-02")
    _, d2 = dep.establish(a, "book flight AUS->SFO 2026-11-03")
    dep.crash_g1()
    dep.raw_sql(dep.g1, "DELETE FROM correlation WHERE upstream_id=?", (d1,))
    a._force_local(d1, 0, None)
    r1 = a.resume(dep.g2, d1)
    wrong = r1.ok and not dep.resumed_correctly(d1)
    r2 = a.resume(dep.g2, d2)
    ok2 = r2.ok and dep.resumed_correctly(d2)
    return Outcome("T06", dep.mode.name, r1.code.value, r1.ok,
                   (not wrong) if r1.ok else None, not r1.ok,
                   {"d2_code": r2.code.value, "d2_correct": ok2})


def s_t07(dep: Deployment) -> Outcome:
    """Two tenants use the identical upstream dialog identifier string."""
    ax = dep.agent_a("alice", "tenant-x")
    ay = dep.agent_a("alice", "tenant-y")
    shared = "dlg_collide" if dep.mode.id_scheme == "global" else "da_collide"
    cx, dx = dep.establish(ax, "x task", dialog_id=shared)
    cy, dy = dep.establish(ay, "y task", dialog_id=shared)
    dep.crash_g1()

    def last_is(tenant: str, d: str) -> bool:
        return dep.b.oracle_resumed[-1] == dep.truth_t.get((tenant, d))

    rx = ax.resume(dep.g2, dx)
    okx = rx.ok and last_is("tenant-x", dx)
    ry = ay.resume(dep.g2, dy) if cy is Code.OK else None
    oky = bool(ry and ry.ok and last_is("tenant-y", dy))
    wrong = (rx.ok and not okx) or bool(ry and ry.ok and not oky)
    return Outcome("T07", dep.mode.name, rx.code.value, rx.ok, not wrong, not rx.ok,
                   {"establish_x": cx.value, "establish_y": cy.value,
                    "resume_y": ry.code.value if ry else None, "y_correct": oky})


def s_t08(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    _, _, t0 = a.state(d_a)
    a.resume(dep.g1, d_a)
    a.resume(dep.g1, d_a)
    dep.crash_g1()
    a._force_local(d_a, 0, t0)  # replay epoch-0 state, incl. epoch-0 ticket
    res = a.resume(dep.g2, d_a)
    return _outcome("T08", dep, res.code, d_a, b_epoch=dep.b.dialog(dep.truth[d_a]).epoch)


def s_t09(dep: Deployment) -> Outcome:
    a = dep.agent_a(ttl=60)
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.clock.advance(120)
    return _outcome("T09", dep, a.resume(dep.g2, d_a).code, d_a)


def s_t10(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.authz.revoke(a.ctx.principal, a.ctx.tenant)
    return _outcome("T10", dep, a.resume(dep.g2, d_a).code, d_a)


def s_t11(dep: Deployment) -> Outcome:
    a = dep.agent_a("alice")
    mallory = dep.agent_a("mallory")  # same tenant, valid grant of her own
    _, d_a = dep.establish(a)
    _, epoch, ticket = a.state(d_a)
    dep.crash_g1()
    res = dep.g2.resume(mallory.ctx, d_a, epoch, new_identifier("rq"), ticket)
    return _outcome("T11", dep, res.code, d_a)


def s_t12(dep: Deployment) -> Outcome:
    """Tamper with recovery material: in store modes, re-point d1's mapping at
    d2's downstream id; in ticket mode, flip a ticket byte; in global-endpoint,
    alter the presented identifier."""
    a = dep.agent_a()
    _, d1 = dep.establish(a, "t1")
    _, d2 = dep.establish(a, "t2")
    dep.crash_g1()
    m = dep.mode
    if m.recovery_source == "store":
        dep.raw_sql(dep.g1, "UPDATE correlation SET downstream_id=? WHERE upstream_id=?",
                    (dep.truth[d2], d1))
        res = a.resume(dep.g2, d1)
    elif m.tickets or m.fed_tickets:
        _, e, t = a.state(d1)
        flipped = t[:-3] + ("A" if t[-3] != "A" else "B") + t[-2:]
        a._force_local(d1, e, flipped)
        res = a.resume(dep.g2, d1)
    elif m.id_scheme == "global":
        res = dep.g2.resume(a.ctx, d1[:-2] + "xx", 0, new_identifier("rq"), None)
    else:
        return Outcome("T12", m.name, "NOT_APPLICABLE", False, None, False,
                       {"reason": "no recovery material exists to tamper with"})
    return _outcome("T12", dep, res.code, d1)


def s_t12b(dep: Deployment) -> Outcome:
    """Rollback: restore an older, validly tagged mapping row. Integrity
    checks pass; freshness checks must catch it."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    snap = None
    if dep.mode.recovery_source == "store":
        c = sqlite3.connect(dep.store_path(dep.g1))
        snap = c.execute("SELECT * FROM correlation WHERE upstream_id=?", (d_a,)).fetchone()
        c.close()
    if snap is None:
        return Outcome("T12b", dep.mode.name, "NOT_APPLICABLE", False, None, False,
                       {"reason": "replacement gateway reads no stored mapping"})
    a.resume(dep.g1, d_a)
    dep.crash_g1()
    if snap is not None:
        dep.raw_sql(dep.g1, "DELETE FROM correlation WHERE upstream_id=?", (d_a,))
        dep.raw_sql(dep.g1, f"INSERT INTO correlation VALUES ({','.join('?' * len(snap))})", snap)
    res = a.resume(dep.g2, d_a)
    return _outcome("T12b", dep, res.code, d_a, rolled_back=snap is not None)


def s_t13(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.injector.arm("authz.unavailable", "active", None)
    res = a.resume(dep.g2, d_a)
    dep.injector.disarm("authz.unavailable")
    return _outcome("T13", dep, res.code, d_a)


def s_t14(dep: Deployment) -> Outcome:
    """B commits the side effect, the acknowledgment is lost, G1 dies."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.injector.arm("b.after_commit_before_ack", "drop")
    lost = a.invoke(dep.g1, d_a, "transfer-report")
    dep.crash_g1()
    res = a.resume(dep.g2, d_a)
    settled = a.settle_unknown_ops(dep.g2, d_a) if res.ok else []
    return _outcome("T14", dep, res.code, d_a, invoke_code=lost.code.value,
                    settled=[s.status for s in settled],
                    tool_executions=dep.b.tool.executions())


def s_t14b(dep: Deployment) -> Outcome:
    """B crashes after the external call but before recording the outcome."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.injector.arm("b.after_tool_before_commit", "crash")
    lost = a.invoke(dep.g1, d_a, "create-ticket")
    dep.crash_g1()
    res = a.resume(dep.g2, d_a)
    settled = a.settle_unknown_ops(dep.g2, d_a) if res.ok else []
    execs_before_reconcile = dep.b.tool.executions()
    rec = dep.b.reconcile(dep.truth[d_a], 1) if res.ok else None
    after = a.invoke(dep.g2, d_a, "create-ticket", seq=1) if res.ok else None
    return _outcome("T14b", dep, res.code, d_a, invoke_code=lost.code.value,
                    settled=[s.code.value for s in settled],
                    executions_before_reconcile=execs_before_reconcile,
                    reconcile=rec, after_reconcile=after.status if after else None,
                    tool_executions=dep.b.tool.executions())


def s_t15(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    rq = new_identifier("rq")
    r1 = dep.g2.resume(a.ctx, d_a, 0, rq, a.state(d_a)[2])
    r2 = dep.g2.resume(a.ctx, d_a, 0, rq, a.state(d_a)[2])
    b_epoch = dep.b.dialog(dep.truth[d_a]).epoch
    same = r1.ok and r2.ok and r1.epoch == r2.epoch
    return _outcome("T15", dep, r2.code, d_a, first=r1.code.value, same_result=same,
                    b_epoch=b_epoch)


def s_t16(dep: Deployment) -> Outcome:
    """G2 crashes inside the finalize transaction after B already advanced."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.injector.arm("gw.g2.finalize_before_commit", "crash")
    r1 = a.resume(dep.g2, d_a)
    g3 = dep.add_gateway("g3")
    r2 = a.resume(g3, d_a)
    dep.clock.advance(dep.cfg.lease_seconds + 1)
    r3 = a.resume(g3, d_a) if not r2.ok else r2
    rec = g3.store.get(a.ctx.tenant, d_a)
    return _outcome("T16", dep, r3.code, d_a, first=r1.code.value, during_lease=r2.code.value,
                    b_epoch=dep.b.dialog(dep.truth[d_a]).epoch,
                    store_state=rec.state if rec else None)


def s_t16b(dep: Deployment) -> Outcome:
    """G1 crashes inside the establishment transaction (after B created its
    dialog, before the mapping committed)."""
    a = dep.agent_a()
    dep.injector.arm("gw.g1.establish_before_commit", "crash")
    code, d_a = dep.establish(a)
    res = a.resume(dep.g2, d_a)
    return _outcome("T16b", dep, res.code, d_a, establish=code.value,
                    orphan_b_dialogs=len(dep.b.oracle_established),
                    mapping_rows=dep.g1.store.count())


def s_t18(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    g3 = dep.add_gateway("g3")
    _, epoch, ticket = a.state(d_a)
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def run(gw: Gateway) -> None:
        barrier.wait()
        results[gw.name] = gw.resume(a.ctx, d_a, epoch, new_identifier("rq"), ticket)

    ts = [threading.Thread(target=run, args=(g,)) for g in (dep.g2, g3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    oks = [r for r in results.values() if r.ok]
    codes = sorted(r.code.value for r in results.values())
    correct = all(dep.b.oracle_resumed and x == dep.truth[d_a] for x in dep.b.oracle_resumed)
    return Outcome("T18", dep.mode.name, "|".join(codes), bool(oks),
                   correct if oks else None, not oks,
                   {"successes": len(oks), "b_epoch": dep.b.dialog(dep.truth[d_a]).epoch,
                    "distinct_epochs": len({r.epoch for r in oks})})


def s_t19(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    _, epoch, ticket = a.state(d_a)
    a.terminate(dep.g1, d_a)
    dep.crash_g1()
    a._force_local(d_a, epoch, ticket)
    return _outcome("T19", dep, a.resume(dep.g2, d_a).code, d_a)


def s_t20(dep: Deployment) -> Outcome:
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    point = dep.g2.store.db.unavailable_point
    dep.injector.arm(point, "active", None)
    res = a.resume(dep.g2, d_a)
    dep.injector.arm("store.b.unavailable", "active", None)
    dep.injector.disarm(point)
    res_b = a.resume(dep.g2, d_a)
    dep.injector.disarm("store.b.unavailable")
    res_after = a.resume(dep.g2, d_a)
    return _outcome("T20", dep, res.code, d_a, b_store_down=res_b.code.value,
                    after_restore=res_after.code.value)


def s_t21(dep: Deployment) -> Outcome:
    """Audit finding 1: resume dialog 1 while presenting dialog 2's valid
    ticket (same principal, same epoch)."""
    a = dep.agent_a()
    _, d1 = dep.establish(a, "dialog one")
    _, d2 = dep.establish(a, "dialog two")
    _, e1, _ = a.state(d1)
    _, _, t2 = a.state(d2)
    dep.crash_g1()
    res = dep.g2.resume(a.ctx, d1, e1, new_identifier("rq"), t2)
    out = _outcome("T21", dep, res.code, d1)
    out.extra = {"d2_epoch_after": dep.b.dialog(dep.truth[d2]).epoch,
                 "ticket_presented": t2 is not None}
    return out


def _block_tool(dep: Deployment) -> tuple[threading.Event, threading.Event]:
    gate, entered = threading.Event(), threading.Event()
    real = dep.b.tool.execute

    def slow(op_key: str, payload: str) -> str:
        entered.set()
        gate.wait(10)
        return real(op_key, payload)

    dep.b.tool.execute = slow  # type: ignore[method-assign]
    return gate, entered


def s_t22(dep: Deployment) -> Outcome:
    """Audit finding 2: reconciliation while the tool call is still running,
    then again after the executor's lease expired (late call is fenced)."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    d_b = dep.truth[d_a]
    gate, entered = _block_tool(dep)
    res: dict[str, Any] = {}
    t = threading.Thread(target=lambda: res.setdefault("r", a.invoke(dep.g1, d_a, "pay-10")))
    t.start()
    entered.wait(10)
    during = dep.b.reconcile(d_b, 1)
    dep.clock.advance(600)
    after_lease = dep.b.reconcile(d_b, 1)
    gate.set()
    t.join(10)
    first = res["r"].code.value
    retry = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    execs = dep.b.tool.executions()
    return Outcome("T22", dep.mode.name, during, False, None, False,
                   {"reconcile_during_call": during, "reconcile_after_lease": after_lease,
                    "stalled_call": first, "retry": retry.code.value,
                    "tool_executions": execs})


def s_t23(dep: Deployment) -> Outcome:
    """Audit finding 3: same seq, different payload."""
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    first = a.invoke(dep.g1, d_a, "pay-10")
    other = a.invoke(dep.g1, d_a, "pay-1000", seq=1)
    same = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    return Outcome("T23", dep.mode.name, other.code.value, False, None, not other.ok,
                   {"first": first.code.value, "same_payload_retry": same.detail or same.code.value,
                    "tool_executions": dep.b.tool.executions()})


SCENARIOS: dict[str, Callable[[Deployment], Outcome]] = {
    "T01": s_t01, "T02": s_t02, "T03": s_t03, "T04": s_t04, "T05": s_t05, "T06": s_t06,
    "T07": s_t07, "T08": s_t08, "T09": s_t09, "T10": s_t10, "T11": s_t11, "T12": s_t12,
    "T12b": s_t12b, "T13": s_t13, "T14": s_t14, "T14b": s_t14b, "T15": s_t15, "T16": s_t16,
    "T16b": s_t16b, "T18": s_t18, "T19": s_t19, "T20": s_t20,
    "T21": s_t21, "T22": s_t22, "T23": s_t23,
}


def run_scenario(name: str, mode: str, unsafe_guess: bool = False) -> Outcome:
    with Deployment(mode, unsafe_guess=unsafe_guess) as dep:
        out = SCENARIOS[name](dep)
        if unsafe_guess:
            out.mode = f"{mode}+UNSAFE_GUESS"
        return out


# --------------------------------------------------------------------------
# Privacy analysis (T17)
# --------------------------------------------------------------------------

def privacy_workload(dep: Deployment, n_dialogs: int) -> dict[str, list[str]]:
    """Establish n dialogs sequentially, one invoke each, crash G1, resume all
    through G2, one more invoke each. Returns dialog -> list of its values."""
    a = dep.agent_a()
    ids = []
    for i in range(n_dialogs):
        _, d = dep.establish(a, f"task-{i}")
        a.invoke(dep.g1, d, f"op-{i}")
        ids.append(d)
    dep.crash_g1()
    for d in ids:
        a.resume(dep.g2, d)
        a.invoke(dep.g2, d, "post")
    out = {}
    for d in ids:
        vals = [d, dep.truth[d]]
        if dep.mode.tickets:  # derived binding value travels only in this mode
            vals.append(upstream_binding(d))
        out[d] = vals
    return out


def analyze_privacy(dep: Deployment, dialogs: dict[str, list[str]]) -> dict[str, Any]:
    by_domain = dep.obs.values_by_domain()
    domains = sorted(by_domain)
    # label every observed value with its dialog (tickets via B's key: oracle)
    label: dict[str, str] = {}
    for d, vals in dialogs.items():
        for v in vals:
            label[v] = d
    for e in dep.obs.entries:
        if e.kind == "ticket" and e.value not in label:
            try:
                if dep.mode.fed_tickets:
                    inner = dep.g1.fed_sealer.open(e.value)["d"]
                else:
                    inner = dep.b.sealer.open(e.value)["d"]
                for d, vals in dialogs.items():
                    if inner in vals:
                        label[e.value] = d
            except TicketInvalid:
                pass
    vals_of: dict[str, set[str]] = {d: set() for d in dialogs}
    for v, d in label.items():
        vals_of[d].add(v)

    def holders(v: str) -> list[str]:
        return [x for x in domains if v in by_domain[x]]

    max_domains_sharing = [max((len(holders(v)) for v in vals_of[d]), default=0)
                           for d in dialogs]
    pairs = {}
    for i, x in enumerate(domains):
        for y in domains[i + 1:]:
            shared = by_domain[x] & by_domain[y]
            joinable = sum(1 for d in dialogs if vals_of[d] & shared)
            pairs[f"{x}~{y}"] = round(joinable / len(dialogs), 4)
    gateway_link = {}
    for g in [x for x in domains if x.startswith("GW")]:
        kinds_by_dialog = {d: set() for d in dialogs}
        for e in dep.obs.entries:
            if e.domain == g and e.value in label:
                kinds_by_dialog[label[e.value]].add(e.kind)
        both = sum(1 for k in kinds_by_dialog.values() if {"upstream_id", "downstream_id"} <= k)
        gateway_link[g] = round(both / len(dialogs), 4)
    # Order-based linkage between A and B with NO identifiers: pair the k-th
    # dialog first seen at A with the k-th dialog first seen at B.
    first: dict[str, list[str]] = {"A": [], "B": []}
    seen: set[tuple[str, str]] = set()
    for e in dep.obs.entries:
        if e.domain in first and e.kind == "dialog_id" and (e.domain, e.value) not in seen:
            seen.add((e.domain, e.value))
            first[e.domain].append(e.value)
    order_hits = sum(1 for va, vb in zip(first["A"], first["B"])
                     if label.get(va) is not None and label.get(va) == label.get(vb))
    ab_shared = by_domain.get("A", set()) & by_domain.get("B", set())
    distinct_ab = [len(vals_of[d] & ab_shared) for d in dialogs]
    return {
        "mode": dep.mode.name,
        "dialogs": len(dialogs),
        "resumptions_per_dialog": 1,
        "mean_distinct_A_B_shared_values_per_dialog": round(statistics.mean(distinct_ab), 3),
        "domains": domains,
        "mean_domains_sharing_one_value": round(statistics.mean(max_domains_sharing), 3),
        "max_domains_sharing_one_value": max(max_domains_sharing),
        "identifier_join_fraction_by_domain_pair": pairs,
        "gateway_can_link_upstream_downstream": gateway_link,
        "endpoint_A_B_identifier_join": pairs.get("A~B", 0.0),
        "order_based_A_B_join_accuracy_sequential_workload":
            round(order_hits / len(dialogs), 4),
    }


# --------------------------------------------------------------------------
# Performance helpers
# --------------------------------------------------------------------------

def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


def summarize(xs: list[float]) -> dict[str, float]:
    return {"n": len(xs), "mean_ms": round(statistics.mean(xs), 4),
            "median_ms": round(statistics.median(xs), 4), "p95_ms": round(pct(xs, 95), 4),
            "stdev_ms": round(statistics.pstdev(xs), 4)}


def benchmark_mode(mode: str, n_dialogs: int, threads: int, workdir: Path) -> dict[str, Any]:
    with Deployment(mode, workdir) as dep:
        a = dep.agent_a()
        est, ids = [], []
        for i in range(n_dialogs):
            t0 = time.perf_counter()
            code, d = dep.establish(a, f"bench-{i}")
            est.append((time.perf_counter() - t0) * 1000)
            assert code is Code.OK, code
            ids.append(d)
        dep.crash_g1()
        lookups = []
        for d in ids:
            t0 = time.perf_counter()
            dep.g1.store.get(a.ctx.tenant, d)
            lookups.append((time.perf_counter() - t0) * 1000)
        half = n_dialogs // 2
        rec_lat, codes = [], {}
        for d in ids[:half]:
            t0 = time.perf_counter()
            r = a.resume(dep.g2, d)
            rec_lat.append((time.perf_counter() - t0) * 1000)
            codes[r.code.value] = codes.get(r.code.value, 0) + 1
        wrong = sum(1 for d, x in zip(ids[:half], dep.b.oracle_resumed) if dep.truth[d] != x)
        # concurrent recovery throughput on the remaining dialogs, distinct
        # dialogs per thread, each thread its own replacement gateway
        rest = ids[half:]
        chunks = [rest[i::threads] for i in range(threads)]
        gws = [dep.add_gateway(f"r{i}") for i in range(threads)]
        tcodes: dict[str, int] = {}
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def worker(gw: Gateway, mine: list[str]) -> None:
            barrier.wait()
            for d in mine:
                st, ep, tk = a.state(d)
                r = gw.resume(a.ctx, d, ep, new_identifier("rq"), tk)
                with lock:
                    tcodes[r.code.value] = tcodes.get(r.code.value, 0) + 1

        ts = [threading.Thread(target=worker, args=(g, c)) for g, c in zip(gws, chunks)]
        t0 = time.perf_counter()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wall = time.perf_counter() - t0
        store_bytes = dep.g1.store.db.size_bytes()
        rows = dep.g1.store.count()
        return {
            "mode": mode, "dialogs": n_dialogs, "threads": threads,
            "establish": summarize(est), "mapping_lookup": summarize(lookups),
            "recovery_after_failover": summarize(rec_lat),
            "recovery_codes": codes, "incorrect_associations": wrong,
            "concurrent_recoveries": len(rest), "concurrent_codes": tcodes,
            "concurrent_throughput_per_s": round(len(rest) / wall, 2),
            "gw1_store_bytes": store_bytes, "gw1_store_rows": rows,
        }


def to_jsonable(o: Any) -> Any:
    if isinstance(o, Outcome):
        return asdict(o)
    return o


def dump_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=to_jsonable, sort_keys=True))
