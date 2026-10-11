"""T18: concurrent recovery through separate gateway instances."""

import threading

import pytest

from agentproto_recovery.evaluation import s_t18
from agentproto_recovery.security import new_identifier

from conftest import RECOVERABLE, STORE_MODES


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t18_two_gateways_race(make, mode):
    o = s_t18(make(mode))
    assert o.correct_association is True
    assert o.extra["b_epoch"] == 1          # B advanced exactly once
    assert o.extra["distinct_epochs"] == 1  # every success reports the same epoch
    if mode in STORE_MODES:
        assert o.extra["successes"] == 1
        assert "CONCURRENT_RECOVERY" in o.code
    else:
        # independent gateways cannot see each other's claims; B's
        # idempotent transition makes both observe the same outcome
        assert o.extra["successes"] in (1, 2)


@pytest.mark.parametrize("mode", STORE_MODES)
def test_eight_gateways_race_single_winner(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    gws = [dep.add_gateway(f"race{i}") for i in range(8)]
    barrier = threading.Barrier(len(gws))
    out = []
    lock = threading.Lock()

    def run(gw):
        barrier.wait()
        r = gw.resume(a.ctx, d_a, 0, new_identifier("rq"), None)
        with lock:
            out.append(r.code.value)

    ts = [threading.Thread(target=run, args=(g,)) for g in gws]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count("OK") == 1
    assert set(out) <= {"OK", "CONCURRENT_RECOVERY", "STALE_REFERENCE"}
    assert dep.b.dialog(dep.truth[d_a]).epoch == 1


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_parallel_recovery_of_distinct_dialogs(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    ids = [dep.establish(a, f"t{i}")[1] for i in range(20)]
    dep.crash_g1()
    gws = [dep.add_gateway(f"p{i}") for i in range(4)]
    res = {}
    lock = threading.Lock()

    def run(gw, mine):
        for d in mine:
            _, e, t = a.state(d)
            r = gw.resume(a.ctx, d, e, new_identifier("rq"), t)
            with lock:
                res[d] = r.code.value

    ts = [threading.Thread(target=run, args=(g, ids[i::4])) for i, g in enumerate(gws)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(v == "OK" for v in res.values()), res
    assert sorted(dep.b.oracle_resumed) == sorted(dep.truth[d] for d in ids)
