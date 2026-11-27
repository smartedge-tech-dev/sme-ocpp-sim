"""OCPP 2.0.1 on the wire: error table, bad-frame policy, optional schema check and the dev peer."""

from __future__ import annotations

import asyncio
import json

import pytest
from rpc_fakes import FakeWs

from sme_ocpp_sim.protocol.v201.codec import dump, parse
from sme_ocpp_sim.rpc import OCPP16_ERRORS, OCPP201_ERRORS, RpcConnection, framer

from v201_fixtures import FIXTURES, ID_TOKEN_MIN, T1


async def _until(cond, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


# --------------------------------------------------------------------------------------------- error table


def test_error_table_wire_names():
    e = OCPP201_ERRORS
    assert {
        "not_implemented": e.not_implemented,
        "not_supported": e.not_supported,
        "internal_error": e.internal_error,
        "protocol_error": e.protocol_error,
        "security_error": e.security_error,
        "format_violation": e.format_violation,
        "property_constraint_violation": e.property_constraint_violation,
        "occurrence_constraint_violation": e.occurrence_constraint_violation,
        "type_constraint_violation": e.type_constraint_violation,
        "generic_error": e.generic_error,
        "rpc_framework_error": e.rpc_framework_error,
        "message_type_not_supported": e.message_type_not_supported,
    } == {
        "not_implemented": "NotImplemented",
        "not_supported": "NotSupported",
        "internal_error": "InternalError",
        "protocol_error": "ProtocolError",
        "security_error": "SecurityError",
        "format_violation": "FormatViolation",
        "property_constraint_violation": "PropertyConstraintViolation",
        "occurrence_constraint_violation": "OccurenceConstraintViolation",
        "type_constraint_violation": "TypeConstraintViolation",
        "generic_error": "GenericError",
        "rpc_framework_error": "RpcFrameworkError",
        "message_type_not_supported": "MessageTypeNotSupported",
    }


def test_v16_table_unchanged_by_the_201_codes():
    assert OCPP16_ERRORS.rpc_framework_error is None
    assert OCPP16_ERRORS.message_type_not_supported is None
    assert OCPP16_ERRORS.bad_frame_reply is None


# --------------------------------------------------------------------------------------------- bad-frame policy


async def _answers(codes, frames) -> list:
    ws = FakeWs()
    rpc = RpcConnection(ws, {}, error_codes=codes)
    rx = asyncio.create_task(rpc.run())
    for f in frames:
        ws.feed(f)
    ws.feed([2, "marker", "FooBar", {}])  # always answered: everything before it has been handled
    await _until(lambda: any(json.loads(r)[1] == "marker" for r in ws.sent))
    await rpc.close()
    await rx
    return [f for f in (json.loads(r) for r in ws.sent) if f[1] != "marker"]


@pytest.mark.parametrize("raw", ["garbage", "{}", [], [2], [2, 17, "Heartbeat", {}]])
def test_unreadable_uniqueid_answered_with_minus_one(raw):
    sent = asyncio.run(_answers(OCPP201_ERRORS, [raw]))
    assert [(f[0], f[1], f[2]) for f in sent] == [(framer.CALLERROR, "-1", "RpcFrameworkError")]


def test_unknown_message_type_answered_with_message_type_not_supported():
    sent = asyncio.run(_answers(OCPP201_ERRORS, [[7, "u7", {}]]))
    assert [(f[1], f[2]) for f in sent] == [("u7", "MessageTypeNotSupported")]
    sent = asyncio.run(_answers(OCPP16_ERRORS, [[7, "u7", {}]]))
    assert [(f[1], f[2]) for f in sent] == [("u7", "ProtocolError")]  # 1.6 behaviour unchanged


def test_unknown_action_and_non_object_payload():
    sent = asyncio.run(_answers(OCPP201_ERRORS, [[2, "a", "FooBar", {}], [2, "b", "Heartbeat", "x"]]))
    # Frame-level errors are answered at once, CALLs from a task: compare without order.
    assert sorted((f[1], f[2]) for f in sent) == [("a", "NotImplemented"), ("b", "FormatViolation")]


# --------------------------------------------------------------------------------------------- optional schema check

schema = pytest.importorskip("sme_ocpp_sim.protocol.v201.schema")
SCHEMA_DIR = schema.ocpp_package_schema_dir()
needs_schemas = pytest.mark.skipif(SCHEMA_DIR is None, reason="ocpp package (2.0.1 schemas) not installed")


@needs_schemas
@pytest.mark.parametrize("action,kind", sorted(FIXTURES))
def test_schema_validator_accepts_fixtures(action, kind):
    v = schema.SchemaValidator(SCHEMA_DIR)
    for payload in FIXTURES[(action, kind)].values():
        v.validate(action, payload, kind)


@needs_schemas
@pytest.mark.parametrize(
    "action,payload,kind",
    [
        ("BootNotification", {"reason": "PowerUp"}, "request"),
        ("Authorize", {"idToken": {"idToken": "X" * 37, "type": "Local"}}, "request"),
        ("Authorize", {"idToken": {"idToken": "X", "type": "Nope"}}, "request"),
        ("Authorize", {"idToken": {"idToken": 1, "type": "Local"}}, "request"),
        ("MeterValues", {"evseId": 1, "meterValue": []}, "request"),
        ("Heartbeat", {"foo": 1}, "request"),
        ("BootNotification", {"currentTime": T1, "interval": "1", "status": "Accepted"}, "response"),
    ],
)
def test_schema_validator_codes_match_the_codec(action, payload, kind):
    v = schema.SchemaValidator(SCHEMA_DIR)
    with pytest.raises(schema.CallError) as by_schema:
        v.validate(action, payload, kind)
    with pytest.raises(schema.CallError) as by_codec:
        parse(action, payload, kind, strict=True)
    assert by_schema.value.code == by_codec.value.code
    assert by_schema.value.details["field"] == by_codec.value.details["field"]


@needs_schemas
def test_schema_validator_unknown_action_and_missing_dir(tmp_path):
    v = schema.SchemaValidator(SCHEMA_DIR)
    with pytest.raises(schema.CallError) as exc:
        v.validate("FooBar", {})
    assert exc.value.code == "NotImplemented"
    with pytest.raises(FileNotFoundError):
        schema.SchemaValidator(tmp_path / "missing")


# --------------------------------------------------------------------------------------------- dev peer

peer_mod = pytest.importorskip("peers.ocpp201_peer")


def test_dev_peer_accepts_every_core_request_and_its_answers_parse():
    """Our dumps pass the ``ocpp`` library's 2.0.1 schema validation, and its answers pass our models."""
    from websockets.asyncio.client import connect

    async def scenario():
        peer = peer_mod.CsmsPeer201(token_statuses={"BLOCKED1": "Blocked"})
        await peer.start()
        answers = {}
        try:
            async with connect(f"{peer.url}/CS001", subprotocols=[peer_mod.SUBPROTOCOL]) as ws:
                assert ws.subprotocol == "ocpp2.0.1"
                calls = [(a, v, p) for (a, k), variants in sorted(FIXTURES.items()) if k == "request"
                         for v, p in variants.items()]
                calls.append(("Authorize", "blocked", {"idToken": dict(ID_TOKEN_MIN, idToken="BLOCKED1")}))
                for n, (action, variant, payload) in enumerate(calls):
                    model, _ = parse(action, payload, strict=True)
                    await ws.send(framer.encode_call(f"m{n}", action, dump(model)))
                    frame = framer.decode(await ws.recv())
                    assert isinstance(frame, framer.CallResult), (action, variant, frame)
                    answer, _ = parse(action, frame.payload, "response", strict=True)
                    answers[(action, variant)] = answer
        finally:
            await peer.stop()
        return peer, answers

    peer, answers = asyncio.run(scenario())
    s = peer.sessions["CS001"]
    assert s.subprotocol == "ocpp2.0.1"
    # Counts / order follow FIXTURES: every request has a "min" then a "full" variant.
    assert s.boots == 2 and s.booted.is_set()
    assert answers[("BootNotification", "min")].status.value == "Accepted"
    assert answers[("Authorize", "blocked")].id_token_info.status.value == "Blocked"
    assert answers[("DataTransfer", "min")].data == "pong"
    assert [e["seq_no"] for e in s.transaction_events] == [0, 7]  # "min" then "full"
    assert [st[2] for st in s.statuses] == ["Available", "Occupied"]
