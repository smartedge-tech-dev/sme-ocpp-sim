"""Unit tests of the OCPP 2.0.1 adapter and station core (v0.2 M2), without a CSMS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sme_ocpp_sim.charging.ev import PRESETS
from sme_ocpp_sim.charging.power import AcSupply
from sme_ocpp_sim.config import ChargerConfig
from sme_ocpp_sim.protocol import base as d
from sme_ocpp_sim.protocol.v201 import station as st
from sme_ocpp_sim.protocol.v201.adapter import Adapter201, auth_result, connector_status
from sme_ocpp_sim.protocol.v201.enums import AuthorizationStatusEnumType as Auth
from sme_ocpp_sim.protocol.v201.schema import SchemaValidator, ocpp_package_schema_dir
from sme_ocpp_sim.protocol.v201.station import Station201
from sme_ocpp_sim.rpc import OCPP201_ERRORS, CallError

T0 = datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc)
SCHEMAS = ocpp_package_schema_dir()


# --------------------------------------------------------------------------------------------- adapter


@pytest.mark.parametrize("status,wire", [
    ("Available", "Available"), ("Preparing", "Occupied"), ("Charging", "Occupied"), ("SuspendedEV", "Occupied"),
    ("SuspendedEVSE", "Occupied"), ("Finishing", "Occupied"), ("Reserved", "Reserved"),
    ("Unavailable", "Unavailable"), ("Faulted", "Faulted"),
])
def test_connector_status_projection(status, wire):
    assert connector_status(d.DomainStatus(status)).value == wire


def test_auth_status_mapping():
    for s in ("Accepted", "Blocked", "Expired", "Invalid", "ConcurrentTx"):
        assert auth_result(Auth(s)) is d.AuthResult(s)
    for s in ("NoCredit", "NotAllowedTypeEVSE", "NotAtThisLocation", "NotAtThisTime", "Unknown"):
        assert auth_result(Auth(s)) is d.AuthResult.INVALID


class _Sink:
    connector_count = 1

    def __init__(self, result):
        self.result, self.cmds = result, []

    async def handle(self, cmd):
        self.cmds.append(cmd)
        return self.result


def test_only_data_transfer_is_served_inbound():
    a = Adapter201()
    assert a.error_codes is OCPP201_ERRORS and a.subprotocol == "ocpp2.0.1"
    sink = _Sink(d.DataTransferResult(d.DataTransferStatus.ACCEPTED, "r"))
    handlers = a.handlers(sink)
    assert set(handlers) == {"DataTransfer"}  # every other action: NotImplemented by the RPC layer
    out = asyncio.run(handlers["DataTransfer"]({"vendorId": "v", "messageId": "m", "data": {"k": [1]}}))
    assert out == {"status": "Accepted", "data": "r"}
    assert sink.cmds == [d.DataTransfer("v", "m", {"k": [1]})]
    with pytest.raises(CallError) as ei:
        asyncio.run(handlers["DataTransfer"]({"messageId": "m"}))
    assert ei.value.code == "OccurenceConstraintViolation"


def test_1_6_shaped_transaction_calls_are_not_used():
    a = Adapter201()
    with pytest.raises(NotImplementedError):
        asyncio.run(a.send_tx_started(d.TxStarted(1, "T", 0, T0)))


@pytest.mark.skipif(SCHEMAS is None, reason="ocpp package schemas not installed")
def test_outbound_payloads_match_the_schemas():
    validator = SchemaValidator(SCHEMAS)
    sent = []
    answers = {
        "BootNotification": {"currentTime": "2026-11-27T09:00:00Z", "interval": 10, "status": "Accepted"},
        "Heartbeat": {"currentTime": "2026-11-27T09:00:00Z"},
        "StatusNotification": {}, "MeterValues": {},
        "Authorize": {"idTokenInfo": {"status": "NoCredit"}},
        "DataTransfer": {"status": "Rejected"},
    }

    async def call(action, payload):
        validator.validate(action, payload)
        sent.append(action)
        return answers[action]

    async def main():
        a = Adapter201()
        a.bind(call)
        boot = await a.send_booting(d.Booting("V" * 50, "M", "S", "F"))
        assert boot.status is d.BootStatus.ACCEPTED and boot.interval == 10
        await a.send_heartbeat(d.Heartbeat())
        await a.send_connector_status(d.ConnectorStatus(2, 1, d.DomainStatus.CHARGING, timestamp=T0))
        assert (await a.send_authorize(d.AuthorizeRequest("TAG"))).status is d.AuthResult.INVALID
        await a.send_meter_sample(d.MeterSample(1, T0, [d.Sample("Energy.Active.Import.Register", 5, "Wh")]))
        assert (await a.send_data_transfer(d.DataTransfer("v"))).status is d.DataTransferStatus.REJECTED
        a.bind(None)
        with pytest.raises(ConnectionError):
            await a.send_heartbeat(d.Heartbeat())
    asyncio.run(main())
    assert sent == ["BootNotification", "Heartbeat", "StatusNotification", "Authorize", "MeterValues",
                    "DataTransfer"]


# --------------------------------------------------------------------------------------------- station


class _Host:
    """Just enough of a StationHost for Station201 outside a runtime."""

    def __init__(self, connectors=2, **cfg):
        self.cfg = ChargerConfig(id="CS", connectors=connectors, **cfg)
        self.ev = PRESETS[self.cfg.session.ev_model]
        self.supply = AcSupply(self.cfg.voltage_v, self.cfg.phases, self.cfg.line_to_line)
        self.connected = False
        self.online = False
        self.paused = False
        self.registration = None
        self.call_timeout = 5.0
        self.spawned = []

    def clock(self):
        return T0

    def monotonic(self):
        return 0.0

    def step_power(self, pw, dt_s, evse_limit_w):
        from sme_ocpp_sim.charging import power
        return power.step(pw, dt_s=dt_s, ev=self.ev, target_soc_pct=90, evse_limit_w=evse_limit_w,
                          evse_max_w=self.cfg.max_power_w, supply=self.supply)

    def spawn(self, coro):
        coro.close()
        self.spawned.append(coro)
        return None


def _queued(s: Station201):
    return [x.req for x in s._outq if x.req is not None]


def test_offline_transaction_events_are_numbered_and_flagged():
    async def main():
        s = Station201(_Host(auth_mode="accept_all"))
        await s.plug(1)
        assert await s.swipe(1, "T") == d.AuthResult.ACCEPTED
        assert await s.stop_tx(1)
        started, ended = _queued(s)
        assert started.transaction_info.transaction_id == ended.transaction_info.transaction_id
        assert (started.seq_no, ended.seq_no) == (0, 1) and started.offline is ended.offline is True
        assert ended.meter_value[0].sampled_value[0].context.value == "Transaction.End"
        assert s.snapshot()["tx_queue"] == 2
        await s.plug(2)
        await s.swipe(2, "U")
        third = _queued(s)[-1]
        assert third.seq_no == 0 and third.transaction_info.transaction_id != started.transaction_info.transaction_id
    asyncio.run(main())


def test_queue_bound_drops_only_periodic_updates(monkeypatch):
    monkeypatch.setattr(st, "MAX_TX_QUEUE", 2)

    async def main():
        s = Station201(_Host(connectors=1, auth_mode="accept_all"))
        await s.plug(1)
        await s.swipe(1, "T")
        e = s.evses[1]
        for _ in range(4):
            s._queue_event(e, e.tx, st.TransactionEventEnumType.updated, st.T.meter_value_periodic,
                           st.m.TransactionType(transaction_id=e.tx.tx_id), droppable=True)
        await s.stop_tx(1)
        seqs = [r.seq_no for r in _queued(s)]
        assert seqs == [0, 3, 4, 5]  # Started and Ended kept; the two oldest periodic Updated dropped
    asyncio.run(main())


def test_unknown_evse_and_unsupported_smart_charging():
    s = Station201(_Host(connectors=1))
    with pytest.raises(ValueError, match="no EVSE 2"):
        asyncio.run(s.plug(2))
    assert s.installed_profiles() == () and s.configuration() == []
    assert s.composite(0, 60, "W").status is d.CommandStatus.REJECTED
    assert asyncio.run(s.handle(d.Reset(d.ResetKind.SOFT))) is d.CommandStatus.NOT_IMPLEMENTED
