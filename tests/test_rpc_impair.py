"""ImpairedSocket: seeded delays, order preservation, direction, live settings, drop modes (v0.2.2 A)."""

import asyncio
import time
import zlib

import pytest
from rpc_fakes import FakeWs

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
        s, _ = _sock(ws, latency_ms=30, jitter_ms=29)
        frames = [f"m{i}" for i in range(25)]
        t0 = time.monotonic()
        await asyncio.gather(*(s.send(f) for f in frames))
        assert ws.sent == frames
        assert time.monotonic() - t0 >= 0.001  # delayed at all (the first frame waits >= 1 ms)
    asyncio.run(main())


def test_received_frames_keep_their_order_and_delays_do_not_add_up():
    async def main():
        ws = FakeWs()
        s, _ = _sock(ws, latency_ms=100, jitter_ms=90)
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
        # every frame arrived at t0: they are due within ~0.19 s, not 10 x 0.1 s one after the other
        assert 0.005 <= elapsed < 0.6
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
