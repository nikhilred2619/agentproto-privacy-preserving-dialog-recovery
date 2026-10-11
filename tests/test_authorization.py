"""T09, T10, T11, T13: authorization is checked separately from correlation."""

import pytest

from agentproto_recovery.errors import Code
from agentproto_recovery.evaluation import s_t09, s_t10, s_t11, s_t13

from conftest import RECOVERABLE


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t09_expired(make, mode):
    assert s_t09(make(mode)).code == "AUTHZ_EXPIRED"


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t10_revoked(make, mode):
    assert s_t10(make(mode)).code == "AUTHZ_REVOKED"


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t11_wrong_principal_with_valid_reference(make, mode):
    assert s_t11(make(mode)).code == "PRINCIPAL_MISMATCH"


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_t13_authz_unavailable_fails_closed(make, mode):
    assert s_t13(make(mode)).code == "AUTHZ_UNAVAILABLE"


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_valid_mapping_is_not_authorization(make, mode):
    """Mapping/ticket intact, principal correct, but no 'resume' grant."""
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.authz.grant("alice", "tenant-x", {"delegate", "invoke"})  # no resume
    assert a.resume(dep.g2, d_a).code is Code.AUTHZ_DENIED


@pytest.mark.parametrize("mode", RECOVERABLE)
def test_b_enforces_authz_even_if_gateway_skipped_it(make, mode):
    """Domain B does not rely on the gateway's authorization decision."""
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.authz.revoke("alice", "tenant-x")
    from agentproto_recovery.errors import RecoveryError
    with pytest.raises(RecoveryError) as e:
        dep.b.resume(a.ctx, 0, ref=dep.truth[d_a])
    assert e.value.code is Code.AUTHZ_REVOKED


def test_unauthenticated_context_rejected(make):
    from agentproto_recovery.messages import AuthContext
    dep = make("scoped-store")
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    r = dep.g1.resume(AuthContext("alice", "tenant-x", authenticated=False), d_a, 0, "rq_x", None)
    assert r.code is Code.UNAUTHENTICATED
