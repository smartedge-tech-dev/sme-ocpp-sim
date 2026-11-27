"""OCPP 2.0.1 models and codec: round trips, CALLERROR mapping, lenient/strict, nulls, datetimes."""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sme_ocpp_sim.protocol.v201 as v201_pkg
from sme_ocpp_sim.protocol.v201 import models as m
from sme_ocpp_sim.protocol.v201.codec import dump, model_for, parse
from sme_ocpp_sim.rpc.errors import OCPP201_ERRORS as E
from sme_ocpp_sim.rpc.errors import CallError

from v201_fixtures import CUSTOM, FIXTURES, ID_TOKEN_MIN, T1, TRANSACTION_EVENT_MIN

CASES = [
    pytest.param(action, kind, variant, id=f"{action}-{kind}-{variant}")
    for (action, kind), variants in FIXTURES.items()
    for variant in variants
]


def test_registry_holds_the_core_messages():
    assert m.ACTIONS == ("BootNotification", "Heartbeat", "StatusNotification", "Authorize",
                         "TransactionEvent", "MeterValues", "DataTransfer", "RequestStartTransaction",
                         "RequestStopTransaction", "UnlockConnector", "Reset", "ChangeAvailability",
                         "TriggerMessage", "GetVariables", "SetVariables", "GetBaseReport", "NotifyReport",
                         "SetChargingProfile", "ClearChargingProfile", "GetCompositeSchedule",
                         "GetChargingProfiles", "ReportChargingProfiles", "NotifyChargingLimit",
                         "ClearedChargingLimit", "LogStatusNotification", "FirmwareStatusNotification",
                         "SendLocalList", "GetLocalListVersion", "ClearCache", "ReserveNow", "CancelReservation",
                         "ReservationStatusUpdate", "UpdateFirmware", "GetLog")
    assert set(FIXTURES) == {(a, k) for a in m.ACTIONS for k in ("request", "response")}


def test_package_is_independent_of_v16():
    """Per-version stacks (#32): no module of protocol.v201 imports from protocol.v16."""
    for path in Path(v201_pkg.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            assert not any("v16" in n for n in names), f"{path.name} imports {names}"


@pytest.mark.parametrize("action,kind,variant", CASES)
def test_round_trip(action, kind, variant):
    payload = FIXTURES[(action, kind)][variant]
    model, ignored = parse(action, payload, kind, strict=True)
    assert ignored == []
    assert isinstance(model, model_for(action, kind))
    assert dump(model) == payload


def _err(action, payload, kind="request", strict=False):
    with pytest.raises(CallError) as exc:
        parse(action, payload, kind, strict=strict)
    return exc.value


BOOT_CS = {"model": "M", "vendorName": "V"}


@pytest.mark.parametrize(
    "action,payload,kind,code",
    [
        # occurrence: required field missing, array bounds
        ("BootNotification", {"reason": "PowerUp"}, "request", E.occurrence_constraint_violation),
        ("Authorize", {"idTokenInfo": {}}, "response", E.occurrence_constraint_violation),
        ("MeterValues", {"evseId": 1, "meterValue": []}, "request", E.occurrence_constraint_violation),
        ("MeterValues", {"evseId": 1, "meterValue": [{"timestamp": T1, "sampledValue": []}]}, "request",
         E.occurrence_constraint_violation),
        ("Heartbeat", {"customData": {}}, "request", E.occurrence_constraint_violation),
        # type
        ("StatusNotification", {"timestamp": T1, "connectorStatus": "Available", "evseId": "1",
                                "connectorId": 1}, "request", E.type_constraint_violation),
        ("BootNotification", {"currentTime": T1, "interval": 1.0, "status": "Accepted"}, "response",
         E.type_constraint_violation),
        ("BootNotification", {"currentTime": T1, "interval": True, "status": "Accepted"}, "response",
         E.type_constraint_violation),
        ("Authorize", {"idToken": {"idToken": 123, "type": "Local"}}, "request", E.type_constraint_violation),
        ("Heartbeat", {"currentTime": "yesterday"}, "response", E.type_constraint_violation),
        ("BootNotification", {"chargingStation": BOOT_CS, "reason": 1}, "request", E.type_constraint_violation),
        ("TransactionEvent", dict(TRANSACTION_EVENT_MIN, offline="false"), "request",
         E.type_constraint_violation),
        ("Authorize", {"idToken": ID_TOKEN_MIN, "certificate": None}, "request", E.type_constraint_violation),
        # property: enum / maxLength
        ("BootNotification", {"chargingStation": BOOT_CS, "reason": "Boot"}, "request",
         E.property_constraint_violation),
        ("StatusNotification", {"timestamp": T1, "connectorStatus": "Charging", "evseId": 1, "connectorId": 1},
         "request", E.property_constraint_violation),
        ("Authorize", {"idToken": {"idToken": "X" * 37, "type": "Local"}}, "request",
         E.property_constraint_violation),
        ("DataTransfer", {"vendorId": "v" * 256}, "request", E.property_constraint_violation),
    ],
)
def test_error_mapping(action, payload, kind, code):
    assert _err(action, payload, kind).code == code


def test_wire_names_of_the_mapped_codes():
    assert (E.format_violation, E.occurrence_constraint_violation) == (
        "FormatViolation", "OccurrenceConstraintViolation")


def test_error_reports_nested_field_path():
    payload = dict(TRANSACTION_EVENT_MIN, meterValue=[{"timestamp": T1, "sampledValue": [{"value": "1"}]}])
    err = _err("TransactionEvent", payload)
    assert err.code == E.type_constraint_violation
    assert err.details["field"] == "meterValue[0].sampledValue[0].value"


def test_strict_unknown_field_is_format_violation():
    err = _err("Heartbeat", {"foo": 1}, strict=True)
    assert err.code == E.format_violation == "FormatViolation"
    assert err.details["field"] == "foo"


def test_lenient_unknown_fields_ignored_and_reported():
    payload = {
        "evseId": 1,
        "vendorX": True,
        "meterValue": [{"timestamp": T1, "sampledValue": [{"value": 1.5, "extra": 2}]}],
    }
    model, ignored = parse("MeterValues", payload)
    assert sorted(ignored) == ["meterValue[0].sampledValue[0].extra", "vendorX"]
    assert dump(model) == {"evseId": 1, "meterValue": [{"timestamp": T1, "sampledValue": [{"value": 1.5}]}]}
    assert "vendorX" in payload  # caller's dict is not modified


def test_lenient_unknown_field_does_not_hide_real_error():
    err = _err("BootNotification", {"chargingStation": BOOT_CS, "reason": "Boot", "foo": 1})
    assert err.code == E.property_constraint_violation


def test_custom_data_keeps_vendor_properties_also_in_strict_mode():
    payload = {"customData": CUSTOM}
    model, ignored = parse("Heartbeat", payload, strict=True)
    assert ignored == []
    assert model.custom_data.vendor_id == "com.example"
    assert dump(model) == payload


def test_null_allowed_only_where_the_schema_allows_any_value():
    model, _ = parse("DataTransfer", {"vendorId": "v", "data": {"k": None}})
    assert dump(model) == {"vendorId": "v", "data": {"k": None}}
    err = _err("DataTransfer", {"vendorId": "v", "messageId": None})
    assert (err.code, err.details["field"]) == (E.type_constraint_violation, "messageId")
    err = _err("Heartbeat", {"customData": {"vendorId": None}})
    assert err.code == E.type_constraint_violation


def test_unknown_action_and_non_object_payload():
    assert _err("FooBar", {}).code == E.not_implemented == "NotImplemented"
    assert _err("BootNotification", {}, "response").code == E.occurrence_constraint_violation
    assert _err("Heartbeat", []).code == E.format_violation


def test_identifiers_stay_strings():
    model, _ = parse("Authorize", {"idToken": ID_TOKEN_MIN})
    assert model.id_token.id_token == "00123"
    model, _ = parse("TransactionEvent", TRANSACTION_EVENT_MIN)
    assert model.transaction_info.transaction_id == "0001"
    assert dump(model)["transactionInfo"] == {"transactionId": "0001"}


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 11, 26, 9, 35, 32, 123456, tzinfo=timezone.utc), "2026-11-26T09:35:32.123Z"),
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
    req = m.TransactionEventRequest(
        event_type="Started", timestamp=datetime(2026, 1, 1), trigger_reason="Authorized", seq_no=0,
        transaction_info=m.TransactionType(transaction_id="tx-1"),
        id_token=m.IdTokenType(id_token="00123", type="ISO14443"),
    )
    assert dump(req) == {
        "eventType": "Started",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "triggerReason": "Authorized",
        "seqNo": 0,
        "transactionInfo": {"transactionId": "tx-1"},
        "idToken": {"idToken": "00123", "type": "ISO14443"},
    }
