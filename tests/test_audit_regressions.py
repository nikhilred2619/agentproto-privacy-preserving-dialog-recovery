"""Regression tests for defects reported against v0.1.0.

These were written from the defect descriptions in the repair request
(2026-10-11); the audit report's own test files were not available. Each test
was run against v0.1.0 first and failed there (see docs/results.md, "Audit
repair").
"""

import threading

import pytest

from agentproto_recovery.security import new_identifier

TICKET_MODES = ["scoped-endpoint-ticket", "scoped-endpoint-medticket"]


# -- 1. a valid ticket for dialog 2 must not resume unrelated dialog 1 -------

@pytest.mark.parametrize("mode", TICKET_MODES)
def test_ticket_of_other_dialog_is_rejected(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d1 = dep.establish(a, "dialog one")
    _, d2 = dep.establish(a, "dialog two")
    _, e1, _ = a.state(d1)
    _, _, t2 = a.state(d2)          # valid, same principal, same epoch (0)
    dep.crash_g1()
    resumed_before = list(dep.b.oracle_resumed)

    r = dep.g2.resume(a.ctx, d1, e1, new_identifier("rq"), t2)

    assert not r.ok, "ticket of dialog 2 resumed dialog 1"
    assert r.code.value == "TICKET_MISMATCH"
    assert dep.b.oracle_resumed == resumed_before        # B resumed nothing
    assert dep.b.dialog(dep.truth[d2]).epoch == 0        # d2 untouched
    assert dep.g2.store.get("tenant-x", d1) is None      # no bad mapping written
    # the legitimate resumptions still work and land correctly
    assert a.resume(dep.g2, d1).ok and dep.resumed_correctly(d1)
    assert a.resume(dep.g2, d2).ok and dep.resumed_correctly(d2)


# -- 2. reconciliation must not clear an operation whose tool call is running -

def _blocking_tool(dep):
    """Wrap Agent B's tool so execute() waits on an event (test hook)."""
    gate, entered = threading.Event(), threading.Event()
    real = dep.b.tool.execute

    def slow(op_key, payload):
        entered.set()
        assert gate.wait(10)
        return real(op_key, payload)

    dep.b.tool.execute = slow
    return gate, entered


def test_reconcile_while_tool_call_in_flight_does_not_clear_pending(make):
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    d_b = dep.truth[d_a]
    gate, entered = _blocking_tool(dep)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", a.invoke(dep.g1, d_a, "pay-10")))
    t.start()
    assert entered.wait(10)                      # PENDING written, tool running

    verdict = dep.b.reconcile(d_b, 1)

    assert verdict == "IN_PROGRESS"
    gate.set()
    t.join(10)
    assert out["r"].ok
    # a retry of the same operation is answered from the ledger
    again = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    assert again.ok and again.detail == "deduplicated"
    assert dep.b.tool.executions() == 1


def test_late_tool_call_after_reconcile_is_fenced_not_duplicated(make):
    """The executor stalls past its lease. Reconciliation then decides the
    call did not happen. The stalled call must not execute afterwards, and the
    retry must execute exactly once."""
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    d_b = dep.truth[d_a]
    gate, entered = _blocking_tool(dep)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", a.invoke(dep.g1, d_a, "pay-10")))
    t.start()
    assert entered.wait(10)
    dep.clock.advance(600)                       # executor lease (30 s) long expired

    assert dep.b.reconcile(d_b, 1) == "NOT_EXECUTED"
    gate.set()                                   # stalled call finally reaches the tool
    t.join(10)
    assert not out["r"].ok
    assert dep.b.tool.executions() == 0          # fenced at the tool

    retry = a.invoke(dep.g1, d_a, "pay-10", seq=1)
    assert retry.ok and retry.status == "COMMITTED"
    assert dep.b.tool.executions() == 1


# -- 3. same sequence number, different payload ------------------------------

@pytest.mark.parametrize("state", ["committed", "pending"])
def test_duplicate_seq_with_different_payload_is_rejected(make, state):
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    if state == "committed":
        assert a.invoke(dep.g1, d_a, "pay-10").ok
    else:
        dep.injector.arm("b.after_tool_before_commit", "crash")
        a.invoke(dep.g1, d_a, "pay-10")          # executed, outcome unrecorded
        dep.g1.store.cas(dep.g1.store.get("tenant-x", d_a), state="ACTIVE")

    r = a.invoke(dep.g1, d_a, "pay-1000", seq=1)

    assert not r.ok
    assert r.code.value == "SEQUENCE_CONFLICT"
    assert dep.b.tool.executions() == 1
    same = a.invoke(dep.g1, d_a, "pay-10", seq=1)  # the genuine retry still works
    assert same.code.value in ("OK", "OUTCOME_UNKNOWN")
    assert dep.b.tool.executions() == 1


