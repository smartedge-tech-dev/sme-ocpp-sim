"""ImpairedSocket: seeded delays, order preservation, direction, live settings, drop modes (v0.2.2 A)."""

import asyncio
import time
import zlib

import pytest
from rpc_fakes import FakeWs
from websockets.exceptions import ConnectionClosed

from sme_ocpp_sim.config import ImpairmentConfig
from sme_ocpp_sim.rpc.impair import ImpairedSocket, impairment_seed


def _sock(ws=None, seed=7, **imp):
    cfg = ImpairmentConfig(**imp)
    return ImpairedSocket(ws or FakeWs(), lambda: cfg, seed), cfg


def test_same_seed_same_delays_and_streams_differ():
    a, _ = _sock(latency_ms=100, jitter_ms=50)
    b, _ = _sock(latency_ms=100, jitter_ms=50)
    c, _ = _sock(seed=8, latency_ms=100, jitter_ms=50)
    seq = [a.next_delay("tx") for _ in range(20)]
    assert seq == [b.next_delay("tx") for _ in range(20)]
    assert seq != [c.next_delay("tx") for _ in range(20)]
    assert all(0.05 <= d <= 0.15 for d in seq) and len(set(seq)) > 1
    # rx has its own stream: drawing rx delays does not shift the tx sequence
    d, _ = _sock(latency_ms=100, jitter_ms=50)
    for _ in range(5):
        d.next_delay("rx")
    assert [d.next_delay("tx") for _ in range(20)] == seq


def test_default_seed_is_crc32_of_the_charger_id():
    assert impairment_seed(None, "CP1") == zlib.crc32(b"CP1") == impairment_seed(None, "CP1")
    assert impairment_seed(42, "CP1") == 42


def test_direction_and_live_settings():
    s, cfg = _sock(latency_ms=100, direction="tx")
    assert s.next_delay("tx") == pytest.approx(0.1) and s.next_delay("rx") == 0.0
    cfg.direction = "rx"  # read per frame: a change applies at once
    assert s.next_delay("tx") == 0.0 and s.next_delay("rx") == pytest.approx(0.1)
    cfg.latency_ms = 0
    assert s.next_delay("rx") == 0.0


def test_concurrent_sends_keep_their_order():
    async def main():
        ws = FakeWs()
        s, _ = _sock(ws, latency_ms=60, jitter_ms=50)
        frames = [f"m{i}" for i in range(25)]
        t0 = time.monotonic()
        await asyncio.gather(*(s.send(f) for f in frames))
        assert ws.sent == frames
        assert time.monotonic() - t0 >= 0.04  # the slowest of 25 draws waits well over 10 ms
    asyncio.run(main())


def test_received_frames_keep_their_order_and_delays_do_not_add_up():
    async def main():
        ws = FakeWs()
        s, _ = _sock(ws, latency_ms=100)
        frames = [f"m{i}" for i in range(10)]
        for f in frames:
            ws.feed(f)
        t0 = time.monotonic()
        got = []
        async for raw in s:
            got.append(raw)
            if len(got) == len(frames):
                break
        elapsed = time.monotonic() - t0
        assert got == frames
        # every frame arrived at t0: all are due after ~0.1 s, not 10 x 0.1 s one after the other
        assert 0.08 <= elapsed < 0.7
        await ws.close()
        with pytest.raises(StopAsyncIteration):
            await s.__anext__()
        await s.aclose_pump()
    asyncio.run(main())


def test_no_delay_passes_frames_straight_through_and_attributes():
    async def main():
        ws = FakeWs()
        ws.close_code = 1006
        s, _ = _sock(ws)
        await s.send("a")
        ws.feed("b")
        assert ws.sent == ["a"] and await s.__anext__() == "b" and s.close_code == 1006 and s.inner is ws
        await s.close()
        await s.aclose_pump()
    asyncio.run(main())


class _Transport:
    aborted = False

    def abort(self):
        self.aborted = True


def test_drop_modes():
    async def main():
        ws = FakeWs()
        ws.transport = _Transport()
        s, _ = _sock(ws)
        await s.drop("abort")
        assert ws.transport.aborted and not ws._closed  # no close frame: the TCP connection is cut
        await s.drop("close")
        assert ws._closed
    asyncio.run(main())


def _held_send_and_read(s, ws):
    ws.feed("in1")
    ws.feed("in2")
    return asyncio.ensure_future(s.send("out")), asyncio.ensure_future(s.__anext__())


@pytest.mark.parametrize("how", ["close", "abort", "drop-close"])
def test_close_and_drop_release_held_frames_at_once(how):
    async def main():
        ws = FakeWs()
        ws.transport = _Transport()
        s, _ = _sock(ws, latency_ms=30_000)
        tx, rx = _held_send_and_read(s, ws)
        await asyncio.sleep(0.2)
        assert not tx.done() and not rx.done()
        t0 = time.monotonic()
        if how == "close":
            await s.close()
        else:
            await s.drop("abort" if how == "abort" else "close")
        with pytest.raises(ConnectionClosed):
            await asyncio.wait_for(tx, 2)  # a frame still held is lost, as on a real network
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(rx, 2)
        with pytest.raises(StopAsyncIteration):  # the queued in2 is discarded
            await asyncio.wait_for(s.__anext__(), 2)
        assert time.monotonic() - t0 < 1.5 and ws.sent == []
        await s.aclose_pump()
    asyncio.run(main())


def test_lowering_the_latency_live_releases_held_frames():
    async def main():
        ws = FakeWs()
        s, cfg = _sock(ws, latency_ms=30_000)
        tx, rx = _held_send_and_read(s, ws)
        await asyncio.sleep(0.2)
        assert not tx.done() and not rx.done()
        cfg.latency_ms = 0  # switched off live
        await asyncio.wait_for(tx, 2)
        assert await asyncio.wait_for(rx, 2) == "in1" and ws.sent == ["out"]
        cfg.latency_ms = 300  # and up again: in2 arrived > 0.2 s ago, waits the rest only
        t0 = time.monotonic()
        assert await asyncio.wait_for(s.__anext__(), 2) == "in2"
        assert time.monotonic() - t0 < 0.5
        await s.close()
        await s.aclose_pump()
    asyncio.run(main())


def test_receive_queue_is_bounded():
    async def main():
        ws = FakeWs()
        cfg = ImpairmentConfig(latency_ms=30_000)
        s = ImpairedSocket(ws, lambda: cfg, 1, rx_queue_max=2)
        for i in range(6):
            ws.feed(f"m{i}")
        rx = asyncio.ensure_future(s.__anext__())
        deadline = time.monotonic() + 3
        while ws._inbox.qsize() > 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        # 1 held by the reader, 2 queued, 1 in the pump's pending put: the rest stays unread
        assert ws._inbox.qsize() == 2 and not rx.done()
        await s.close()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(rx, 2)
        await s.aclose_pump()
    asyncio.run(main())


class _HangingCloseWs(FakeWs):
    async def close(self, code=1000, reason=""):
        await asyncio.sleep(30)


def test_close_mode_drop_falls_back_to_abort_when_the_close_hangs():
    async def main():
        ws = _HangingCloseWs()
        ws.transport = _Transport()
        s, _ = _sock(ws)
        t0 = time.monotonic()
        await s.drop("close")
        assert ws.transport.aborted and 0.8 <= time.monotonic() - t0 < 2.5
    asyncio.run(main())