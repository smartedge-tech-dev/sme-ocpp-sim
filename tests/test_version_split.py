"""v0.2 M0: the shared shell hosts a per-version station; fault codes and the bad-frame policy come from the version."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from rpc_fakes import FakeWs

from sme_ocpp_sim.config import ChargerConfig, SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.protocol import base as d
from sme_ocpp_sim.protocol.registry import get_station_class
from sme_ocpp_sim.protocol.station import VersionStation
from sme_ocpp_sim.protocol.v16.enums import ChargePointErrorCode
from sme_ocpp_sim.protocol.v16.station import Station16
from sme_ocpp_sim.rpc import OCPP16_ERRORS, ErrorCodes, RpcConnection
from sme_ocpp_sim.runtime.charger import ChargerRuntime
from sme_ocpp_sim.web import FAULT_CODES, create_app

DEAD_URL = "ws://127.0.0.1:9/ocpp"


async def _until(cond, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.005)


# --------------------------------------------------------------------------------------------- fault codes


def test_fault_codes_are_provided_by_the_version_station():
    expected = tuple(e.value for e in ChargePointErrorCode if e is not ChargePointErrorCode.no_error)
    assert get_station_class("1.6") is Station16
    assert Station16.fault_codes == expected and "NoError" not in expected
    rt = ChargerRuntime(ChargerConfig(id="CP"), DEAD_URL)
    assert isinstance(rt.station, Station16) and rt.fault_codes == expected
    assert set(expected) <= set(FAULT_CODES)  # the OpenAPI enum is the union over the versions


def test_charger_detail_returns_its_fault_codes_and_fault_checks_them(tmp_path):
    cfg = SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [{"id": "CP1"}]})
    engine = OcppEngine(cfg)
    with TestClient(create_app(engine, tmp_path / "sim.yaml")) as client:
        detail = client.get("/api/chargers/CP1").json()
        assert detail["id"] == "CP1" and detail["ocpp_version"] == "1.6"
        assert detail["fault_codes"] == list(Station16.fault_codes)
        assert client.get("/api/chargers/NOPE").status_code == 404
        # The charger's own list decides (the request model only knows the union of all versions).
        engine.charger("CP1").station.fault_codes = ("OtherError",)
        r = client.post("/api/chargers/CP1/fault", json={"error_code": "GroundFailure"})
        assert r.status_code == 400 and "GroundFailure" in r.json()["error"]
        assert client.post("/api/chargers/CP1/fault", json={"error_code": "OtherError"}).status_code == 200
        assert engine.charger("CP1").faulted(1) == "OtherError"


# --------------------------------------------------------------------------------------------- bad-frame policy


async def _bad_frame_answers(codes: ErrorCodes) -> Tuple[List[Any], List[Optional[str]]]:
    ws = FakeWs()
    notes: List[Optional[str]] = []
    rpc = RpcConnection(ws, {}, error_codes=codes, on_frame=lambda f: notes.append(f.note))
    rx = asyncio.create_task(rpc.run())
    ws.feed("garbage")                    # no uniqueId at all
    ws.feed([2, "u1", "FooBar", {}])      # a marker that is always answered
    await _until(lambda: any(json.loads(r)[1] == "u1" for r in ws.sent))
    await rpc.close()
    await rx
    return [json.loads(r) for r in ws.sent], notes


def test_bad_frame_policy_v16_drops():
    assert OCPP16_ERRORS.bad_frame_reply is None
    assert Station16(_FakeHost()).adapter.error_codes is OCPP16_ERRORS
    sent, notes = asyncio.run(_bad_frame_answers(OCPP16_ERRORS))
    assert [f[1] for f in sent] == ["u1"]  # nothing went out for the unreadable frame
    assert any(n and n.startswith("dropped") for n in notes)


def test_bad_frame_policy_reply_is_chosen_by_the_error_table():
    codes = ErrorCodes(rpc_framework_error="RpcFrameworkError", bad_frame_reply="-1")
    sent, _ = asyncio.run(_bad_frame_answers(codes))
    assert [(f[0], f[1], f[2]) for f in sent] == [(4, "-1", "RpcFrameworkError"), (4, "u1", "NotImplemented")]


# --------------------------------------------------------------------------------------------- fake version station


class _FakeHost:
    """Just enough of a StationHost to construct a Station16 outside a runtime."""

    def __init__(self) -> None:
        self.cfg = ChargerConfig(id="CP")
        self.ev = self.supply = None


class _FakeAdapter(d.VersionAdapter):
    version = "9.9"
    subprotocol = "ocpp9.9"
    error_codes = ErrorCodes()

    def __init__(self, *, strict: bool = False) -> None:
        super().__init__(strict=strict)
        self.boots = 0
        self.heartbeats = 0

    def handlers(self, runtime: d.CommandSink) -> Dict[str, Any]:
        async def reset(payload: dict) -> dict:
            return {"status": (await runtime.handle(d.Reset(d.ResetKind.SOFT))).value}
        return {"Reset": reset}

    async def send_booting(self, ev: d.Booting) -> d.BootResult:
        self.boots += 1
        return d.BootResult(d.BootStatus.ACCEPTED, 0, datetime.now(timezone.utc))

    async def send_heartbeat(self, ev: d.Heartbeat) -> Optional[datetime]:
        self.heartbeats += 1
        return datetime.now(timezone.utc)

    async def send_connector_status(self, ev): ...
    async def send_authorize(self, ev): ...
    async def send_tx_started(self, ev): ...
    async def send_tx_updated(self, ev): ...
    async def send_tx_ended(self, ev): ...
    async def send_meter_sample(self, ev): ...
    async def send_data_transfer(self, cmd): ...


class _FakeStation(VersionStation):
    """A minimal version: one connector, a 0.02 s heartbeat, Reset ends the session."""

    fault_codes = ("Broken",)

    def __init__(self, host, *, strict: bool = False) -> None:
        super().__init__(host, strict=strict)
        self._adapter = _FakeAdapter(strict=strict)
        self.events: List[str] = []
        self.ticks = 0
        self.hb_interval: Optional[int] = None

    @property
    def adapter(self) -> _FakeAdapter:
        return self._adapter

    @property
    def connector_count(self) -> int:
        return 1

    async def handle(self, cmd: Any) -> Any:
        if isinstance(cmd, d.Reset):
            self.host.after_reply(self.host.end_session_for_reset)
            return d.CommandStatus.ACCEPTED
        return d.CommandStatus.NOT_IMPLEMENTED

    async def on_online(self) -> None:
        self.events.append(f"online:{self.host.online}")
        self.host.start_ticks()

    def on_session_ended(self) -> None:
        self.events.append("ended")

    async def wait_heartbeat(self) -> bool:
        await asyncio.sleep(0.02)
        return False

    def set_heartbeat_interval(self, seconds: int) -> None:
        self.hb_interval = seconds

    def settle(self) -> None:
        self.events.append("settle")

    def tick(self) -> None:
        self.ticks += 1

    def snapshot(self) -> Dict[str, Any]:
        return {"fake": True}

    def configuration(self) -> List[d.ConfigEntry]:
        return [d.ConfigEntry("FakeKey", True, "1")]


def test_shell_hosts_a_minimal_fake_version_station():
    sockets: List[FakeWs] = []

    async def connect(_cfg):
        ws = FakeWs()
        sockets.append(ws)
        return ws

    async def main():
        rt = ChargerRuntime(ChargerConfig(id="CP"), "ws://unused/ocpp", "1.6", connect=connect, tick_s=0.01,
                            station=_FakeStation)
        st = rt.station
        assert isinstance(st, _FakeStation) and rt.adapter is st.adapter
        assert rt.transport.subprotocol == "ocpp9.9" and rt.connector_count == 1
        assert rt.fault_codes == ("Broken",)
        assert [e.key for e in rt.configuration()] == ["FakeKey"]
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        await _until(lambda: rt.online and st.ticks >= 3 and st.adapter.heartbeats >= 2)
        assert st.events == ["online:True"] and st.adapter.boots == 1 and rt.boots == 1
        snap = rt.snapshot()
        assert snap["ocpp_version"] == "9.9" and snap["fake"] is True and snap["registration"] == "Accepted"
        rt.set_paused(True)
        assert rt.paused and st.events[-1] == "settle"
        # An inbound CALL reaches the station through the fake adapter's handlers; Reset closes the session
        # and the shell reconnects at once (no back-off after a Reset).
        sockets[0].feed([2, "r1", "Reset", {}])
        await _until(lambda: len(sockets) == 2 and rt.online)
        assert [json.loads(r) for r in sockets[0].sent] == [[3, "r1", {"status": "Accepted"}]]
        assert st.events[:3] == ["online:True", "settle", "ended"]
        stop.set()
        async with asyncio.timeout(3):
            await task
        assert st.events[-1] == "ended" and not rt.connected

    asyncio.run(main())


@pytest.mark.parametrize("op,args", [("plug", (1,)), ("swipe", (1, "T")), ("fault", (1, "Broken"))])
def test_operator_methods_default_to_not_implemented(op, args):
    rt = ChargerRuntime(ChargerConfig(id="CP"), DEAD_URL, station=_FakeStation)
    with pytest.raises(NotImplementedError):
        asyncio.run(getattr(rt, op)(*args))
