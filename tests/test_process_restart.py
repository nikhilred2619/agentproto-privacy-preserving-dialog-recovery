"""Acceptance tests for Agent A process-restart recovery (v0.2.2).

Every test runs Agent A in separate OS processes (see restart_harness.py):
P1 persists an operation as SENT and is SIGKILLed before recording the ack;
P2 (and in H, P3) is a NEW process opened on the same SQLite ledger. The
downstream server process (gateways, Agent B, external tool) outlives P1, so
the original execution can complete late. All state is checked by reading
Agent A's ledger, Agent B's ledger and the tool's execution table directly.

Applicable configurations: all six modes when the gateway that carried the
operation survives (A-I). The combined case where that gateway also fails is
run for the five modes that can resume through a replacement gateway; for
scoped-endpoint-noticket it is a documented unsupported variant and the test
asserts that it stays explicitly unresolved (fail closed).
"""

import hashlib

import pytest

from restart_harness import Cluster

ALL = ["global-store", "global-endpoint", "scoped-store", "scoped-endpoint-noticket",
       "scoped-endpoint-ticket", "scoped-endpoint-medticket"]
FAILOVER_OK = [m for m in ALL if m != "scoped-endpoint-noticket"]


@pytest.fixture
def cluster(request, tmp_path):
    c = Cluster(tmp_path, request.param)
    yield c
    c.close()


def crash_p1(c: Cluster, gate: str):
    """P1: establish, persist seq 1 'pay-10' as SENT, request held at `gate`
    ('tool' = executing at B; 'pre_b' = not yet received by B); SIGKILL P1."""
    c.ctl("arm", gate=gate)
    p1 = c.agent("P1")
    est = p1.call("establish", task="transfer")
    assert est["code"] == "OK"
    d = est["dialog"]
    p1.send("invoke", dialog=d, payload="pay-10")
    assert c.ctl("wait_entered", gate=gate)
    assert p1.sigkill() == -9
    return p1, d


def one(rows):
    assert len(rows) == 1, rows
    return rows[0]


def late_reply(c: Cluster, p1):
    """Reply the server produced for P1's original invoke (P1 never got it)."""
    return one([x for x in c.done("invoke") if x["pid"] == p1.pid])["reply"]


def no_submissions(c: Cluster, proc):
    return c.calls("invoke", pid=proc.pid) == []


# -- A + B ---------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_a_b_sent_survives_restart_with_identity(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    before = one(c.a_ops())
    dialog_before = one(c.a_dialogs())
    assert before["state"] == "SENT" and before["owner"] == p1.incarnation
    assert (before["dialog_id"], before["seq"], before["payload"], before["attempt"]) == \
        (d, 1, "pay-10", 1)
    assert dialog_before["next_seq"] == 2

    p2 = c.agent("P2")
    assert len({p1.pid, p2.pid, c.server_pid}) == 3
    assert p2.incarnation != p1.incarnation

    recovered = p2.call("recover")

    assert recovered == [{"dialog_id": d, "seq": 1, "payload": "pay-10", "attempt": 1}]
    after = one(c.a_ops())
    assert after == {**before, "state": "IN_DOUBT"}          # only the state changed
    assert one(c.a_dialogs()) == dialog_before                # next_seq, epoch, ticket kept
    assert p2.call("unresolved", dialog=d) == [
        {"dialog_id": d, "seq": 1, "payload": "pay-10", "state": "IN_DOUBT", "attempt": 1}]
    b = one(c.b_ops())
    assert b["state"] == "PENDING"
    assert b["payload_digest"] == hashlib.sha256(b"pay-10").hexdigest()
    assert c.executions() == []
    assert no_submissions(c, p2)
    c.ctl("release", gate="tool")


# -- C -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_c_original_execution_completes_late_and_is_recorded_once(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    p2.call("recover")

    first = p2.call("reconcile", dialog=d)
    assert [(r["outcome"], r["code"], r["status"]) for r in first] == \
        [("UNRESOLVED", "OUTCOME_UNKNOWN", "IN_PROGRESS")]
    assert one(c.a_ops())["state"] == "IN_DOUBT"
    assert c.executions() == []

    c.ctl("release", gate="tool")                       # executor outlived P1
    assert c.ctl("wait_done", method="invoke", n=1)
    assert late_reply(c, p1)["code"] == "OK"            # delivered to nobody

    second = p2.call("reconcile", dialog=d)
    assert [(r["outcome"], r["applied"]) for r in second] == [("COMMITTED", True)]
    row = one(c.a_ops())
    assert (row["state"], row["result"], row["payload"], row["attempt"]) == \
        ("COMMITTED", "done:pay-10", "pay-10", 1)
    assert one(c.b_ops())["state"] == "COMMITTED"
    ex = one(c.executions())
    assert ex["payload"] == "pay-10" and ex["op_key"].endswith(":1:1")
    assert no_submissions(c, p2)


# -- D -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_d_not_yet_executed_is_fenced_then_retried_with_original_identity(cluster):
    c = cluster
    p1, d = crash_p1(c, "pre_b")                        # request still in flight to B
    p2 = c.agent("P2")
    p2.call("recover")

    res = p2.call("reconcile", dialog=d)
    assert [(r["outcome"], r["applied"]) for r in res] == [("NOT_EXECUTED", True)]
    assert one(c.a_ops())["state"] == "NOT_EXECUTED"
    assert one(c.b_ops())["state"] == "NOT_EXECUTED"
    assert one(c.b_table("op_fences"))["fenced_through"] == 1
    assert c.executions() == []

    c.ctl("release", gate="pre_b")                      # the stale request arrives late
    assert c.ctl("wait_done", method="invoke", n=1)
    assert late_reply(c, p1)["code"] == "OPERATION_ABORTED"
    assert c.executions() == []                         # fenced: did not execute

    retry = p2.call("retry", dialog=d, seq=1)
    assert retry["code"] == "OK" and retry["status"] == "COMMITTED"
    row = one(c.a_ops())
    assert (row["dialog_id"], row["seq"], row["payload"], row["state"], row["attempt"]) == \
        (d, 1, "pay-10", "COMMITTED", 2)
    assert one(c.a_dialogs())["next_seq"] == 2
    assert one(c.executions())["payload"] == "pay-10"
    assert len(c.calls("invoke", pid=p2.pid)) == 1      # only the explicit retry


@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_d2_zombie_executor_after_simulated_b_restart_is_fenced_at_the_tool(cluster):
    """Agent A really restarts (P1 SIGKILLed, P2 a new OS process). Agent B's
    restart is SIMULATED inside the downstream server process: B gets a new
    incarnation id while the old executor thread is still inside the tool
    call. Reconciliation fences the attempt at the tool (assumption M7); the
    old executor is then refused; the retry executes once under a new key."""
    c = cluster
    p1, d = crash_p1(c, "tool")
    c.ctl("restart_b")
    p2 = c.agent("P2")
    p2.call("recover")

    res = p2.call("reconcile", dialog=d)
    assert [r["outcome"] for r in res] == ["NOT_EXECUTED"]
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert late_reply(c, p1)["code"] == "OPERATION_ABORTED"
    assert c.executions() == []

    assert p2.call("retry", dialog=d, seq=1)["code"] == "OK"
    ex = one(c.executions())
    assert ex["payload"] == "pay-10" and ex["op_key"].endswith(":1:2")
    assert one(c.a_ops())["state"] == "COMMITTED"


# -- E -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_e_dialog_resumption_submits_nothing(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    snap = c.snapshot()

    for _ in range(2):
        assert p2.call("resume", dialog=d)["code"] == "OK"

    after = c.snapshot()
    assert after["a_ops"] == snap["a_ops"]              # still SENT, untouched
    assert after["b_ops"] == snap["b_ops"]
    assert after["executions"] == snap["executions"] == []
    assert one(after["a_dialogs"])["next_seq"] == one(snap["a_dialogs"])["next_seq"] == 2
    assert {x["method"] for x in c.calls(pid=p2.pid)} == {"resume"}

    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert [r["outcome"] for r in p2.call("reconcile", dialog=d)] == ["COMMITTED"]
    assert p2.call("resume", dialog=d)["code"] == "OK"
    assert len(c.executions()) == 1
    assert no_submissions(c, p2)


# -- F -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_f_conflicting_payload_rejected_after_restart(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    before = one(c.a_ops())

    r = p2.call("invoke", dialog=d, payload="pay-1000", seq=1)       # while in doubt
    assert r["code"] == "SEQUENCE_CONFLICT"
    assert one(c.a_ops()) == before
    assert c.calls(pid=p2.pid) == []                                  # nothing sent at all

    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    p2.call("reconcile", dialog=d)
    committed = one(c.a_ops())
    assert committed["state"] == "COMMITTED"

    r = p2.call("invoke", dialog=d, payload="pay-1000", seq=1)       # after commit
    assert r["code"] == "SEQUENCE_CONFLICT"
    same = p2.call("invoke", dialog=d, payload="pay-10", seq=1)
    assert same["code"] == "OK" and same["detail"] == "deduplicated"
    assert one(c.a_ops()) == committed
    assert len(c.executions()) == 1
    assert no_submissions(c, p2)


# -- G -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_g_late_stale_answer_cannot_overwrite_commit(cluster):
    """P2 asks B twice. The first answer (NOT_EXECUTED for attempt 1) is held
    in P2 and delivered only after the second answer led to a retry that
    COMMITTED as attempt 2. The stale answer must not change the row."""
    c = cluster
    p1, d = crash_p1(c, "pre_b")
    p2 = c.agent("P2")
    p2.call("recover")

    assert p2.call("reconcile_async_hold", dialog=d) is True
    assert p2.call("wait_held") is True
    assert one(c.a_ops())["state"] == "IN_DOUBT"         # held answer not yet applied

    assert [r["outcome"] for r in p2.call("reconcile", dialog=d)] == ["NOT_EXECUTED"]
    assert p2.call("retry", dialog=d, seq=1)["code"] == "OK"
    committed = one(c.a_ops())
    assert (committed["state"], committed["attempt"]) == ("COMMITTED", 2)

    stale = p2.call("release_held")
    assert [(r["outcome"], r["applied"], r["ledger_state"]) for r in stale] == \
        [("NOT_EXECUTED", False, "COMMITTED")]
    assert one(c.a_ops()) == committed

    c.ctl("release", gate="pre_b")                      # and the original attempt arrives
    assert c.ctl("wait_done", method="invoke", n=2)
    # B checks "already committed with this payload" before the fence, so the
    # stale attempt is answered from B's ledger; nothing executes (in D, with
    # nothing committed yet, the fence answers OPERATION_ABORTED instead).
    late = late_reply(c, p1)
    assert (late["code"], late["status"], late["detail"]) == ("OK", "COMMITTED", "deduplicated")
    assert one(c.a_ops()) == committed
    assert len(c.executions()) == 1


# -- H -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_h_repeated_reconciliation_and_second_restart_are_idempotent(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    p2.call("recover")
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert [r["outcome"] for r in p2.call("reconcile", dialog=d)] == ["COMMITTED"]
    snap = c.snapshot()

    assert p2.call("reconcile", dialog=d) == []
    assert p2.call("reconcile", dialog=d) == []
    assert c.snapshot() == snap

    assert p2.sigkill() == -9
    p3 = c.agent("P3")
    assert len({p1.pid, p2.pid, p3.pid}) == 3
    assert p3.call("recover") == []
    assert c.snapshot() == snap
    assert p3.call("reconcile", dialog=d) == []
    assert p3.call("settle", dialog=d) == []
    same = p3.call("invoke", dialog=d, payload="pay-10", seq=1)
    assert same["code"] == "OK" and same["detail"] == "deduplicated"
    assert c.snapshot() == snap
    assert c.calls(pid=p3.pid) == []
    assert len(c.executions()) == 1


@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_h2_two_restarts_before_resolution(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    p2.call("recover")
    assert [r["outcome"] for r in p2.call("reconcile", dialog=d)] == ["UNRESOLVED"]
    assert p2.sigkill() == -9

    p3 = c.agent("P3")
    assert p3.call("recover") == []                     # already IN_DOUBT, not SENT
    assert [u["state"] for u in p3.call("unresolved", dialog=d)] == ["IN_DOUBT"]
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert [r["outcome"] for r in p3.call("reconcile", dialog=d)] == ["COMMITTED"]
    assert len(c.executions()) == 1
    assert no_submissions(c, p2) and no_submissions(c, p3)


# -- I -------------------------------------------------------------------------

@pytest.mark.parametrize("cluster", ALL, indirect=True)
def test_i_unavailable_downstream_leaves_explicitly_unresolved(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    p2 = c.agent("P2")
    p2.call("recover")

    c.ctl("store_b_unavailable", on=True)
    r = one(p2.call("reconcile", dialog=d))
    assert (r["outcome"], r["code"]) == ("UNRESOLVED", "DOWNSTREAM_UNAVAILABLE")
    c.ctl("store_b_unavailable", on=False)

    c.ctl("set_alive", gw="g1", alive=False)
    r = one(p2.call("reconcile", dialog=d))
    assert (r["outcome"], r["code"]) == ("UNRESOLVED", "GATEWAY_DOWN")
    assert [x["code"] for x in p2.call("settle", dialog=d)] == ["GATEWAY_DOWN"]
    retry = p2.call("invoke", dialog=d, payload="pay-10", seq=1)
    assert (retry["code"], retry["status"]) == ("OUTCOME_UNKNOWN", "IN_DOUBT")
    assert p2.call("retry", dialog=d, seq=1)["code"] == "INVALID_STATE"

    assert one(c.a_ops())["state"] == "IN_DOUBT"
    assert c.executions() == []
    assert no_submissions(c, p2)

    c.ctl("set_alive", gw="g1", alive=True)
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert [r["outcome"] for r in p2.call("reconcile", dialog=d)] == ["COMMITTED"]
    assert len(c.executions()) == 1
    assert no_submissions(c, p2)


# -- combined: the gateway that carried the operation also fails ---------------

@pytest.mark.parametrize("cluster", FAILOVER_OK, indirect=True)
def test_restart_combined_with_gateway_failover(cluster):
    c = cluster
    p1, d = crash_p1(c, "tool")
    c.ctl("set_alive", gw="g1", alive=False)            # in-flight call keeps running
    p2 = c.agent("P2")
    p2.call("recover")

    assert p2.call("resume", gw="g2", dialog=d)["code"] == "OK"
    assert [r["outcome"] for r in p2.call("reconcile", gw="g2", dialog=d)] == ["UNRESOLVED"]
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    assert [r["outcome"] for r in p2.call("reconcile", gw="g2", dialog=d)] == ["COMMITTED"]
    assert one(c.a_ops())["state"] == "COMMITTED"
    assert len(c.executions()) == 1
    assert no_submissions(c, p2)


@pytest.mark.parametrize("cluster", ["scoped-endpoint-noticket"], indirect=True)
def test_restart_combined_with_failover_unsupported_variant_fails_closed(cluster):
    """Documented unsupported variant: scoped ids, no ticket, the carrying
    gateway is gone and its replacement cannot see its state. P2 cannot reach
    Agent B's record for the operation, so it must stay explicitly unresolved
    even after the original execution completed."""
    c = cluster
    p1, d = crash_p1(c, "tool")
    c.ctl("set_alive", gw="g1", alive=False)
    p2 = c.agent("P2")
    p2.call("recover")

    assert p2.call("resume", gw="g2", dialog=d)["code"] == "MAPPING_NOT_FOUND"
    c.ctl("release", gate="tool")
    assert c.ctl("wait_done", method="invoke", n=1)
    r = one(p2.call("reconcile", gw="g2", dialog=d))
    assert (r["outcome"], r["code"]) == ("UNRESOLVED", "MAPPING_NOT_FOUND")
    assert one(c.a_ops())["state"] == "IN_DOUBT"
    assert len(c.executions()) == 1                      # happened; A cannot learn it here
    assert no_submissions(c, p2)
