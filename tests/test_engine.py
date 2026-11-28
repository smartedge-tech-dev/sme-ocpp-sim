"""OcppEngine: per-charger diff reload, task registry / stop, pause, throttled push and resync (plan §5 M5)."""

import asyncio
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine

FAST = {"min_s": 0.05, "max_s": 0.2, "jitter": 0}
CHARGE = {"auth_mode": "accept_all"}


def _config(url, ids, **charger_kw):
    return SimConfig.model_validate({
        "csms_defaults": {"url": url, "reconnect": FAST},
        "chargers": [{"id": cid, **charger_kw.get(cid, {})} for cid in ids],
    })


async def _until(cond, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


def _connections(peer):
    return Counter(s.cp_id for s in peer.history)


def _drain(q, into):
    while not q.empty():
        into.append(q.get_nowait())
    return into


def _conn1(rt):
    return next(c for c in rt.snapshot()["connectors"] if c["id"] == 1)


async def _charge(rt):
    await rt.plug(1)
    await rt.swipe(1, "TAG")
    await _until(lambda: rt.station.conns[1].tx is not None and rt.station.conns[1].tx.tx_id is not None)


def _run(body, ids=("A", "B", "C"), **charger_kw):
    async def main():
        peer = CsmsPeer(hb_interval=30)
        await peer.start()
        engine = OcppEngine(_config(peer.url, ids, **charger_kw))
        engine.push_interval = 0.05
        await engine.start()
        try:
            await _until(lambda: all(rt.online for rt in engine.chargers.values()))
            await body(peer, engine)
        finally:
            await engine.stop()
            await peer.stop()
    asyncio.run(main())


def _slow_halt(engine, delay):
    """Make the next non-empty ``_halt`` wait ``delay`` seconds after stopping its chargers."""
    halt, state = engine._halt, {"armed": True}

    async def slow(cids):
        await halt(cids)
        if cids and state["armed"]:
            state["armed"] = False
            await asyncio.sleep(delay)
    engine._halt = slow


def test_reload_add_remove_change_restarts_only_changed():
    async def body(peer, engine):
        q = engine.subscribe()
        old = engine.chargers
        await engine.reload(_config(peer.url, ["A", "B", "D"], B={"vendor": "Other"}))
        await _until(lambda: _connections(peer) == {"A": 1, "B": 2, "C": 1, "D": 1}
                     and all(rt.online for rt in engine.chargers.values()))
        assert list(engine.chargers) == ["A", "B", "D"]
        assert engine.chargers["A"] is old["A"] and engine.chargers["B"] is not old["B"]
        assert set(engine._runs) == {"A", "B", "D"}
        assert not old["C"].connected and not old["B"].connected
        msgs = []

        def seen():
            _drain(q, msgs)
            pushed = {c["id"] for m in msgs if m["type"] == "charger_state" for c in m["chargers"]}
            return {"reload", "charger_state", "values", "frame"} <= {m["type"] for m in msgs} and {"B", "D"} <= pushed
        await _until(seen)
    _run(body)


def test_change_that_fails_rolls_back():
    async def body(peer, engine):
        old_config, old = engine.config, engine.chargers
        launch = engine._launch

        def failing(cid, rt):
            if cid == "B" and rt is not old["B"]:
                raise RuntimeError("boom")
            launch(cid, rt)
        engine._launch = failing
        with pytest.raises(RuntimeError, match="boom"):
            await engine.reload(_config(peer.url, ["A", "B", "C"], B={"vendor": "Other"}))
        assert engine.config is old_config and engine.chargers == old
        await _until(lambda: _connections(peer)["B"] == 2 and old["B"].online)
        assert _connections(peer)["A"] == 1 and _connections(peer)["C"] == 1
        assert engine._runs["B"].runtime is old["B"]
    _run(body)


def test_rolled_back_charger_keeps_its_transaction():
    async def body(peer, engine):
        old = engine.chargers
        await _charge(old["B"])
        tx = old["B"].station.conns[1].tx
        launch = engine._launch

        def failing(cid, rt):
            if cid == "B" and rt is not old["B"]:
                raise RuntimeError("boom")
            launch(cid, rt)
        engine._launch = failing
        with pytest.raises(RuntimeError, match="boom"):
            await engine.reload(_config(peer.url, ["A", "B", "C"], B={"vendor": "Other", **CHARGE}))
        await _until(lambda: _connections(peer)["B"] == 2 and old["B"].online)
        assert engine._runs["B"].runtime is old["B"]
        assert old["B"].station.conns[1].tx is tx and tx.tx_id is not None and tx.ended is None
    _run(body, B=CHARGE)


def test_cancelled_reload_rolls_back():
    async def body(peer, engine):
        old_config, old = engine.config, engine.chargers
        _slow_halt(engine, 30)
        task = asyncio.create_task(engine.reload(_config(peer.url, ["A", "C"])))
        await _until(lambda: "B" not in engine._runs)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.config is old_config and engine.chargers == old
        assert {cid: r.runtime for cid, r in engine._runs.items()} == old
        await _until(lambda: _connections(peer)["B"] == 2 and old["B"].online)
    _run(body)


def test_reload_resuming_after_stop_launches_nothing():
    async def body(peer, engine):
        _slow_halt(engine, 0.3)
        task = asyncio.create_task(engine.reload(_config(peer.url, ["A", "B", "D"], B={"vendor": "Other"})))
        await _until(lambda: "B" not in engine._runs)
        await engine.stop()
        await task
        assert not engine._runs and list(engine.chargers) == ["A", "B", "D"]
        assert _connections(peer)["D"] == 0
    _run(body)


def test_reload_before_start_time_scale_and_pause():
    engine = OcppEngine(_config("ws://127.0.0.1:1/ocpp", ["A", "B"]))
    a = engine.chargers["A"]
    asyncio.run(engine.reload(_config("ws://127.0.0.1:1/ocpp", ["A", "C"])))
    assert list(engine.chargers) == ["A", "C"] and engine.chargers["A"] is a and not engine._runs
    engine.set_running(time_scale=10)
    assert all(rt._time_scale == 10 for rt in engine.chargers.values())
    engine.set_running(running=False)
    assert all(rt.paused for rt in engine.chargers.values())
    cfg = _config("ws://127.0.0.1:1/ocpp", ["A", "C", "D"])
    cfg.simulation.running, cfg.simulation.time_scale = False, 10
    asyncio.run(engine.reload(cfg))
    assert engine.chargers["D"].paused and engine.chargers["D"]._time_scale == 10


def test_pause_freezes_power_model_without_catch_up():
    async def body(peer, engine):
        rt = engine.chargers["A"]
        engine.set_running(time_scale=600)  # 6 kW x 600: about 1 kWh per wall-clock second
        await _charge(rt)
        await _until(lambda: _conn1(rt)["meter_wh"] > 0)
        engine.set_running(running=False)
        assert rt.paused
        frozen = _conn1(rt)
        await asyncio.sleep(0.3)
        rt.station.settle()
        now = _conn1(rt)
        assert {k: now[k] for k in ("power_w", "meter_wh", "soc_pct")} == \
            {k: frozen[k] for k in ("power_w", "meter_wh", "soc_pct")}
        assert rt.online  # the OCPP session keeps running
        engine.set_running(running=True)
        rt.station.settle()
        assert _conn1(rt)["meter_wh"] - frozen["meter_wh"] < 100  # the paused 0.3 s (~300 Wh) is not caught up
        await _until(lambda: _conn1(rt)["meter_wh"] > frozen["meter_wh"] + 50)
    _run(body, ids=("A",), A=CHARGE)


def test_lost_state_push_triggers_full_resync():
    async def body(peer, engine):
        engine.queue_size = 4
        q = engine.subscribe()
        msgs = []
        await _until(lambda: any(m.get("full") for m in _drain(q, msgs)))  # a new subscriber starts full
        engine.publish({"type": "values", "values": {}})
        for i in range(10):  # overflow: the values delta above is dropped
            engine.publish({"type": "junk", "i": i})
        last = max(m["push_seq"] for m in msgs if "push_seq" in m)
        _drain(q, [])
        engine.wake()
        later = []
        await _until(lambda: any(m["type"] == "charger_state" and m["full"] for m in _drain(q, later)))
        full = next(m for m in later if m["type"] == "charger_state" and m["full"])
        assert {c["id"] for c in full["chargers"]} == {"A", "B", "C"} and full["push_seq"] > last
        seqs = [m["push_seq"] for m in msgs + later if "push_seq" in m]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    _run(body)


def test_crashed_charger_task_is_reported_and_start_is_idempotent():
    async def main():
        engine = OcppEngine(_config("ws://127.0.0.1:1/ocpp", ["A"]))

        async def crash(stop):
            raise RuntimeError("kaputt")
        engine.chargers["A"].run = crash
        await engine.start()
        task = engine._runs["A"].task
        await engine.start()
        try:
            assert engine._runs["A"].task is task
            await _until(lambda: any("kaputt" in e["message"] for e in engine.events))
            assert next(e for e in engine.events if "kaputt" in e["message"])["level"] == "error"
        finally:
            await engine.stop()
    asyncio.run(main())


def test_stop_ends_backoff_promptly():
    async def main():
        peer = CsmsPeer()
        await peer.start()
        url = peer.url
        await peer.stop()  # nothing listens any more: every attempt fails, then backs off
        cfg = SimConfig.model_validate({"csms_defaults": {"url": url, "reconnect": {"min_s": 30, "max_s": 60}},
                                        "chargers": [{"id": "A"}, {"id": "B"}]})
        engine = OcppEngine(cfg)
        await engine.start()
        await _until(lambda: all(rt.last_error for rt in engine.chargers.values()))
        t0 = time.monotonic()
        await engine.stop()
        assert time.monotonic() - t0 < 1.0
        assert not engine._runs and engine._push_later is None
    asyncio.run(main())


def test_frame_push_is_throttled():
    async def main():
        engine = OcppEngine(SimConfig.model_validate({"simulation": {"tick_seconds": 5}}))
        engine.push_interval = 0.3
        await engine.start()
        q = engine.subscribe()
        on_frame = engine._sink.callback("X")
        msgs = []
        try:
            t0 = time.monotonic()
            for i in range(50):
                on_frame(SimpleNamespace(direction="tx", raw="[]", message_type=2, uid=str(i), action="Heartbeat"))
                await asyncio.sleep(0.02)
            # the trailing push delivers the rest
            await _until(lambda: any(m["type"] == "frame" and m["last_seq"] == 50 for m in _drain(q, msgs)))
            elapsed = time.monotonic() - t0
        finally:
            await engine.stop()
        frames = [m for m in msgs if m["type"] == "frame"]
        assert 2 <= len(frames) <= elapsed / engine.push_interval + 2
        seqs = [f["seq"] for m in frames for f in m["frames"]]
        assert seqs == list(range(1, 51))
    asyncio.run(main())


def test_gave_up_charger_can_be_connected_again():
    async def main():
        peer = CsmsPeer(hb_interval=30, boot_statuses=["Rejected"] * 2, retry_interval=1)
        await peer.start()
        engine = OcppEngine(_config(peer.url, ["A"], A={"boot_max_retries": 0}))
        engine.push_interval = 0.05
        await engine.start()
        try:
            rt = engine.chargers["A"]
            await _until(lambda: rt.gave_up is not None and "A" not in engine._runs)
            assert engine.charger_detail("A")["gave_up"]["reason"] == "boot_max_retries"
            ev = next(e for e in engine.events if "gave up" in e["message"])
            assert ev["level"] == "warning" and "1 boots" in ev["message"]
            assert not any("ended unexpectedly" in e["message"] for e in engine.events)
            # Held like an operator disconnect: the UI enables Connect, and neither a reload nor the rollback
            # of a failed reload relaunches it.
            assert engine.charger_detail("A")["enabled"] is False
            launch, launched = engine._launch, []

            def failing(cid, rt_):
                launched.append(cid)
                if cid == "B":
                    raise RuntimeError("boom")
                launch(cid, rt_)
            engine._launch = failing
            with pytest.raises(RuntimeError, match="boom"):
                await engine.reload(_config(peer.url, ["A", "B"], A={"boot_max_retries": 0}))
            engine._launch = launch
            assert "A" not in launched and "A" not in engine._runs and engine.chargers["A"] is rt
            # The second boot is Rejected again and gives up again; the third run is Accepted.
            assert await engine.connect("A")
            await _until(lambda: rt.gave_up is not None and "A" not in engine._runs and rt.boots == 2)
            assert await engine.connect("A")
            await _until(lambda: rt.online)
            assert rt.gave_up is None and "A" in engine._runs
            assert engine.charger_detail("A")["enabled"] is True
        finally:
            await engine.stop()
            await peer.stop()
    asyncio.run(main())


def test_disconnect_after_give_up_clears_gave_up():
    async def main():
        peer = CsmsPeer(hb_interval=30, boot_statuses=["Rejected"], retry_interval=1)
        await peer.start()
        engine = OcppEngine(_config(peer.url, ["A"], A={"boot_max_retries": 0}))
        await engine.start()
        try:
            rt = engine.chargers["A"]
            await _until(lambda: rt.gave_up is not None and "A" not in engine._runs)
            assert not await engine.disconnect("A")
            assert rt.gave_up is None and engine.charger_detail("A")["enabled"] is False
        finally:
            await engine.stop()
            await peer.stop()
    asyncio.run(main())


def test_impairments_only_reload_applies_live_without_restart():
    async def body(peer, engine):
        old = engine.chargers
        block = {"A": {"impairments": {"block": {"Reset": "NoReply", "RemoteStartTransaction": "NotSupported"}}}}
        await engine.reload(_config(peer.url, ["A", "B", "C"], **block))
        assert engine.chargers == old and _connections(peer) == {"A": 1, "B": 1, "C": 1}
        assert old["A"]._block_mode("Reset") == "NoReply" and old["A"].impaired and not old["B"].impaired
        assert old["A"].snapshot()["impaired"] is True
        # an action the version does not receive fails the reload; the running block stays
        with pytest.raises(ValueError, match="NoSuchAction"):
            await engine.reload(_config(peer.url, ["A", "B", "C"],
                                        A={"impairments": {"block": {"NoSuchAction": "NoReply"}}}))
        assert old["A"]._block_mode("Reset") == "NoReply"
        # a failure after the impairments step rolls them back with the rest: _apply_clock (the step after
        # _apply_impairments) raises once, then _undo's own _apply_clock / _apply_impairments run normally
        clock, calls = engine._apply_clock, []

        def failing_clock(runtimes, time_scale, running):
            calls.append(old["A"]._block_mode("Reset"))
            if len(calls) == 1:
                raise RuntimeError("boom")
            clock(runtimes, time_scale, running)
        engine._apply_clock = failing_clock
        with pytest.raises(RuntimeError, match="boom"):
            await engine.reload(_config(peer.url, ["A", "B", "C"],
                                        A={"impairments": {"block": {"Reset": "NotSupported"}}}))
        engine._apply_clock = clock
        assert calls[0] == "NotSupported"  # the new block was live when the failure hit
        assert old["A"]._block_mode("Reset") == "NoReply" and engine.chargers == old
        await engine.reload(_config(peer.url, ["A", "B", "C"]))
        assert not old["A"].impaired and engine.chargers == old
        assert _connections(peer)["A"] == 1
    _run(body)
