"""v0.3 M2: the remaining anomaly switches of ``impairments`` (remote_start, meter, response_delay_ms), both versions."""

import asyncio
import time

import pytest
from ocpp.v16 import call as call16
from ocpp.v201 import call as call201
from pydantic import ValidationError
from test_runtime_charger import CP, VERSIONS, _scenario, _until

from sme_ocpp_sim.config import ImpairmentConfig, MeterImpairment
from sme_ocpp_sim.mcp_tools import _model_props
from sme_ocpp_sim.protocol.base import Sample
from sme_ocpp_sim.protocol.meter_faults import MeterFaults, meter_faults

TICK = {"meter_sample_interval_s": 1, "auth_mode": "accept_all"}


def _imp(**meter):
    return ImpairmentConfig(seed=7, meter=MeterImpairment(**meter))


# ---------------------------------------------------------------- config / schema / helper


def test_config_ranges_and_active():
    assert not ImpairmentConfig().active
    assert ImpairmentConfig(remote_start="accept_no_tx").active
    assert ImpairmentConfig(response_delay_ms={"Reset": 100}).active
    assert not ImpairmentConfig(response_delay_ms={"Reset": 0}).active
    assert not _imp(reorder_window=1).active and _imp(reorder_window=2).active
    assert _imp(drop_ratio=0.1).active and _imp(end_after_stop=True).active and _imp(voltage_noise_v=2).active
    for bad in ({"meter": {"drop_ratio": 1.5}}, {"meter": {"reorder_window": -1}}, {"remote_start": "x"},
                {"response_delay_ms": {"Reset": -1}}, {"meter": {"unknown": 1}}):
        with pytest.raises(ValidationError):
            ImpairmentConfig.model_validate(bad)


def test_mcp_schema_inlines_the_meter_block():
    props = _model_props(ImpairmentConfig)
    assert "$ref" not in str(props)
    assert set(props["meter"]["properties"]) == {"drop_ratio", "reorder_window", "end_after_stop", "voltage_noise_v"}
    assert props["remote_start"]["enum"] == ["normal", "accept_no_tx"]


def test_drop_pattern_is_fixed_by_the_seed_and_per_connector():
    def pattern(imp, cid):
        f = MeterFaults(imp, CP, cid)
        return [f.drop() for _ in range(40)]
    imp = _imp(drop_ratio=0.3)
    assert pattern(imp, 1) == pattern(imp, 1)
    assert pattern(imp, 1) != pattern(imp, 2)
    assert 4 <= sum(pattern(imp, 1)) <= 20
    assert not any(pattern(_imp(), 1))
    assert all(pattern(_imp(drop_ratio=1), 1))


def test_reorder_holds_a_window_and_flushes_shuffled():
    f = MeterFaults(_imp(reorder_window=4), CP, 1)
    assert [f.hold(i) for i in range(3)] == [[], [], []]
    out = f.hold(3)
    assert sorted(out) == [0, 1, 2, 3] and out != [0, 1, 2, 3]  # seed 7 shuffles this window
    assert f.hold(4) == [] and f.flush() == [4] and f.flush() == []
    off = MeterFaults(_imp(), CP, 1)
    assert off.hold("a") == ["a"]


def test_changed_settings_keep_the_held_messages():
    cache, host = {}, type("H", (), {"impairments": _imp(reorder_window=5)})()
    meter_faults(cache, host, CP, 1).hold("a")
    host.impairments = _imp()  # switched off: the next message releases the held one too
    assert sorted(meter_faults(cache, host, CP, 1).hold("b")) == ["a", "b"]


def test_voltage_noise_is_bounded_and_only_on_voltage():
    f = MeterFaults(_imp(voltage_noise_v=3), CP, 1)
    vs = [f.noisy([Sample("Voltage", 230.0, "V"), Sample("Energy.Active.Import.Register", 5.0)]) for _ in range(50)]
    assert all(227 <= v[0].value <= 233 and v[1].value == 5.0 for v in vs)
    assert len({v[0].value for v in vs}) > 10


# ---------------------------------------------------------------- remote_start: accept_no_tx


def _remote_start(peer, version, connector=1):
    if version == "1.6":
        return peer.request(CP, call16.RemoteStartTransaction(id_tag="T", connector_id=connector))
    return peer.request(CP, call201.RequestStartTransaction(id_token={"id_token": "T", "type": "Central"},
                                                            remote_start_id=1, evse_id=connector))


def _status_count(s):
    return len(s.statuses) if hasattr(s, "statuses") else 0


@VERSIONS
def test_accept_no_tx_answers_accepted_and_starts_nothing(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        await rt.plug(1)
        await asyncio.sleep(0.3)
        n, status = _status_count(s), rt.snapshot()["connectors"][0]["status"]
        rt.set_impairments(ImpairmentConfig(remote_start="accept_no_tx"))
        res = await _remote_start(peer, version)
        assert res.status == "Accepted"
        await asyncio.sleep(1.0)
        assert not rt.station.has_transaction(1)
        assert rt.snapshot()["connectors"][0]["status"] == status and _status_count(s) == n
        if version == "1.6":
            assert not s.starts
        else:
            assert not s.transaction_events
        rt.set_impairments(ImpairmentConfig())  # live switch off: the next request starts a transaction
        assert (await _remote_start(peer, version)).status == "Accepted"
        await _until(lambda: rt.station.has_transaction(1))
    _scenario(body, version=version, cfg_kw={"auth_mode": "accept_all"})


# ---------------------------------------------------------------- meter: drop / reorder / end_after_stop


def _periodic(s, version):
    if version == "1.6":
        return [m for m in s.meter_values if m["transaction_id"] is not None]
    return [e for e in s.transaction_events if e["trigger_reason"] == "MeterValuePeriodic"]


@VERSIONS
def test_drop_all_meter_values_leaves_a_seq_no_gap(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        rt.set_impairments(_imp(drop_ratio=1))
        await rt.plug(1)
        await rt.swipe(1, "T")
        await asyncio.sleep(2.6)
        await rt.stop_tx(1)
        if version == "1.6":
            await _until(lambda: s.stops)
            assert not _periodic(s, version)
        else:
            await _until(lambda: any(e["event_type"] == "Ended" for e in s.transaction_events))
            assert not _periodic(s, version)
            seqs = [e["seq_no"] for e in s.transaction_events]
            assert seqs[0] == 0 and seqs[-1] - len(seqs) + 1 >= 2  # at least two dropped Updated took a seqNo
    _scenario(body, version=version, cfg_kw=TICK)


@VERSIONS
def test_reorder_window_and_end_after_stop(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        rt.set_impairments(_imp(reorder_window=10, end_after_stop=True))
        await rt.plug(1)
        await rt.swipe(1, "T")
        await asyncio.sleep(3.5)
        assert not _periodic(s, version)  # held: the window is not full yet
        await rt.stop_tx(1)
        await _until(lambda: len(_periodic(s, version)) >= 4)
        await asyncio.sleep(0.3)
        frames = [(d, raw) for d, raw in s.frames if d == "rx"]
        stop_action = '"StopTransaction"' if version == "1.6" else '"Ended"'
        i_stop = next(i for i, (_, raw) in enumerate(frames) if stop_action in raw)
        meter_action = '"MeterValues"' if version == "1.6" else '"MeterValuePeriodic"'
        meters = [i for i, (_, raw) in enumerate(frames) if meter_action in raw]
        assert meters[-1] > i_stop and all(i < i_stop for i in meters[:-1])  # held ones first, the last one late
        if version == "2.0.1":
            ended = next(e for e in s.transaction_events if e["event_type"] == "Ended")
            assert _periodic(s, version)[-1]["seq_no"] == ended["seq_no"] + 1
            seqs = [e["seq_no"] for e in s.transaction_events if e["trigger_reason"] == "MeterValuePeriodic"][:-1]
            assert sorted(seqs) == list(range(min(seqs), min(seqs) + len(seqs)))  # no gap, maybe out of order
    _scenario(body, version=version, cfg_kw=TICK)


@VERSIONS
def test_voltage_noise_on_station_samples(version):
    async def body(peer, rt):
        await _until(lambda: rt.online)
        conn = rt.station.conns[1] if version == "1.6" else rt.station.evses[1]
        plain = rt.station._samples(conn, ["Voltage"])[0].value
        rt.set_impairments(_imp(voltage_noise_v=5))
        values = {rt.station._samples(conn, ["Voltage"])[0].value for _ in range(20)}
        assert len(values) > 5 and all(abs(v - plain) <= 5 for v in values)
    _scenario(body, version=version)


# ---------------------------------------------------------------- response_delay_ms


@VERSIONS
def test_response_delay_only_for_the_named_action(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        attempts = rt.connect_attempts
        rt.set_impairments(ImpairmentConfig(response_delay_ms={"DataTransfer": 400}))
        t0 = time.monotonic()
        await peer.data_transfer(CP, "acme")
        assert time.monotonic() - t0 >= 0.35
        rt.set_impairments(ImpairmentConfig(response_delay_ms={"Reset": 400}))
        t0 = time.monotonic()
        await peer.data_transfer(CP, "acme")
        assert time.monotonic() - t0 < 0.3
        assert rt.connect_attempts == attempts and rt.online
        with pytest.raises(ValueError, match="response_delay_ms"):
            rt.set_impairments(ImpairmentConfig(response_delay_ms={"NoSuchAction": 1}))
    _scenario(body, version=version)
