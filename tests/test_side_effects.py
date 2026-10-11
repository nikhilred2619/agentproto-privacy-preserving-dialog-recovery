"""T14, T14b: no duplicate side effects after recovery."""

import pytest

from agentproto_recovery.errors import Code
from agentproto_recovery.evaluation import s_t14, s_t14b

from conftest import RECOVERABLE


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t14_committed_but_ack_lost(make, mode):
    o = s_t14(make(mode))
    assert o.code == "OK" and o.correct_association
    assert o.extra["invoke_code"] == "DOWNSTREAM_UNAVAILABLE"
    assert o.extra["settled"] == ["COMMITTED"]   # answered from B's ledger
    assert o.extra["tool_executions"] == 1


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t14b_unknown_outcome_requires_reconciliation(make, mode):
    o = s_t14b(make(mode))
    assert o.extra["settled"] == ["OUTCOME_UNKNOWN"]
    assert o.extra["executions_before_reconcile"] == 1
    assert o.extra["reconcile"] == "COMMITTED"
    assert o.extra["after_reconcile"] == "COMMITTED"
    assert o.extra["tool_executions"] == 1


def test_resend_same_seq_is_deduplicated(make):
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    assert a.invoke(dep.g1, d_a, "p").ok
    r = a.invoke(dep.g1, d_a, "p", seq=1)
    assert r.ok and r.detail == "deduplicated"
    assert dep.b.tool.executions() == 1


def test_reconcile_when_tool_never_ran(make):
    """Write-ahead intent recorded but the tool call itself was lost."""
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    d_b = dep.truth[d_a]
    with dep.b.db.tx() as c:
        c.execute("INSERT INTO ops VALUES (?,?,?,NULL)", (d_b, 1, "PENDING"))
    assert dep.b.op_status(a.ctx, d_b, 1).code is Code.OUTCOME_UNKNOWN
    assert dep.b.reconcile(d_b, 1) == "NOT_EXECUTED"
    assert a.invoke(dep.g1, d_a, "p", seq=1).status == "COMMITTED"
    assert dep.b.tool.executions() == 1
