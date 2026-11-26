"""RPC + transport against the ``ocpp``-library CSMS peer (schema-validated by the peer)."""

import asyncio
import base64
import json

import pytest
from peers.certs import make_pki
from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.rpc import HandshakeError, RpcConnection, TransportConfig, open_connection, wait_or_stop

PASSWORD = "p:ss:word"
CP_ID = "CP 001/x"


class MiniCharger:
    """Just enough charger to drive the RPC layer (the real runtime comes in M3)."""

    def __init__(self, cfg: TransportConfig, log: FrameLog) -> None:
        self.cfg, self.log = cfg, log
        self.config = {"HeartbeatInterval": "0"}
        self.heartbeats = 0

    async def remote_start(self, payload):
        return {"status": "Accepted"}

    async def get_configuration(self, payload):
        keys = payload.get("key") or list(self.config)
        result = {"configurationKey": [{"key": k, "readonly": False, "value": self.config[k]}
                                       for k in keys if k in self.config]}
        unknown = [k for k in keys if k not in self.config]
        if unknown:
            result["unknownKey"] = unknown
        return result

    async def run(self, stop: asyncio.Event) -> None:
        ws = await open_connection(self.cfg)
        rpc = RpcConnection(ws, {"RemoteStartTransaction": self.remote_start,
                                 "GetConfiguration": self.get_configuration},
                            call_timeout=5, on_frame=self.log.callback(self.cfg.charger_id),
                            name=self.cfg.charger_id)
        rx = asyncio.create_task(rpc.run())
        try:
            boot = await rpc.call("BootNotification", {"chargePointVendor": "SME",
                                                       "chargePointModel": "OCPPSim"})
            assert boot["status"] == "Accepted"
            self.config["HeartbeatInterval"] = str(boot["interval"])
            while not await wait_or_stop(stop, boot["interval"]):
                await rpc.call("Heartbeat", {})
                self.heartbeats += 1
        finally:
            await rpc.close()
            await rx


async def _until(cond, timeout=10.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.05)
    await asyncio.wait_for(poll(), timeout)


def _scenario(tmp_path=None):
    async def main():
        ssl_ctx = pki = None
        if tmp_path is not None:
            pki = make_pki(tmp_path)
            ssl_ctx = pki.server_context()
        peer = CsmsPeer(hb_interval=1, exercise=True, registered={CP_ID},
                        passwords={CP_ID: PASSWORD}, ssl_context=ssl_ctx)
        await peer.start()
        log = FrameLog()
        cfg = TransportConfig(peer.url, CP_ID, password=PASSWORD,
                              ca_file=str(pki.ca_file) if pki else None)
        cp = MiniCharger(cfg, log)
        stop = asyncio.Event()
        task = asyncio.create_task(cp.run(stop))
        try:
            await _until(lambda: CP_ID in peer.sessions and peer.sessions[CP_ID].exercised.is_set()
                         and cp.heartbeats >= 2)
            s = peer.sessions[CP_ID]
            await _until(lambda: sum(1 for d, r in s.frames if d == "rx" and json.loads(r)[0] == 4) >= 4)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)
            await peer.stop()
        return s, log
    return asyncio.run(main())


def _check(s, log):
    assert s.subprotocol == "ocpp1.6"
    scheme, token = s.authorization.split(" ", 1)
    assert (scheme, *base64.b64decode(token).decode().split(":", 1)) == ("Basic", CP_ID, PASSWORD)
    assert s.boots == 1 and s.heartbeats >= 2
    assert s.results["RemoteStartTransaction"] == "Accepted"
    conf, unknown = s.results["GetConfiguration"]
    assert conf == [{"key": "HeartbeatInterval", "readonly": False, "value": "1"}]
    assert unknown == ["NoSuchKey"]
    assert s.results["FooBarUnknownAction"] == "NotImplemented"
    errors = {m[1]: m[2] for m in (json.loads(r) for d, r in s.frames if d == "rx") if m[0] == 4}
    assert {k: errors[k] for k in ("bad-payload", "bad-shape", "bad-type")} == {
        "bad-payload": "FormationViolation", "bad-shape": "FormationViolation",
        "bad-type": "ProtocolError"}
    entries = log.since(0, charger=CP_ID)
    assert {e.direction for e in entries} == {"rx", "tx"}
    assert any(e.direction == "rx" and e.message_type == "CALLRESULT" and e.action == "BootNotification"
               for e in entries)
    assert PASSWORD not in log.to_jsonl()


def test_profile1_against_ocpp_peer():
    _check(*_scenario())


def test_profile2_against_ocpp_peer_with_own_ca(tmp_path):
    _check(*_scenario(tmp_path))


@pytest.mark.parametrize("cp_id, password, kind", [
    ("UNKNOWN", PASSWORD, "not_found"),
    (CP_ID, "wrong", "unauthorized"),
    (CP_ID, None, "unauthorized"),
])
def test_rejected_handshakes(cp_id, password, kind):
    async def main():
        peer = CsmsPeer(registered={CP_ID}, passwords={CP_ID: PASSWORD})
        await peer.start()
        try:
            await open_connection(TransportConfig(peer.url, cp_id, password=password))
        finally:
            await peer.stop()
    with pytest.raises(HandshakeError) as ei:
        asyncio.run(main())
    assert ei.value.kind == kind
