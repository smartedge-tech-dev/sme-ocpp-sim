"""OCPP 2.1 core (v0.3.1, 2.1-M1): framing, models, store-and-ack messages, vendored schemas, outbound schema check.

The 2.0.1 station suites also run as 2.1 (``OCPPSIM_V201_SUITE=2.1``, see ``test_station201.py`` and CI).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from rpc_fakes import FakeWs
from test_rpc_connection import _rpc, _until
from test_mini_csms import _cfg
from test_station201 import CP, FAST, _online, _until as _until_rt
from test_station201_remote import _start

from sme_ocpp_sim.config import ChargerConfig
from sme_ocpp_sim.csms.handlers import SPECS, build_handlers
from sme_ocpp_sim.csms.session import CpSession
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.protocol.registry import check_version
from sme_ocpp_sim.protocol.v201 import enums as e201
from sme_ocpp_sim.protocol.v201.schema import SchemaValidator, bundled_schema_dir, ocpp_package_schema_dir
from sme_ocpp_sim.protocol.v21 import codec as c21
from sme_ocpp_sim.protocol.v21.station import Station21
from sme_ocpp_sim.rpc import CallError, framer
from sme_ocpp_sim.rpc.errors import OCPP21_ERRORS, OCPP201_ERRORS
from sme_ocpp_sim.runtime.charger import ChargerRuntime

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = bundled_schema_dir("ocpp2.1")


def _validator():
    pytest.importorskip("jsonschema")
    return SchemaValidator(SCHEMAS)


# ---------------------------------------------------------------------- vendored schemas / generated models


def test_schemas_are_bundled_byte_identical_to_the_ocpp_package():
    assert SCHEMAS is not None and len(list(SCHEMAS.glob("*.json"))) == 181
    ref = ocpp_package_schema_dir("v21")
    if ref is None:
        pytest.skip("the ocpp dev dependency (2.1.0) is not installed")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert {p.name: digest(p) for p in SCHEMAS.glob("*.json")} == {p.name: digest(p) for p in ref.glob("*.json")}


def test_generated_models_are_up_to_date():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_v21_models.py"), "--check"])
    assert r.returncode == 0, "run scripts/gen_v21_models.py"


def test_registry_and_version_attributes():
    assert check_version("2.1") == "2.1"
    rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2), "ws://127.0.0.1:9/ocpp", "2.1")
    assert isinstance(rt.station, Station21)
    assert rt.transport.subprotocol == "ocpp2.1" and rt.adapter.version == "2.1"
    assert rt.adapter.error_codes is OCPP21_ERRORS


@pytest.mark.parametrize("action,payload", [
    ("Authorize", {"idToken": {"idToken": "x" * 255, "type": "MacAddress"}}),  # 36 -> 255, enum -> string
    ("Authorize", {"idToken": {"idToken": "A", "type": "VendorSpecificType"}}),
    ("Reset", {"type": "ImmediateAndResume"}),
    ("SetChargingProfile", {"evseId": 1, "chargingProfile": {
        "id": 1, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile", "chargingProfileKind": "Absolute",
        "chargingSchedule": [{"id": 1, "chargingRateUnit": "W", "startSchedule": "2026-01-01T00:00:00.000Z",
                              "chargingSchedulePeriod": [{"startPeriod": 0, "setpoint": 1000.0,
                                                          "operationMode": "CentralSetpoint"}]}]}}),
    ("TriggerMessage", {"requestedMessage": "CustomTrigger", "customTrigger": "x"}),
    ("GetLog", {"logType": "DataCollectorLog", "requestId": 1, "log": {"remoteLocation": "ftp://h/"}}),
])
def test_2_1_payloads_parse(action, payload):
    model, ignored = c21.parse(action, payload, strict=True)
    assert ignored == []
    assert c21.dump(model) == payload


def test_known_enum_values_stay_2_0_1_members_and_errors_have_clean_paths():
    model, _ = c21.parse("Reset", {"type": "Immediate"})
    assert model.type is e201.ResetEnumType.immediate
    with pytest.raises(CallError) as ei:
        c21.parse("Reset", {"type": "Bogus"})
    assert ei.value.code == "PropertyConstraintViolation" and ei.value.details == {"field": "type"}


def test_transaction_event_response_with_2_1_fields_is_accepted():
    payload = {"transactionLimit": {"maxEnergy": 1000.0}, "idTokenInfo": {"status": "Accepted"}}
    model, ignored = c21.parse("TransactionEvent", payload, "response", strict=True)
    assert ignored == [] and model.transaction_limit.max_energy == 1000.0


# ---------------------------------------------------------------------- framing


def test_framer_send_and_callresulterror_only_under_2_1():
    raw = framer.encode_send("u1", "NotifyPeriodicEventStream", {"id": 1})
    assert json.loads(raw) == [6, "u1", "NotifyPeriodicEventStream", {"id": 1}]
    assert framer.decode(raw, True) == framer.Send("u1", "NotifyPeriodicEventStream", {"id": 1})
    err = framer.encode_result_error("u2", "FormatViolation", "bad")
    assert framer.decode(err, True) == framer.CallResultErrorFrame("u2", "FormatViolation", "bad", {})
    for r in (raw, err):
        with pytest.raises(framer.BadFrame) as ei:
            framer.decode(r)
        assert ei.value.kind == framer.UNKNOWN_TYPE


def _run(scenario):
    async def main():
        return await scenario(FakeWs())
    return asyncio.run(main())


@pytest.mark.parametrize("codes,answered", [(OCPP21_ERRORS, False), (OCPP201_ERRORS, True)])
def test_unknown_message_type_is_ignored_under_2_1_only(codes, answered):
    async def scenario(ws):
        rpc, frames = _rpc(ws, error_codes=codes)
        rx = asyncio.create_task(rpc.run())
        ws.feed([7, "x1", "Foo", {}])
        ws.feed([2, "x2", "Heartbeat", {}])  # answered NotImplemented: proves the loop went past the first frame
        await _until(lambda: any(f[1] == "x2" for f in ws.sent_frames()))
        await rpc.close()
        await rx
        return ws.sent_frames()
    sent = _run(scenario)
    assert any(f[1] == "x1" and f[2] == "MessageTypeNotSupported" for f in sent) is answered


def test_send_is_dispatched_unanswered_and_sent_one_way():
    async def scenario(ws):
        got = []

        async def handler(payload):
            got.append(payload)
            return {}
        rpc, frames = _rpc(ws, {"NotifyPeriodicEventStream": handler}, error_codes=OCPP21_ERRORS)
        rx = asyncio.create_task(rpc.run())
        ws.feed([6, "s1", "NotifyPeriodicEventStream", {"id": 1, "pending": 0, "basetime": "t", "data": []}])
        ws.feed([5, "r1", "FormatViolation", "bad result", {}])
        await _until(lambda: got and any(f.message_type == framer.CALLRESULTERROR for f in frames))
        await rpc.send("NotifyPeriodicEventStream", {"id": 2})
        await rpc.close()
        await rx
        return ws.sent_frames(), frames
    sent, frames = _run(scenario)
    assert sent == [[6, "id1", "NotifyPeriodicEventStream", {"id": 2}]]  # nothing answered the SEND or type 5


def test_malformed_callresult_gets_a_callresulterror():
    async def scenario(ws):
        rpc, _ = _rpc(ws, error_codes=OCPP21_ERRORS)
        ws.on_send = lambda raw: ws.feed([3, json.loads(raw)[1], "not an object"]) if json.loads(raw)[0] == 2 else None
        rx = asyncio.create_task(rpc.run())
        with pytest.raises(CallError):
            await rpc.call("Heartbeat", {})
        await _until(lambda: len(ws.sent) == 2)
        await rpc.close()
        await rx
        return ws.sent_frames()
    sent = _run(scenario)
    assert sent[1][:3] == [5, "id1", "FormatViolation"]


def test_malformed_send_is_dropped_without_a_reply():
    async def scenario(ws):
        rpc, frames = _rpc(ws, {"NotifyPeriodicEventStream": lambda p: None}, error_codes=OCPP21_ERRORS)
        rx = asyncio.create_task(rpc.run())
        ws.feed([6, "x", "A", []])  # payload is not an object
        ws.feed([6, "x"])  # too short for a SEND
        ws.feed([2, "x2", "Heartbeat", {}])  # answered: proves both frames were consumed
        await _until(lambda: any(f[1] == "x2" for f in ws.sent_frames()))
        await rpc.close()
        await rx
        return ws.sent_frames(), frames
    sent, frames = _run(scenario)
    assert [f[1] for f in sent] == ["x2"]  # nothing went out for the two bad SENDs
    assert sum(1 for f in frames if f.direction == "rx" and f.message_type == framer.SEND) == 2


def test_malformed_callresulterror_leaves_pending_calls_alone():
    async def scenario(ws):
        rpc, _ = _rpc(ws, error_codes=OCPP21_ERRORS)
        rx = asyncio.create_task(rpc.run())
        call = asyncio.create_task(rpc.call("Heartbeat", {}))
        await _until(lambda: len(ws.sent) == 1)
        ws.feed([5, "id1", "FormatViolation"])  # too short: malformed, uid matches the pending CALL
        ws.feed([5, "id1", 7, "d", {}])  # errorCode is not a string
        await asyncio.sleep(0.1)
        assert not call.done()
        ws.feed([3, "id1", {"currentTime": "t"}])
        assert await asyncio.wait_for(call, 2) == {"currentTime": "t"}
        await rpc.close()
        await rx
        return ws.sent_frames()
    sent = _run(scenario)
    assert len(sent) == 1  # no CALLERROR / CALLRESULTERROR in answer


def test_send_for_a_non_send_action_runs_no_handler_and_gets_no_reply():
    async def scenario(ws):
        ran = []

        async def reset(payload):
            ran.append(payload)
            return {"status": "Accepted"}
        rpc, frames = _rpc(ws, {"Reset": reset}, error_codes=OCPP21_ERRORS)
        rx = asyncio.create_task(rpc.run())
        ws.feed([6, "s1", "Reset", {"type": "Immediate"}])
        ws.feed([2, "x2", "Heartbeat", {}])
        await _until(lambda: any(f[1] == "x2" for f in ws.sent_frames()))
        await rpc.close()
        await rx
        return ran, ws.sent_frames(), frames
    ran, sent, frames = _run(scenario)
    assert ran == [] and [f[1] for f in sent] == ["x2"]
    assert any(f.note == "not a SEND action: ignored" for f in frames)


# ---------------------------------------------------------------------- inherited 2.0.1 behaviour under 2.1


def _station21():
    rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2), "ws://127.0.0.1:9/ocpp", "2.1")
    return rt, rt.station


@pytest.mark.parametrize("reset_type", ["Immediate", "ImmediateAndResume"])
def test_reset_immediate_and_resume_behaves_like_immediate(reset_type):
    rt, st = _station21()
    later = []
    rt.after_reply = later.append
    res = c21.dump(asyncio.run(st.handle(c21.parse("Reset", {"type": reset_type})[0])))
    assert res == {"status": "Accepted"}
    assert later == [st._do_reset]


def test_trigger_message_custom_trigger_is_not_implemented():
    rt, st = _station21()
    req = {"requestedMessage": "CustomTrigger", "customTrigger": "x"}
    assert c21.dump(asyncio.run(st.handle(c21.parse("TriggerMessage", req)[0])))["status"] == "NotImplemented"


# ---------------------------------------------------------------------- Mini CSMS answers vs the 2.1 schemas


def _sample(schema, defs):
    """A minimal instance of ``schema``: required properties only."""
    if "$ref" in schema:
        return _sample(defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: _sample(schema["properties"][k], defs) for k in schema.get("required", ())}
    if t == "array":
        return [_sample(schema["items"], defs) for _ in range(schema.get("minItems", 0))]
    if t == "string":
        return "2026-01-01T00:00:00Z" if schema.get("format") == "date-time" else "a"
    if t in ("integer", "number"):
        return schema.get("minimum", 0)
    return False


def test_every_mini_csms_2_1_answer_matches_the_2_1_response_schema():
    v = _validator()
    spec = SPECS["2.1"]

    class Ctx:
        cfg = _cfg()

        def next_tx_id(self):
            return 1

    handlers = build_handlers(spec, CpSession("X", "2.1", spec.subprotocol), Ctx())
    assert set(handlers) == set(spec.cp_actions) and "GetCertificateChainStatus" in handlers
    for action, h in handlers.items():
        if action == "NotifyPeriodicEventStream":
            continue  # a SEND: never answered, no response schema
        schema = json.loads((SCHEMAS / f"{action}Request.json").read_text(encoding="utf-8"))
        request = _sample(schema, schema.get("definitions", {}))
        v.validate(action, request, "request")
        answer = asyncio.run(h(request))
        v.validate(action, answer, "response")


# ---------------------------------------------------------------------- store-and-ack messages


TARIFF = {"tariffId": "T1", "currency": "EUR", "energy": {"prices": [{"priceKwh": 0.3}]}}
DER = {"isDefault": False, "controlId": "C1", "controlType": "VoltWatt", "curve": {
    "priority": 1, "yUnit": "PctMaxW", "curveData": [{"x": 100.0, "y": 50.0}]}}
DER_GRADIENT = {"isDefault": True, "controlId": "G1", "controlType": "Gradients",
                "gradient": {"priority": 1, "gradient": 1.0, "softGradient": 1.0}}


def test_store_and_ack_answers_are_schema_valid():
    v = _validator()
    rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2), "ws://127.0.0.1:9/ocpp", "2.1")
    st: Station21 = rt.station

    def call(action, payload):
        v.validate(action, payload, "request")
        res = c21.dump(asyncio.run(st.handle(c21.parse(action, payload)[0])))
        v.validate(action, res, "response")
        return res

    assert call("AFRRSignal", {"timestamp": "2026-01-01T00:00:00Z", "signal": 1}) == {"status": "Accepted"}
    assert call("NotifyAllowedEnergyTransfer", {"transactionId": "nope", "allowedEnergyTransfer": ["AC_BPT"]}
                )["status"] == "Rejected"
    assert call("GetTariffs", {"evseId": 0}) == {"status": "NoTariff"}
    assert call("SetDefaultTariff", {"evseId": 0, "tariff": TARIFF}) == {"status": "Accepted"}
    assert call("GetTariffs", {"evseId": 1})["tariffAssignments"] == [
        {"tariffId": "T1", "tariffKind": "DefaultTariff", "evseIds": [1]}]
    assert call("ChangeTransactionTariff", {"transactionId": "nope", "tariff": TARIFF}) == {"status": "TxNotFound"}
    assert call("ClearTariffs", {"tariffIds": ["T1", "T9"]})["clearTariffsResult"] == [
        {"tariffId": "T1", "status": "Accepted"}, {"tariffId": "T9", "status": "NoTariff"}]
    assert call("GetDERControl", {"requestId": 1}) == {"status": "NotFound"}
    assert call("SetDERControl", DER) == {"status": "Accepted"}
    assert call("SetDERControl", DER_GRADIENT) == {"status": "Accepted"}
    report = c21.dump(st._der_report(5, list(st.der_controls.values())))
    v.validate("ReportDERControl", report, "request")
    assert report["requestId"] == 5 and report["curve"][0]["id"] == "C1" and report["gradient"][0]["id"] == "G1"
    assert call("ClearDERControl", {"isDefault": False, "controlId": "C1"}) == {"status": "Accepted"}
    assert call("ClearDERControl", {"isDefault": False, "controlId": "C1"}) == {"status": "NotFound"}


@pytest.mark.parametrize("action", ["RequestBatterySwap", "NotifyWebPaymentStarted", "UpdateDynamicSchedule",
                                    "UsePriorityCharging", "GetPeriodicEventStream", "AdjustPeriodicEventStream"])
def test_not_implemented_2_1_messages(action):
    with pytest.raises(CallError) as ei:
        c21.parse(action, {})
    assert ei.value.code == "NotImplemented"


# ---------------------------------------------------------------------- outbound messages vs the 2.1 schemas


def test_outbound_messages_validate_against_the_2_1_schemas(monkeypatch):
    from peers import ocpp201_peer
    v = _validator()
    monkeypatch.setattr(ocpp201_peer, "SUBPROTOCOL", "ocpp2.1")

    async def main():
        peer = ocpp201_peer.CsmsPeer201(hb_interval=30)
        await peer.start()
        rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2), peer.url, "2.1", reconnect=FAST,
                            frame_log=FrameLog(), call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            s = await _online(peer, rt)
            assert s.subprotocol == "ocpp2.1"
            await rt.plug(2)
            assert (await peer.request(CP, _start(evse_id=2, remote_id=1))).status == "Accepted"
            await _until_rt(lambda: len(s.transaction_events) >= 1)
            return [json.loads(raw) for _, raw in s.frames], [dr for dr, _ in s.frames]
        finally:
            stop.set()
            async with asyncio.timeout(3):
                await task
            await peer.stop()

    frames, dirs = asyncio.run(main())
    actions = {f[1]: f[2] for f, d in zip(frames, dirs) if d == "tx" and f[0] == 2}
    checked = 0
    for f, d in zip(frames, dirs):
        if d != "rx":
            continue
        if f[0] == 2:
            v.validate(f[2], f[3], "request")
            checked += 1
        elif f[0] == 3 and f[1] in actions:
            v.validate(actions[f[1]], f[2], "response")
            checked += 1
    assert checked >= 5
