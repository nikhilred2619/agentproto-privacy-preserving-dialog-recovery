"""Pre-publication technical audit of v0.2.2 (2026-10-12), carried out within
this project (not an external review).

Issue 1: which local operation states may be resent by invoke(seq=...),
         and NOT_EXECUTED retry only through an atomic retry_op().
Issue 2: authorization and lifecycle policy of Gateway.resolve_op /
         AgentB.resolve, which can fence (abort) attempts.

In-process deployments are enough here: both issues are about state checks
inside Agent A, the gateway and Agent B, not about process boundaries. The
Agent A process-restart tests live in test_process_restart*.py.
"""

import threading

import pytest

from agentproto_recovery.errors import Code, RecoveryError
from agentproto_recovery.messages import AuthContext

MODES = ["global-store", "scoped-store", "scoped-endpoint-ticket", "scoped-endpoint-medticket"]


def gateway_calls(gw, method="invoke"):
    calls = []
    real = getattr(gw, method)

    def counted(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    setattr(gw, method, counted)
    return calls


def lost_before_b(dep, a, payload="pay-10"):
    """seq 1 is dropped between gateway and B: A records UNKNOWN; B has no row."""
    _, d = dep.establish(a)
    dep.injector.arm("gw.g1.forward_invoke", "drop")
    assert a.invoke(dep.g1, d, payload).code is Code.DOWNSTREAM_UNAVAILABLE
    assert a.op(d, 1)["state"] == "UNKNOWN"
    assert a.resume(dep.g1, d).ok          # gateway marked the dialog INTERRUPTED
    return d


def not_executed(dep, a):
    d = lost_before_b(dep, a)
    [r] = a.reconcile_ops(dep.g1, d)
    assert r["outcome"] == "NOT_EXECUTED"
    assert a.op(d, 1)["state"] == "NOT_EXECUTED"
    return d


def b_fences(dep):
    return [dict(r) for r in dep.b.db.query("SELECT * FROM op_fences")]


def b_ops(dep):
    return [dict(r) for r in dep.b.db.query("SELECT * FROM ops")]


def tool_fences(dep):
    return [dict(r) for r in dep.b.tool.db.query("SELECT * FROM fences")]


# =============================================================================
# Issue 1: local resend policy
# =============================================================================

@pytest.mark.parametrize("mode", MODES)
def test_1a_invoke_cannot_retry_not_executed_bypassing_retry_op(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    d = not_executed(dep, a)
    calls = gateway_calls(dep.g1)
    before = a.op(d, 1)

    r = a.invoke(dep.g1, d, "pay-10", seq=1)

    assert r.code is Code.INVALID_STATE and r.status == "NOT_EXECUTED"
    assert calls == []
    assert a.op(d, 1) == before
    assert dep.b.tool.executions() == 0
    # the documented path still works, once
    ok = a.retry_op(dep.g1, d, 1)
    assert ok.ok and a.op(d, 1)["state"] == "COMMITTED" and a.op(d, 1)["attempt"] == 2
    assert len(calls) == 1 and dep.b.tool.executions() == 1


@pytest.mark.parametrize("mode", MODES)
def test_1b_retry_op_state_check_is_atomic(make, mode):
    """Two concurrent retry_op() calls on one NOT_EXECUTED operation: only one
    may reserve and dispatch. The hook on op() lets both callers pass a
    non-atomic pre-check before either reserves (it is a no-op if retry_op
    does not use op())."""
    dep = make(mode)
    a = dep.agent_a()
    d = not_executed(dep, a)
    calls = gateway_calls(dep.g1)
    both_checked = threading.Barrier(2, timeout=5)
    real_op = a.op

    def op_hook(dialog_id, seq):
        row = real_op(dialog_id, seq)
        try:
            both_checked.wait()
        except threading.BrokenBarrierError:
            pass
        return row

    a.op = op_hook
    out = []
    ts = [threading.Thread(target=lambda: out.append(a.retry_op(dep.g1, d, 1)))
          for _ in range(2)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    a.op = real_op

    assert len(calls) == 1, f"{len(calls)} dispatches for one retry"
    codes = sorted(r.code.value for r in out)
    assert codes in (["INVALID_STATE", "OK"], ["INVALID_STATE", "OUTCOME_UNKNOWN"]), codes
    assert dep.b.tool.executions() == 1
    assert a.op(d, 1)["state"] == "COMMITTED"


@pytest.mark.parametrize("mode", MODES)
def test_1c_conflict_is_not_resent(make, mode):
    """CONFLICT means Agent B holds a different payload for this seq; that is
    permanent, so a resend can only be rejected again. Refuse locally."""
    dep = make(mode)
    a = dep.agent_a()
    _, d = dep.establish(a)
    assert a.invoke(dep.g1, d, "pay-10").ok
    with a.db.tx() as c:                      # Agent A lost its ledger row
        c.execute("DELETE FROM ops WHERE dialog_id=? AND seq=1", (d,))
        c.execute("UPDATE dialogs SET next_seq=1 WHERE dialog_id=?", (d,))
    assert a.invoke(dep.g1, d, "pay-1000", seq=1).code is Code.SEQUENCE_CONFLICT
    assert a.op(d, 1)["state"] == "CONFLICT"
    calls = gateway_calls(dep.g1)
    before = a.op(d, 1)

    r = a.invoke(dep.g1, d, "pay-1000", seq=1)

    assert r.code is Code.SEQUENCE_CONFLICT and r.status == "CONFLICT"
    assert calls == []
    assert a.op(d, 1) == before
    assert dep.b.tool.executions() == 1


@pytest.mark.parametrize("mode", MODES)
def test_1d_failed_before_execution_may_be_resent(make, mode):
    """Preserved behaviour: FAILED is only recorded for rejections that happen
    before Agent B writes or executes anything (authz, lifecycle, mapping), so
    a resend after the cause is fixed executes once."""
    dep = make(mode)
    a = dep.agent_a()
    _, d = dep.establish(a)
    dep.authz.revoke("alice", "tenant-x")
    assert a.invoke(dep.g1, d, "pay-10").code is Code.AUTHZ_REVOKED
    assert a.op(d, 1)["state"] == "FAILED"
    assert b_ops(dep) == []
    dep.authz.grant("alice", "tenant-x", {"delegate", "invoke", "resume"})

    r = a.invoke(dep.g1, d, "pay-10", seq=1)

    assert r.ok and a.op(d, 1)["state"] == "COMMITTED"
    assert dep.b.tool.executions() == 1


@pytest.mark.parametrize("mode", MODES)
def test_1e_unknown_resend_executes_at_most_once(make, mode):
    """Preserved behaviour: UNKNOWN (transport failure seen by this process)
    may be resent with the same seq and payload; B deduplicates."""
    dep = make(mode)
    a = dep.agent_a()
    _, d = dep.establish(a)
    dep.injector.arm("b.after_commit_before_ack", "drop")   # executed, ack lost
    assert a.invoke(dep.g1, d, "pay-10").code is Code.DOWNSTREAM_UNAVAILABLE
    assert a.resume(dep.g1, d).ok
    r = a.invoke(dep.g1, d, "pay-10", seq=1)
    assert r.ok and r.detail == "deduplicated"
    assert dep.b.tool.executions() == 1


# =============================================================================
# Issue 2: authorization policy of resolve
# =============================================================================

def revoke(dep):
    dep.authz.revoke("alice", "tenant-x")


def expire(dep):
    dep.clock.advance(10**7)


def unavailable(dep):
    dep.injector.arm("authz.unavailable", "active", None)


DENIALS = [(revoke, "AUTHZ_REVOKED"), (expire, "AUTHZ_EXPIRED"),
           (unavailable, "AUTHZ_UNAVAILABLE")]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("deny,code", DENIALS, ids=["revoked", "expired", "authz-unavailable"])
def test_2a_fencing_an_unseen_operation_requires_authorization(make, mode, deny, code):
    dep = make(mode)
    a = dep.agent_a()
    d = lost_before_b(dep, a)
    deny(dep)

    [r] = a.reconcile_ops(dep.g1, d)

    assert (r["outcome"], r["code"]) == ("UNRESOLVED", code)
    assert a.op(d, 1)["state"] == "UNKNOWN"
    assert b_ops(dep) == [] and b_fences(dep) == []      # nothing written at B


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("deny,code", DENIALS, ids=["revoked", "expired", "authz-unavailable"])
def test_2b_aborting_a_stale_pending_attempt_requires_authorization(make, mode, deny, code):
    """B's executor is inside the tool call when B gets a new incarnation (a
    simulated B restart). Without authority the attempt is NOT fenced at the
    tool: A stays unresolved, and the executor may still complete, once."""
    dep = make(mode)
    a = dep.agent_a()
    _, d = dep.establish(a)
    gate, entered = threading.Event(), threading.Event()
    real = dep.b.tool.execute

    def held(op_key, payload):
        entered.set()
        assert gate.wait(10)
        return real(op_key, payload)

    dep.b.tool.execute = held
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", a.invoke(dep.g1, d, "pay-10")))
    t.start()
    assert entered.wait(10)
    dep.b.restart()
    deny(dep)

    # The in-process Agent A row is SENT and owned by the live thread, so ask
    # the gateway directly, as a restarted Agent A's reconcile_ops() would.
    r = dep.g1.resolve_op(a.ctx, d, 1, "pay-10", attempt=1)

    assert r.code.value == code
    assert tool_fences(dep) == [] and b_fences(dep) == []
    assert [x["state"] for x in b_ops(dep)] == ["PENDING"]
    gate.set()
    t.join(10)
    assert out["r"].ok
    assert dep.b.tool.executions() == 1
    # once it executed, discovery needs no authority (see 2c)
    r2 = dep.g1.resolve_op(a.ctx, d, 1, "pay-10", attempt=1)
    assert (r2.code, r2.status) == (Code.OK, "COMMITTED")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("deny,code", DENIALS, ids=["revoked", "expired", "authz-unavailable"])
def test_2c_reading_an_existing_result_needs_no_authorization(make, mode, deny, code):
    """Documented policy: learning the outcome of one's own operation is not a
    new action. With principal binding intact, COMMITTED is reported even when
    authorization is revoked, expired or unavailable."""
    dep = make(mode)
    a = dep.agent_a()
    _, d = dep.establish(a)
    dep.injector.arm("b.after_commit_before_ack", "drop")
    assert a.invoke(dep.g1, d, "pay-10").code is Code.DOWNSTREAM_UNAVAILABLE
    deny(dep)

    [r] = a.reconcile_ops(dep.g1, d)

    assert (r["outcome"], r["applied"]) == ("COMMITTED", True)
    assert a.op(d, 1)["result"] == "done:pay-10"
    assert dep.b.tool.executions() == 1


@pytest.mark.parametrize("mode", MODES)
def test_2d_already_fenced_not_executed_is_readable_without_authorization(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    d = not_executed(dep, a)
    fences = b_fences(dep)
    revoke(dep)
    res = dep.g1.resolve_op(a.ctx, d, 1, "pay-10", attempt=1)
    assert (res.code, res.status) == (Code.OK, "NOT_EXECUTED")
    assert b_fences(dep) == fences
    # raising the fence further is a new abort and needs authority
    res = dep.g1.resolve_op(a.ctx, d, 1, "pay-10", attempt=5)
    assert res.code is Code.AUTHZ_REVOKED
    assert b_fences(dep) == fences


@pytest.mark.parametrize("mode", MODES)
def test_2e_unauthenticated_caller_is_rejected_at_gateway_and_b(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    d = lost_before_b(dep, a)
    anon = AuthContext("alice", "tenant-x", authenticated=False)

    assert dep.g1.resolve_op(anon, d, 1, "pay-10", attempt=1).code is Code.UNAUTHENTICATED
    rec = dep.g1.store.get("tenant-x", d)
    with pytest.raises(RecoveryError) as e:
        dep.b.resolve(anon, rec.downstream_id, 1, "pay-10", 1)
    assert e.value.code is Code.UNAUTHENTICATED
    assert b_ops(dep) == [] and b_fences(dep) == []


@pytest.mark.parametrize("mode", MODES)
def test_2f_correlation_reference_alone_does_not_authorize_a_fence(make, mode):
    """Another principal of the same tenant, with a valid grant of its own,
    presents the correct dialog reference, seq and payload."""
    dep = make(mode)
    a = dep.agent_a()
    mallory = dep.agent_a("mallory")
    d = lost_before_b(dep, a)

    res = dep.g1.resolve_op(mallory.ctx, d, 1, "pay-10", attempt=1)
    assert res.code is Code.PRINCIPAL_MISMATCH
    rec = dep.g1.store.get("tenant-x", d)
    with pytest.raises(RecoveryError) as e:
        dep.b.resolve(mallory.ctx, rec.downstream_id, 1, "pay-10", 1)
    assert e.value.code is Code.PRINCIPAL_MISMATCH
    assert b_ops(dep) == [] and b_fences(dep) == []


@pytest.mark.parametrize("mode", MODES)
def test_2g_authorized_fence_still_works(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    d = lost_before_b(dep, a)
    [r] = a.reconcile_ops(dep.g1, d)
    assert r["outcome"] == "NOT_EXECUTED"
    assert b_fences(dep)[0]["fenced_through"] == 1
    late = dep.b.invoke(a.ctx, dep.g1.store.get("tenant-x", d).downstream_id, 1, "pay-10",
                        client_attempt=1)
    assert late.code is Code.OPERATION_ABORTED and dep.b.tool.executions() == 0
