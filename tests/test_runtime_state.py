import pytest

from sme_ocpp_sim.protocol.base import DomainStatus as S
from sme_ocpp_sim.protocol.v16.state import CHARGER_TRANSITIONS, TRANSITIONS, ConnectorState, IllegalTransition


def test_table_covers_every_state():
    assert set(TRANSITIONS) == set(S)
    for src, dsts in TRANSITIONS.items():
        assert src not in dsts


def test_normal_session():
    c = ConnectorState(1)
    for st in (S.PREPARING, S.CHARGING, S.SUSPENDED_EV, S.CHARGING, S.SUSPENDED_EVSE, S.FINISHING, S.AVAILABLE):
        assert c.transition(st)
    assert c.status is S.AVAILABLE


@pytest.mark.parametrize("src,dst", [
    (S.AVAILABLE, S.FINISHING),
    (S.PREPARING, S.RESERVED),
    (S.PREPARING, S.UNAVAILABLE),
    (S.CHARGING, S.PREPARING),
    (S.CHARGING, S.RESERVED),
    (S.FINISHING, S.CHARGING),
    (S.RESERVED, S.CHARGING),
    (S.UNAVAILABLE, S.RESERVED),
])
def test_illegal_transitions_rejected(src, dst):
    c = ConnectorState(1, initial=src)
    assert not c.can_transition(dst)
    with pytest.raises(IllegalTransition):
        c.transition(dst)
    assert c.status is src


def test_same_state_is_noop():
    c = ConnectorState(1)
    assert c.transition(S.AVAILABLE) is False


def test_faulted_details():
    c = ConnectorState(1, initial=S.CHARGING)
    with pytest.raises(ValueError):
        c.transition(S.FAULTED)
    assert c.transition(S.FAULTED, error_code="GroundFailure", info="leak")
    assert (c.error_code, c.info) == ("GroundFailure", "leak")
    assert c.transition(S.FAULTED, error_code="GroundFailure", info="leak") is False
    assert c.transition(S.FAULTED, error_code="OverVoltage") is True
    assert c.error_code == "OverVoltage"
    assert c.transition(S.AVAILABLE)
    assert c.error_code is None


def test_connector_zero():
    assert set(CHARGER_TRANSITIONS) == {S.AVAILABLE, S.UNAVAILABLE, S.FAULTED}
    c = ConnectorState(0)
    assert c.transition(S.UNAVAILABLE) and c.transition(S.AVAILABLE)
    with pytest.raises(IllegalTransition):
        c.transition(S.CHARGING)
    assert c.transition(S.FAULTED, error_code="OtherError") and c.transition(S.UNAVAILABLE)
    with pytest.raises(IllegalTransition):
        ConnectorState(0, initial=S.PREPARING)
