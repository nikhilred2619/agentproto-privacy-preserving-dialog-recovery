"""T17: identifier exposure and linkability, plus FR-15 log hygiene."""

import io
import json
import logging

import pytest

from agentproto_recovery.diagnostics import JsonFormatter, LOGGER_NAME
from agentproto_recovery.evaluation import analyze_privacy, privacy_workload

from conftest import RECOVERABLE

N = 12


@pytest.fixture
def report(make):
    def _r(mode):
        dep = make(mode)
        return analyze_privacy(dep, privacy_workload(dep, N)), dep
    return _r


@pytest.mark.parametrize("mode", ["global-store", "global-endpoint"])
def test_global_id_lets_endpoints_join(report, mode):
    r, _ = report(mode)
    assert r["endpoint_A_B_identifier_join"] == 1.0
    assert r["max_domains_sharing_one_value"] == len(r["domains"])


@pytest.mark.parametrize("mode", ["scoped-store", "scoped-endpoint-medticket"])
def test_scoped_ids_prevent_endpoint_identifier_join(report, mode):
    r, _ = report(mode)
    assert r["endpoint_A_B_identifier_join"] == 0.0
    assert r["max_domains_sharing_one_value"] < len(r["domains"])


def test_b_sealed_ticket_is_shared_by_a_and_b_but_rotates(report):
    """Finding: an endpoint-sealed ticket is a value both endpoints hold, so
    colluding endpoints can join on it. Unlike a global ID it changes on
    every resumption (two distinct shared values over one resumption)."""
    r, _ = report("scoped-endpoint-ticket")
    assert r["endpoint_A_B_identifier_join"] == 1.0
    assert r["mean_distinct_A_B_shared_values_per_dialog"] == 2


def test_global_id_is_one_value_forever(report):
    r, _ = report("global-store")
    assert r["mean_distinct_A_B_shared_values_per_dialog"] == 1


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_gateways_always_link_both_sides(report, mode):
    r, _ = report(mode)
    assert r["gateway_can_link_upstream_downstream"]["GW1"] == 1.0


@pytest.mark.parametrize("mode", ["scoped-endpoint-ticket", "scoped-endpoint-medticket"])
def test_independent_gateway_operators_can_join(report, mode):
    """The failed and replacement gateway operators both see A's id, B's id
    and the ticket that was issued through G1: their logs join."""
    r, _ = report(mode)
    assert r["identifier_join_fraction_by_domain_pair"]["GW1~GW2"] == 1.0


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_opaque_ids_do_not_stop_order_based_linkage(report, mode):
    """With a sequential workload, event order alone links A and B."""
    r, _ = report(mode)
    assert r["order_based_A_B_join_accuracy_sequential_workload"] == 1.0


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_logs_contain_no_raw_identifiers(make, mode):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    log = logging.getLogger(LOGGER_NAME)
    log.addHandler(h)
    log.setLevel(logging.INFO)
    try:
        dep = make(mode)
        privacy_workload(dep, 4)
        a = dep.agent_a("mallory")
        dep.g2.resume(a.ctx, "da_nonexistent", 0, "rq_x", None)
    finally:
        log.removeHandler(h)
    text = buf.getvalue()
    lines = [json.loads(x) for x in text.splitlines()]
    assert lines, "expected structured events"
    raw = {e.value for e in dep.obs.entries}
    assert not [v for v in raw if v in text]


def test_redaction_is_domain_keyed(make):
    dep = make("global-store")
    a = dep.agent_a()
    _, gid = dep.establish(a)
    from agentproto_recovery.security import Redactor, new_key
    assert Redactor(new_key())(gid) != Redactor(new_key())(gid)
