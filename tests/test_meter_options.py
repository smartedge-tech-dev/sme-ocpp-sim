"""Transaction meter data and the ``meter:`` simulator options (v0.2.2 B) against the schema-validating peers.

1.6: StopTransaction.req transactionData (StopTxnSampledData / StopTxnAlignedData, §3.16.1 / §9.1.25-27),
MeterValues Transaction.Begin / End (``meter.begin_end_messages``). 2.0.1: Ended meterValue (Sampled / Aligned
TxEndedInterval and TxEndedMeasurands). Both: per-phase values and the post-transaction delay.
"""

import asyncio
import time
from datetime import datetime

from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig
from sme_ocpp_sim.framelog import FrameLog
from sme_ocpp_sim.runtime.charger import ChargerRuntime

CP = "CP1"
FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)


async def _until(cond, timeout=8.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def _run(version, cfg, body):
    async def main():
        peer = (CsmsPeer if version == "1.6" else CsmsPeer201)(hb_interval=30)
        await peer.start()
        rt = ChargerRuntime(ChargerConfig(id=CP, auth_mode="accept_all", **cfg), peer.url, version, reconnect=FAST,
                            frame_log=FrameLog(), call_timeout=5, tick_s=0.1)
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            if version == "1.6":
                await _until(lambda: rt.online and CP in peer.sessions)
                await body(peer.sessions[CP], rt, peer)
            else:
                await _until(lambda: rt.online and len(peer.history) > 0)
                await body(peer.history[0], rt, peer)
        finally:
            stop.set()
            await asyncio.wait_for(task, 3)
            await peer.stop()
    asyncio.run(main())


async def _charge(rt, seconds):
    await rt.plug(1)
    assert await rt.swipe(1, "TAG1") == "Accepted"
    await asyncio.sleep(seconds)
    assert await rt.stop_tx(1)


def _contexts(meter_values):
    return [mv["sampled_value"][0]["context"] for mv in meter_values]


def _ts(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# ------------------------------------------------------------------------------------------------ OCPP 1.6

def _keys16(**kw):
    return {"MeterValueSampleInterval": "1", "StopTxnSampledData": "Energy.Active.Import.Register", **kw}


def test_16_transaction_data_samples_every_interval_from_the_start():
    async def body(s, rt, peer):
        await _charge(rt, 2.6)
        await _until(lambda: s.stops)
        data = s.stops[0]["transaction_data"]
        ctx = _contexts(data)
        assert ctx[-1] == "Transaction.End" and set(ctx[:-1]) == {"Sample.Periodic"} and len(ctx) >= 3
        assert all(v["sampled_value"][0]["measurand"] == "Energy.Active.Import.Register" for v in data)
        stamps = [_ts(v["timestamp"]) for v in data]
        assert stamps == sorted(stamps)
    _run("1.6", {"config_keys": _keys16()}, body)


def test_16_transaction_data_cap_keeps_the_newest_readings():
    async def body(s, rt, peer):
        await _charge(rt, 2.6)
        await _until(lambda: s.stops)
        assert _contexts(s.stops[0]["transaction_data"]) == ["Sample.Periodic", "Transaction.End"]
    _run("1.6", {"config_keys": _keys16(), "meter": {"tx_data_max_samples": 1}}, body)


def test_16_both_keys_empty_no_transaction_data():
    async def body(s, rt, peer):
        await _charge(rt, 1.3)
        await _until(lambda: s.stops)
        assert not s.stops[0]["transaction_data"]  # §3.16.5: no meter values at all
    _run("1.6", {"config_keys": _keys16(StopTxnSampledData="")}, body)


def test_16_stop_txn_aligned_data_on_clock_boundaries():
    async def body(s, rt, peer):
        await _charge(rt, 2.3)
        await _until(lambda: s.stops)
        data = s.stops[0]["transaction_data"]
        assert len(data) >= 1 and _contexts(data) == ["Sample.Clock"] * len(data)
        assert all(v["sampled_value"][0]["measurand"] == "Voltage" for v in data)
        assert all(_ts(v["timestamp"]).microsecond == 0 for v in data)  # the boundary, not the tick
    _run("1.6", {"config_keys": _keys16(StopTxnSampledData="", StopTxnAlignedData="Voltage",
                                        ClockAlignedDataInterval="1")}, body)


def test_16_change_configuration_rejects_unsupported_measurand():
    async def body(s, rt, peer):
        assert (await peer.change_configuration(CP, "StopTxnSampledData", "Temperature")).status == "Rejected"
        assert (await peer.change_configuration(CP, "StopTxnAlignedData", "SoC,Voltage")).status == "Accepted"
    _run("1.6", {}, body)


def test_16_begin_end_meter_values():
    async def body(s, rt, peer):
        await _charge(rt, 0.5)
        await _until(lambda: s.stops)
        assert [mv["meter_value"][0]["sampled_value"][0]["context"] for mv in s.meter_values] == [
            "Transaction.Begin", "Transaction.End"]
        assert all(mv["transaction_id"] == s.starts[0]["transaction_id"] for mv in s.meter_values)
        measurands = {v["measurand"] for v in s.meter_values[1]["meter_value"][0]["sampled_value"]}
        assert measurands == {"Energy.Active.Import.Register", "Power.Active.Import"}  # MeterValuesSampledData
    _run("1.6", {"config_keys": _keys16(MeterValueSampleInterval="0"), "meter": {"begin_end_messages": True}}, body)


def test_16_phase_values_line_to_line():
    async def body(s, rt, peer):
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == "Accepted"
        await _until(lambda: s.meter_values)
        values = s.meter_values[0]["meter_value"][0]["sampled_value"]
        assert [(v["measurand"], v["phase"]) for v in values] == [
            ("Voltage", "L1-L2"), ("Voltage", "L2-L3"), ("Voltage", "L3-L1"),
            ("Current.Import", "L1"), ("Current.Import", "L2"), ("Current.Import", "L3")]
        assert float(values[0]["value"]) == 398.4 and float(values[3]["value"]) > 0
    _run("1.6", {"phases": 3, "voltage_v": 230.0, "max_power_w": 11000,
                 "config_keys": _keys16(MeterValuesSampledData="Voltage,Current.Import"),
                 "meter": {"phase_values": "L-L"}}, body)


def test_16_post_transaction_delay():
    async def body(s, rt, peer):
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == "Accepted"
        t0 = time.monotonic()
        assert await rt.stop_tx(1)
        await _until(lambda: s.stops)
        assert time.monotonic() - t0 >= 0.9
        assert s.stops[0]["reason"] == "Local"
    _run("1.6", {"config_keys": _keys16(), "meter": {"post_tx_delay_s": 1.0}}, body)


# ------------------------------------------------------------------------------------------------ OCPP 2.0.1

def _var(component, variable, value):
    return {"component": component, "variable": variable, "value": value}


def _ended(s):
    evs = [e for e in s.transaction_events if e["event_type"] == "Ended"]
    return evs[0] if evs else None


def test_201_ended_interval_zero_only_start_and_end():
    async def body(s, rt, peer):
        await _charge(rt, 1.3)
        await _until(lambda: _ended(s))
        assert _contexts(_ended(s)["meter_value"]) == ["Transaction.Begin", "Transaction.End"]
    _run("2.0.1", {}, body)


def test_201_ended_interval_samples_from_the_start():
    async def body(s, rt, peer):
        await _charge(rt, 2.6)
        await _until(lambda: _ended(s))
        ctx = _contexts(_ended(s)["meter_value"])
        assert ctx[0] == "Transaction.Begin" and ctx[-1] == "Transaction.End"
        assert set(ctx[1:-1]) == {"Sample.Periodic"} and len(ctx) >= 4
    _run("2.0.1", {"variables": [_var("SampledDataCtrlr", "TxEndedInterval", "1")]}, body)


def test_201_ended_aligned_union_of_measurands():
    async def body(s, rt, peer):
        await _charge(rt, 2.3)
        await _until(lambda: _ended(s))
        mvs = _ended(s)["meter_value"]
        ctx = _contexts(mvs)
        assert ctx[0] == "Transaction.Begin" and ctx[-1] == "Transaction.End" and "Sample.Clock" in ctx
        assert {v["measurand"] for v in mvs[0]["sampled_value"]} == {"Energy.Active.Import.Register", "Voltage"}
        clock = [mv for mv in mvs if mv["sampled_value"][0]["context"] == "Sample.Clock"]
        assert all({v["measurand"] for v in mv["sampled_value"]} == {"Voltage"} for mv in clock)
    _run("2.0.1", {"variables": [_var("AlignedDataCtrlr", "TxEndedInterval", "1"),
                                 _var("AlignedDataCtrlr", "TxEndedMeasurands", "Voltage")]}, body)


def test_201_tx_data_cap():
    async def body(s, rt, peer):
        await _charge(rt, 2.6)
        await _until(lambda: _ended(s))
        assert _contexts(_ended(s)["meter_value"]) == ["Transaction.Begin", "Sample.Periodic", "Transaction.End"]
    _run("2.0.1", {"variables": [_var("SampledDataCtrlr", "TxEndedInterval", "1")],
                   "meter": {"tx_data_max_samples": 1}}, body)


def test_201_phase_values_line_to_neutral():
    async def body(s, rt, peer):
        await _charge(rt, 0.3)
        await _until(lambda: _ended(s))
        values = _ended(s)["meter_value"][-1]["sampled_value"]
        assert [(v["measurand"], v.get("phase")) for v in values] == [
            ("Voltage", "L1-N"), ("Voltage", "L2-N"), ("Voltage", "L3-N")]
        assert float(values[0]["value"]) == 230.0
    _run("2.0.1", {"phases": 3, "voltage_v": 230.0, "meter": {"phase_values": "L-N"},
                   "variables": [_var("SampledDataCtrlr", "TxEndedMeasurands", "Voltage")]}, body)


def test_201_post_transaction_delay():
    async def body(s, rt, peer):
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == "Accepted"
        await _until(lambda: s.transaction_events)
        t0 = time.monotonic()
        assert await rt.stop_tx(1)
        await _until(lambda: _ended(s))
        assert time.monotonic() - t0 >= 0.9
    _run("2.0.1", {"meter": {"post_tx_delay_s": 1.0}}, body)
