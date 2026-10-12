"""Agent A operation-ledger integrity (audit finding against v0.2.0).

A COMMITTED operation in Agent A's local ledger must keep its state, payload
and result whatever happens afterwards: conflicting retries, late error
responses for older attempts, or concurrent submissions.

These tests read the ledger with plain SQL so that they run unchanged against
v0.2.0 (where they reproduce the defect) and v0.2.1.
"""

import threading

import pytest

from agentproto_recovery.messages import OpResult
from agentproto_recovery.errors import Code

# Test H: the repair must not depend on the recovery configuration.
MODES = ["global-store", "scoped-store", "scoped-endpoint-ticket", "scoped-endpoint-medticket"]


def ledger(a, d_a, seq):
    rows = a.db.query("SELECT state, payload, result FROM ops WHERE dialog_id=? AND seq=?",
                      (d_a, seq))
    return dict(rows[0]) if rows else None


def count_gateway_calls(gw):
    """Wrap gw.invoke and count calls (Test B instrumentation)."""
    calls = []
    real = gw.invoke

    def counted(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    gw.invoke = counted
    return calls


# -- Test A -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_a_committed_result_preserved_after_conflicting_retry(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    first = a.invoke(dep.g1, d_a, "pay-10")
    assert first.ok
    before = ledger(a, d_a, 1)
    assert before == {"state": "COMMITTED", "payload": "pay-10", "result": "done:pay-10"}

    r = a.invoke(dep.g1, d_a, "pay-1000", seq=1)

    assert r.code is Code.SEQUENCE_CONFLICT
    assert ledger(a, d_a, 1) == before
    assert dep.b.tool.executions() == 1


# -- Test B -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_b_conflict_rejected_locally_without_dispatch(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    assert a.invoke(dep.g1, d_a, "pay-10").ok
    calls = count_gateway_calls(dep.g1)
    resumed_b = list(dep.b.oracle_resumed)

    r = a.invoke(dep.g1, d_a, "pay-1000", seq=1)

    assert r.code is Code.SEQUENCE_CONFLICT
    assert calls == []                       # gateway never contacted
    assert dep.b.tool.executions() == 1      # so neither was Agent B's tool
    assert dep.b.oracle_resumed == resumed_b


# -- Test C -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_c_identical_retry_is_idempotent(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    first = a.invoke(dep.g1, d_a, "pay-10")
    again = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    assert again.ok and again.result == first.result
    assert ledger(a, d_a, 1)["state"] == "COMMITTED"
    assert dep.b.tool.executions() == 1


# -- Test D -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_d_lost_ack_retry_settles_once(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.injector.arm("b.after_commit_before_ack", "drop")
    lost = a.invoke(dep.g1, d_a, "pay-10")
    assert lost.code is Code.DOWNSTREAM_UNAVAILABLE
    assert ledger(a, d_a, 1)["state"] == "UNKNOWN"
    assert a.resume(dep.g1, d_a).ok
    settled = a.settle_unknown_ops(dep.g1, d_a)
    assert [s.status for s in settled] == ["COMMITTED"]
    assert ledger(a, d_a, 1) == {"state": "COMMITTED", "payload": "pay-10",
                                 "result": "done:pay-10"}
    assert dep.b.tool.executions() == 1


# -- Test E -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("how", ["dropped_before_b", "executed_outcome_unrecorded"])
def test_e_unsettled_operation_rejects_different_payload(make, mode, how):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    if how == "dropped_before_b":
        dep.injector.arm("gw.g1.forward_invoke", "drop")   # never reached B
    else:
        dep.injector.arm("b.after_tool_before_commit", "crash")
    a.invoke(dep.g1, d_a, "pay-10")
    before = ledger(a, d_a, 1)
    assert before["state"] == "UNKNOWN" and before["payload"] == "pay-10"
    calls = count_gateway_calls(dep.g1)
    executions = dep.b.tool.executions()

    r = a.invoke(dep.g1, d_a, "pay-1000", seq=1)

    assert r.code is Code.SEQUENCE_CONFLICT
    assert ledger(a, d_a, 1) == before
    assert calls == []
    assert dep.b.tool.executions() == executions


# -- Test F -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("prior_row", [False, True])
def test_f_concurrent_conflicting_submissions(make, mode, prior_row):
    """Two threads submit the same seq with incompatible payloads.
    prior_row=False: neither has a local row yet (explicit-seq adoption race).
    prior_row=True: the seq exists locally as UNKNOWN (dropped before B)."""
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    if prior_row:
        dep.injector.arm("gw.g1.forward_invoke", "drop")
        a.invoke(dep.g1, d_a, "pay-10")
        assert a.resume(dep.g1, d_a).ok
    barrier = threading.Barrier(2)
    out = {}

    def run(payload):
        barrier.wait()
        out[payload] = a.invoke(dep.g1, d_a, payload, seq=1)

    ts = [threading.Thread(target=run, args=(p,)) for p in ("pay-10", "pay-1000")]
    [t.start() for t in ts]
    [t.join(20) for t in ts]

    row = ledger(a, d_a, 1)
    accepted = [p for p, r in out.items() if r.ok]
    rejected = [p for p, r in out.items() if r.code is Code.SEQUENCE_CONFLICT]
    assert len(accepted) == 1 and len(rejected) == 1, {p: r.code for p, r in out.items()}
    assert row["state"] == "COMMITTED"
    assert row["payload"] == accepted[0]
    assert row["result"] == f"done:{accepted[0]}"
    if prior_row:
        assert accepted == ["pay-10"]           # the recorded payload wins
    assert dep.b.tool.executions() == 1


# -- Test G -------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_g_late_failure_cannot_overwrite_commit(make, mode):
    """Attempt 1 reaches B and commits, but its response is held and finally
    arrives as an error. Meanwhile attempt 2 (same seq, same payload) is
    answered from B's ledger and commits locally. The late error must lose."""
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    real = dep.g1.invoke
    hold, reached = threading.Event(), threading.Event()
    n = {"calls": 0}

    def delayed(ctx, up, seq, payload, attempt=None):  # v0.2.2: forwards attempt
        n["calls"] += 1
        res = real(ctx, up, seq, payload, attempt=attempt)
        if n["calls"] == 1:                      # the first attempt's reply is late
            reached.set()
            assert hold.wait(10)
            return OpResult(Code.DOWNSTREAM_UNAVAILABLE, detail="late timeout")
        return res

    dep.g1.invoke = delayed
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("late", a.invoke(dep.g1, d_a, "pay-10")))
    t.start()
    assert reached.wait(10)
    second = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    assert second.ok
    committed = ledger(a, d_a, 1)
    assert committed["state"] == "COMMITTED"

    hold.set()
    t.join(10)
    assert out["late"].code is Code.DOWNSTREAM_UNAVAILABLE
    assert ledger(a, d_a, 1) == committed      # late error did not overwrite
    assert dep.b.tool.executions() == 1


# -- Agent B's payload-digest check stays in force (defence in depth) --------
# Agent A now rejects conflicts locally, so the v0.2.0 audit test no longer
# reaches Agent B. These tests bypass Agent A's ledger to keep B's check covered.

@pytest.mark.parametrize("mode", MODES)
def test_b_digest_check_still_rejects_when_a_is_bypassed(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    assert a.invoke(dep.g1, d_a, "pay-10").ok
    via_gateway = dep.g1.invoke(a.ctx, d_a, 1, "pay-1000")
    assert via_gateway.code is Code.SEQUENCE_CONFLICT
    assert dep.b.tool.executions() == 1


@pytest.mark.parametrize("mode", MODES)
def test_adopted_seq_conflicting_with_b_is_marked_not_committed(make, mode):
    """Explicit seq with no local row (Agent A lost its ledger) and a payload
    that differs from what B executed: B rejects, A records CONFLICT, and the
    next automatic seq skips past the adopted number."""
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    assert a.invoke(dep.g1, d_a, "pay-10").ok
    with a.db.tx() as c:                       # simulate the lost local row
        c.execute("DELETE FROM ops WHERE dialog_id=? AND seq=1", (d_a,))
        c.execute("UPDATE dialogs SET next_seq=1 WHERE dialog_id=?", (d_a,))

    r = a.invoke(dep.g1, d_a, "pay-1000", seq=1)

    assert r.code is Code.SEQUENCE_CONFLICT
    assert ledger(a, d_a, 1) == {"state": "CONFLICT", "payload": "pay-1000", "result": None}
    nxt = a.invoke(dep.g1, d_a, "pay-20")
    assert nxt.ok
    assert ledger(a, d_a, 2)["state"] == "COMMITTED"
    assert dep.b.tool.executions() == 2
