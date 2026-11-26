"""OcppEngine: per-charger diff reload, task registry / stop, throttled push (plan §5 M5)."""

import asyncio
import time
from collections import Counter
from types import SimpleNamespace

import pytest
from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine

FAST = {"min_s": 0.05, "max_s": 0.2, "jitter": 0}


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


def test_reload_add_remove_change_restarts_only_changed():
    async def body(peer, engine):
        q = engine.subscribe()
        old = engine.chargers
        await engine.reload(_config(peer.url, ["A", "B", "D"], B={"vendor": "Other"}))
        await _until(lambda: _connections(peer)["B"] == 2 and _connections(peer)["D"] == 1
                     and all(rt.online for rt in engine.chargers.values()))
        assert list(engine.chargers) == ["A", "B", "D"]
        assert engine.chargers["A"] is old["A"] and engine.chargers["B"] is not old["B"]
        assert set(engine._runs) == {"A", "B", "D"}
        assert not old["C"].connected and not old["B"].connected
        await asyncio.sleep(0.3)
        assert _connections(peer) == {"A": 1, "B": 2, "C": 1, "D": 1}
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        kinds = {m["type"] for m in msgs}
        assert {"reload", "charger_state", "values", "frame"} <= kinds
        pushed = {c["id"] for m in msgs if m["type"] == "charger_state" for c in m["chargers"]}
        assert {"B", "D"} <= pushed
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


def test_reload_before_start_and_time_scale():
    engine = OcppEngine(_config("ws://127.0.0.1:1/ocpp", ["A", "B"]))
    a = engine.chargers["A"]
    asyncio.run(engine.reload(_config("ws://127.0.0.1:1/ocpp", ["A", "C"])))
    assert list(engine.chargers) == ["A", "C"] and engine.chargers["A"] is a and not engine._runs
    engine.set_running(time_scale=10)
    assert all(rt._time_scale == 10 for rt in engine.chargers.values())


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
        assert not engine._runs
    asyncio.run(main())


def test_frame_push_is_throttled():
    async def main():
        engine = OcppEngine(SimConfig.model_validate({"simulation": {"tick_seconds": 5}}))
        engine.push_interval = 0.3
        await engine.start()
        q = engine.subscribe()
        on_frame = engine._sink.callback("X")
        try:
            for i in range(50):
                on_frame(SimpleNamespace(direction="tx", raw="[]", message_type=2, uid=str(i), action="Heartbeat"))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.5)  # trailing push
        finally:
            await engine.stop()
        frames = []
        while not q.empty():
            m = q.get_nowait()
            if m["type"] == "frame":
                frames.append(m)
        assert 2 <= len(frames) <= 6
        seqs = [f["seq"] for m in frames for f in m["frames"]]
        assert seqs == list(range(1, 51))
    asyncio.run(main())
