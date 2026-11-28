"""ChargerRuntime against the ``ocpp``-library CSMS peer (the peer schema-validates every message)."""

import asyncio
import time

import pytest
from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.runtime.charger import ChargerRuntime

CP = "CP1"
FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)
#: CSMS peer per OCPP version, for the tests that do not depend on version messages.
PEERS = {"1.6": CsmsPeer, "2.0.1": CsmsPeer201}
VERSIONS = pytest.mark.parametrize("version", list(PEERS))


def _cfg(**kw):
    keys = {"MeterValueSampleInterval": "1", "StopTxnSampledData": "Energy.Active.Import.Register"}
    keys.update(kw.pop("config_keys", {}))
    return ChargerConfig(id=kw.pop("id", CP), config_keys=keys, **kw)


async def _until(cond, timeout=8.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def _scenario(body, peer_kw=None, cfg_kw=None, reconnect=FAST, version="1.6"):
    async def main():
        peer = PEERS[version](**{"hb_interval": 30, **(peer_kw or {})})
        await peer.start()
        rt = ChargerRuntime(_cfg(**(cfg_kw or {})), peer.url, version, reconnect=reconnect, frame_log=FrameLog(),
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
    await _until(lambda: rt.online and CP in peer.sessions and len(peer.sessions[CP].statuses) >= 1 + len(rt.station.conns))
    return peer.sessions[CP]


def test_boot_accepted_heartbeats_and_status():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert sorted(c for c, _, _ in s.statuses) == [0, 1, 2]
        assert all(st == "Available" and e == "NoError" for _, st, e in s.statuses)
        await _until(lambda: s.heartbeats >= 2)
        assert rt.station.store.value("HeartbeatInterval") == "1"
        snap = rt.snapshot()
        assert snap["registration"] == "Accepted" and snap["boots"] == 1 and snap["last_heartbeat"]
        assert rt.station.store.value("NumberOfConnectors") == "2"
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
        assert await rt.stop_tx(1)  # queued, never waits for the CSMS
        await _until(lambda: s.stops)
        stop = s.stops[0]
        assert stop["reason"] == "Local" and stop["meter_stop"] > s.starts[0]["meter_start"]
        assert stop["transaction_data"][-1]["sampled_value"][0]["context"] == "Transaction.End"
        await rt.unplug(1)
        await _until(lambda: len(_statuses(s, 1)) == 5)
        assert _statuses(s, 1) == ["Available", "Preparing", "Charging", "Finishing", "Available"]
        assert s.authorizes == []  # local_only never asks the CSMS
    _scenario(body, cfg_kw={"auth_mode": "local_only", "id_tags": ["TAG1"], "max_power_w": 36000})


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
        await _until(lambda: _statuses(s, 1)[-1] == "Unavailable")
        assert rt.snapshot()["connectors"][0]["operative"] is False
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
        assert rt.station.store.value("HeartbeatInterval") == "7"
        r = await peer.get_configuration(CP)
        assert len(r.configuration_key) == len(rt.station.store.get()[0]) and not r.unknown_key
        n = len(s.statuses)
        assert (await peer.trigger(CP, "StatusNotification", 1)).status == "Accepted"
        await _until(lambda: len(s.statuses) == n + 1)
        assert (await peer.trigger(CP, "Heartbeat")).status == "Accepted"
        await _until(lambda: s.heartbeats == 1)
        assert (await peer.trigger(CP, "MeterValues", 1)).status == "Accepted"
        await _until(lambda: len(s.meter_values) == 1)
        assert (await peer.trigger(CP, "DiagnosticsStatusNotification")).status == "Accepted"  # M7
        await _until(lambda: s.diagnostics_statuses == ["Idle"])
        assert (await peer.trigger(CP, "BootNotification")).status == "Accepted"
        await _until(lambda: s.boots == 2)
        assert (await peer.unlock(CP, 1)).status == "Unlocked"
        assert (await peer.data_transfer(CP, "acme", "hello")).status == "UnknownVendorId"
        res = await rt.send_data_transfer("acme", "ping", "x")
        assert res.status.value == "Accepted" and res.data == "pong"
        assert s.data_transfers == [{"vendor_id": "acme", "message_id": "ping", "data": "x"}]
        await rt.fault(1, "GroundFailure")
        await _until(lambda: s.statuses[-1] == (1, "Faulted", "GroundFailure"))
        await rt.clear_fault(1)
        await _until(lambda: s.statuses[-1] == (1, "Available", "NoError"))
    _scenario(body)


@VERSIONS
def test_unregistered_charger_404_retried_with_backoff(version):
    async def body(peer, rt):
        await _until(lambda: rt.connect_attempts >= 3)
        assert rt.last_error["kind"] == "not_found" and not rt.connected
        assert CP not in peer.sessions
    _scenario(body, {"registered": {"OTHER"}}, version=version)


@VERSIONS
def test_boot_pending_then_accepted_resets_the_error(version):
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        assert not rt.online and rt.boots == 1
        await _until(lambda: rt.online)
        assert rt.boots == 2 and rt.last_error is None and rt.snapshot()["registration"] == "Accepted"
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 1}, version=version)


@VERSIONS
def test_stop_ends_loop_promptly(version):
    async def main():
        peer = PEERS[version](registered={"OTHER"})
        await peer.start()
        try:
            # long back-off: stop must interrupt the wait
            rt = ChargerRuntime(_cfg(), peer.url, version, reconnect=ReconnectConfig(min_s=30, max_s=60))
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            await _until(lambda: rt.last_error is not None)
            t0 = time.monotonic()
            stop.set()
            await asyncio.wait_for(task, 2)
            assert time.monotonic() - t0 < 1
            # connected: stop closes the session
            peer.registered = None
            rt = ChargerRuntime(_cfg(), peer.url, version, reconnect=FAST)
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


def test_stop_while_offline_is_queued_and_delivered_after_reconnect():
    async def main():
        peer = CsmsPeer(hb_interval=30)
        port = await peer.start()
        rt = ChargerRuntime(_cfg(auth_mode="accept_all", connectors=2,
                                 config_keys={"MeterValueSampleInterval": "0"}), peer.url, reconnect=FAST,
                            call_timeout=5)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            s1 = await _online(peer, rt)
            for cid in (1, 2):
                await rt.plug(cid)
                assert await rt.swipe(cid, "T") == "Accepted"
            tx, tx2 = (st["transaction_id"] for st in s1.starts)
            await peer.stop()
            await _until(lambda: not rt.connected and rt.last_error is not None)
            assert await rt.stop_tx(1) and await rt.stop_tx(2)
            assert s1.stops == [] and rt.snapshot()["tx_queue"] == 2
            assert rt.snapshot()["connectors"][0]["status"] == "Finishing"
            assert rt.last_error["kind"] == "disconnected"
            await peer.start(port=port)
            # both queued StopTransactions go out after the boot, in order, with no other trigger (no meter loop)
            await _until(lambda: len(peer.history) == 2 and len(peer.history[1].stops) == 2)
            s2 = peer.history[1]
            assert [x["transaction_id"] for x in s2.stops] == [tx, tx2] and s2.stops[0]["reason"] == "Local"
            await _until(lambda: rt.snapshot()["tx_queue"] == 0)
            assert (1, "Charging", "NoError") not in s2.statuses  # state refreshed before reporting
        finally:
            stop.set()
            await asyncio.wait_for(task, 3)
            await peer.stop()
    asyncio.run(main())


class _RecordingBackoff:
    def __init__(self):
        self.nexts = self.resets = 0

    def next(self):
        self.nexts += 1
        return 0.01

    def reset(self):
        self.resets += 1


@VERSIONS
def test_backoff_reset_only_after_accepted_boot(version):
    from rpc_fakes import FakeWs

    async def connect(_cfg):
        ws = FakeWs()
        await ws.close()  # the CSMS accepts the socket and drops it at once
        return ws

    async def main():
        bo = _RecordingBackoff()
        rt = ChargerRuntime(_cfg(), "ws://unused/ocpp", version, connect=connect, backoff=bo)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        await _until(lambda: rt.connect_attempts >= 4)
        stop.set()
        await asyncio.wait_for(task, 3)
        assert bo.resets == 0 and bo.nexts >= 3
        assert rt.last_error["kind"] == "disconnected"

        peer = PEERS[version](hb_interval=30)
        await peer.start()
        try:
            bo = _RecordingBackoff()
            rt = ChargerRuntime(_cfg(), peer.url, version, backoff=bo)
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            await _until(lambda: rt.online)
            assert bo.resets == 1 and rt.last_error is None
            stop.set()
            await asyncio.wait_for(task, 3)
        finally:
            await peer.stop()
    asyncio.run(main())


def test_triggered_boot_keeps_registration():
    async def body(peer, rt):
        s = await _online(peer, rt)
        peer.boot_statuses = ["Pending"]
        assert (await peer.trigger(CP, "BootNotification")).status == "Accepted"
        await _until(lambda: s.boots == 2)
        await asyncio.sleep(0.1)
        assert s.boot_statuses[-1] == "Pending"
        assert rt.registration == "Accepted" and rt.online
    _scenario(body)


def test_swipe_rechecks_state_after_authorize():
    async def body(peer, rt):
        await _online(peer, rt)
        await rt.plug(1)
        real = rt.station._authorize_info

        async def auth_then_fault(tag):
            res = await real(tag)
            await rt.fault(1, "GroundFailure")
            return res
        rt.station._authorize_info = auth_then_fault
        assert await rt.swipe(1, "T") == "Unavailable"
        await rt.clear_fault(1)

        async def auth_then_other_start(tag):
            res = await real(tag)
            await rt.station._start_tx(rt.station.conns[1], "OTHER")
            return res
        rt.station._authorize_info = auth_then_other_start
        assert await rt.swipe(1, "T") == "Busy"
        assert rt.snapshot()["connectors"][0]["transaction"]["id_tag"] == "OTHER"
        rt.station._authorize_info = real
        assert await rt.swipe(1, "X") == "Busy"
        assert await rt.swipe(1, "OTHER") == "Stopped"
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_heartbeat_interval_change_takes_effect_promptly():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert rt.station.store.value("HeartbeatInterval") == "30" and s.heartbeats == 0
        assert (await peer.change_configuration(CP, "HeartbeatInterval", "1")).status == "Accepted"
        await _until(lambda: s.heartbeats >= 1, timeout=4)
    _scenario(body)


def test_meter_interval_change_takes_effect_promptly():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        await asyncio.sleep(0.3)
        assert s.meter_values == []
        assert (await peer.change_configuration(CP, "MeterValueSampleInterval", "1")).status == "Accepted"
        await _until(lambda: len(s.meter_values) >= 1, timeout=4)
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "config_keys": {"MeterValueSampleInterval": "600"}})


def _rx_seen(s, action):
    return any(d == "rx" and f'"{action}"' in raw for d, raw in s.frames)


def test_stop_during_inflight_start_sends_one_stop_after_answer():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        gate = peer.gates["StartTransaction"] = asyncio.Event()
        swipe = asyncio.create_task(rt.swipe(1, "T"))
        await _until(lambda: _rx_seen(s, "StartTransaction"))
        assert await rt.stop_tx(1)  # returns at once although StartTransaction is unanswered
        gate.set()
        assert await swipe == "Accepted"
        await _until(lambda: s.stops)
        await asyncio.sleep(0.2)
        assert len(s.stops) == 1 and s.stops[0]["transaction_id"] == s.starts[0]["transaction_id"]
        await _until(lambda: _statuses(s, 1)[-1] == "Finishing")
        assert "Charging" not in _statuses(s, 1)
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_cancelled_caller_does_not_lose_stop():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        peer.delays["StopTransaction"] = 0.2
        t = asyncio.create_task(rt.stop_tx(1))
        await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await _until(lambda: s.stops)
        assert s.stops[0]["transaction_id"] == s.starts[0]["transaction_id"]
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_unplug_returns_promptly_while_csms_is_slow():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.swipe(1, "T")
        peer.delays.update({"StopTransaction": 0.5, "StatusNotification": 0.5})
        t0 = time.monotonic()
        await rt.unplug(1)
        await rt.fault(1, "GroundFailure")
        assert time.monotonic() - t0 < 0.1
        await _until(lambda: s.stops and _statuses(s, 1)[-1] == "Faulted")
        assert _statuses(s, 1)[-2:] == ["Available", "Faulted"]
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_lost_answer_keeps_start_queued_and_resends_it():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        gate = peer.gates["StartTransaction"] = asyncio.Event()
        swipe = asyncio.create_task(rt.swipe(1, "T"))
        await _until(lambda: _rx_seen(s, "StartTransaction"))
        rt._abort.set()  # drop the connection while StartTransaction is in flight
        assert await swipe == "Pending"
        assert rt.snapshot()["connectors"][0]["status"] == "Preparing" and rt.snapshot()["tx_queue"] == 1
        gate.set()
        await _until(lambda: len(peer.history) == 2 and rt.snapshot()["connectors"][0]["status"] == "Charging")
        s2 = peer.history[1]
        assert rt.station.conns[1].tx.tx_id == str(s2.starts[-1]["transaction_id"])
        await _until(lambda: (1, "Charging", "NoError") in s2.statuses)
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_reset_during_inflight_start():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        gate = peer.gates["StartTransaction"] = asyncio.Event()
        swipe = asyncio.create_task(rt.swipe(1, "T"))
        await _until(lambda: _rx_seen(s, "StartTransaction"))
        reset = asyncio.create_task(peer.reset(CP, "Soft"))  # answered once the peer reads again
        await _until(lambda: rt.station.conns[1].tx is None)  # Reset ended the pending transaction
        gate.set()
        assert await swipe == "Accepted" and (await reset).status == "Accepted"
        await _until(lambda: len(peer.history) == 2 and rt.online)
        stops = [x for h in peer.history for x in h.stops]
        starts = [x for h in peer.history for x in h.starts]
        assert len(starts) == 1 and len(stops) == 1
        assert stops[0]["transaction_id"] == starts[0]["transaction_id"] and stops[0]["reason"] == "SoftReset"
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def _rx_index(s, action):
    return next(i for i, (d, raw) in enumerate(s.frames) if d == "rx" and f'"{action}"' in raw)


def test_reset_waits_for_stop_queued_by_inflight_start_answer():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        gate = peer.gates["StartTransaction"] = asyncio.Event()
        swipe = asyncio.create_task(rt.swipe(1, "T"))
        await _until(lambda: _rx_seen(s, "StartTransaction"))
        reset = asyncio.create_task(peer.reset(CP, "Soft"))
        await _until(lambda: rt.station.conns[1].tx is None)  # Reset ended the pending transaction, no Stop queued yet
        gate.set()
        assert await swipe == "Accepted" and (await reset).status == "Accepted"
        await _until(lambda: len(peer.history) == 2 and rt.online)
        # The StopTransaction queued by the StartTransaction answer went out on the first session, after it.
        assert len(s.stops) == 1 and peer.history[1].stops == []
        assert s.stops[0]["transaction_id"] == s.starts[0]["transaction_id"]
        assert _rx_index(s, "StartTransaction") < _rx_index(s, "StopTransaction")
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_exception_applying_start_answer_restarts_session():
    async def body(peer, rt):
        await _online(peer, rt)
        await rt.plug(1)
        real = rt.station._on_started

        def boom(c, tx, res):
            rt.station._on_started = real
            raise RuntimeError("injected")
        rt.station._on_started = boom
        async with asyncio.timeout(5):
            assert await rt.swipe(1, "T") == "Error"
        await _until(lambda: len(peer.history) == 2 and rt.online)
        assert rt.station.conns[1].tx is None and rt.snapshot()["tx_queue"] == 0
        assert rt.snapshot()["connectors"][0]["status"] == "Preparing"  # plugged, no transaction
        assert await rt.swipe(1, "T") == "Accepted"  # the new session's worker runs
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_rejected_remote_start_keeps_newer_pending_tag():
    async def body(peer, rt):
        await _online(peer, rt)
        c = rt.station.conns[1]

        async def other_start_then_reject(tag):
            assert await rt.station._start_tx(c, "OTHER") == "Accepted"  # unplugged: OTHER waits for the cable
            return AuthResult.INVALID
        c.pending_tag = "R1"  # reserved by RequestStartTransaction
        rt.station._authorize = other_start_then_reject
        await rt.station._remote_start_go(c, "R1")
        assert c.pending_tag == "OTHER"
        c.pending_tag = "R2"

        async def reject(tag):
            return AuthResult.INVALID
        rt.station._authorize = reject
        await rt.station._remote_start_go(c, "R2")
        assert c.pending_tag is None
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "config_keys": {"AuthorizeRemoteTxRequests": "true"}})


def test_queue_bound_drops_oldest_meter_values_only(monkeypatch):
    from sme_ocpp_sim.protocol.v16 import station as ch
    monkeypatch.setattr(ch, "MAX_TX_QUEUE", 2)

    async def main():
        rt = ChargerRuntime(_cfg(), "ws://unused/ocpp")

        def noop():
            return None
        kinds = [ch._Kind.STOP, ch._Kind.METER, ch._Kind.METER, ch._Kind.START, ch._Kind.METER]
        msgs = [ch._OutMsg(k, noop) for k in kinds]
        for m in msgs:
            rt.station._outq_push(m)
        assert list(rt.station._outq) == [msgs[0], msgs[2], msgs[3], msgs[4]]
        assert rt.snapshot()["tx_queue"] == 4
    asyncio.run(main())


# ---------------------------------------------------------------------- retry limits (reconnect / boot)

async def _refused(_cfg):
    # Like a closed port, without the seconds Windows spends retrying the SYN.
    from sme_ocpp_sim.rpc import HandshakeError
    raise HandshakeError("network", "connection refused")


@VERSIONS
def test_reconnect_max_retries_gives_up(version):
    async def main():
        rt = ChargerRuntime(_cfg(), "ws://unused/ocpp", version, connect=_refused,
                            reconnect=ReconnectConfig(min_s=0.05, max_s=0.1, jitter=0, max_retries=2))
        await asyncio.wait_for(rt.run(asyncio.Event()), 5)  # returns by itself
        assert rt.connect_attempts == 3
        assert rt.gave_up == {"reason": "max_retries", "attempts": 3, "last_kind": "network"}
        assert rt.last_error["kind"] == "retries_exhausted" and rt.snapshot()["gave_up"]["attempts"] == 3
        # A new run (the operator's Connect) counts from zero again.
        await asyncio.wait_for(rt.run(asyncio.Event()), 5)
        assert rt.connect_attempts == 6 and rt.gave_up["attempts"] == 3
    asyncio.run(main())


@VERSIONS
def test_reconnect_max_retries_counts_sessions_closed_before_accepted(version):
    from rpc_fakes import FakeWs

    async def connect(_cfg):
        ws = FakeWs()
        await ws.close()
        return ws

    async def main():
        rt = ChargerRuntime(_cfg(), "ws://unused/ocpp", version, connect=connect,
                            reconnect=ReconnectConfig(min_s=0.05, max_s=0.1, jitter=0, max_retries=1))
        await asyncio.wait_for(rt.run(asyncio.Event()), 5)
        assert rt.gave_up["reason"] == "max_retries" and rt.gave_up["last_kind"] == "disconnected"
        assert rt.connect_attempts == 2
    asyncio.run(main())


@VERSIONS
def test_boot_max_retries_gives_up_and_closes_the_session(version):
    async def main():
        peer = PEERS[version](hb_interval=30, boot_statuses=["Rejected"] * 5, retry_interval=1)
        await peer.start()
        try:
            rt = ChargerRuntime(_cfg(boot_max_retries=1), peer.url, version, reconnect=FAST, call_timeout=5)
            await asyncio.wait_for(rt.run(asyncio.Event()), 8)
            assert rt.gave_up == {"reason": "boot_max_retries", "attempts": 2, "last_kind": "Rejected"}
            assert rt.boots == 2 and not rt.connected and not rt.online
            assert rt.last_error["kind"] == "retries_exhausted" and rt.connect_attempts == 1
            assert peer.sessions[CP].boot_statuses == ["Rejected", "Rejected"]
        finally:
            await peer.stop()
    asyncio.run(main())


def test_boot_max_retries_counts_pending_and_resets_on_accepted():
    async def body(peer, rt):
        await _until(lambda: rt.online)
        assert rt.gave_up is None and rt._boot_failures == 0
    # One Pending is within boot_max_retries=1; the second boot is Accepted.
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 1}, {"boot_max_retries": 1})


def test_boot_max_retries_pending_at_the_limit_gives_up():
    async def main():
        peer = CsmsPeer(hb_interval=30, boot_statuses=["Pending"] * 3, retry_interval=1)
        await peer.start()
        try:
            rt = ChargerRuntime(_cfg(boot_max_retries=0), peer.url, "1.6", reconnect=FAST, call_timeout=5)
            await asyncio.wait_for(rt.run(asyncio.Event()), 8)
            assert rt.gave_up == {"reason": "boot_max_retries", "attempts": 1, "last_kind": "Pending"}
            assert "1 boots" in rt.last_error["message"]
        finally:
            await peer.stop()
    asyncio.run(main())


@VERSIONS
def test_reconnect_failures_reset_after_accepted_boot(version):
    async def main():
        peer = PEERS[version](hb_interval=30)
        await peer.start()
        try:
            rt = ChargerRuntime(_cfg(), peer.url, version, call_timeout=5,
                                reconnect=ReconnectConfig(min_s=0.05, max_s=0.1, jitter=0, max_retries=1))
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            for n in range(1, 4):  # more accepted-then-dropped sessions than max_retries allows failures
                await _until(lambda: rt.online and len(peer.history) == n)
                assert rt._failures == 0
                rt._abort.set()  # drop the connection after the accepted boot
                await _until(lambda: not rt.online)
            await _until(lambda: rt.online)
            assert rt.gave_up is None and not task.done()
            stop.set()
            await asyncio.wait_for(task, 3)
        finally:
            await peer.stop()
    asyncio.run(main())


# ------------------------------------------------------------------ network impairments (v0.2.2 A part 2)

def test_forced_drop_reconnects_after_the_pause_and_keeps_the_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == "Accepted"
        attempts = rt.connect_attempts
        rt.drop("abort", reconnect_after_s=0.3)
        await _until(lambda: (rt.last_error or {}).get("kind") == "simulated_drop")
        assert not rt.connected and rt.drops == 1
        assert await rt.stop_tx(1)  # queued while offline, delivered on the next session
        t0 = time.monotonic()
        await _until(lambda: rt.online and len(peer.history) == 2)
        assert time.monotonic() - t0 >= 0.25  # the pause, not the (shorter) back-off
        assert rt.connect_attempts == attempts + 1 and rt.last_error is None
        await _until(lambda: peer.history[-1].stops)
        assert peer.history[-1].stops[0]["transaction_id"] == s.starts[0]["transaction_id"]
        assert rt.snapshot()["drops"] == 1
    _scenario(body, cfg_kw={"auth_mode": "local_only", "id_tags": ["TAG1"]})


def test_drop_needs_a_connection_and_a_valid_mode():
    rt = ChargerRuntime(_cfg(), "ws://127.0.0.1:1/ocpp", "1.6")
    with pytest.raises(ConnectionError):
        rt.drop()
    rt.connected = True
    for kw in ({"mode": "unplug"}, {"reconnect_after_s": -1}):
        with pytest.raises(ValueError):
            rt.drop(**kw)


@VERSIONS
def test_simulated_drops_do_not_count_against_max_retries(version):
    # The documented choice: a simulated drop is not a failed connection, even before an accepted boot, where a
    # real disconnect would count (and max_retries=0 would give up at once).
    async def main():
        peer = PEERS[version](hb_interval=30, boot_statuses=["Pending"] * 10, retry_interval=30)
        await peer.start()
        try:
            rt = ChargerRuntime(_cfg(), peer.url, version, call_timeout=5,
                                reconnect=ReconnectConfig(min_s=0.05, max_s=0.1, jitter=0, max_retries=0))
            stop = asyncio.Event()
            task = asyncio.create_task(rt.run(stop))
            for n in range(1, 4):
                await _until(lambda: rt.connected and rt.registration == "Pending" and len(peer.history) == n)
                rt.drop("close", reconnect_after_s=0.05)
                await _until(lambda: not rt.connected)
            await _until(lambda: rt.connected and len(peer.history) == 4)
            assert rt.gave_up is None and rt._failures == 0 and rt.drops == 3 and not task.done()
            stop.set()
            await asyncio.wait_for(task, 3)
        finally:
            await peer.stop()
    asyncio.run(main())


@VERSIONS
def test_periodic_drop_and_live_switch_off(version):
    async def body(peer, rt):
        await _until(lambda: rt.online)
        assert rt.drops == 0
        rt.set_impairments(rt.impairments.model_copy(update={"drop_every_s": 0.3, "drop_jitter_s": 0.1,
                                                             "down_s": 0.1, "drop_mode": "close"}))
        assert rt.impaired
        await _until(lambda: rt.drops >= 2 and rt.online)
        assert rt.gave_up is None and len(peer.history) >= 3
        rt.set_impairments(rt.impairments.model_copy(update={"drop_every_s": None}))
        await _until(lambda: rt.online)
        drops = rt.drops
        await asyncio.sleep(0.8)  # well past the old interval
        assert rt.drops == drops and rt.online and not rt.impaired
    _scenario(body, version=version)


def test_latency_applies_live_to_the_open_session():
    async def body(peer, rt):
        await _until(lambda: rt.online)
        attempts = rt.connect_attempts
        sock = rt._rpc._ws
        assert sock.next_delay("tx") == 0.0
        rt.set_impairments(rt.impairments.model_copy(update={"latency_ms": 200, "direction": "tx"}))
        assert sock.next_delay("tx") == pytest.approx(0.2) and sock.next_delay("rx") == 0.0
        t0 = time.monotonic()
        await rt.heartbeat()  # CP -> CSMS: the CALL waits 0.2 s before it is written
        assert time.monotonic() - t0 >= 0.18
        assert rt.connect_attempts == attempts and rt.online  # no reconnect
    _scenario(body)