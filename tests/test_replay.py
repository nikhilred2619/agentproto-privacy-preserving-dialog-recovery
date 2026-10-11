"""T08, T15 and ticket freshness."""

import pytest

from agentproto_recovery.evaluation import s_t08, s_t15
from agentproto_recovery.security import new_identifier

from conftest import RECOVERABLE


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t08_stale_reference_replay(make, mode):
    o = s_t08(make(mode))
    assert o.code == "STALE_REFERENCE"
    assert o.extra["b_epoch"] == 2  # unchanged by the replay


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t15_repeated_recovery_request_is_idempotent(make, mode):
    o = s_t15(make(mode))
    assert o.extra["first"] == "OK" and o.code == "OK"
    assert o.extra["same_result"]
    assert o.extra["b_epoch"] == 1


@pytest.mark.parametrize("mode", ["scoped-endpoint-ticket", "scoped-endpoint-medticket"])
def test_tickets_rotate_and_old_ticket_is_stale(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    t0 = a.state(d_a)[2]
    assert a.resume(dep.g1, d_a).ok
    t1 = a.state(d_a)[2]
    assert a.resume(dep.g1, d_a).ok
    t2 = a.state(d_a)[2]
    assert len({t0, t1, t2}) == 3  # fresh nonce and new epoch each time
    dep.crash_g1()
    # two epochs behind, new request id, independent gateway -> stale
    r = dep.g2.resume(a.ctx, d_a, 0, new_identifier("rq"), t0)
    assert r.code.value == "STALE_REFERENCE"
    assert dep.b.dialog(dep.truth[d_a]).epoch == 2


def test_one_step_behind_is_treated_as_idempotent_retry(make):
    """Design choice: B answers a repeat of its most recent transition with
    the recorded result (no state change). Needed so that a retry through a
    different gateway after a lost response does not strand the dialog."""
    dep = make("scoped-endpoint-ticket")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    t0 = a.state(d_a)[2]
    assert a.resume(dep.g1, d_a).ok
    dep.crash_g1()
    r = dep.g2.resume(a.ctx, d_a, 0, new_identifier("rq"), t0)
    assert r.ok and r.epoch == 1
    assert dep.b.dialog(dep.truth[d_a]).epoch == 1


@pytest.mark.parametrize("mode", ["scoped-endpoint-ticket", "scoped-endpoint-medticket"])
def test_ticket_epoch_must_match_presented_epoch(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    t0 = a.state(d_a)[2]
    dep.crash_g1()
    r = dep.g2.resume(a.ctx, d_a, 5, new_identifier("rq"), t0)
    assert r.code.value == "STALE_REFERENCE"
