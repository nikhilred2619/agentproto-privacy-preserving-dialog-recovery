"""T01, T02, T19 and lifecycle / checkpoint invariants."""

import pytest

from agentproto_recovery.errors import Code
from agentproto_recovery.evaluation import s_t01, s_t02, s_t19
from agentproto_recovery.lifecycle import IllegalTransition, State, check_transition

from conftest import ALL_MODES


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t01_establish(make, mode):
    o = s_t01(make(mode))
    assert o.code == "OK"
    assert o.extra["b_dialog_exists"]
    assert o.extra["a_state"] == State.ACTIVE.value


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t02_continuation(make, mode):
    o = s_t02(make(mode))
    assert o.code == "OK"
    assert o.extra["tool_executions"] == 2


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t19_terminated_dialog_not_resumed(make, mode):
    o = s_t19(make(mode))
    assert not o.recovered
    expected = "MAPPING_NOT_FOUND" if mode == "scoped-endpoint-noticket" else "DIALOG_TERMINATED"
    assert o.code == expected


@pytest.mark.parametrize("mode", ALL_MODES)
def test_identifiers_are_independent_in_scoped_mode(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    if dep.mode.id_scheme == "scoped":
        assert dep.truth[d_a] != d_a
        assert d_a.startswith("da_") and dep.truth[d_a].startswith("db_")
    else:
        assert dep.truth[d_a] == d_a


def test_terminated_is_absorbing():
    for s in State:
        if s is not State.TERMINATED:
            with pytest.raises(IllegalTransition):
                check_transition(State.TERMINATED, s)


def test_older_checkpoint_cannot_overwrite_newer_state(make):
    """FR-13: restoring a saved record older than the stored one is refused."""
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    snap = dep.g1.store.get("tenant-x", d_a)
    assert a.resume(dep.g1, d_a).ok
    newer = dep.g1.store.get("tenant-x", d_a)
    from agentproto_recovery.errors import RecoveryError
    with pytest.raises(RecoveryError) as e:
        dep.g1.store.restore_checkpoint(snap)
    assert e.value.code is Code.STALE_REFERENCE
    assert dep.g1.store.get("tenant-x", d_a) == newer


def test_invoke_requires_active_mapping(make):
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.injector.arm("gw.g1.forward_invoke", "drop")
    assert a.invoke(dep.g1, d_a, "x").code is Code.DOWNSTREAM_UNAVAILABLE
    # gateway marked the dialog INTERRUPTED; new work must wait for resumption
    assert a.invoke(dep.g1, d_a, "y").code is Code.INVALID_STATE
    assert a.resume(dep.g1, d_a).ok
