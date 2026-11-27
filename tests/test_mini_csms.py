"""Mini CSMS (plan M9, #20): our charger <-> our Mini CSMS (1.6 + 2.0.1), Mini CSMS <-> stations written with the
``ocpp`` library (independent cross-check), subprotocol negotiation, Basic Auth, plan, JSON-editor CALLs.

Every wait is on a condition (``_until``), never on a fixed sleep.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from pathlib import Path

import jsonschema
import ocpp
import pytest
from fastapi.testclient import TestClient
from ocpp.v16 import call as call16
from ocpp.v201 import call as call201
from peers.ocpp_cp_peer import LibCP16, LibCP201, LibStation
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from sme_ocpp_sim.config import ChargerConfig, MiniCsmsConfig, ReconnectConfig, SimConfig, parse_text
from sme_ocpp_sim.csms import MiniCsms
from sme_ocpp_sim.csms.handlers import SPECS, build_handlers
from sme_ocpp_sim.csms.session import CpSession
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.runtime.charger import ChargerRuntime
from sme_ocpp_sim.web import PASSWORD_MASK, create_app

FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)
SCHEMAS = Path(ocpp.__file__).resolve().parent

PROFILE16 = {"connectorId": 1, "csChargingProfiles": {
    "chargingProfileId": 7, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile",
    "chargingProfileKind": "Relative",
    "chargingSchedule": {"chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 3000}]}}}
PROFILE201 = {"evseId": 1, "chargingProfile": {
    "id": 7, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile", "chargingProfileKind": "Relative",
    "chargingSchedule": [{"id": 1, "chargingRateUnit": "W",
                          "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 3000}]}]}}
PROFILES = {"1.6": PROFILE16, "2.0.1": PROFILE201}
SUBPROTOCOL = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1"}


async def _until(cond, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cfg(**kw) -> MiniCsmsConfig:
    return MiniCsmsConfig(enabled=True, port=0, **kw)


async def _with_csms(cfg: MiniCsmsConfig, body):
    csms = MiniCsms(cfg)
    port = await csms.start()
    try:
        await body(csms, f"ws://127.0.0.1:{port}/ocpp")
    finally:
        await csms.stop()


async def _with_charger(url, version, body, **cfg_kw):
    rt = ChargerRuntime(ChargerConfig(id=cfg_kw.pop("id", "CP1"), **cfg_kw), url, version, reconnect=FAST,
                        frame_log=FrameLog(), call_timeout=5)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.run(stop))
    try:
        await body(rt)
    finally:
        stop.set()
        async with asyncio.timeout(3):
            await task


def _conn(rt, i=1):
    return rt.snapshot()["connectors"][i - 1]


# --------------------------------------------------------------------------------------- our charger <-> Mini CSMS


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_our_charger_full_flow(version):
    """Boot, statuses, a plan SetChargingProfile installed on the charger, a transaction with meter values, a manual
    CALL; the Smart Charging panel route of the charger then lists the planned profile."""
    plan = [{"at_s": 0, "after": "boot", "action": "SetChargingProfile", "payload": PROFILES[version],
             "ocpp_version": version}]
    metered = ({"config_keys": {"MeterValueSampleInterval": 1}} if version == "1.6" else
               {"meter_sample_interval_s": 1})

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: rt.online and "CP1" in csms.sessions)
                s = csms.sessions["CP1"]
                assert s.version == version and s.boot_status == "Accepted"
                await _until(lambda: s.plan and s.plan[0]["status"] == "ok")
                assert s.plan[0]["result"] == {"status": "Accepted"}
                await _until(lambda: rt.installed_profiles(1))
                await _until(lambda: len(s.statuses) >= 1)

                await rt.plug(1)
                assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
                await _until(lambda: any(t["active"] for t in s.transactions.values()))
                tx = next(t for t in s.transactions.values() if t["active"])
                assert tx["id_tag"] == "TAG1" and tx["connector"] == 1
                await _until(lambda: any(m["transaction"] == tx["id"] for m in s.meter))
                assert await rt.stop_tx(1)
                await _until(lambda: not s.transactions[tx["id"]]["active"])

                action, payload = (("GetConfiguration", {"key": ["HeartbeatInterval"]}) if version == "1.6" else
                                   ("GetVariables", {"getVariableData": [
                                       {"component": {"name": "OCPPCommCtrlr"},
                                        "variable": {"name": "HeartbeatInterval"}}]}))
                res = await csms.call("CP1", action, payload)
                assert res["ok"] and "warning" not in res, res
                assert csms.frame_log.since(0, charger="CP1", action=action)
            await _with_charger(url, version, body, auth_mode="accept_all", **metered)
        await _with_csms(_cfg(plan=plan), with_csms)
    asyncio.run(main())


@pytest.mark.parametrize("status", ["Pending", "Rejected"])
def test_boot_status_configurable(status):
    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: "CP1" in csms.sessions and csms.sessions["CP1"].boot_status == status)
                # The CSMS records its answer as it sends it; the charger handles it a moment later.
                await _until(lambda: (rt.snapshot()["registration"] or "").lower() == status.lower())
                assert not rt.online
                # The plan's boot trigger never fired: Pending / Rejected is no accepted boot.
                assert csms.sessions["CP1"].plan[0]["status"] == "waiting"
            await _with_charger(url, "2.0.1", body)
        plan = [{"action": "Reset", "payload": {"type": "Immediate"}, "ocpp_version": "2.0.1"}]
        await _with_csms(_cfg(boot={"status": status, "interval": 1}, plan=plan), with_csms)
    asyncio.run(main())


def test_authorize_statuses_from_config():
    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: rt.online)
                await rt.plug(1)
                assert await rt.swipe(1, "BAD") == AuthResult.BLOCKED
                assert not rt.has_transaction(1)
            await _with_charger(url, "1.6", body, auth_mode="csms_only")
        await _with_csms(_cfg(authorize={"tags": {"BAD": "Blocked"}}), with_csms)
    asyncio.run(main())


# --------------------------------------------------------------------------------------- handshake


def test_subprotocol_negotiation_and_rejection():
    async def main():
        async def with_csms(csms, url):
            # The station's preference wins among the enabled versions.
            async with connect(f"{url}/A", subprotocols=["ocpp2.1", "ocpp2.0.1", "ocpp1.6"]) as ws:
                assert ws.subprotocol == "ocpp2.0.1"
            # 1.6 is not enabled: no subprotocol in the answer, then the server closes.
            ws = await connect(f"{url}/B", subprotocols=["ocpp1.6"])
            assert ws.subprotocol is None
            await _until(lambda: ws.close_code is not None)
            assert ws.close_code == 1002
            assert any(r["id"] == "B" and "no supported subprotocol" in r["reason"] for r in csms.rejections)
            # Our own 1.6 charger cannot register there.
            async def body(rt):
                await _until(lambda: any(r["id"] == "C" for r in csms.rejections))
                assert not rt.online and "C" not in csms.sessions
            await _with_charger(url, "1.6", body, id="C")
        await _with_csms(_cfg(versions=["2.0.1"]), with_csms)
    asyncio.run(main())


def test_basic_auth_accept_and_reject():
    auth = {"basic": True, "credentials": [{"id": "CP1", "password": "secret-1"}]}

    def hdr(user, pw):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    async def main():
        async def with_csms(csms, url):
            for cp, headers in (("CP1", hdr("CP1", "wrong")), ("CP1", None), ("CP9", hdr("CP9", "secret-1")),
                                ("CP1", hdr("CP2", "secret-1"))):
                with pytest.raises(InvalidStatus) as e:
                    await connect(f"{url}/{cp}", subprotocols=["ocpp1.6"], additional_headers=headers)
                assert e.value.response.status_code == 401
            assert len(csms.rejections) == 4 and "CP1" not in csms.sessions

            async def body(rt):
                await _until(lambda: rt.online)
                assert csms.sessions["CP1"].basic_auth
            await _with_charger(url, "1.6", body, password="secret-1")
        await _with_csms(_cfg(auth=auth), with_csms)
    asyncio.run(main())


def test_unknown_action_is_not_implemented_and_bad_payload_tolerated():
    async def main():
        async def with_csms(csms, url):
            async with connect(f"{url}/RAW", subprotocols=["ocpp2.0.1"]) as ws:
                await ws.send(json.dumps([2, "u1", "FooBar", {}]))
                assert json.loads(await ws.recv())[:3] == [4, "u1", "NotImplemented"]
                # Tolerant mode: a Heartbeat with an unknown field is answered and a warning recorded.
                await ws.send(json.dumps([2, "u2", "Heartbeat", {"bogus": 1}]))
                ans = json.loads(await ws.recv())
                assert ans[0] == 3 and "currentTime" in ans[2]
                assert csms.sessions["RAW"].counts == {"Heartbeat": 1}
                csms.update(_cfg(strict=True))
                await ws.send(json.dumps([2, "u3", "StatusNotification", {"evseId": 1}]))
                ans = json.loads(await ws.recv())
                assert ans[0] == 4 and ans[2] == "OccurrenceConstraintViolation"
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


# --------------------------------------------------------------------------------------- ocpp-library stations


def test_library_station_16():
    plan = [{"at_s": 0, "action": "SetChargingProfile", "payload": PROFILE16, "ocpp_version": "1.6"},
            {"at_s": 0, "after": "connect", "action": "RemoteStartTransaction", "payload": {"idTag": "R1"},
             "charger": "LIB*", "ocpp_version": "1.6"}]

    async def main():
        async def with_csms(csms, url):
            async with LibStation(LibCP16, "LIB16", url, "ocpp1.6") as cp:
                # The library validates our answers against its 1.6 schemas.
                boot = await cp.call(call16.BootNotification(charge_point_vendor="V", charge_point_model="M"))
                assert boot.status == "Accepted" and boot.interval == 300
                await cp.call(call16.StatusNotification(connector_id=1, error_code="NoError", status="Available"))
                assert (await cp.call(call16.Authorize(id_tag="T"))).id_tag_info["status"] == "Accepted"
                start = await cp.call(call16.StartTransaction(connector_id=1, id_tag="T", meter_start=10,
                                                              timestamp="2026-01-01T00:00:00Z"))
                await cp.call(call16.MeterValues(connector_id=1, transaction_id=start.transaction_id, meter_value=[
                    {"timestamp": "2026-01-01T00:01:00Z",
                     "sampled_value": [{"value": "110", "unit": "Wh"}]}]))
                await cp.call(call16.StopTransaction(meter_stop=200, timestamp="2026-01-01T00:02:00Z",
                                                     transaction_id=start.transaction_id, id_tag="T"))
                await cp.call(call16.Heartbeat())
                assert (await cp.call(call16.DataTransfer(vendor_id="x"))).status == "UnknownVendorId"
                await cp.call(call16.DiagnosticsStatusNotification(status="Idle"))
                await cp.call(call16.FirmwareStatusNotification(status="Idle"))
                s = csms.sessions["LIB16"]
                tx = s.transactions[str(start.transaction_id)]
                assert not tx["active"] and tx["meter_start_wh"] == 10 and tx["meter_stop_wh"] == 200
                assert tx["energy"] == "110 Wh" and s.statuses["1"]["status"] == "Available"
                # The library validates the planned CALLs we sent against its schemas before answering them.
                await _until(lambda: cp.profiles and cp.remote_starts)
                assert cp.profiles[0][0] == 1 and cp.remote_starts[0][0] == "R1"
                await _until(lambda: all(p["status"] == "ok" for p in s.plan))
                assert not s.warnings, s.warnings
        await _with_csms(_cfg(plan=plan), with_csms)
    asyncio.run(main())


def test_library_station_201():
    plan = [{"at_s": 0, "action": "SetChargingProfile", "payload": PROFILE201, "ocpp_version": "2.0.1"}]

    async def main():
        async def with_csms(csms, url):
            async with LibStation(LibCP201, "LIB201", url, "ocpp2.0.1") as cp:
                boot = await cp.call(call201.BootNotification(
                    charging_station={"vendor_name": "V", "model": "M"}, reason="PowerUp"))
                assert boot.status == "Accepted"
                await cp.call(call201.StatusNotification(timestamp="2026-01-01T00:00:00Z",
                                                         connector_status="Available", evse_id=1, connector_id=1))
                auth = await cp.call(call201.Authorize(id_token={"id_token": "T", "type": "ISO14443"}))
                assert auth.id_token_info["status"] == "Accepted"
                ev = dict(timestamp="2026-01-01T00:00:00Z", evse={"id": 1, "connector_id": 1})
                r = await cp.call(call201.TransactionEvent(
                    event_type="Started", trigger_reason="Authorized", seq_no=0,
                    transaction_info={"transaction_id": "TX-1"},
                    id_token={"id_token": "T", "type": "ISO14443"}, **ev))
                assert r.id_token_info["status"] == "Accepted"
                await cp.call(call201.MeterValues(evse_id=1, meter_value=[
                    {"timestamp": "2026-01-01T00:01:00Z", "sampled_value": [{"value": 1.5}]}]))
                await cp.call(call201.TransactionEvent(
                    event_type="Ended", trigger_reason="StopAuthorized", seq_no=1,
                    transaction_info={"transaction_id": "TX-1", "stopped_reason": "Local"},
                    meter_value=[{"timestamp": "2026-01-01T00:02:00Z",
                                  "sampled_value": [{"value": 2000.0, "unit_of_measure": {"unit": "Wh"}}]}],
                    **ev))
                await cp.call(call201.Heartbeat())
                await cp.call(call201.NotifyReport(request_id=1, generated_at="2026-01-01T00:00:00Z", seq_no=0))
                await cp.call(call201.NotifyEvent(generated_at="2026-01-01T00:00:00Z", seq_no=0, event_data=[{
                    "event_id": 1, "timestamp": "2026-01-01T00:00:00Z", "trigger": "Alerting",
                    "actual_value": "1", "event_notification_type": "HardWiredNotification",
                    "component": {"name": "X"}, "variable": {"name": "Y"}}]))
                s = csms.sessions["LIB201"]
                tx = s.transactions["TX-1"]
                assert not tx["active"] and tx["stop_reason"] == "Local" and tx["energy"] == "2000.0 Wh"
                assert s.statuses["1/1"]["status"] == "Available" and s.counts["NotifyEvent"] == 1
                await _until(lambda: cp.profiles)
                assert cp.profiles[0][0] == 1
                res = await csms.call("LIB201", "RequestStartTransaction",
                                      {"idToken": {"idToken": "R", "type": "Central"}, "remoteStartId": 5})
                assert res == {"ok": True, "result": {"status": "Accepted"}}
                assert not s.warnings, s.warnings
        await _with_csms(_cfg(plan=plan), with_csms)
    asyncio.run(main())


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_every_cp_action_answer_matches_library_schema(version):
    """Each CP -> CSMS action of the version gets an answer valid against the ``ocpp`` library's response schema."""
    spec = SPECS[version]
    folder = SCHEMAS / ("v16" if version == "1.6" else "v201") / "schemas"

    class Ctx:
        cfg = _cfg()

        def next_tx_id(self):
            return 1

    handlers = build_handlers(spec, CpSession("X", version, spec.subprotocol), Ctx())
    assert set(handlers) == set(spec.cp_actions)
    for action, h in handlers.items():
        answer = asyncio.run(h({}))
        schema = json.loads((folder / f"{action}Response.json").read_text(encoding="utf-8"))
        jsonschema.validate(answer, schema)


# --------------------------------------------------------------------------------------- engine + Web API


def _demo_config(port: int, version: str) -> SimConfig:
    return SimConfig.model_validate({
        "csms_defaults": {"url": f"ws://127.0.0.1:{port}/ocpp", "ocpp_version": version,
                          "reconnect": {"min_s": 0.05, "max_s": 0.2, "jitter": 0}},
        "chargers": [{"id": "CP-1"}],
        "mini_csms": {"enabled": True, "port": port, "plan": [
            {"at_s": 0, "action": "SetChargingProfile", "payload": PROFILES[version], "ocpp_version": version}]},
    })


def _poll(fn, timeout=8.0):
    import time
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > end:
            raise TimeoutError
        time.sleep(0.02)


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_demo_engine_and_web_api(version):
    port = _free_port()
    engine = OcppEngine(_demo_config(port, version))
    with TestClient(create_app(engine)) as c:
        st = c.get("/api/csms").json()
        assert st["running"] and st["url"] == f"ws://127.0.0.1:{port}/ocpp"
        _poll(lambda: [x for x in c.get("/api/csms").json()["chargers"] if x["boot_status"] == "Accepted"])
        # The planned profile reaches the charger's Smart Charging panel.
        prof = _poll(lambda: c.get("/api/chargers/CP-1/profiles").json())
        assert "7" in json.dumps(prof)
        d = _poll(lambda: [p for p in c.get("/api/csms/chargers/CP-1").json()["plan"] if p["status"] == "ok"])
        assert d[0]["action"] == "SetChargingProfile"

        # JSON editor: schema error path (field named, nothing sent), then a valid CALL.
        frames_before = c.get("/api/csms/frames").json()["last_seq"]
        r = c.post("/api/csms/chargers/CP-1/call", json={"action": "Reset", "payload": {"type": "Bogus"}})
        assert r.status_code == 400 and "type" in r.text
        r = c.post("/api/csms/chargers/CP-1/call", json={"action": "BootNotification", "payload": {}})
        assert r.status_code == 400 and "not a CSMS -> CP action" in r.text
        assert c.get("/api/csms/frames").json()["last_seq"] == frames_before
        action = "GetLocalListVersion"
        r = c.post("/api/csms/chargers/CP-1/call", json={"action": action, "payload": {}})
        assert r.status_code == 200 and r.json()["ok"] and "warning" not in r.json(), r.text
        assert r.json()["result"] == ({"listVersion": 0} if version == "1.6" else {"versionNumber": 0})
        assert c.post("/api/csms/chargers/NOPE/call", json={"action": action}).status_code == 404
        acts = c.get("/api/csms/actions", params={"version": version}).json()["actions"]
        assert "SetChargingProfile" in acts and "BootNotification" not in acts
        assert c.get("/api/csms/transactions").json() == {"transactions": []}


def test_web_api_when_disabled():
    with TestClient(create_app(OcppEngine())) as c:
        st = c.get("/api/csms").json()
        assert st["enabled"] is False and st["running"] is False and st["chargers"] == []
        assert c.get("/api/csms/transactions").status_code == 409


def test_engine_reload_restarts_or_updates():
    port = _free_port()

    async def main():
        engine = OcppEngine(SimConfig.model_validate({"mini_csms": {"enabled": True, "port": port}}))
        await engine.start()
        try:
            first = engine.csms
            assert first is not None and first.running
            cfg = engine.config.model_copy(deep=True)
            cfg.mini_csms = MiniCsmsConfig(enabled=True, port=port, boot={"status": "Pending"})
            await engine.reload(cfg)
            assert engine.csms is first and first.cfg.boot.status == "Pending"  # applied in place
            cfg = engine.config.model_copy(deep=True)
            cfg.mini_csms = MiniCsmsConfig(enabled=False, port=port)
            await engine.reload(cfg)
            assert engine.csms is None and not first.running
        finally:
            await engine.stop()
    asyncio.run(main())


def test_engine_port_in_use_is_reported_not_fatal():
    async def main():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            engine = OcppEngine(SimConfig.model_validate({"mini_csms": {"enabled": True, "port": port}}))
            await engine.start()
            try:
                assert engine.csms is None and "cannot listen" in engine.csms_error
            finally:
                await engine.stop()
    asyncio.run(main())


# --------------------------------------------------------------------------------------- config


def test_config_plan_payload_checked():
    with pytest.raises(ValueError, match=r"plan\[0\].*typo"):
        MiniCsmsConfig(plan=[{"action": "Reset", "payload": {"typo": 1}, "ocpp_version": "2.0.1"}])
    with pytest.raises(ValueError, match="not a CSMS -> CP action"):
        MiniCsmsConfig(plan=[{"action": "Heartbeat"}])
    # Without ocpp_version the payload must fit every enabled version.
    with pytest.raises(ValueError, match="OCPP 2.0.1"):
        MiniCsmsConfig(plan=[{"action": "SetChargingProfile", "payload": PROFILE16}])
    assert MiniCsmsConfig(versions=["1.6"], plan=[{"action": "SetChargingProfile", "payload": PROFILE16}])


def test_config_yaml_and_masked_password_kept():
    text = ('mini_csms:\n  enabled: false\n  auth:\n    basic: true\n'
            '    credentials:\n      - { id: "CP1", password: "pw-1" }\n')
    cfg = parse_text(text)
    engine = OcppEngine(cfg, config_text=text)
    with TestClient(create_app(engine)) as c:
        shown = c.get("/api/config").text
        assert "pw-1" not in shown and PASSWORD_MASK in shown, shown
        r = c.put("/api/config", content=shown.encode(), headers={"Content-Type": "text/plain"})
        assert r.status_code == 200, r.text
    assert engine.config.mini_csms.auth.credentials[0].password == "pw-1"


# --------------------------------------------------------------------------------------- review fixes (M9)

SECRET16 = "0123456789abcdef0123"          # 1.6 AuthorizationKey: 16..40 characters
SECRET201 = "secret-password-0123456789"   # 2.0.1 BasicAuthPassword: 16..64 characters
SECRET_CALLS = {
    "1.6": ("ChangeConfiguration", {"key": "AuthorizationKey", "value": SECRET16}, SECRET16),
    "2.0.1": ("SetVariables", {"setVariableData": [{"attributeValue": SECRET201, "component": {"name": "SecurityCtrlr"},
                                                    "variable": {"name": "BasicAuthPassword"}}]}, SECRET201),
}


def test_mask_helpers():
    from sme_ocpp_sim.framelog import SECRET_MASK, mask_frame, mask_payload
    for action, payload, secret in SECRET_CALLS.values():
        raw = json.dumps([2, "u1", action, payload])
        masked = mask_frame(raw, action)
        assert secret not in masked and SECRET_MASK in masked
        assert mask_frame(raw) == masked  # action unknown: parsed anyway
        out = mask_payload(action, payload)
        assert secret not in json.dumps(out) and secret in json.dumps(payload)  # the original is not mutated
    other = json.dumps([2, "u2", "ChangeConfiguration", {"key": "HeartbeatInterval", "value": "60"}])
    assert mask_frame(other, "ChangeConfiguration") is other
    assert mask_frame('[3,"u1",{}]', "ChangeConfiguration") == '[3,"u1",{}]'
    assert mask_frame("not json") == "not json"
    net = {"configurationSlot": 1, "connectionData": {
        "ocppVersion": "OCPP20", "ocppTransport": "JSON", "ocppCsmsUrl": "ws://x", "messageTimeout": 30,
        "securityProfile": 1, "ocppInterface": "Wired0",
        "vpn": {"server": "s", "user": "u", "password": "vpn-pw", "key": "vpn-key", "type": "IPSec"},
        "apn": {"apn": "a", "apnPassword": "apn-pw", "simPin": 1234, "apnAuthentication": "AUTO"}}}
    m = json.dumps(mask_payload("SetNetworkProfile", net))
    assert not any(s in m for s in ("vpn-pw", "vpn-key", "apn-pw", "1234"))


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_secrets_masked_on_both_sides_and_in_api(version):
    action, payload, secret = SECRET_CALLS[version]
    port = _free_port()
    data = _demo_config(port, version).model_dump()
    data["mini_csms"]["auth"]["credentials"] = [{"id": "CP-1", "password": "cfg-password-1"}]
    engine = OcppEngine(SimConfig.model_validate(data))
    with TestClient(create_app(engine)) as c:
        _poll(lambda: [x for x in c.get("/api/csms").json()["chargers"] if x["boot_status"] == "Accepted"])
        session = engine.csms.sessions["CP-1"]
        r = c.post("/api/csms/chargers/CP-1/call", json={"action": action, "payload": payload})
        assert r.status_code == 200 and r.json()["ok"], r.text
        assert secret not in r.text
        # The station got the real value: it accepted it (the mask would be too short for both versions).
        res = r.json()["result"]
        status = res["status"] if version == "1.6" else res["setVariableResult"][0]["attributeStatus"]
        assert status in ("Accepted", "RebootRequired"), r.text
        # Charger side: its frame log has the CALL it received, masked.
        rx = _poll(lambda: [f for f in c.get("/api/frames").json()["frames"]
                            if f["action"] == action and f["message_type"] == "CALL"])
        assert "********" in rx[0]["raw"]
        for route in ("/api/frames", "/api/csms", "/api/csms/chargers/CP-1", "/api/csms/frames", "/api/config"):
            text = c.get(route).text
            assert secret not in text and "cfg-password-1" not in text, route
        # The recorded call (of that connection: the station reconnects with the new password afterwards).
        calls = [x for x in session.calls if x["action"] == action]
        assert calls and "********" in json.dumps(calls[0]["payload"]) and secret not in json.dumps(calls)


def test_reconnect_same_id_while_open_replaces_connection_and_reruns_plan():
    plan = [{"at_s": 0, "after": "connect", "action": "GetLocalListVersion", "payload": {}, "ocpp_version": "1.6"}]

    async def main():
        async def with_csms(csms, url):
            ws1 = await connect(f"{url}/R1", subprotocols=["ocpp1.6"])
            first = json.loads(await ws1.recv())
            assert first[2] == "GetLocalListVersion"
            old = csms.sessions["R1"]
            ws2 = await connect(f"{url}/R1", subprotocols=["ocpp1.6"])
            await _until(lambda: ws1.close_code is not None)
            again = json.loads(await ws2.recv())
            new = csms.sessions["R1"]
            assert new is not old and new.connected and csms.connected("R1")
            await _until(lambda: not old.connected)
            assert old.plan[0]["status"] == "cancelled"
            assert again[2] == "GetLocalListVersion"
            await ws2.send(json.dumps([3, again[1], {"listVersion": 0}]))
            await _until(lambda: new.plan[0]["status"] == "ok")
            assert [s["id"] for s in csms.snapshot()["chargers"]] == ["R1"]
            await ws2.close()
        await _with_csms(_cfg(plan=plan), with_csms)
    asyncio.run(main())


def test_plan_tasks_cancelled_on_disconnect():
    plan = [{"at_s": 30, "after": "connect", "action": "GetLocalListVersion", "payload": {}, "ocpp_version": "1.6"}]

    def plan_tasks():
        return [t for t in asyncio.all_tasks() if t.get_name().startswith("csms-plan")]

    async def main():
        async def with_csms(csms, url):
            ws = await connect(f"{url}/P1", subprotocols=["ocpp1.6"])
            await _until(lambda: "P1" in csms.sessions and csms.sessions["P1"].plan[0]["due_at"])
            assert plan_tasks()
            await ws.close()
            s = csms.sessions["P1"]
            await _until(lambda: not s.connected and not plan_tasks())
            assert s.plan[0]["status"] == "cancelled"
        await _with_csms(_cfg(plan=plan), with_csms)
    asyncio.run(main())


def test_basic_auth_failures_are_one_generic_401():
    auth = {"basic": True, "credentials": [{"id": "CP1", "password": "secret-1"}]}

    def b64(text):
        return "Basic " + base64.b64encode(text.encode()).decode()

    cases = [("CP1", b64("CP1:wrong"), "bad credentials"), ("CP9", b64("CP9:secret-1"), "unknown charging station"),
             ("CP1", "Basic !!!not-base64", "malformed"), ("CP1", "Bearer abc", "malformed"),
             ("CP1", b64("no-colon"), "malformed"), ("CP1", None, "malformed")]

    async def main():
        async def with_csms(csms, url):
            bodies = set()
            for cp, header, _ in cases:
                with pytest.raises(InvalidStatus) as e:
                    await connect(f"{url}/{cp}", subprotocols=["ocpp1.6"],
                                  additional_headers={"Authorization": header} if header else None)
                resp = e.value.response
                assert resp.status_code == 401
                assert resp.headers["WWW-Authenticate"].startswith('Basic realm="')
                bodies.add(bytes(resp.body))
            assert bodies == {b"unauthorized\n"}  # nothing tells an unknown id from a wrong password
            reasons = [r["reason"] for r in csms.rejections]
            assert len(reasons) == len(cases)
            assert all(want in got for (_, _, want), got in zip(cases, reasons)), reasons
            assert not csms.sessions
        await _with_csms(_cfg(auth=auth), with_csms)
    asyncio.run(main())


def test_caps(monkeypatch):
    from sme_ocpp_sim.csms import server as server_mod
    from sme_ocpp_sim.csms import session as session_mod
    s = CpSession("X", "1.6", "ocpp1.6")
    s.tx_start("A", 1, "TAG", 0, None)  # stays active
    for i in range(session_mod.MAX_TRANSACTIONS + 10):
        s.tx_start(f"T{i}", 2, "TAG", 0, None)
        s.tx_stop(f"T{i}", 1, None, None)
    assert len(s.transactions) == session_mod.MAX_TRANSACTIONS and s.transactions["A"]["active"]
    for i in range(session_mod.MAX_STATUSES + 20):
        s.status(str(i), "Available", None, None)
    assert len(s.statuses) == session_mod.MAX_STATUSES and str(session_mod.MAX_STATUSES + 19) in s.statuses

    monkeypatch.setattr(server_mod, "MAX_SESSIONS", 3)
    csms = MiniCsms(_cfg(), clock=lambda: 1_700_000_123.5)
    csms._remember(CpSession("LIVE", "1.6", "ocpp1.6"))
    for i in range(5):
        old = CpSession(f"OLD{i}", "1.6", "ocpp1.6")
        old.closed()
        csms._remember(old)
    assert list(csms.sessions) == ["LIVE", "OLD3", "OLD4"]
    # 1.6 transactionId: seeded from the clock (no repeat after a restart), int32-safe.
    assert csms.next_tx_id() == 700_000_124
    csms._tx_seq = 2 ** 31 - 1
    assert csms.next_tx_id() == 1


def test_max_connections(monkeypatch):
    from sme_ocpp_sim.csms import server as server_mod
    monkeypatch.setattr(server_mod, "MAX_CONNECTIONS", 1)

    async def main():
        async def with_csms(csms, url):
            ws = await connect(f"{url}/A", subprotocols=["ocpp1.6"])
            await _until(lambda: csms.connected("A"))
            with pytest.raises(InvalidStatus) as e:
                await connect(f"{url}/B", subprotocols=["ocpp1.6"])
            assert e.value.response.status_code == 503
            ws2 = await connect(f"{url}/A", subprotocols=["ocpp1.6"])  # the same id may replace itself
            await _until(lambda: ws.close_code is not None)
            await ws2.close()
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


def test_call_to_disconnected_station_is_409():
    port = _free_port()
    engine = OcppEngine(_demo_config(port, "1.6"))
    with TestClient(create_app(engine)) as c:
        _poll(lambda: [x for x in c.get("/api/csms").json()["chargers"] if x["connected"]])
        engine.csms._conns.clear()  # as if the station had just gone away
        r = c.post("/api/csms/chargers/CP-1/call", json={"action": "GetLocalListVersion", "payload": {}})
        assert r.status_code == 409 and "not connected" in r.text


def test_engine_csms_port_change_with_connected_stations(caplog):
    p1, p2 = _free_port(), _free_port()

    async def main():
        data = _demo_config(p1, "1.6").model_dump()
        data["mini_csms"]["plan"] = []
        engine = OcppEngine(SimConfig.model_validate(data))
        await engine.start()
        try:
            first = engine.csms
            rt = engine.state["CP-1"].runtime
            await _until(lambda: rt.online and first.connected("CP-1"))
            old_session = first.sessions["CP-1"]
            new = engine.config.model_copy(deep=True)
            new.mini_csms = MiniCsmsConfig(enabled=True, port=p2)
            await engine.reload(new)
            second = engine.csms
            assert second is not first and not first.running and second.running and second.port == p2
            await _until(lambda: not old_session.connected and not rt.online)
            assert not second.sessions  # the charger still points at p1
            back = engine.config.model_copy(deep=True)
            back.mini_csms = MiniCsmsConfig(enabled=True, port=p1, host="0.0.0.0")
            with caplog.at_level("WARNING", logger="sme_ocpp_sim.engine"):
                await engine.reload(back)
            assert any("without Basic Auth" in r.getMessage() for r in caplog.records)
            await _until(lambda: rt.online and engine.csms.connected("CP-1"))
        finally:
            await engine.stop()
    asyncio.run(main())


def test_is_loopback():
    from sme_ocpp_sim.engine import _is_loopback
    assert _is_loopback("127.0.0.1") and _is_loopback("::1") and _is_loopback("localhost")
    assert not _is_loopback("0.0.0.0") and not _is_loopback("::") and not _is_loopback("192.168.1.2")
