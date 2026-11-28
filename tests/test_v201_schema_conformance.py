"""Check the 2.0.1 models against the official OCPP 2.0.1 JSON schemas, both ways.

Schema dir: env ``OCPP201_SCHEMA_DIR``, else the copy shipped with the dev dependency ``ocpp``
(``ocpp/v201/schemas``; never copied into this repository). Skipped when neither is there.

1. The pydantic JSON schema of every core model matches the official one (properties, required,
   types, enums, maxLength, array bounds, additionalProperties).
2. Every fixture parses strictly and its dump validates against the official schema.
3. Payloads the official schema rejects are rejected by the models too.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from sme_ocpp_sim.protocol.v201 import models as m  # noqa: E402
from sme_ocpp_sim.protocol.v201.codec import dump, parse  # noqa: E402
from sme_ocpp_sim.protocol.v201.schema import ocpp_package_schema_dir  # noqa: E402
from sme_ocpp_sim.rpc.errors import CallError  # noqa: E402

from v201_fixtures import CHARGING_PROFILE_MIN, FIXTURES, ID_TOKEN_MIN, T1, TRANSACTION_EVENT_MIN  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_env_dir = os.environ.get("OCPP201_SCHEMA_DIR")
SCHEMA_DIR = Path(_env_dir) if _env_dir else ocpp_package_schema_dir()

pytestmark = pytest.mark.skipif(
    SCHEMA_DIR is None or not (SCHEMA_DIR / "BootNotificationRequest.json").is_file(),
    reason=f"OCPP 2.0.1 schemas not found (OCPP201_SCHEMA_DIR / ocpp package): {SCHEMA_DIR}",
)


def _schema(action, kind):
    name = action + ("Response" if kind == "response" else "Request")
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _model(action, kind):
    return (m.REQUESTS if kind == "request" else m.RESPONSES)[action]


def _resolve(node, defs):
    while True:
        if "$ref" in node:
            node = defs[node["$ref"].rsplit("/", 1)[-1]]
        elif "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            assert len(options) == 1, node
            node = options[0]
        elif "allOf" in node and len(node["allOf"]) == 1:
            node = node["allOf"][0]
        else:
            return node


def _closed(node):
    """additionalProperties as a bool: absent means open (JSON Schema default)."""
    return node.get("additionalProperties", True) is False


def _compare(oca, mine, oca_defs, my_defs, path, problems):
    oca, mine = _resolve(oca, oca_defs), _resolve(mine, my_defs)
    where = path or "<root>"
    for key in ("type", "format", "maxLength", "minItems", "maxItems"):
        if oca.get(key) != mine.get(key):
            problems.append(f"{where}: {key} {mine.get(key)!r} != schema {oca.get(key)!r}")
    if set(oca.get("enum", ())) != set(mine.get("enum", ())):
        problems.append(f"{where}: enum {sorted(mine.get('enum', ()))} != {sorted(oca.get('enum', ()))}")
    if oca.get("type") == "object":
        if _closed(oca) != _closed(mine):
            problems.append(f"{where}: additionalProperties closed={_closed(mine)} != schema {_closed(oca)}")
        o_props, m_props = oca.get("properties", {}), mine.get("properties", {})
        if set(o_props) != set(m_props):
            problems.append(f"{where}: properties {sorted(m_props)} != {sorted(o_props)}")
        if set(oca.get("required", ())) != set(mine.get("required", ())):
            problems.append(
                f"{where}: required {sorted(mine.get('required', ()))} != {sorted(oca.get('required', ()))}"
            )
        for name in set(o_props) & set(m_props):
            sub = f"{path}.{name}" if path else name
            _compare(o_props[name], m_props[name], oca_defs, my_defs, sub, problems)
    elif oca.get("type") == "array":
        _compare(oca["items"], mine.get("items", {}), oca_defs, my_defs, path + "[]", problems)


KEYS = sorted(FIXTURES)


def test_fixtures_cover_the_core_registry():
    expected = {(a, k) for a in m.ACTIONS for k in ("request", "response")}
    assert set(FIXTURES) == expected


@pytest.mark.parametrize("action,kind", KEYS, ids=[f"{a}-{k}" for a, k in KEYS])
def test_model_schema_matches_official(action, kind):
    oca = _schema(action, kind)
    mine = _model(action, kind).model_json_schema(by_alias=True)
    problems: list[str] = []
    _compare(oca, mine, oca.get("definitions", {}), mine.get("$defs", {}), "", problems)
    assert problems == []


@pytest.mark.parametrize("action,kind", KEYS, ids=[f"{a}-{k}" for a, k in KEYS])
def test_fixtures_valid_against_official(action, kind):
    validator = jsonschema.Draft6Validator(_schema(action, kind))
    for payload in FIXTURES[(action, kind)].values():
        validator.validate(payload)
        model, _ = parse(action, payload, kind, strict=True)
        validator.validate(dump(model))


LONG_TX_ID = dict(TRANSACTION_EVENT_MIN, transactionInfo={"transactionId": "T" * 37})

INVALID = [
    # required field missing (top level and nested)
    ("BootNotification", "request", {"reason": "PowerUp"}),
    ("BootNotification", "request", {"chargingStation": {"model": "M"}, "reason": "PowerUp"}),
    ("Heartbeat", "response", {}),
    ("StatusNotification", "request", {"timestamp": T1, "connectorStatus": "Available", "evseId": 1}),
    ("Authorize", "request", {"idToken": {"idToken": "X"}}),
    ("TransactionEvent", "request", {k: v for k, v in TRANSACTION_EVENT_MIN.items() if k != "seqNo"}),
    ("MeterValues", "request", {"evseId": 1}),
    ("DataTransfer", "request", {"messageId": "x"}),
    ("DataTransfer", "response", {}),
    # maxLength
    ("BootNotification", "request", {"chargingStation": {"model": "M" * 21, "vendorName": "V"}, "reason": "PowerUp"}),
    ("Authorize", "request", {"idToken": {"idToken": "X" * 37, "type": "Local"}}),
    ("TransactionEvent", "request", LONG_TX_ID),
    ("DataTransfer", "request", {"vendorId": "v" * 256}),
    # enum
    ("BootNotification", "request", {"chargingStation": {"model": "M", "vendorName": "V"}, "reason": "Boot"}),
    ("StatusNotification", "request",
     {"timestamp": T1, "connectorStatus": "Charging", "evseId": 1, "connectorId": 1}),
    ("BootNotification", "response", {"currentTime": T1, "interval": 1, "status": "accepted"}),
    # JSON type
    ("StatusNotification", "request",
     {"timestamp": T1, "connectorStatus": "Available", "evseId": "1", "connectorId": 1}),
    ("BootNotification", "response", {"currentTime": T1, "interval": 1.5, "status": "Accepted"}),
    ("MeterValues", "request", {"evseId": 1, "meterValue": [{"timestamp": T1, "sampledValue": [{"value": "1"}]}]}),
    ("TransactionEvent", "request", dict(TRANSACTION_EVENT_MIN, offline="false")),
    ("Authorize", "request", {"idToken": ID_TOKEN_MIN, "certificate": None}),
    # array bounds
    ("MeterValues", "request", {"evseId": 1, "meterValue": []}),
    ("MeterValues", "request", {"evseId": 1, "meterValue": [{"timestamp": T1, "sampledValue": []}]}),
    ("Authorize", "request", {"idToken": ID_TOKEN_MIN, "iso15118CertificateHashData": [
        FIXTURES[("Authorize", "request")]["full"]["iso15118CertificateHashData"][0]] * 5}),
    # unknown field (closed objects); customData's vendorId is required
    ("Heartbeat", "request", {"foo": 1}),
    ("Authorize", "response", {"idTokenInfo": {"status": "Accepted", "foo": 1}}),
    ("Heartbeat", "request", {"customData": {"other": 1}}),
    # M3 remote control
    ("RequestStartTransaction", "request", {"idToken": ID_TOKEN_MIN}),
    ("RequestStartTransaction", "request", {"idToken": ID_TOKEN_MIN, "remoteStartId": "1"}),
    ("RequestStartTransaction", "request", {"idToken": ID_TOKEN_MIN, "remoteStartId": 1,
                                            "chargingProfile": dict(CHARGING_PROFILE_MIN, chargingSchedule=[])}),
    ("RequestStartTransaction", "request", {"idToken": ID_TOKEN_MIN, "remoteStartId": 1, "chargingProfile": dict(
        CHARGING_PROFILE_MIN, chargingSchedule=CHARGING_PROFILE_MIN["chargingSchedule"] * 4)}),
    ("RequestStartTransaction", "request", {"idToken": ID_TOKEN_MIN, "remoteStartId": 1, "chargingProfile": dict(
        CHARGING_PROFILE_MIN, chargingProfilePurpose="Tx")}),
    ("RequestStartTransaction", "response", {"status": "Scheduled"}),
    ("RequestStopTransaction", "request", {"transactionId": "T" * 37}),
    ("UnlockConnector", "request", {"evseId": 1}),
    ("UnlockConnector", "response", {"status": "NotSupported"}),
    ("Reset", "request", {"type": "Hard"}),
    ("Reset", "response", {"status": "Accepted", "statusInfo": {"reasonCode": "R" * 21}}),
    ("ChangeAvailability", "request", {"evse": {"connectorId": 1}, "operationalStatus": "Operative"}),
    ("ChangeAvailability", "request", {"operationalStatus": "Unavailable"}),
    ("TriggerMessage", "request", {"requestedMessage": "DiagnosticsStatusNotification"}),
    ("TriggerMessage", "response", {"status": "NotSupported"}),
    # M4 Device Model
    ("GetVariables", "request", {"getVariableData": []}),
    ("GetVariables", "request", {"getVariableData": [{"component": {"name": "C"}, "variable": {"name": "V" * 51}}]}),
    ("GetVariables", "request", {"getVariableData": [{"attributeType": "Minimum", "component": {"name": "C"},
                                                      "variable": {"name": "V"}}]}),
    ("GetVariables", "response", {"getVariableResult": [{"attributeStatus": "RebootRequired",
                                                         "component": {"name": "C"}, "variable": {"name": "V"}}]}),
    ("SetVariables", "request", {"setVariableData": [{"component": {"name": "C"}, "variable": {"name": "V"}}]}),
    ("SetVariables", "request", {"setVariableData": [{"attributeValue": "x" * 1001, "component": {"name": "C"},
                                                      "variable": {"name": "V"}}]}),
    ("SetVariables", "response", {"setVariableResult": [{"attributeStatus": "Accepted", "component": {"name": "C"}}]}),
    ("GetBaseReport", "request", {"requestId": 1, "reportBase": "Everything"}),
    ("GetBaseReport", "request", {"requestId": "1", "reportBase": "FullInventory"}),
    ("GetBaseReport", "response", {"status": "Scheduled"}),
    ("NotifyReport", "request", {"requestId": 1, "generatedAt": T1}),
    ("NotifyReport", "request", {"requestId": 1, "generatedAt": T1, "seqNo": 0, "reportData": []}),
    ("NotifyReport", "request", {"requestId": 1, "generatedAt": T1, "seqNo": 0, "reportData": [
        {"component": {"name": "C"}, "variable": {"name": "V"}, "variableAttribute": [{}] * 5}]}),
    ("NotifyReport", "request", {"requestId": 1, "generatedAt": T1, "seqNo": 0, "reportData": [
        {"component": {"name": "C"}, "variable": {"name": "V"}, "variableAttribute": [{}],
         "variableCharacteristics": {"dataType": "passwordString", "supportsMonitoring": False}}]}),
]


INVALID += [
    # M5 smart charging
    ("SetChargingProfile", "request", {"chargingProfile": CHARGING_PROFILE_MIN}),
    ("SetChargingProfile", "request", {"evseId": 1, "chargingProfile": dict(
        CHARGING_PROFILE_MIN, chargingProfilePurpose="ChargePointMaxProfile")}),
    ("SetChargingProfile", "request", {"evseId": 1, "chargingProfile": dict(CHARGING_PROFILE_MIN, chargingSchedule=[
        CHARGING_PROFILE_MIN["chargingSchedule"][0]] * 4)}),
    ("SetChargingProfile", "response", {"status": "Unknown"}),
    ("ClearChargingProfile", "request", {"chargingProfileCriteria": {"evseId": "1"}}),
    ("ClearChargingProfile", "response", {"status": "Rejected"}),
    ("GetCompositeSchedule", "request", {"duration": 60}),
    ("GetCompositeSchedule", "request", {"duration": 60, "evseId": 1, "chargingRateUnit": "kW"}),
    ("GetCompositeSchedule", "response", {"status": "Accepted", "schedule": {
        "evseId": 1, "duration": 60, "scheduleStart": T1, "chargingRateUnit": "W", "chargingSchedulePeriod": []}}),
    ("GetChargingProfiles", "request", {"requestId": 1}),
    ("GetChargingProfiles", "request", {"requestId": 1, "chargingProfile": {"chargingLimitSource": []}}),
    ("GetChargingProfiles", "request", {"requestId": 1, "chargingProfile": {"chargingProfileId": []}}),
    ("GetChargingProfiles", "response", {"status": "Rejected"}),
    ("ReportChargingProfiles", "request", {"requestId": 1, "chargingLimitSource": "CSO", "evseId": 1,
                                           "chargingProfile": []}),
    ("ReportChargingProfiles", "request", {"requestId": 1, "chargingLimitSource": "CSMS", "evseId": 1,
                                           "chargingProfile": [CHARGING_PROFILE_MIN]}),
    ("NotifyChargingLimit", "request", {}),
    ("NotifyChargingLimit", "request", {"chargingLimit": {"chargingLimitSource": "EMS"}, "chargingSchedule": []}),
    ("ClearedChargingLimit", "request", {"evseId": 1}),
]


@pytest.mark.parametrize("action,kind,payload", INVALID)
def test_schema_invalid_payloads_rejected(action, kind, payload):
    assert not jsonschema.Draft6Validator(_schema(action, kind)).is_valid(payload)
    with pytest.raises(CallError):
        parse(action, payload, kind, strict=True)


def test_no_official_schema_files_in_the_repository():
    """The OCPP schemas carry no license grant for redistribution: they must never be committed."""
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    names = {f"{a}{k}.json" for a in m.ACTIONS for k in ("Request", "Response")}
    # sme_ocpp_sim/schemas/ocpp2.1/ is the deliberately shipped OCPP 2.1 reference copy (CC BY-ND, THIRD-PARTY-NOTICES).
    hits = [p for p in tracked.splitlines()
            if (p.rsplit("/", 1)[-1] in names and not p.startswith("sme_ocpp_sim/schemas/ocpp2.1/"))
            or p.startswith(".oca-schemas/")]
    assert hits == []
