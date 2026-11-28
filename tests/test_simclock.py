"""SimClock (v0.3 M0): segmented sim time on a fake monotonic clock, pause, time_scale changes, sleep_sim,
and the engine keeping its clock in step with run / pause / time_scale."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.simclock import SimClock

ANCHOR = datetime(2026, 12, 1, 7, 55, tzinfo=timezone(timedelta(hours=9)))


class FakeMono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_segments_pause_and_scale_changes():
    mono = FakeMono()
    clock = SimClock(time_scale=60, anchor=ANCHOR, monotonic=mono)
    assert clock.elapsed_sim() == 0 and clock.now_sim() == ANCHOR
    mono.t += 10                                  # 10 wall s x 60 = 600 sim s
    assert clock.elapsed_sim() == 600
    clock.set(time_scale=1)                       # segment closed: no jump
    assert clock.elapsed_sim() == 600 and clock.time_scale == 1
    mono.t += 5
    assert clock.elapsed_sim() == 605
    clock.set(running=False)
    mono.t += 100                                 # paused: sim stands still
    assert clock.elapsed_sim() == 605 and not clock.running
    clock.set(time_scale=10)                      # a change while paused applies after resume
    mono.t += 100
    assert clock.elapsed_sim() == 605
    clock.set(running=True)
    mono.t += 2
    assert clock.elapsed_sim() == 625
    assert clock.now_sim() == ANCHOR + timedelta(seconds=625)
    assert clock.now_sim().utcoffset() == timedelta(hours=9)


def test_set_noop_reset_and_conversions():
    mono = FakeMono()
    clock = SimClock(time_scale=4, anchor=ANCHOR, monotonic=mono)
    mono.t += 3
    clock.set(time_scale=4, running=True)         # unchanged: nothing happens
    clock.set()
    assert clock.elapsed_sim() == 12
    assert clock.to_wall(8) == 2 and clock.to_sim(2) == 8
    later = ANCHOR + timedelta(hours=1)
    clock.reset(later)
    assert clock.elapsed_sim() == 0 and clock.now_sim() == later and clock.time_scale == 4
    clock.set(running=False)
    clock.reset()                                 # default anchor: now (aware, local zone)
    assert clock.anchor.tzinfo is not None and not clock.running


def test_default_anchor_is_aware_local_now():
    before = datetime.now().astimezone()
    clock = SimClock()
    assert clock.anchor.tzinfo is not None and abs((clock.anchor - before).total_seconds()) < 5
    naive = datetime(2026, 12, 1, 8, 0)
    assert SimClock(anchor=naive).anchor.replace(tzinfo=None) == naive


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
def test_time_scale_must_be_positive(bad):
    with pytest.raises(ValueError):
        SimClock(time_scale=bad)
    with pytest.raises(ValueError):
        SimClock().set(time_scale=bad)


def test_sleep_sim_follows_scale_and_pause():
    async def main():
        clock = SimClock(time_scale=1000)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await clock.sleep_sim(50)                 # 50 ms wall
        assert 0.04 <= loop.time() - t0 < 1.0
        await clock.sleep_sim(0)
        await clock.sleep_sim(-5)                 # nothing to wait for

        clock.set(running=False)
        task = asyncio.create_task(clock.sleep_sim(10))
        await asyncio.sleep(0.05)
        assert not task.done()                    # paused: no sim time passes
        clock.set(running=True)                   # wakes the waiter, 10 ms wall left
        await asyncio.wait_for(task, 1.0)

        clock.set(time_scale=1)
        task = asyncio.create_task(clock.sleep_sim(3600))  # one hour at x1 ...
        await asyncio.sleep(0.01)
        clock.set(time_scale=3600 * 100)          # ... then a speed-up applies at once
        await asyncio.wait_for(task, 1.0)

    asyncio.run(main())


def test_engine_keeps_its_clock_in_step():
    engine = OcppEngine(SimConfig.model_validate({"simulation": {"time_scale": 30, "running": False}}))
    assert engine.clock.time_scale == 30 and not engine.clock.running
    engine.set_running(True, 120)
    assert engine.clock.time_scale == 120 and engine.clock.running
    engine.set_running(False)
    assert not engine.clock.running and engine.clock.time_scale == 120

    async def main():
        await engine.reload(SimConfig.model_validate({"simulation": {"time_scale": 5, "running": True}}))
        assert engine.clock.time_scale == 5 and engine.clock.running

        sync, calls = engine._sync_clock, []

        def boom(time_scale, running):  # applies the new clock, then fails the reload once
            sync(time_scale, running)
            calls.append(time_scale)
            if len(calls) == 1:
                raise RuntimeError("boom")
        engine._sync_clock = boom
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await engine.reload(SimConfig.model_validate({"simulation": {"time_scale": 7}}))
        finally:
            del engine._sync_clock
        assert calls == [7, 5]  # the rollback put the old clock back
        assert engine.clock.time_scale == 5 and engine.time_scale == 5

    asyncio.run(main())
