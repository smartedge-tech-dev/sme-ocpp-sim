"""Multi-charger simulation engine (plan §2.2 / §5 M5).

:class:`OcppEngine` runs one :class:`ChargerRuntime` per configured charger on top of
``sme_sim_common.engine.BaseEngine`` (pub / sub, event log, reload with rollback, tick loop).

Who steps the power model: each runtime keeps its own tick loop. That loop is a *session task* of the runtime: a
Set/ClearChargingProfile takes effect on the tick scheduled right after its CALLRESULT (``_after_reply``), meter
readings and StopTransaction integrate up to "now" themselves, and the ticks stop and restart with the session.
Stepping the same power models from the engine as well would integrate them twice, so the engine's ``_step`` stays
the base no-op. The engine tick only *observes*: ``_on_tick`` diffs charger snapshots and pushes the changes.
That is pure CPU work (no I/O) and runs under the engine lock, so it never interleaves with a reload.
``running`` / pause therefore stops only the engine clock; ``time_scale`` is forwarded to every runtime.

Tasks: one ``ChargerRuntime.run`` task per started charger in ``_runs``. Stopping a charger sets its stop event,
gives it ``stop_grace_s`` to close its socket (a back-off wait ends at once) and then cancels it; ``stop`` does
that for all chargers together.

Reload: ``_build`` re-validates the whole :class:`SimConfig` and builds a runtime per charger without touching
the running ones. ``_apply_diff`` keeps the running runtime of every charger whose effective settings are
unchanged, stops removed / changed ones and starts added / changed ones; if that fails, ``_rollback`` stops what
was started and restarts what was stopped (same runtime objects, so their transactions survive).

Push (only while someone is subscribed, at most every ``push_interval`` seconds, with a trailing push):

* ``{"type": "charger_state", "chargers": [snapshot, ...]}`` - chargers whose connection / registration /
  connector status / transaction changed (full charger snapshot);
* ``{"type": "values", "values": {charger_id: [{"id", "power_w", "meter_wh", "soc_pct"}, ...]}}`` - chargers
  whose measured values changed;
* ``{"type": "frame", "frames": [entry, ...], "last_seq": n}`` - new frame log entries (at most
  ``max_frames_per_push``; a client sees a gap from ``seq`` and can fetch the rest from the frame log).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sme_sim_common.engine import BaseEngine

from .config import ChargerConfig, SimConfig
from .framelog import FrameLog
from .runtime.charger import ChargerRuntime

#: Connector fields pushed as ``values``; the rest of a charger snapshot is ``charger_state``.
VALUE_KEYS = ("power_w", "meter_wh", "soc_pct")


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
        log, wake = self._engine.frame_log, self._engine.wake

        def on_frame(info: Any) -> None:
            log.add(charger, info)
            wake()
        return on_frame


def _split(snap: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """A charger snapshot without the measured values, and the values per connector."""
    conns, values = [], []
    for c in snap["connectors"]:
        conns.append({k: v for k, v in c.items() if k not in VALUE_KEYS})
        values.append({"id": c["id"], **{k: c[k] for k in VALUE_KEYS}})
    return {**snap, "connectors": conns}, values


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
        self._undo: Tuple[List[str], List[str]] = ([], [])
        self._push_at = 0.0
        self._push_later: Optional[asyncio.TimerHandle] = None
        self._last_state: Dict[str, Dict[str, Any]] = {}
        self._last_values: Dict[str, List[Dict[str, Any]]] = {}
        self._frame_seq = 0
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
        sim, out = config.simulation, {}
        for c in config.chargers:
            url, version = config.resolve_csms(c)
            rt = ChargerRuntime(c, url, version, reconnect=config.csms_defaults.reconnect, frame_log=self._sink,
                                call_timeout=self.call_timeout, ev=config.resolve_ev(c),
                                tick_s=sim.tick_seconds, time_scale=sim.time_scale)
            out[c.id] = _Entry(self._spec(config, c), rt)
        return out

    def _install(self, state: Dict[str, _Entry]) -> None:
        self.state = state
        self._last_state.clear()
        self._last_values.clear()

    async def _apply_diff(self, old_config: SimConfig, new_config: SimConfig, new_state: Dict[str, _Entry]) -> None:
        old = self.state
        keep = {cid for cid, e in new_state.items() if cid in old and old[cid].spec == e.spec}
        merged = {cid: old[cid] if cid in keep else e for cid, e in new_state.items()}
        gone = [cid for cid in old if cid not in keep]       # removed or changed
        fresh = [cid for cid in new_state if cid not in keep]  # added or changed
        self._undo = (gone, fresh)
        self.frame_log.resize(new_config.frame_log.size)
        if self._started:
            await self._halt(gone)
            for cid in fresh:
                self._launch(cid, merged[cid].runtime)
        for cid in keep:
            merged[cid].runtime.set_time_scale(new_config.simulation.time_scale)
        self._install(merged)
        if gone or fresh:
            self.log_event("config", f"chargers restarted: {sorted(set(gone) | set(fresh))}")

    async def _rollback(self, old_config: SimConfig, old_state: Dict[str, _Entry], error: BaseException) -> None:
        gone, fresh = self._undo
        if self._started:
            await self._halt([cid for cid in fresh if cid in self._runs
                              and (cid not in old_state or self._runs[cid].runtime is not old_state[cid].runtime)])
            for cid in gone:
                if cid not in self._runs:
                    self._launch(cid, old_state[cid].runtime)
        self.frame_log.resize(old_config.frame_log.size)
        self._install(old_state)

    # ------------------------------------------------------------ task registry

    def _launch(self, cid: str, rt: ChargerRuntime) -> None:
        stop = asyncio.Event()
        self._runs[cid] = _Run(rt, stop, asyncio.create_task(rt.run(stop), name=f"charger:{cid}"))

    async def _halt(self, cids: List[str]) -> None:
        runs = [self._runs.pop(cid) for cid in cids if cid in self._runs]
        if not runs:
            return
        for r in runs:
            r.stop.set()
        tasks = [r.task for r in runs]
        _, pending = await asyncio.wait(tasks, timeout=self.stop_grace_s)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def start(self) -> None:
        await super().start()
        self._started = True
        for cid, e in self.state.items():
            self._launch(cid, e.runtime)

    async def stop(self) -> None:
        self._started = False
        self._stopping = True
        if self._push_later is not None:
            self._push_later.cancel()
        await self._halt(list(self._runs))
        await super().stop()

    # ------------------------------------------------------------ clock

    def set_running(self, running: Optional[bool] = None, time_scale: Optional[float] = None) -> None:
        super().set_running(running, time_scale)
        for e in self.state.values():
            e.runtime.set_time_scale(self.time_scale)

    # ------------------------------------------------------------ push

    def _on_tick(self) -> None:
        if not self._subs:  # nobody listening: a new subscriber starts from GET /api/state
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
        self.wake()

    def _push(self) -> None:
        states, values = [], {}
        for cid, e in self.state.items():
            snap = e.runtime.snapshot()
            st, vals = _split(snap)
            if self._last_state.get(cid) != st:
                self._last_state[cid] = st
                states.append(snap)
            if self._last_values.get(cid) != vals:
                self._last_values[cid] = vals
                values[cid] = vals
        if states:
            self.publish({"type": "charger_state", "chargers": states})
        if values:
            self.publish({"type": "values", "values": values})
        if self.frame_log.last_seq > self._frame_seq:
            frames = self.frame_log.since(self._frame_seq, limit=self.max_frames_per_push)
            self._frame_seq = self.frame_log.last_seq
            self.publish({"type": "frame", "frames": [f.to_dict() for f in frames], "last_seq": self._frame_seq})

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
