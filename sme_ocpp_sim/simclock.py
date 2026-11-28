"""Simulation clock (v0.3 plan §3.1).

:class:`SimClock` is the time base of scenarios and the traffic generator: ``sim = anchor + sum(wall segment x
time_scale)``. It stops while paused, and a ``time_scale`` change closes the current segment and starts a new one,
so sim time never jumps. The engine owns one (``OcppEngine.clock``), restarts it when the engine starts and keeps
it in step with its run / pause and ``time_scale`` (``set_running``, start, reload and its rollback).

What it is not: OCPP wire timestamps and sampling intervals stay wall clock (a CSMS is never shown the future);
the power model of each charger keeps its own ``time_scale`` / pause handling (``ChargerRuntime``). The clock only
times what is scheduled *in sim time* (scenario steps, generated arrivals and durations).

Time zone: :meth:`now_sim` is computed in UTC and shown in ``tz`` - by default the anchor's own (fixed) offset;
pass an IANA zone (``zoneinfo.ZoneInfo("Europe/Berlin")``) for a run that should follow DST changes.

``monotonic`` is injectable (a fake clock in tests). :meth:`sleep_sim` waits on the event loop and wakes up early
on every :meth:`set` / :meth:`reset`, so a pause, a speed change or a restart during the wait is honoured at once
(a restart keeps the sleeper's remaining sim duration).
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional

from sme_sim_common.engine import BaseEngine


class SimClock:
    #: Same bound as the engine's ``set_running`` (``BaseEngine.max_time_scale``).
    max_time_scale: float = BaseEngine.max_time_scale

    def __init__(self, time_scale: float = 1.0, running: bool = True, anchor: Optional[datetime] = None,
                 monotonic: Callable[[], float] = time.monotonic, tz: Optional[tzinfo] = None) -> None:
        self._mono = monotonic
        self._scale = self._check_scale(time_scale)
        self._running = bool(running)
        # ``_total`` (closed segments) never restarts; elapsed sim = total - ``_offset`` (the total at the last
        # reset). Sleepers wait on the total, so a reset does not move their deadline.
        self._total = 0.0
        self._offset = 0.0
        self._seg_start = self._mono()      # wall start of the open segment (only counts while running)
        self._changed = asyncio.Event()
        self._anchor, self._tz = self._aware(anchor, tz)

    # ------------------------------------------------------------ state

    @classmethod
    def _check_scale(cls, time_scale: Any) -> float:
        if isinstance(time_scale, bool) or not isinstance(time_scale, (int, float)):
            raise ValueError(f"time_scale must be a number, not {type(time_scale).__name__}")
        ts = float(time_scale)
        if not (0 < ts <= cls.max_time_scale and math.isfinite(ts)):
            raise ValueError(f"time_scale must be in (0, {cls.max_time_scale:g}]")
        return ts

    @staticmethod
    def _aware(anchor: Optional[datetime], tz: Optional[tzinfo]) -> "tuple[datetime, tzinfo]":
        """The anchor as an aware datetime and the zone :meth:`now_sim` is shown in. No anchor = now; a naive one
        is local time (or in ``tz``, when given). No ``tz`` = the anchor's own offset (the local one by default)."""
        if anchor is None:
            anchor = datetime.now(tz).astimezone(tz)
        elif anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=tz) if tz is not None else anchor.astimezone()
        elif tz is not None:
            anchor = anchor.astimezone(tz)
        return anchor, tz if tz is not None else anchor.tzinfo

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
            self._total += (now - self._seg_start) * self._scale
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

    def reset(self, anchor: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> None:
        """Restart at ``anchor`` (none = now): sim elapsed is 0 again, speed and run state are kept. Running
        :meth:`sleep_sim` calls keep their remaining sim duration."""
        self._anchor, self._tz = self._aware(anchor, tz)
        self._close_segment()
        self._offset = self._total
        self._wake()

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()  # current waiters hold the set one; later waits get a fresh one

    # ------------------------------------------------------------ reading

    def _total_now(self) -> float:
        if not self._running:
            return self._total
        return self._total + (self._mono() - self._seg_start) * self._scale

    def total_sim(self) -> float:
        """Sim seconds since the clock was made, never reset (:meth:`reset` does not move it): the time base of
        durations that must survive a scenario's ``clock.start_at``."""
        return self._total_now()

    def elapsed_sim(self) -> float:
        """Sim seconds since the anchor."""
        return self._total_now() - self._offset

    def now_sim(self) -> datetime:
        """The current sim time (aware, in ``tz``; computed in UTC, so an IANA zone follows DST)."""
        utc = self._anchor.astimezone(timezone.utc) + timedelta(seconds=self.elapsed_sim())
        return utc.astimezone(self._tz)

    def to_wall(self, dt_sim: float) -> float:
        """Wall seconds that ``dt_sim`` sim seconds take at the current speed (ignores a pause)."""
        return float(dt_sim) / self._scale

    def to_sim(self, dt_wall: float) -> float:
        """Sim seconds that pass in ``dt_wall`` wall seconds at the current speed (ignores a pause)."""
        return float(dt_wall) * self._scale

    # ------------------------------------------------------------ waiting

    async def sleep_sim(self, dt: float) -> None:
        """Wait until ``dt`` sim seconds have passed (none while paused; a speed change applies at once)."""
        target = self._total_now() + max(0.0, float(dt))
        while True:
            left = target - self._total_now()
            if left <= 0:
                return
            changed = self._changed
            timeout = self.to_wall(left) if self._running else None
            # asyncio.wait, not wait_for: wait_for (3.11) can swallow a cancellation that races with the event
            waiter = asyncio.ensure_future(changed.wait())
            try:
                await asyncio.wait({waiter}, timeout=timeout)
            finally:
                waiter.cancel()
