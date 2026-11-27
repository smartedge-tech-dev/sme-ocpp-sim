"""Station201 (OCPP 2.0.1 core, v0.2 M2) against the ``ocpp``-library 2.0.1 CSMS peer.

The peer schema-validates every message the charger sends; wire assertions use the peer's raw frame log.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from ocpp.v201 import call
from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig, SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.protocol import base as d
from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.protocol.v201.station import Station201
from sme_ocpp_sim.runtime.charger import ChargerRuntime

CP = "CS1"
FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)


async def _until(cond, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


def _scenario(body, peer_kw=None, cfg_kw=None, rt_kw=None):
    async def main():
        peer = CsmsPeer201(**{"hb_interval": 30, **(peer_kw or {})})
        await peer.start()
        rt = ChargerRuntime(ChargerConfig(id=CP, **(cfg_kw or {})), peer.url, "2.0.1", reconnect=FAST,
                            frame_log=FrameLog(), call_timeout=5, **(rt_kw or {}))
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            await body(peer, rt)
        finally:
            stop.set()
            async with asyncio.timeout(3):
                await task
            await peer.stop()
    asyncio.run(main())


async def _online(peer, rt, session=0):
    await _until(lambda: rt.online and len(peer.history) > session
                 and len(peer.history[session].statuses) >= rt.connector_count)
    return peer.history[session]


def _statuses(s, evse):
    return [st for e, _, st in s.statuses if e == evse]


def _frames(s, direction="rx"):
    return [json.loads(raw) for dr, raw in s.frames if dr == direction]


def _actions(s):
    """Actions of the CALLs the charger sent, in order."""
    return [f[2] for f in _frames(s) if f[0] == 2]


def _events(s, tx_id=None):
    return [e for e in s.transaction_events if tx_id is None or e["transaction_info"]["transaction_id"] == tx_id]


# --------------------------------------------------------------------------------------------- boot / status


def test_station201_is_hosted_and_speaks_ocpp201():
    rt = ChargerRuntime(ChargerConfig(id=CP, connectors=3), "ws://127.0.0.1:9/ocpp", "2.0.1")
    assert isinstance(rt.station, Station201) and rt.connector_count == 3
    assert rt.transport.subprotocol == "ocpp2.0.1" and rt.adapter.version == "2.0.1"
    assert rt.fault_codes == Station201.fault_codes and "GroundFault" in rt.fault_codes
    snap = rt.snapshot()
    assert snap["ocpp_version"] == "2.0.1" and snap["tx_queue"] == 0
    assert [(c["evse_id"], c["connector_id"], c["status"]) for c in snap["connectors"]] == [
        (1, 1, "Available"), (2, 1, "Available"), (3, 1, "Available")]


def test_config_accepts_201():
    cfg = SimConfig.model_validate({"csms_defaults": {"url": "ws://h/ocpp", "ocpp_version": "2.0.1"},
                                    "chargers": [{"id": "A"}]})
    assert cfg.resolve_csms(cfg.chargers[0]) == ("ws://h/ocpp", "2.0.1")


def test_boot_accepted_heartbeats_and_status_per_evse():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert s.subprotocol == "ocpp2.0.1"
        assert s.statuses == [(1, 1, "Available"), (2, 1, "Available")]  # one connector per EVSE, no EVSE 0
        boot = s.boot_requests[0]
        assert boot["reason"] == "PowerUp"
        assert boot["charging_station"]["vendor_name"] == "SmartEdge" and boot["charging_station"]["model"]
        await _until(lambda: s.heartbeats >= 2)
        assert rt.station.heartbeat_interval_s == 1
        snap = rt.snapshot()
        assert snap["registration"] == "Accepted" and snap["boots"] == 1 and snap["last_heartbeat"]
    _scenario(body, {"hb_interval": 1}, {"connectors": 2})


def test_boot_pending_answers_csms_calls_and_holds_messages_back():
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        s = peer.history[0]
        rt.station.data_transfer_rules[("acme", None)] = d.DataTransferResult(d.DataTransferStatus.ACCEPTED, "ok")
        r = await peer.data_transfer(CP, "acme", "m1", "hello")
        assert r.status == "Accepted" and r.data == "ok"
        assert (await peer.data_transfer(CP, "other")).status == "UnknownVendorId"
        await peer.request(CP, call.ClearCache())  # answered, even while Pending
        assert any(f[0] == 4 and f[2] == "NotImplemented" for f in _frames(s))
        # A local start while Pending is held back: nothing but BootNotification goes out before Accepted.
        await rt.plug(1)
        assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
        assert set(_actions(s)) == {"BootNotification"} and s.statuses == []
        await _until(lambda: len(s.transaction_events) == 1)
        assert s.boot_statuses == ["Pending", "Accepted"]
        started = s.transaction_events[0]
        assert started["event_type"] == "Started" and "offline" not in started  # connected, not offline
        first_after_boot = _actions(s).index("StatusNotification")
        assert _actions(s)[:first_after_boot] == ["BootNotification", "BootNotification"]
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 1}, {"auth_mode": "accept_all"})


def test_boot_rejected_is_retried_and_sends_nothing_else():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert s.boot_statuses == ["Rejected", "Accepted"] and rt.boots == 2
        acts = _actions(s)
        assert acts[:2] == ["BootNotification", "BootNotification"] and "BootNotification" not in acts[2:]
    _scenario(body, {"boot_statuses": ["Rejected"], "retry_interval": 1})


@pytest.mark.parametrize("action", [
    call.ClearCache(),
    call.GetReport(request_id=1),
    call.Get15118EVCertificate(iso15118_schema_version="urn:iso:15118:2:2013:MsgDef", action="Install",
                               exi_request="AAAA"),
    call.CertificateSigned(certificate_chain="x"),
], ids=lambda a: type(a).__name__)
def test_other_actions_answer_not_implemented(action):
    async def body(peer, rt):
        s = await _online(peer, rt)
        await peer.request(CP, action)
        errors = [f for f in _frames(s) if f[0] == 4]
        assert errors and errors[-1][2] == "NotImplemented"
    _scenario(body)


# --------------------------------------------------------------------------------------------- authorize


@pytest.mark.parametrize("mode,tag,expect,asks", [
    ("accept_all", "ANY", "Accepted", False),
    ("local_only", "TAG1", "Accepted", False),
    ("local_only", "BAD", "Invalid", False),
    ("local_then_csms", "TAG1", "Accepted", False),
    ("local_then_csms", "BLK", "Blocked", True),
    ("csms_only", "TAG1", "Accepted", True),
    ("csms_only", "NEW", "NotAtThisTime", True),  # a 2.0.1-only status is reported as Invalid
])
def test_authorize_per_auth_mode(mode, tag, expect, asks):
    async def body(peer, rt):
        s = await _online(peer, rt)
        result = await rt.swipe(1, tag)
        assert result == (expect if expect != "NotAtThisTime" else "Invalid")
        assert ([a["id_token"] for a in s.authorizes] == [tag]) is asks
        if asks:
            assert s.authorizes[0]["type"] == "ISO14443"
        assert rt.snapshot()["connectors"][0]["pending_tag"] == (tag if result == "Accepted" else None)
        assert s.transaction_events == []  # not plugged: the token waits for the cable
    _scenario(body, {"token_statuses": {"BLK": "Blocked", "NEW": "NotAtThisTime"}},
              {"auth_mode": mode, "id_tags": ["TAG1"]})


def test_csms_only_offline_is_refused():
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        assert await rt.swipe(1, "T") == AuthResult.OFFLINE
    _scenario(body, {"boot_statuses": ["Pending"] * 5, "retry_interval": 30}, {"auth_mode": "csms_only"})


# --------------------------------------------------------------------------------------------- transactions


def test_full_transaction_started_updated_ended():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await _until(lambda: _statuses(s, 1) == ["Available", "Occupied"])
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        await _until(lambda: len([e for e in s.transaction_events if e["event_type"] == "Updated"]) >= 2)
        assert await rt.swipe(1, "TAG1") == AuthResult.STOPPED
        await _until(lambda: s.transaction_events[-1]["event_type"] == "Ended")
        evs = s.transaction_events
        tx_id = evs[0]["transaction_info"]["transaction_id"]
        assert str(uuid.UUID(tx_id)) == tx_id  # generated by the station
        assert all(e["transaction_info"]["transaction_id"] == tx_id for e in evs)
        assert [e["seq_no"] for e in evs] == list(range(len(evs)))  # continuous from 0
        started, ended = evs[0], evs[-1]
        assert started["trigger_reason"] == "Authorized" and started["id_token"] == {"id_token": "TAG1",
                                                                                      "type": "ISO14443"}
        assert started["evse"] == {"id": 1, "connector_id": 1}
        assert started["transaction_info"]["charging_state"] == "Charging"
        begin = started["meter_value"][0]["sampled_value"][0]
        assert begin["context"] == "Transaction.Begin" and begin["measurand"] == "Energy.Active.Import.Register"
        periodic = [e for e in evs[1:-1] if e["trigger_reason"] == "MeterValuePeriodic"]
        assert len(periodic) >= 2
        assert {v["measurand"] for v in periodic[0]["meter_value"][0]["sampled_value"]} == {
            "Energy.Active.Import.Register", "Power.Active.Import"}
        assert ended["trigger_reason"] == "StopAuthorized" and ended["id_token"]["id_token"] == "TAG1"
        assert ended["transaction_info"]["stopped_reason"] == "Local"
        assert ended["transaction_info"]["charging_state"] == "EVConnected"
        end = ended["meter_value"][0]["sampled_value"][0]
        assert end["context"] == "Transaction.End" and end["value"] > begin["value"]
        assert end["value"] == round(rt.snapshot()["connectors"][0]["meter_wh"])
        assert s.authorizes == []  # local_only
        await rt.unplug(1)
        await _until(lambda: _statuses(s, 1) == ["Available", "Occupied", "Available"])
        assert len(s.transaction_events) == len(evs)  # no event after the transaction ended
    _scenario(body, cfg_kw={"auth_mode": "local_only", "id_tags": ["TAG1"], "max_power_w": 36000,
                            "meter_sample_interval_s": 1})


def test_plug_after_authorize_starts_and_unplug_ends():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
        assert s.transaction_events == [] and rt.snapshot()["connectors"][0]["status"] == "Available"
        await rt.plug(1)
        await _until(lambda: len(s.transaction_events) == 1)
        assert s.transaction_events[0]["trigger_reason"] == "CablePluggedIn"
        await rt.unplug(1)
        await _until(lambda: len(s.transaction_events) == 2)
        ended = s.transaction_events[1]
        assert ended["trigger_reason"] == "EVCommunicationLost" and "id_token" not in ended
        assert ended["transaction_info"]["stopped_reason"] == "EVDisconnected"
        assert ended["transaction_info"]["charging_state"] == "Idle" and ended["seq_no"] == 1
        await _until(lambda: _statuses(s, 1) == ["Available", "Occupied", "Available"])
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "meter_sample_interval_s": 0})


def test_token_refused_in_started_answer_ends_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "BAD") == AuthResult.ACCEPTED  # accept_all: the CSMS refuses it later
        await _until(lambda: len(s.transaction_events) == 2)
        ended = s.transaction_events[1]
        assert ended["trigger_reason"] == "Deauthorized"
        assert ended["transaction_info"]["stopped_reason"] == "DeAuthorized"
        assert not rt.has_transaction(1)
    _scenario(body, {"token_statuses": {"BAD": "Invalid"}}, {"auth_mode": "accept_all"})


def test_fault_ends_transaction_and_clear():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        await rt.fault(1, "GroundFault")
        await _until(lambda: len(s.transaction_events) == 2)
        ended = s.transaction_events[1]
        assert ended["trigger_reason"] == "AbnormalCondition"
        assert ended["transaction_info"]["stopped_reason"] == "GroundFault"
        assert rt.faulted(1) == "GroundFault" and await rt.clear_fault(1)
        await _until(lambda: _statuses(s, 1) == ["Available", "Occupied", "Faulted", "Occupied"])
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_charging_state_change_is_reported():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        await _until(lambda: any(e["trigger_reason"] == "ChargingStateChanged" for e in s.transaction_events))
        change = next(e for e in s.transaction_events if e["trigger_reason"] == "ChargingStateChanged")
        assert change["event_type"] == "Updated" and change["transaction_info"]["charging_state"] == "SuspendedEV"
        assert _statuses(s, 1) == ["Available", "Occupied"]  # still Occupied: no StatusNotification
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "max_power_w": 50000, "meter_sample_interval_s": 0,
                            "session": {"initial_soc": 89.99, "target_soc": 90}},
              rt_kw={"tick_s": 0.05, "time_scale": 1000})


def test_events_raised_offline_are_queued_and_resent_with_offline_flag():
    async def main():
        peer = CsmsPeer201(hb_interval=30)
        port = await peer.start()
        rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2, auth_mode="accept_all", meter_sample_interval_s=0),
                            peer.url, "2.0.1", reconnect=FAST, call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            s1 = await _online(peer, rt)
            await rt.plug(1)
            await rt.swipe(1, "A")
            await _until(lambda: len(s1.transaction_events) == 1)
            await _until(lambda: rt.snapshot()["tx_queue"] == 0)  # the answer arrived (else Started is resent)
            tx1 = s1.transaction_events[0]["transaction_info"]["transaction_id"]
            await peer.stop()
            await _until(lambda: not rt.connected and rt.last_error is not None)
            assert await rt.stop_tx(1)                      # Ended of tx1, raised offline
            await rt.plug(2)
            assert await rt.swipe(2, "B") == "Accepted"     # a whole new transaction, offline
            assert rt.snapshot()["tx_queue"] == 2
            tx2 = rt.snapshot()["connectors"][1]["transaction"]["tx_id"]
            await peer.start(port=port)
            s2 = await _online(peer, rt, session=1)
            await _until(lambda: len(s2.transaction_events) == 2)
            ended, started = s2.transaction_events
            assert ended["transaction_info"]["transaction_id"] == tx1 and ended["event_type"] == "Ended"
            assert ended["offline"] is True and ended["seq_no"] == 1  # continues tx1's numbering
            assert started["transaction_info"]["transaction_id"] == tx2 and started["event_type"] == "Started"
            assert started["offline"] is True and started["seq_no"] == 0
            await _until(lambda: rt.snapshot()["tx_queue"] == 0)
            # The state was refreshed before reporting: EVSE 1 Occupied (cable still in), EVSE 2 Occupied.
            assert s2.statuses[:2] == [(1, 1, "Occupied"), (2, 1, "Occupied")]
        finally:
            stop.set()
            async with asyncio.timeout(3):
                await task
            await peer.stop()
    asyncio.run(main())


def test_lost_answer_keeps_event_queued_and_resends_it():
    async def main():
        peer = CsmsPeer201(hb_interval=30)
        await peer.start()
        rt = ChargerRuntime(ChargerConfig(id=CP, auth_mode="accept_all", meter_sample_interval_s=0), peer.url,
                            "2.0.1", reconnect=FAST, call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            s1 = await _online(peer, rt)
            peer.gates["TransactionEvent"] = asyncio.Event()  # never answered in this session
            await rt.plug(1)
            await rt.swipe(1, "A")
            await _until(lambda: any(f[0] == 2 and f[2] == "TransactionEvent" for f in _frames(s1)))
            rt.abort_session()  # the connection drops with the answer outstanding
            await _until(lambda: not rt.connected)
            peer.gates.pop("TransactionEvent").set()  # the peer answers into the closed connection
            s2 = await _online(peer, rt, session=1)
            await _until(lambda: len(s2.transaction_events) == 1)
            assert s2.transaction_events[0]["event_type"] == "Started" and "offline" not in s2.transaction_events[0]
            assert s2.transaction_events[0]["seq_no"] == 0
        finally:
            stop.set()
            async with asyncio.timeout(3):
                await task
            await peer.stop()
    asyncio.run(main())


# --------------------------------------------------------------------------------------------- MeterValues / DataTransfer


def test_meter_values_and_data_transfer():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.station.send_meter_values(2)
        assert s.meter_values[0]["evse_id"] == 2
        sv = s.meter_values[0]["meter_value"][0]["sampled_value"]
        assert sv[0]["measurand"] == "Energy.Active.Import.Register" and sv[0]["context"] == "Other"
        res = await rt.send_data_transfer("acme", "ping", "x")
        assert res.status is d.DataTransferStatus.ACCEPTED and res.data == "pong"
        assert s.data_transfers[0] == {"vendor_id": "acme", "message_id": "ping", "data": "x"}
    _scenario(body, cfg_kw={"connectors": 2})


# --------------------------------------------------------------------------------------------- version switch


def _engine_config(urls, versions):
    return SimConfig.model_validate({
        "csms_defaults": {"url": urls["1.6"], "reconnect": {"min_s": 0.05, "max_s": 0.2, "jitter": 0}},
        "chargers": [{"id": cid, "auth_mode": "accept_all", "ocpp_version": v, "csms_url": urls[v]}
                     for cid, v in versions.items()]})


def test_version_switch_reconnects_only_that_charger_and_clears_its_transactions():
    async def main():
        p16, p201 = CsmsPeer(hb_interval=30), CsmsPeer201(hb_interval=30)
        await p16.start()
        await p201.start()
        urls = {"1.6": p16.url, "2.0.1": p201.url}
        engine = OcppEngine(_engine_config(urls, {"A": "1.6", "B": "1.6"}))
        await engine.start()
        try:
            await _until(lambda: all(engine.charger(c).online for c in ("A", "B")))
            await engine.charger("A").plug(1)
            assert await engine.charger("A").swipe(1, "T") == "Accepted"
            b = engine.charger("B")
            await engine.reload(_engine_config(urls, {"A": "2.0.1", "B": "1.6"}))
            a = engine.charger("A")
            assert a.adapter.version == "2.0.1" and not a.has_transaction(1)
            await _until(lambda: a.online and "A" in p201.sessions)
            assert engine.charger("B") is b and b.online and len([h for h in p16.history if h.cp_id == "B"]) == 1
            await engine.reload(_engine_config(urls, {"A": "1.6", "B": "1.6"}))
            a16 = engine.charger("A")
            await _until(lambda: a16.online)
            assert a16.adapter.version == "1.6" and not a16.has_transaction(1)
            assert len([h for h in p16.history if h.cp_id == "A"]) == 2
            assert engine.charger("B") is b and len([h for h in p16.history if h.cp_id == "B"]) == 1
        finally:
            await engine.stop()
            await p16.stop()
            await p201.stop()
    asyncio.run(main())


def test_web_api_serves_a_201_charger(tmp_path):
    from fastapi.testclient import TestClient

    from sme_ocpp_sim.web import FAULT_CODES, create_app

    cfg = SimConfig.model_validate({"csms_defaults": {"url": "ws://127.0.0.1:9/ocpp", "ocpp_version": "2.0.1"},
                                    "chargers": [{"id": "CP1", "connectors": 2}]})
    with TestClient(create_app(OcppEngine(cfg), tmp_path / "sim.yaml")) as client:
        detail = client.get("/api/chargers/CP1").json()
        assert detail["ocpp_version"] == "2.0.1" and detail["fault_codes"] == list(Station201.fault_codes)
        assert set(Station201.fault_codes) <= set(FAULT_CODES)
        assert [c["connector_status"] for c in detail["connectors"]] == ["Available", "Available"]
        assert client.get("/api/chargers/CP1/profiles").json() == {"profiles": []}
        conf = client.get("/api/chargers/CP1/configuration").json()
        assert conf["keys"] == [] and conf["ocpp_version"] == "2.0.1"
        hb = next(v for v in conf["variables"] if v["variable"] == "HeartbeatInterval")
        assert hb["component"] == "OCPPCommCtrlr" and hb["attributes"] == {"Actual": "300"}
        assert client.get("/api/chargers/CP1/composite?connector=1").json()["periods"] == [
            {"start_period": 0, "limit": 6000.0, "number_phases": None}]
        assert client.post("/api/chargers/CP1/fault", json={"error_code": "GroundFault", "connector": 1}).status_code == 200
