"""Simulation clock (v0.3 plan §3.1).

:class:`SimClock` is the time base of scenarios and the traffic generator: ``sim = anchor + sum(wall segment x
time_scale)``. It stops while paused, and a ``time_scale`` change closes the current segment and starts a new one,
so sim time never jumps. The engine owns one (``OcppEngine.clock``) and keeps it in step with its run / pause and
``time_scale`` (``set_running``, start, reload and its rollback).

What it is not: OCPP wire timestamps and sampling intervals stay wall clock (a CSMS is never shown the future);
the power model of each charger keeps its own ``time_scale`` / pause handling (``ChargerRuntime``). The clock only
times what is scheduled *in sim time* (scenario steps, generated arrivals and durations).

``monotonic`` is injectable (a fake clock in tests). :meth:`sleep_sim` waits on the event loop and wakes up early
on every :meth:`set`, so a pause or a speed change during the wait is honoured at once.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from typing import Callable, Optional


class SimClock:
    def __init__(self, time_scale: float = 1.0, running: bool = True, anchor: Optional[datetime] = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._mono = monotonic
        self._scale = self._check_scale(time_scale)
        self._running = bool(running)
        self._acc = 0.0                     # sim seconds of the closed segments
        self._seg_start = self._mono()      # wall start of the open segment (only counts while running)
        self._anchor = self._aware(anchor)
        self._changed = asyncio.Event()

    # ------------------------------------------------------------ state

    @staticmethod
    def _check_scale(time_scale: float) -> float:
        ts = float(time_scale)
        if not (ts > 0 and math.isfinite(ts)):
            raise ValueError("time_scale must be > 0")
        return ts

    @staticmethod
    def _aware(anchor: Optional[datetime]) -> datetime:
        """The anchor as an aware datetime (none = now, local wall time; a naive one = local time zone)."""
        return (anchor or datetime.now()).astimezone()

    @property
    def time_scale(self) -> float:
        return self._scale

    @property
    def running(self) -> bool:
        return self._running

    @property
    def anchor(self) -> datetime:
        return self._anchor

    def _close_segment(self) -> None:
        now = self._mono()
        if self._running:
            self._acc += (now - self._seg_start) * self._scale
        self._seg_start = now

    def set(self, time_scale: Optional[float] = None, running: Optional[bool] = None) -> None:
        """Change speed and / or run state from now on (``None`` = unchanged); the time so far is kept."""
        scale = self._scale if time_scale is None else self._check_scale(time_scale)
        run = self._running if running is None else bool(running)
        if scale == self._scale and run == self._running:
            return
        self._close_segment()
        self._scale, self._running = scale, run
        self._wake()

    def reset(self, anchor: Optional[datetime] = None) -> None:
        """Restart at ``anchor`` (none = now): sim elapsed is 0 again, speed and run state are kept."""
        self._anchor = self._aware(anchor)
        self._acc = 0.0
        self._seg_start = self._mono()
        self._wake()

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()  # current waiters hold the set one; later waits get a fresh one

    # ------------------------------------------------------------ reading

    def elapsed_sim(self) -> float:
        """Sim seconds since the anchor."""
        if not self._running:
            return self._acc
        return self._acc + (self._mono() - self._seg_start) * self._scale

    def now_sim(self) -> datetime:
        """The current sim time (aware, in the anchor's offset)."""
        return self._anchor + timedelta(seconds=self.elapsed_sim())

    def to_wall(self, dt_sim: float) -> float:
        """Wall seconds that ``dt_sim`` sim seconds take at the current speed (ignores a pause)."""
        return float(dt_sim) / self._scale

    def to_sim(self, dt_wall: float) -> float:
        """Sim seconds that pass in ``dt_wall`` wall seconds at the current speed (ignores a pause)."""
        return float(dt_wall) * self._scale

    # ------------------------------------------------------------ waiting

    async def sleep_sim(self, dt: float) -> None:
        """Wait until ``dt`` sim seconds have passed (none while paused; a speed change applies at once)."""
        target = self.elapsed_sim() + max(0.0, float(dt))
        while True:
            left = target - self.elapsed_sim()
            if left <= 0:
                return
            changed = self._changed
            timeout = self.to_wall(left) if self._running else None
            try:
                await asyncio.wait_for(changed.wait(), timeout)
            except asyncio.TimeoutError:
                pass
