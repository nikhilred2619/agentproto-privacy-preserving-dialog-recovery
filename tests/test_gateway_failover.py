"""T03, T04, T05, T16, T16b, T20."""

import pytest

from agentproto_recovery.evaluation import s_t03, s_t04, s_t05, s_t16, s_t16b, s_t20
from agentproto_recovery.lifecycle import State

from conftest import ALL_MODES, RECOVERABLE


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t03_network_interruption_then_resume(make, mode):
    o = s_t03(make(mode))
    assert o.code == "OK" and o.correct_association
    assert o.extra["invoke_during_outage"] == "DOWNSTREAM_UNAVAILABLE"
    # the dropped request never reached B; resending it executed it once
    assert o.extra["settled"] == ["COMMITTED"]
    assert o.extra["tool_executions"] == 1


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t04_g1_crash_g2_recovery(make, mode):
    o = s_t04(make(mode))
    assert o.extra["g1_alive"] is False
    if mode in RECOVERABLE:
        assert o.code == "OK" and o.correct_association
        assert o.extra["invoke_after"] == "OK"
        assert o.extra["tool_executions"] == 3
    else:
        # scoped IDs, replacement gateway cannot read G1's store, no ticket:
        # recovery is impossible and must be reported, not guessed
        assert o.code == "MAPPING_NOT_FOUND" and o.explicit_failure


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t05_missing_mapping(make, mode):
    o = s_t05(make(mode))
    if mode == "global-endpoint":
        # the global identifier itself is sufficient recovery material
        assert o.code == "OK" and o.correct_association
    else:
        assert o.code == "MAPPING_NOT_FOUND" and o.correct_association is None


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t16_crash_inside_finalize_transaction(make, mode):
    o = s_t16(make(mode))
    assert o.extra["first"] == "GATEWAY_DOWN"
    if mode in ("global-store", "scoped-store"):
        assert o.extra["during_lease"] == "CONCURRENT_RECOVERY"
    assert o.code == "OK" and o.correct_association
    assert o.extra["b_epoch"] == 1           # exactly one transition at B
    assert o.extra["store_state"] == State.ACTIVE.value


def test_t16_store_is_consistent_after_crash(make):
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.injector.arm("gw.g2.finalize_before_commit", "crash")
    a.resume(dep.g2, d_a)
    rec = dep.g2.store.get("tenant-x", d_a)  # tag verifies => no torn write
    assert rec.state == State.RESUMING.value and rec.owner == "g2" and rec.epoch == 0


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t16b_crash_during_establishment(make, mode):
    o = s_t16b(make(mode))
    assert o.extra["establish"] == "GATEWAY_DOWN"
    assert o.extra["mapping_rows"] == 0
    assert o.extra["orphan_b_dialogs"] == 1   # known limitation: B-side orphan
    if mode == "global-endpoint":
        assert o.code == "OK" and o.correct_association
    else:
        assert o.code == "MAPPING_NOT_FOUND"


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t20_storage_unavailable(make, mode):
    o = s_t20(make(mode))
    assert o.code == "STORE_UNAVAILABLE"
    if mode in RECOVERABLE:
        assert o.extra["b_store_down"] == "DOWNSTREAM_UNAVAILABLE"
        assert o.extra["after_restore"] == "OK"


def test_t20_establish_fails_closed_without_store(make):
    dep = make("scoped-store")
    a = dep.agent_a()
    dep.injector.arm(dep.g1.store.db.unavailable_point, "active", None)
    code, d_a = dep.establish(a)
    assert code.value == "STORE_UNAVAILABLE"
    assert a.state(d_a)[0] == State.ESTABLISHING.value
