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


async def _never(cond, timeout):
    """Poll ``cond`` for ``timeout`` s; fail as soon as it holds (for things that must not happen)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        assert not cond()
        await asyncio.sleep(0.02)


def _faults(rt):
    return rt.station._faults(rt.station.conns[1] if hasattr(rt.station, "conns") else rt.station.evses[1])


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
        n0 = _status_count(s)
        await rt.plug(1)
        await _until(lambda: _status_count(s) > n0)  # the plug is reported
        n, status = _status_count(s), rt.snapshot()["connectors"][0]["status"]
        rt.set_impairments(ImpairmentConfig(remote_start="accept_no_tx"))
        res = await _remote_start(peer, version)
        assert res.status == "Accepted"
        await _never(lambda: rt.station.has_transaction(1) or _status_count(s) != n, 1.0)
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
        await _until(lambda: _faults(rt).dropped >= 2)
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
        await _until(lambda: len(_faults(rt).held) >= 3)
        assert not _periodic(s, version)  # held: the window is not full yet
        held = len(_faults(rt).held)
        await rt.stop_tx(1)
        stop_action = '"StopTransaction"' if version == "1.6" else '"Ended"'
        meter_action = '"MeterValues"' if version == "1.6" else '"MeterValuePeriodic"'

        def order():
            frames = [raw for d, raw in s.frames if d == "rx"]
            i_stop = next((i for i, raw in enumerate(frames) if stop_action in raw), None)
            return i_stop, [i for i, raw in enumerate(frames) if meter_action in raw]
        await _until(lambda: order()[0] is not None and order()[1] and order()[1][-1] > order()[0])
        i_stop, meters = order()
        assert len(meters) >= held + 1
        assert meters[-1] > i_stop and all(i < i_stop for i in meters[:-1])  # held ones first, the last one late
        if version == "2.0.1":
            # the peer records the raw frame before its handler parses it: wait for the parsed late event too
            await _until(lambda: len(_periodic(s, version)) >= len(meters))
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


# ---------------------------------------------------------------- review follow-ups


def test_streams_persist_across_live_edits_and_restart_on_a_new_seed():
    def draws(edits):
        cache, host = {}, type("H", (), {})()
        host.impairments = _imp(drop_ratio=0.5)
        out = []
        for i in range(30):
            if i in edits:
                host.impairments = edits[i]
            out.append(meter_faults(cache, host, CP, 1).drop())
        return out
    base = draws({})
    assert draws({10: _imp(drop_ratio=0.5, voltage_noise_v=2)}) == base  # another field: the stream goes on
    reseeded = draws({10: ImpairmentConfig(seed=8, meter=MeterImpairment(drop_ratio=0.5))})
    assert reseeded[:10] == base[:10] and reseeded[10:] != base[10:]


def test_mcp_inlining_keeps_sibling_keys_and_rejects_recursion():
    from pydantic import BaseModel, Field

    class Inner(BaseModel):
        x: int = 1

    class Outer(BaseModel):
        inner: Inner = Field(default_factory=Inner, description="the inner block")

    props = _model_props(Outer)["inner"]
    assert props["description"] == "the inner block" and props["properties"]["x"]["type"] == "integer"

    class Node(BaseModel):
        child: "Node | None" = None

    class Tree(BaseModel):
        root: Node = Node()
    with pytest.raises(ValueError, match="recursive"):
        _model_props(Tree)


@VERSIONS
def test_shrinking_the_window_releases_held_messages_at_once(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        rt.set_impairments(_imp(reorder_window=50))
        await rt.plug(1)
        await rt.swipe(1, "T")
        await _until(lambda: len(_faults(rt).held) >= 2)
        held = len(_faults(rt).held)
        rt.set_impairments(_imp())
        assert not _faults(rt).held
        await _until(lambda: len(_periodic(s, version)) >= held)
        assert rt.station.has_transaction(1)
    _scenario(body, version=version, cfg_kw=TICK)


@VERSIONS
def test_a_delayed_answer_is_not_sent_after_a_disconnect(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        rt.set_impairments(ImpairmentConfig(response_delay_ms={"DataTransfer": 600}))
        rpc = rt._rpc
        req = asyncio.create_task(peer.data_transfer(CP, "acme"))
        await _until(lambda: rpc._tasks)  # the handler waits for its delay
        rt.drop(reconnect_after_s=0.1)
        await _until(lambda: rpc.closed.is_set() and not rpc._tasks)  # cancelled with the session
        await _until(lambda: rt.online and peer.sessions[CP] is not s)
        # Past the delay: the DataTransfer answer never reached the CSMS, on the old session or the new one.
        await _never(lambda: any(d == "rx" and raw.startswith("[3") and '"status"' in raw
                                 for x in (s, peer.sessions[CP]) for d, raw in x.frames), 0.8)
        req.cancel()
        await asyncio.gather(req, return_exceptions=True)
    _scenario(body, version=version)


@VERSIONS
def test_a_delay_longer_than_the_csms_timeout(version):
    async def body(peer, rt):
        await _until(lambda: rt.online and CP in peer.sessions)
        s = peer.sessions[CP]
        cp = s.cp if version == "1.6" else s.cs
        cp._response_timeout = 0.3
        rt.set_impairments(ImpairmentConfig(response_delay_ms={"DataTransfer": 700}))
        with pytest.raises(asyncio.TimeoutError):
            await peer.data_transfer(CP, "acme")
        # The late answer still arrives (the CSMS drops it); the charger keeps its connection.
        await _until(lambda: any(d == "rx" and raw.startswith("[3") and '"status"' in raw for d, raw in s.frames))
        assert rt.online and peer.sessions[CP] is s
    _scenario(body, version=version)
