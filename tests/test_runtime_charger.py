"""ChargerRuntime against the ``ocpp``-library CSMS peer (the peer schema-validates every message)."""

import asyncio
import time

from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.runtime.charger import ChargerRuntime

CP = "CP1"
FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)


def _cfg(**kw):
    keys = {"MeterValueSampleInterval": "1", "StopTxnSampledData": "Energy.Active.Import.Register"}
    keys.update(kw.pop("config_keys", {}))
    return ChargerConfig(id=kw.pop("id", CP), config_keys=keys, **kw)


async def _until(cond, timeout=8.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def _scenario(body, peer_kw=None, cfg_kw=None, reconnect=FAST):
    async def main():
        peer = CsmsPeer(**{"hb_interval": 30, **(peer_kw or {})})
        await peer.start()
        rt = ChargerRuntime(_cfg(**(cfg_kw or {})), peer.url, reconnect=reconnect, frame_log=FrameLog(),
                            call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            await body(peer, rt)
        finally:
            stop.set()
            await asyncio.wait_for(task, 3)
            await peer.stop()
    asyncio.run(main())


def _statuses(s, cid):
    return [st for c, st, _ in s.statuses if c == cid]


async def _online(peer, rt):
    await _until(lambda: rt.online and CP in peer.sessions and len(peer.sessions[CP].statuses) >= 1 + len(rt.conns))
    return peer.sessions[CP]


def test_boot_accepted_heartbeats_and_status():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert sorted(c for c, _, _ in s.statuses) == [0, 1, 2]
        assert all(st == "Available" and e == "NoError" for _, st, e in s.statuses)
        await _until(lambda: s.heartbeats >= 2)
        assert rt.store.value("HeartbeatInterval") == "1"
        snap = rt.snapshot()
        assert snap["registration"] == "Accepted" and snap["boots"] == 1 and snap["last_heartbeat"]
        assert rt.store.value("NumberOfConnectors") == "2"
    _scenario(body, {"hb_interval": 1}, {"connectors": 2})


def test_boot_pending_serves_csms_calls_then_accepted():
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        r = await peer.get_configuration(CP, ["HeartbeatInterval", "Nope"])
        assert r.configuration_key[0]["key"] == "HeartbeatInterval" and r.unknown_key == ["Nope"]
        s = peer.sessions[CP]
        assert s.statuses == []  # nothing but Boot while Pending
        await _online(peer, rt)
        assert s.boot_statuses == ["Pending", "Accepted"] and rt.boots == 2
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 1})


def test_local_transaction_with_meter_values():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "BAD") == "Invalid"
        assert await rt.swipe(1, "TAG1") == "Accepted"
        assert s.starts[0]["id_tag"] == "TAG1" and s.starts[0]["connector_id"] == 1
        await _until(lambda: len(s.meter_values) >= 1)
        mv = s.meter_values[0]
        assert mv["transaction_id"] == s.starts[0]["transaction_id"]
        measurands = {v["measurand"] for v in mv["meter_value"][0]["sampled_value"]}
        assert measurands == {"Energy.Active.Import.Register", "Power.Active.Import"}
        assert await rt.stop_tx(1)
        stop = s.stops[0]
        assert stop["reason"] == "Local" and stop["meter_stop"] > s.starts[0]["meter_start"]
        assert stop["transaction_data"][0]["sampled_value"][0]["context"] == "Transaction.End"
        await rt.unplug(1)
        assert _statuses(s, 1) == ["Available", "Preparing", "Charging", "Finishing", "Available"]
        assert s.authorizes == []  # local_only never asks the CSMS
    _scenario(body, cfg_kw={"auth_mode": "local_only", "id_tags": ["TAG1"], "power_w": 36000})


def test_remote_start_stop_with_authorize():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert (await peer.remote_start(CP, "R1", 1)).status == "Accepted"
        await _until(lambda: len(s.starts) == 1)
        assert s.authorizes == ["R1"]  # AuthorizeRemoteTxRequests=true, csms_only
        tx = s.starts[0]["transaction_id"]
        await _until(lambda: rt.snapshot()["connectors"][0]["status"] == "Charging")
        assert (await peer.remote_start(CP, "R2", 1)).status == "Rejected"  # busy
        assert (await peer.remote_stop(CP, tx + 100)).status == "Rejected"
        assert (await peer.remote_stop(CP, tx)).status == "Accepted"
        await _until(lambda: len(s.stops) == 1)
        assert s.stops[0]["reason"] == "Remote" and s.stops[0]["transaction_id"] == tx
    _scenario(body, cfg_kw={"auth_mode": "csms_only", "config_keys": {"AuthorizeRemoteTxRequests": "true"}})


def test_reset_soft_during_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        assert (await peer.reset(CP, "Soft")).status == "Accepted"
        await _until(lambda: len(peer.history) == 2 and peer.history[1].boots == 1 and rt.online)
        assert s.stops[0]["reason"] == "SoftReset"
        assert rt.boots == 2 and rt.snapshot()["connectors"][0]["transaction"] is None
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_change_availability_scheduled_during_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        assert (await peer.change_availability(CP, 1, "Inoperative")).status == "Scheduled"
        assert rt.snapshot()["connectors"][0]["operative"] is True
        await rt.stop_tx(1)
        assert _statuses(s, 1)[-1] == "Unavailable" and rt.snapshot()["connectors"][0]["operative"] is False
        assert await rt.swipe(1, "T") == "Unavailable"
        assert (await peer.change_availability(CP, 0, "Operative")).status == "Accepted"
        await _until(lambda: _statuses(s, 1)[-1] == "Finishing")
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_misc_remote_control():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await peer.change_configuration(CP, "HeartbeatInterval", "7")).status == "Accepted"
        assert (await peer.change_configuration(CP, "NumberOfConnectors", "3")).status == "Rejected"
        assert (await peer.change_configuration(CP, "NoSuchKey", "1")).status == "NotSupported"
        assert rt.store.value("HeartbeatInterval") == "7"
        r = await peer.get_configuration(CP)
        assert len(r.configuration_key) == len(rt.store.get()[0]) and not r.unknown_key
        n = len(s.statuses)
        assert (await peer.trigger(CP, "StatusNotification", 1)).status == "Accepted"
        await _until(lambda: len(s.statuses) == n + 1)
        assert (await peer.trigger(CP, "Heartbeat")).status == "Accepted"
        await _until(lambda: s.heartbeats == 1)
        assert (await peer.trigger(CP, "MeterValues", 1)).status == "Accepted"
        await _until(lambda: len(s.meter_values) == 1)
        assert (await peer.trigger(CP, "DiagnosticsStatusNotification")).status == "NotImplemented"
        assert (await peer.trigger(CP, "BootNotification")).status == "Accepted"
        await _until(lambda: s.boots == 2)
        assert (await peer.unlock(CP, 1)).status == "Unlocked"
        assert (await peer.data_transfer(CP, "acme", "hello")).status == "UnknownVendorId"
        res = await rt.send_data_transfer("acme", "ping", "x")
        assert res.status.value == "Accepted" and res.data == "pong"
        assert s.data_transfers == [{"vendor_id": "acme", "message_id": "ping", "data": "x"}]
        await rt.fault(1, "GroundFailure")
        assert s.statuses[-1] == (1, "Faulted", "GroundFailure")
        await rt.clear_fault(1)
        assert s.statuses[-1] == (1, "Available", "NoError")
    _scenario(body)


def test_unregistered_charger_404_retried_with_backoff():
    async def body(peer, rt):
        await _until(lambda: rt.connect_attempts >= 3)
        assert rt.last_error["kind"] == "not_found" and not rt.connected
        assert CP not in peer.sessions
    _scenario(body, {"registered": {"OTHER"}})


def test_stop_ends_loop_promptly():
    async def main():
        peer = CsmsPeer(registered={"OTHER"})
        await peer.start()
        try:
            # long back-off: stop must interrupt the wait
            rt = ChargerRuntime(_cfg(), peer.url, reconnect=ReconnectConfig(min_s=30, max_s=60))
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            await _until(lambda: rt.last_error is not None)
            t0 = time.monotonic()
            stop.set()
            await asyncio.wait_for(task, 2)
            assert time.monotonic() - t0 < 1
            # connected: stop closes the session
            peer.registered = None
            rt = ChargerRuntime(_cfg(), peer.url, reconnect=FAST)
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            await _until(lambda: rt.online)
            t0 = time.monotonic()
            stop.set()
            await asyncio.wait_for(task, 2)
            assert time.monotonic() - t0 < 1 and not rt.connected
        finally:
            await peer.stop()
    asyncio.run(main())
