"""Multi-charger simulation engine (plan §2.2 / §5 M5).

:class:`OcppEngine` runs one :class:`ChargerRuntime` per configured charger on top of
``sme_sim_common.engine.BaseEngine`` (pub / sub, event log, reload with rollback, tick loop).

Who steps the power model: each runtime keeps its own tick loop. That loop is a *session task* of the runtime: a
Set/ClearChargingProfile takes effect on the tick scheduled right after its CALLRESULT (``_after_reply``), meter
readings and StopTransaction integrate up to "now" themselves, and the ticks stop and restart with the session.
Stepping the same power models from the engine as well would integrate them twice, so the engine's ``_step`` stays
the base no-op. The engine tick only *observes*: ``_on_tick`` diffs charger snapshots and pushes the changes.
That is pure CPU work (no I/O) and runs under the engine lock, so it never interleaves with a reload.

Clock: ``time_scale`` and run / pause are forwarded to every runtime (``set_running``, reload, start). Pause freezes
every charger's power model (no energy / SoC / ramp advance, power reported as-is, no catch-up of the paused
interval on resume, see :meth:`ChargerRuntime.set_paused`); the OCPP sessions (heartbeats, CSMS calls) keep running.

Tasks: one ``ChargerRuntime.run`` task per started charger in ``_runs``. Stopping a charger sets its stop event,
gives it ``stop_grace_s`` to close its socket (a back-off wait ends at once) and then cancels it; ``stop`` does
that for all chargers together. A task that ends with an exception while still registered is reported as an
``error`` event; ``_halt`` reports the exceptions of the tasks it stops. ``stop`` does not take the reload lock: a
reload that resumes after ``stop`` sees ``_started`` cleared and launches nothing.

Reload: ``_build`` re-validates the whole :class:`SimConfig` and builds a runtime only for chargers that are new
or whose effective settings changed (an unchanged charger's entry is reused; running ones are never touched).
``_apply_diff`` keeps the running runtime of every unchanged charger, stops removed / changed ones and starts
added / changed ones; the new state (and the frame log size) is installed only after every fallible step. If
anything fails or the reload is cancelled, it stops what it started and restarts what it stopped (same runtime
objects, so their transactions survive) in a shielded undo, then re-raises.

Push (only while someone is subscribed, at most every ``push_interval`` seconds, with a trailing push). Every push
message carries ``push_seq``, increasing by one per message *per subscriber*, and ``full`` (state / values):

* ``{"type": "charger_state", "chargers": [snapshot, ...]}`` - chargers whose connection / registration /
  connector status / transaction changed (full charger snapshot);
* ``{"type": "values", "values": {charger_id: [{"id", "power_w", "meter_wh", "soc_pct"}, ...]}}`` - chargers
  whose measured values changed;
* ``{"type": "frame", "frames": [entry, ...], "last_seq": n}`` - new frame log entries (at most
  ``max_frames_per_push``; a client sees a gap from ``seq`` and can fetch the rest from the frame log).

Resync (client contract for the web layer): subscriber queues drop their oldest message when full. If a dropped
message was a ``charger_state`` / ``values`` delta, the next push to that subscriber is a full one (``full: true``,
every charger), and so is the first push to a new subscriber; a client therefore never has to refetch state
itself. A gap in ``push_seq`` only tells it something was lost (frames: fetch them from the frame log).
"""


from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, Iterable, List, Optional, Set, Tuple

from sme_sim_common.engine import BaseEngine

from .config import ChargerConfig, SimConfig
from .framelog import FrameLog
from .runtime.charger import ChargerRuntime

#: Connector fields pushed as ``values``; the rest of a charger snapshot is ``charger_state``.
VALUE_KEYS = ("power_w", "meter_wh", "soc_pct")
#: Push messages whose loss makes the next push to that subscriber a full one.
_STATE_TYPES = ("charger_state", "values")


@dataclass
class _Entry:
    spec: Tuple[Any, ...]  # effective settings; equal spec = keep the running runtime
    runtime: ChargerRuntime


@dataclass
class _Run:
    runtime: ChargerRuntime
    stop: asyncio.Event
    task: asyncio.Task


class _FrameSink:
    """``frame_log`` for the runtimes: records into the engine's FrameLog and wakes the push."""

    def __init__(self, engine: "OcppEngine") -> None:
        self._engine = engine

    def callback(self, charger: str):
        engine = self._engine

        def on_frame(info: Any) -> None:
            engine.frame_log.add(charger, info)
            engine.wake()
        return on_frame


def _split(snap: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A charger snapshot without the measured values, and the values per connector."""
    conns, values = [], []
    for c in snap["connectors"]:
        conns.append({k: v for k, v in c.items() if k not in VALUE_KEYS})
        values.append({"id": c["id"], **{k: c[k] for k in VALUE_KEYS}})
    return {**snap, "connectors": conns}, values


async def _shielded(aw: Awaitable[Any]) -> None:
    """Run ``aw`` to completion even if the caller is cancelled meanwhile; re-raise that cancellation afterwards."""
    task = asyncio.ensure_future(aw)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    task.result()


class OcppEngine(BaseEngine):
    logger_name = "sme_ocpp_sim.engine"
    push_interval = 0.5        # seconds between incremental pushes
    max_frames_per_push = 500
    stop_grace_s = 2.0         # time a stopping charger gets to close its connection before it is cancelled
    call_timeout = 30.0

    def __init__(self, config: Optional[SimConfig] = None, **kwargs: Any):
        config = config if config is not None else SimConfig()
        self.frame_log = FrameLog(config.frame_log.size)
        self._sink = _FrameSink(self)
        self._runs: Dict[str, _Run] = {}
        self._started = False
        self._push_at = 0.0
        self._push_later: Optional[asyncio.TimerHandle] = None
        self._last_state: Dict[str, Dict[str, Any]] = {}
        self._last_values: Dict[str, List[Dict[str, Any]]] = {}
        self._frame_seq = 0
        self._resync: Set[asyncio.Queue] = set()        # subscribers whose next push is a full one
        self._push_seqs: Dict[asyncio.Queue, int] = {}  # last push_seq sent to each subscriber
        super().__init__(config, **kwargs)

    @property
    def chargers(self) -> Dict[str, ChargerRuntime]:
        return {cid: e.runtime for cid, e in self.state.items()}

    # ------------------------------------------------------------ build / reload

    @staticmethod
    def _spec(config: SimConfig, c: ChargerConfig) -> Tuple[Any, ...]:
        url, version = config.resolve_csms(c)
        return (c.model_dump(), url, version, config.resolve_ev(c),
                config.csms_defaults.reconnect.model_dump(), config.simulation.tick_seconds)

    def _build(self, config: SimConfig) -> Dict[str, _Entry]:
        # Config objects are mutable (UI edits, set_running): validate the whole tree again before using it.
        SimConfig.model_validate(config.model_dump())
        sim, old, out = config.simulation, self.state or {}, {}
        for c in config.chargers:
            spec = self._spec(config, c)
            prev = old.get(c.id)
            if prev is not None and prev.spec == spec:  # unchanged: _apply_diff keeps the running runtime anyway
                out[c.id] = prev
                continue
            url, version = config.resolve_csms(c)
            rt = ChargerRuntime(c, url, version, reconnect=config.csms_defaults.reconnect, frame_log=self._sink,
                                call_timeout=self.call_timeout, ev=config.resolve_ev(c),
                                tick_s=sim.tick_seconds, time_scale=sim.time_scale)
            out[c.id] = _Entry(spec, rt)
        return out

    def _install(self, state: Dict[str, _Entry]) -> None:
        self.state = state
        self._last_state.clear()
        self._last_values.clear()

    @staticmethod
    def _apply_clock(runtimes: Iterable[ChargerRuntime], time_scale: float, running: bool) -> None:
        for rt in runtimes:
            rt.set_time_scale(time_scale)
            rt.set_paused(not running)

    async def _apply_diff(self, old_config: SimConfig, new_config: SimConfig, new_state: Dict[str, _Entry]) -> None:
        old = self.state
        keep = {cid for cid, e in new_state.items() if cid in old and old[cid].spec == e.spec}
        merged = {cid: old[cid] if cid in keep else e for cid, e in new_state.items()}
        gone = [cid for cid in old if cid not in keep]       # removed or changed
        fresh = [cid for cid in new_state if cid not in keep]  # added or changed
        launched: List[str] = []
        try:
            if self._started:
                await self._halt(gone)
                if self._started and not self._stopping:  # stop() may have run while we waited
                    for cid in fresh:
                        self._launch(cid, merged[cid].runtime)
                        launched.append(cid)
            sim = new_config.simulation
            self._apply_clock((e.runtime for e in merged.values()), sim.time_scale, sim.running)
        except BaseException:  # incl. cancellation, which BaseEngine.reload does not roll back
            await _shielded(self._undo(old, launched))
            raise
        self.frame_log.resize(new_config.frame_log.size)  # last: a rollback never loses frames
        self._install(merged)
        if gone or fresh:
            self.log_event("config", f"chargers restarted: {sorted(set(gone) | set(fresh))}")

    async def _undo(self, old_state: Dict[str, _Entry], launched: List[str]) -> None:
        """Undo a partly applied diff: stop the runtimes it launched, restart the old ones it stopped and restore
        the engine clock on every old runtime. ``self.state`` has not been replaced yet. BaseEngine's own
        ``_rollback`` (reinstall the old state) then only repeats what is already true."""
        try:
            await self._halt(launched)
            if self._started and not self._stopping:
                for cid, e in old_state.items():
                    if cid not in self._runs:
                        self._launch(cid, e.runtime)
            self._apply_clock((e.runtime for e in old_state.values()), self.time_scale, self.running)
        except Exception as e:  # noqa: BLE001 - surface it; the caller re-raises the original error
            self.log_event("error", f"reload rollback failed: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ task registry

    def _launch(self, cid: str, rt: ChargerRuntime) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop), name=f"charger:{cid}")
        self._runs[cid] = _Run(rt, stop, task)
        task.add_done_callback(lambda t, cid=cid: self._run_done(cid, t))

    def _run_done(self, cid: str, task: asyncio.Task) -> None:
        run = self._runs.get(cid)
        if run is None or run.task is not task or task.cancelled():
            return  # halted: _halt reports its own tasks
        exc = task.exception()
        if exc is not None:
            self.log_event("error", f"charger {cid} task crashed: {type(exc).__name__}: {exc}")
        else:
            self.log_event("warning", f"charger {cid} task ended unexpectedly")

    async def _halt(self, cids: List[str]) -> None:
        """Stop the given chargers. Cancellation-safe: the tasks are cancelled and awaited before it propagates."""
        runs = [self._runs.pop(cid) for cid in cids if cid in self._runs]
        if not runs:
            return
        for r in runs:
            r.stop.set()
        tasks = [r.task for r in runs]
        try:
            await asyncio.wait(tasks, timeout=self.stop_grace_s)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.wait(tasks)
        for t in tasks:
            if not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                self.log_event("error", f"{t.get_name()} failed: {type(exc).__name__}: {exc}")

    async def start(self) -> None:
        if self._started:
            return
        await super().start()
        self._started = True
        self._apply_clock((e.runtime for e in self.state.values()), self.time_scale, self.running)
        for cid, e in self.state.items():
            self._launch(cid, e.runtime)

    async def stop(self) -> None:
        self._started = False
        self._stopping = True
        self._cancel_push_later()
        await self._halt(list(self._runs))
        await super().stop()
        self._cancel_push_later()

    # ------------------------------------------------------------ clock

    def set_running(self, running: Optional[bool] = None, time_scale: Optional[float] = None) -> None:
        """Run / pause and time scale, forwarded to every runtime (pause freezes the power models only)."""
        super().set_running(running, time_scale)
        self._apply_clock((e.runtime for e in self.state.values()), self.time_scale, self.running)

    # ------------------------------------------------------------ pub / sub and push

    def subscribe(self) -> asyncio.Queue:
        q = super().subscribe()
        self._resync.add(q)  # the first push to a new subscriber is a full one
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        super().unsubscribe(q)
        self._resync.discard(q)
        self._push_seqs.pop(q, None)

    def publish(self, msg: Dict[str, Any]) -> None:
        """BaseEngine's fan-out (a full queue drops its oldest message) that also notices lost state deltas."""
        for q in list(self._subs):
            self._put(q, msg)

    def _put(self, q: asyncio.Queue, msg: Dict[str, Any]) -> None:
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                lost = q.get_nowait()
                if isinstance(lost, dict) and lost.get("type") in _STATE_TYPES:
                    self._resync.add(q)
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(msg)

    def _cancel_push_later(self) -> None:
        if self._push_later is not None:
            self._push_later.cancel()
            self._push_later = None

    def _on_tick(self) -> None:
        if self._stopping:
            return
        if not self._subs:  # nobody listening; a new subscriber gets a full push first
            self._last_state.clear()
            self._last_values.clear()
            self._frame_seq = self.frame_log.last_seq
            return
        now = time.monotonic()
        if now < self._push_at:
            if self._push_later is None and self._loop is not None:
                self._push_later = self._loop.call_later(self._push_at - now, self._trailing_push)
            return
        self._push_at = now + self.push_interval
        self._push()

    def _trailing_push(self) -> None:
        self._push_later = None
        if not self._stopping:
            self.wake()

    def _push(self) -> None:
        states, values, all_states, all_values = [], {}, [], {}
        for cid, e in self.state.items():
            snap = e.runtime.snapshot()
            st, vals = _split(snap)
            all_states.append(snap)
            all_values[cid] = vals
            if self._last_state.get(cid) != st:
                self._last_state[cid] = st
                states.append(snap)
            if self._last_values.get(cid) != vals:
                self._last_values[cid] = vals
                values[cid] = vals
        frame_msg = None
        if self.frame_log.last_seq > self._frame_seq:
            frames = self.frame_log.since(self._frame_seq, limit=self.max_frames_per_push)
            self._frame_seq = self.frame_log.last_seq
            frame_msg = {"type": "frame", "frames": [f.to_dict() for f in frames], "last_seq": self._frame_seq}
        subs = list(self._subs)
        resync = self._resync.intersection(subs)
        self._resync.clear()
        self._push_seqs = {q: n for q, n in self._push_seqs.items() if q in resync or q in subs}
        for q in subs:
            full = q in resync
            st, va = (all_states, all_values) if full else (states, values)
            msgs = []
            if st or full:
                msgs.append({"type": "charger_state", "chargers": st, "full": full})
            if va or full:
                msgs.append({"type": "values", "values": va, "full": full})
            if frame_msg is not None:
                msgs.append(frame_msg)
            for m in msgs:
                n = self._push_seqs.get(q, 0) + 1
                self._push_seqs[q] = n
                self._put(q, {**m, "push_seq": n})

    # ------------------------------------------------------------ state

    def stats_snapshot(self) -> Optional[Dict[str, Any]]:
        rts = [e.runtime for e in self.state.values()]
        return {"chargers": len(rts), "connected": sum(r.connected for r in rts),
                "online": sum(r.online for r in rts), "frame_seq": self.frame_log.last_seq}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sim_time": self.sim_time,
            "running": self.running,
            "time_scale": self.time_scale,
            "frame_seq": self.frame_log.last_seq,
            "chargers": [e.runtime.snapshot() for e in self.state.values()],
        }
