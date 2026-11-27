"""OCPP 1.6J models and codec: round trips, CALLERROR mapping, lenient/strict, datetimes."""

from datetime import datetime, timedelta, timezone

import pytest

from sme_ocpp_sim.protocol.v16 import models as m
from sme_ocpp_sim.protocol.v16.codec import dump, parse
from sme_ocpp_sim.rpc.errors import OCPP16_ERRORS, CallError

from v16_fixtures import FIXTURES, PROFILE_MIN, T1

CASES = [
    pytest.param(action, kind, variant, id=f"{action}-{kind}-{variant}")
    for (action, kind), variants in FIXTURES.items()
    for variant in variants
]


def test_fixtures_cover_registry():
    expected = {(a, "request") for a in m.REQUESTS} | {(a, "response") for a in m.RESPONSES}
    assert set(FIXTURES) == expected
    assert len(m.REQUESTS) == len(m.RESPONSES) == 28 + len(m.SECURITY_ACTIONS) == 35


@pytest.mark.parametrize("action,kind,variant", CASES)
def test_round_trip(action, kind, variant):
    payload = FIXTURES[(action, kind)][variant]
    model, ignored = parse(action, payload, kind, strict=True)
    assert ignored == []
    assert isinstance(model, (m.REQUESTS if kind == "request" else m.RESPONSES)[action])
    assert dump(model) == payload


def _code(action, payload, kind="request", strict=False):
    with pytest.raises(CallError) as exc:
        parse(action, payload, kind, strict=strict)
    return exc.value


@pytest.mark.parametrize(
    "action,payload,kind,code",
    [
        # missing required field
        ("BootNotification", {"chargePointVendor": "SME"}, "request",
         OCPP16_ERRORS.occurrence_constraint_violation),
        ("AuthorizeResponse", {"idTagInfo": {}}, "response",
         OCPP16_ERRORS.occurrence_constraint_violation),
        ("MeterValues", {"connectorId": 1, "meterValue": []}, "request",
         OCPP16_ERRORS.occurrence_constraint_violation),
        # wrong JSON type
        ("UnlockConnector", {"connectorId": "1"}, "request",
         OCPP16_ERRORS.type_constraint_violation),
        ("UnlockConnector", {"connectorId": 1.0}, "request",
         OCPP16_ERRORS.type_constraint_violation),
        ("UnlockConnector", {"connectorId": True}, "request",
         OCPP16_ERRORS.type_constraint_violation),
        ("Authorize", {"idTag": 123}, "request", OCPP16_ERRORS.type_constraint_violation),
        ("Authorize", {"idTagInfo": "Accepted"}, "response",
         OCPP16_ERRORS.type_constraint_violation),
        ("Reset", {"type": 1}, "request", OCPP16_ERRORS.type_constraint_violation),
        ("StartTransaction",
         {"connectorId": 1, "idTag": "A", "meterStart": 0, "timestamp": "yesterday"},
         "request", OCPP16_ERRORS.type_constraint_violation),
        ("StatusNotification",
         {"connectorId": 1, "errorCode": "NoError", "status": "Available", "info": None},
         "request", OCPP16_ERRORS.type_constraint_violation),
        # value not allowed
        ("Reset", {"type": "Warm"}, "request", OCPP16_ERRORS.property_constraint_violation),
        ("Authorize", {"idTag": "X" * 21}, "request",
         OCPP16_ERRORS.property_constraint_violation),
        ("ChangeConfiguration", {"key": "K", "value": "v" * 501}, "request",
         OCPP16_ERRORS.property_constraint_violation),
    ],
)
def test_error_mapping(action, payload, kind, code):
    action = action.removesuffix("Response")
    assert _code(action, payload, kind).code == code


def test_error_reports_nested_field_path():
    payload = {"connectorId": 1, "csChargingProfiles": dict(PROFILE_MIN, stackLevel="0")}
    err = _code("SetChargingProfile", payload)
    assert err.code == OCPP16_ERRORS.type_constraint_violation
    assert err.details["field"] == "csChargingProfiles.stackLevel"


def test_strict_unknown_field_is_formation_violation():
    err = _code("Heartbeat", {"foo": 1}, strict=True)
    assert err.code == OCPP16_ERRORS.format_violation == "FormationViolation"
    assert err.details["field"] == "foo"


def test_lenient_unknown_fields_ignored_and_reported():
    payload = {
        "connectorId": 1,
        "vendorX": True,
        "meterValue": [{"timestamp": T1, "sampledValue": [{"value": "1", "extra": 2}]}],
    }
    model, ignored = parse("MeterValues", payload)
    assert sorted(ignored) == ["meterValue[0].sampledValue[0].extra", "vendorX"]
    assert dump(model) == {
        "connectorId": 1,
        "meterValue": [{"timestamp": T1, "sampledValue": [{"value": "1"}]}],
    }
    assert "vendorX" in payload  # caller's dict is not modified


def test_lenient_unknown_field_does_not_hide_real_error():
    err = _code("Reset", {"type": "Warm", "foo": 1})
    assert err.code == OCPP16_ERRORS.property_constraint_violation


def test_unknown_action_and_non_object_payload():
    assert _code("FooBar", {}).code == OCPP16_ERRORS.not_implemented
    assert _code("Heartbeat", []).code == OCPP16_ERRORS.format_violation


def test_id_tag_leading_zeros_preserved():
    model, _ = parse("Authorize", {"idTag": "00123"})
    assert model.id_tag == "00123"
    assert dump(model) == {"idTag": "00123"}


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 11, 26, 9, 35, 32, 123456, tzinfo=timezone.utc),
         "2026-11-26T09:35:32.123Z"),
        (datetime(2026, 11, 26, 9, 35, 32), "2026-11-26T09:35:32.000Z"),  # naive = UTC
        (datetime(2026, 11, 26, 18, 35, 32, 5000, tzinfo=timezone(timedelta(hours=9))),
         "2026-11-26T09:35:32.005Z"),
    ],
)
def test_datetime_output_format(value, expected):
    assert dump(m.HeartbeatResponse(current_time=value)) == {"currentTime": expected}


def test_inbound_offset_datetime_normalized_to_utc():
    model, _ = parse("Heartbeat", {"currentTime": "2026-11-26T18:35:32+09:00"}, "response")
    assert dump(model) == {"currentTime": "2026-11-26T09:35:32.000Z"}


def test_dump_excludes_none_and_uses_aliases():
    req = m.StartTransactionRequest(
        connector_id=1, id_tag="00123", meter_start=0, timestamp=datetime(2026, 1, 1)
    )
    assert dump(req) == {
        "connectorId": 1,
        "idTag": "00123",
        "meterStart": 0,
        "timestamp": "2026-01-01T00:00:00.000Z",
    }
