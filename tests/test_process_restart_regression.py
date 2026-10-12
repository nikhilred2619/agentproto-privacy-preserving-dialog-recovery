"""Regression: Agent A process restart with an operation persisted as SENT.

Scenario suggested by N. Templeman in the AgentProto discussion after he
reviewed v0.2.1: persist an operation as SENT, kill
Agent A's process before the acknowledgement is recorded, restart Agent A in
a NEW OS process from the same SQLite ledger, and let the original downstream
execution, which outlives the old process, finish late.

These tests drive Agent A only through operations that already existed in
v0.2.1 (establish, invoke, resume, settle_unknown_ops), so they run unchanged
against the v0.2.1 baseline, where they fail, and against v0.2.2.

Processes: pytest controller, downstream server (gateways + Agent B + tool),
Agent A process P1 (SIGKILLed), Agent A process P2 (restart).
"""

import pytest

from restart_harness import Cluster

MODES = ["global-store", "scoped-store", "scoped-endpoint-ticket", "scoped-endpoint-medticket"]


@pytest.fixture
def cluster(request, tmp_path):
    c = Cluster(tmp_path, request.param)
    yield c
    c.close()


def crash_p1_mid_operation(c: Cluster):
    """P1 establishes a dialog, persists op seq=1 as SENT, its request reaches
    the external tool and is held there; P1 is then SIGKILLed."""
    c.ctl("arm", gate="tool")
    p1 = c.agent("P1")
    est = p1.call("establish", task="transfer")
    assert est["code"] == "OK"
    d = est["dialog"]
    p1.send("invoke", dialog=d, payload="pay-10")      # P1 blocks waiting for the ack
    assert c.ctl("wait_entered", gate="tool")          # B recorded PENDING; tool running
    [row] = c.a_ops()
    assert (row["dialog_id"], row["seq"], row["payload"], row["state"]) == (d, 1, "pay-10", "SENT")
    rc = p1.sigkill()
    assert rc == -9                                     # killed, never recorded the ack
    return p1, d


@pytest.mark.parametrize("cluster", MODES, indirect=True)
def test_restarted_agent_does_not_resend_an_in_doubt_operation(cluster):
    c = cluster
    p1, d = crash_p1_mid_operation(c)
    p2 = c.agent("P2")
    assert p2.pid not in (p1.pid, c.server_pid)

    # The application asks the restarted agent to retry seq 1 with the same
    # payload while the original execution is still running downstream.
    r = p2.call("invoke", dialog=d, payload="pay-10", seq=1)

    assert r["code"] == "OUTCOME_UNKNOWN"
    assert c.calls("invoke", pid=p2.pid) == [], "SENT row was treated as permission to resend"
    assert [x["state"] for x in c.a_ops()] != ["COMMITTED"]
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert len(c.executions()) == 1


@pytest.mark.parametrize("cluster", MODES, indirect=True)
def test_restarted_agent_reaches_authoritative_outcome(cluster):
    c = cluster
    p1, d = crash_p1_mid_operation(c)
    p2 = c.agent("P2")
    assert p2.pid not in (p1.pid, c.server_pid)

    assert p2.call("resume", dialog=d)["code"] == "OK"
    p2.call("settle", dialog=d)                          # original still executing
    [row] = c.a_ops()
    assert row["state"] not in ("COMMITTED", "FAILED", "NOT_EXECUTED")
    assert c.calls("invoke", pid=p2.pid) == []

    c.ctl("release", gate="tool")                        # original finishes late
    assert c.ctl("wait_done", method="invoke", n=1)
    p2.call("settle", dialog=d)

    [row] = c.a_ops()
    assert (row["dialog_id"], row["seq"], row["payload"]) == (d, 1, "pay-10")
    assert row["state"] == "COMMITTED", f"restarted ledger left the operation {row['state']}"
    assert row["result"] == "done:pay-10"
    assert len(c.executions()) == 1
    assert c.calls("invoke", pid=p2.pid) == []
    p2.exit()
