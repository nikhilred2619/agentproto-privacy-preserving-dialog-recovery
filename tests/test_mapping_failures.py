"""T06, T07, T12, T12b and the unsafe-guess negative control."""

import pytest

from agentproto_recovery.evaluation import s_t06, s_t07, s_t12, s_t12b

from conftest import ALL_MODES, STORE_MODES


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t06_similar_dialogs_never_confused(make, mode):
    o = s_t06(make(mode))
    assert o.correct_association in (None, True)
    if mode == "global-endpoint":
        assert o.code == "OK"
    else:
        assert o.code == "MAPPING_NOT_FOUND"
    if mode != "scoped-endpoint-noticket":
        assert o.extra["d2_correct"]


def test_t06_negative_control_detects_wrong_association(make):
    """A gateway that guesses on a missing mapping resumes the wrong dialog.
    This shows the oracle is not vacuous."""
    o = s_t06(make("scoped-store", unsafe_guess=True))
    assert o.recovered and o.correct_association is False


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t07_cross_tenant_identical_local_ids(make, mode):
    o = s_t07(make(mode))
    assert o.correct_association in (None, True)
    if mode.startswith("global"):
        # B keys dialogs by the global id: the second tenant's establishment
        # is refused explicitly (collision / squatting surface)
        assert o.extra["establish_y"] == "INVALID_STATE"
    else:
        assert o.extra["establish_y"] == "OK"
    if mode in ("scoped-store", "scoped-endpoint-ticket", "scoped-endpoint-medticket"):
        assert o.code == "OK" and o.extra["y_correct"]


@pytest.mark.parametrize("mode", ALL_MODES)
def test_t12_tampered_recovery_material(make, mode):
    o = s_t12(make(mode))
    expected = {
        "global-store": "MAPPING_INTEGRITY_FAILURE",
        "scoped-store": "MAPPING_INTEGRITY_FAILURE",
        "scoped-endpoint-ticket": "TICKET_INVALID",
        "scoped-endpoint-medticket": "TICKET_INVALID",
        "global-endpoint": "DIALOG_NOT_FOUND",
        "scoped-endpoint-noticket": "NOT_APPLICABLE",
    }[mode]
    assert o.code == expected and not o.recovered


@pytest.mark.parametrize("mode", STORE_MODES)
def test_t12b_rollback_passes_integrity_but_fails_freshness(make, mode):
    o = s_t12b(make(mode))
    assert o.extra["rolled_back"]
    assert o.code == "STALE_REFERENCE"


@pytest.mark.parametrize("mode", STORE_MODES)
def test_corrupted_row_garbage(make, mode):
    dep = make(mode)
    a = dep.agent_a()
    _, d_a = dep.establish(a)
    dep.crash_g1()
    dep.raw_sql(dep.g1, "UPDATE correlation SET state='BOGUS', tag='00' WHERE upstream_id=?",
                (d_a,))
    assert a.resume(dep.g2, d_a).code.value == "MAPPING_INTEGRITY_FAILURE"
