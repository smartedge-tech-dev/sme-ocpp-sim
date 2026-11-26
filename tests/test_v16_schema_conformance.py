"""Check the 1.6J models against the official OCA JSON schemas.

Schema dir: env ``OCPP16_SCHEMA_DIR`` or ``<repo>/.oca-schemas/ocpp1.6/`` (filled by
``scripts/fetch_oca_schemas.py``). Skipped when absent.
"""

import json
import os
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from sme_ocpp_sim.protocol.v16 import models as m  # noqa: E402
from sme_ocpp_sim.protocol.v16.codec import dump, parse  # noqa: E402
from sme_ocpp_sim.rpc.errors import CallError  # noqa: E402

from v16_fixtures import FIXTURES, T1  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SCHEMA_DIR = Path(os.environ.get("OCPP16_SCHEMA_DIR") or REPO / ".oca-schemas" / "ocpp1.6")

pytestmark = pytest.mark.skipif(
    not (SCHEMA_DIR / "BootNotification.json").is_file(),
    reason=f"OCA 1.6 schemas not found in {SCHEMA_DIR}",
)

# Deliberate model/schema differences: (action, kind, path) -> enum values only the model has.
KNOWN_EXTRA_ENUM_VALUES: dict = {}

# Deliberate model/schema differences: (action, kind, path) -> minItems the model enforces
# although the official schema has none. Source: OCPP 1.6 core spec ed.2 §6.31
# MeterValues.req (p.67) "meterValue ... 1..*" and §7.33 MeterValue (p.88)
# "sampledValue ... 1..*". Neither errata sheet relaxes this.
KNOWN_STRICTER_MIN_ITEMS = {
    ("MeterValues", "request", "meterValue"): 1,
    ("MeterValues", "request", "meterValue[].sampledValue"): 1,
}


def _schema(action, kind):
    name = action + ("Response" if kind == "response" else "")
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


def _compare(oca, mine, defs, key, path, problems):
    mine = _resolve(mine, defs)
    where = path or "<root>"
    if oca.get("type") != mine.get("type"):
        problems.append(f"{where}: type {mine.get('type')!r} != schema {oca.get('type')!r}")
    if oca.get("maxLength") != mine.get("maxLength"):
        problems.append(f"{where}: maxLength {mine.get('maxLength')} != {oca.get('maxLength')}")
    expected_min = KNOWN_STRICTER_MIN_ITEMS.get(key + (path,), oca.get("minItems"))
    if expected_min != mine.get("minItems"):
        problems.append(f"{where}: minItems {mine.get('minItems')} != {oca.get('minItems')}")
    if "enum" in oca or "enum" in mine:
        extra = set(mine.get("enum", ())) - set(oca.get("enum", ()))
        missing = set(oca.get("enum", ())) - set(mine.get("enum", ()))
        if missing or extra != KNOWN_EXTRA_ENUM_VALUES.get(key + (path,), set()):
            problems.append(f"{where}: enum missing {sorted(missing)} extra {sorted(extra)}")
    if oca.get("type") == "object":
        o_props, m_props = oca.get("properties", {}), mine.get("properties", {})
        if set(o_props) != set(m_props):
            problems.append(f"{where}: properties {sorted(m_props)} != {sorted(o_props)}")
        if set(oca.get("required", ())) != set(mine.get("required", ())):
            problems.append(
                f"{where}: required {sorted(mine.get('required', ()))}"
                f" != {sorted(oca.get('required', ()))}"
            )
        for name in set(o_props) & set(m_props):
            sub = f"{path}.{name}" if path else name
            _compare(o_props[name], m_props[name], defs, key, sub, problems)
    elif oca.get("type") == "array":
        _compare(oca["items"], mine.get("items", {}), defs, key, path + "[]", problems)


KEYS = sorted(FIXTURES)


@pytest.mark.parametrize("action,kind", KEYS, ids=[f"{a}-{k}" for a, k in KEYS])
def test_model_schema_matches_oca(action, kind):
    mine = _model(action, kind).model_json_schema(by_alias=True)
    problems: list[str] = []
    _compare(_schema(action, kind), mine, mine.get("$defs", {}), (action, kind), "", problems)
    assert problems == []


@pytest.mark.parametrize("action,kind", KEYS, ids=[f"{a}-{k}" for a, k in KEYS])
def test_fixtures_valid_against_oca(action, kind):
    schema = _schema(action, kind)
    for payload in FIXTURES[(action, kind)].values():
        model, _ = parse(action, payload, kind, strict=True)
        jsonschema.validate(dump(model), schema)


INVALID = [
    ("BootNotification", "request", {"chargePointVendor": "SME"}),
    ("BootNotification", "request",
     {"chargePointVendor": "SME", "chargePointModel": "M" * 21}),
    ("UnlockConnector", "request", {"connectorId": "1"}),
    ("UnlockConnector", "request", {"connectorId": 1.5}),
    ("Reset", "request", {"type": "Warm"}),
    ("Heartbeat", "request", {"foo": 1}),
    ("Authorize", "response", {"idTagInfo": {"status": "Accepted", "foo": 1}}),
    ("StatusNotification", "request",
     {"connectorId": 1, "errorCode": "NoError", "status": "Available", "info": None}),
    ("GetCompositeSchedule", "request", {"connectorId": 0}),
]


@pytest.mark.parametrize("action,kind,payload", INVALID)
def test_schema_invalid_payloads_rejected(action, kind, payload):
    schema = _schema(action, kind)
    assert not jsonschema.Draft4Validator(schema).is_valid(payload)
    with pytest.raises(CallError):
        parse(action, payload, kind, strict=True)


# Payloads the official schema accepts but the spec text forbids (see
# KNOWN_STRICTER_MIN_ITEMS): the model must reject them.
SPEC_STRICTER = [
    ("MeterValues", "request", {"connectorId": 1, "meterValue": []}),
    ("MeterValues", "request",
     {"connectorId": 1, "meterValue": [{"timestamp": T1, "sampledValue": []}]}),
]


@pytest.mark.parametrize("action,kind,payload", SPEC_STRICTER)
def test_spec_stricter_than_schema(action, kind, payload):
    assert jsonschema.Draft4Validator(_schema(action, kind)).is_valid(payload)
    with pytest.raises(CallError):
        parse(action, payload, kind, strict=True)
