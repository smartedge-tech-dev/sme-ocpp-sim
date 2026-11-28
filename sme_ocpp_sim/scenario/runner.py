"""Scenario runner (v0.3 plan §3.2 / §3.5, M3): runs the tracks of one scenario in parallel on the engine's sim
clock and judges its assertions on the charger frame stream.

- **Timing**: ``at`` / ``after`` / ``wait`` / ``within_sim`` / ``until`` are sim time (``engine.clock``, so they
  follow ``time_scale`` and pause; Q2); ``within`` is wall time. OCPP timestamps on the wire stay wall time.
- **Actions** go through :mod:`.ops`, like the web routes (same checks, same effects); ``wait`` / ``log`` are the
  runner's own.
- **Frames**: the runner listens on ``engine.frame_listeners`` from its start and keeps every frame of its
  chargers (independent of the frame log's ring size). An ``expect`` / ``expect_not`` / ``await_csms`` looks at
  the frames after the start of the track's previous action step (frame seq), so an answer that came before the
  assertion started is not missed.
- **Failure**: a failed assertion or a rejected action with ``on_fail: stop`` (default) stops the whole scenario;
  ``continue`` goes on with the next step. Steps that did not run are ``skipped``.
- **Result**: ``aborted`` (stopped from outside) > ``error`` (an action failed / setup failed) > ``fail`` >
  ``pass``. ``until`` ends the run: a step still running then fails.

One scenario runs at a time (``engine.scenario``); its chargers are :attr:`ScenarioRunner.chargers` (the traffic
generator, M4, pauses itself there: Q3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..framelog import FrameEntry
from . import ops
from .match import frame_payload, match_all
from .model import (DEFAULT_WITHIN_S, FrameSpec, PlannedStep, PlannedTrack, Scenario, ScenarioError, expand,
                    parse_time)

log = logging.getLogger("sme_ocpp_sim.scenario")

VERDICT_FORMAT = "ocppsim-verdict/1"
#: Wall seconds between state checks of an assertion (frames wake it at once).
POLL_S = 0.05
_REPLIES = ("CALLRESULT", "CALLERROR")


class _Stop(Exception):
    """A step failed with ``on_fail: stop``: the scenario ends."""


@dataclass
class StepRecord:
    id: str
    track: str
    kind: str
    status: str = "pending"          # pending | running | pass | fail | error | skipped | aborted
    t_wall: Optional[float] = None   # wall seconds since the scenario start, when the step started
    t_sim: Optional[float] = None    # sim seconds since the scenario start
    message: str = ""
    evidence: List[int] = field(default_factory=list)   # frame seqs


def _short(v: Any, n: int = 300) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


class ScenarioRunner:
    def __init__(self, engine: Any, scenario: Scenario, *, source: Optional[str] = None) -> None:
        self.engine = engine
        self.scenario = scenario
        self.source = source
        self.tracks: List[PlannedTrack] = expand(scenario, list(engine.chargers))
        self.chargers = frozenset(t.charger for t in self.tracks)
        self.records: Dict[Tuple[str, str], StepRecord] = {
            (t.name, s.id): StepRecord(s.id, t.name, s.step.kind) for t in self.tracks for s in t.steps}
        self.state = "pending"                      # pending | running | done
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.prompts: Dict[str, str] = {}
        self.frames: List[FrameEntry] = []
        self.trace_path: Optional[str] = None
        self._new_frame = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._aborted = False
        self._end_reason: Optional[str] = None      # until | stop
        self._started_at: Optional[datetime] = None
        self._w0 = self._s0 = 0.0
        self._wall_s = self._sim_s = 0.0
        self._ref: Optional[datetime] = None
        self._scale_before: Optional[float] = None
        self._seq0 = 0

    # ------------------------------------------------------------ control

    def start(self) -> asyncio.Task:
        if self._task is None:
            self.state, self._w0, self._s0 = "running", time.monotonic(), self.engine.clock.elapsed_sim()
            self._task = asyncio.ensure_future(self.run())
        return self._task

    def abort(self) -> bool:
        """Stop the run from outside (result ``aborted``); False when it is not running."""
        if self._task is None or self._task.done():
            return False
        self._aborted = True
        self._task.cancel()
        return True

    async def wait(self) -> Dict[str, Any]:
        if self._task is not None:
            await asyncio.wait({self._task})
            if self.state != "done":  # cancelled before it ran its first line
                self._finish()
        return self.verdict()

    @property
    def done(self) -> bool:
        return self.state == "done"

    # ------------------------------------------------------------ clock helpers

    @property
    def clock(self):
        return self.engine.clock

    def _sim(self) -> float:
        return self.clock.elapsed_sim() - self._s0

    def _wall(self) -> float:
        return time.monotonic() - self._w0

    def _target(self, spec: Tuple[str, Any]) -> float:
        """Sim seconds since the start at which ``spec`` (a parsed ``at`` / ``until``) falls."""
        kind, v = spec
        if kind == "rel":
            return v
        ref = self._ref
        if kind == "clock":
            t = ref.replace(hour=v.hour, minute=v.minute, second=v.second, microsecond=0)
            if t < ref.replace(microsecond=0):
                t += timedelta(days=1)
        else:
            t = v if v.tzinfo is not None else v.replace(tzinfo=ref.tzinfo)
        return (t - ref).total_seconds()

    async def _sleep_until(self, t_sim: float) -> None:
        left = t_sim - self._sim()
        if left > 0:
            await self.clock.sleep_sim(left)

    # ------------------------------------------------------------ frames

    def _on_frame(self, e: FrameEntry) -> None:
        if e.charger in self.chargers:
            self.frames.append(e)
            ev, self._new_frame = self._new_frame, asyncio.Event()
            ev.set()

    def _find(self, charger: str, since: int, spec: FrameSpec) -> List[FrameEntry]:
        out = []
        for e in self.frames:
            if (e.seq <= since or e.charger != charger or (spec.dir and e.direction != spec.dir)
                    or (spec.action and e.action != spec.action)
                    or (spec.message_type and e.message_type != spec.message_type)):
                continue
            if spec.payload and match_all(frame_payload(e.raw), spec.payload) is not None:
                continue
            out.append(e)
        return out

    def _reply(self, call: FrameEntry) -> Optional[FrameEntry]:
        for e in self.frames:
            if (e.seq > call.seq and e.charger == call.charger and e.uid == call.uid
                    and e.direction != call.direction and e.message_type in _REPLIES):
                return e
        return None

    async def _until(self, deadline: Tuple[str, float]) -> bool:
        """Wait for a new frame or the next poll; False once ``deadline`` (``("wall"|"sim", t)``) has passed."""
        kind, t = deadline
        now = self._wall() if kind == "wall" else self._sim()
        if now >= t:
            return False
        left = t - now if kind == "wall" else self.clock.to_wall(t - now)
        ev = self._new_frame
        try:
            await asyncio.wait_for(ev.wait(), min(POLL_S, max(left, 0.001)))
        except asyncio.TimeoutError:
            pass
        return True

    def _deadline(self, step) -> Tuple[str, float]:
        if step.within_sim is not None:
            return "sim", self._sim() + step.within_sim
        return "wall", self._wall() + (step.within if step.within is not None else DEFAULT_WITHIN_S)

    @staticmethod
    def _window(step) -> str:
        if step.within_sim is not None:
            return f"{step.within_sim:g} s sim"
        return f"{step.within if step.within is not None else DEFAULT_WITHIN_S:g} s"

    # ------------------------------------------------------------ steps

    async def _do(self, cid: str, ps: PlannedStep, rec: StepRecord) -> None:
        s, eng = ps.step, self.engine
        if s.do == "wait":
            await self.clock.sleep_sim(s.duration or 0.0)
            rec.message = f"waited {s.duration:g} s sim"
            return
        if s.do == "log":
            eng.log_event("scenario", f"{self.scenario.name}: {s.message}")
            rec.message = s.message or ""
            return
        calls = {
            "plug": lambda: ops.plug(eng, cid, s.connector), "unplug": lambda: ops.unplug(eng, cid, s.connector),
            "swipe": lambda: ops.swipe(eng, cid, s.connector, s.id_tag),
            "start": lambda: ops.start(eng, cid, s.connector, s.id_tag),
            "stop": lambda: ops.stop(eng, cid, s.connector),
            "fault": lambda: ops.fault(eng, cid, s.connector, s.error_code or "OtherError"),
            "clear_fault": lambda: ops.clear_fault(eng, cid, s.connector),
            "connect": lambda: ops.connect(eng, cid), "disconnect": lambda: ops.disconnect(eng, cid),
            "impair": lambda: ops.impair(eng, cid, s.impairments or {}),
            "send": lambda: ops.send(eng, cid, action=s.action, raw=s.raw, timeout=s.timeout,
                                     payload=s.payload if "payload" in s.model_fields_set else ops.UNSET,
                                     validate=True if s.validate_ is None else s.validate_),
        }
        sync = {"drop": lambda: ops.drop(eng, cid, s.mode, s.reconnect_after_s),
                "set_ev": lambda: ops.set_ev(eng, cid, s.connector, s.ev_model, s.initial_soc, s.target_soc)}
        out = sync[s.do]() if s.do in sync else await calls[s.do]()
        rec.message = _short(out)

    async def _expect_frame(self, cid: str, since: int, ps: PlannedStep, rec: StepRecord, *,
                            spec: FrameSpec, then=None, what: str) -> bool:
        deadline = self._deadline(ps.step)
        why = ""
        while True:
            for e in self._find(cid, since, spec):
                if then is None:
                    rec.evidence = [e.seq]
                    rec.message = f"{what}: seq {e.seq}"
                    return True
                r = self._reply(e)
                if r is None:
                    why = f"no answer yet to seq {e.seq}"
                    continue
                if then.message_type and r.message_type != then.message_type:
                    why = f"answer seq {r.seq} is a {r.message_type}"
                    rec.evidence = [e.seq, r.seq]
                    continue
                miss = match_all(frame_payload(r.raw), then.payload) if then.payload else None
                if miss is None:
                    rec.evidence = [e.seq, r.seq]
                    rec.message = f"{what}: seq {e.seq}, answer seq {r.seq}"
                    return True
                why, rec.evidence = f"answer seq {r.seq}: {miss}", [e.seq, r.seq]
            if not await self._until(deadline):
                rec.message = f"{what}: no match within {self._window(ps.step)}" + (f" ({why})" if why else "")
                return False

    def _state_values(self, cid: str, connector: int) -> Dict[str, Any]:
        rt = ops.runtime(self.engine, cid)
        ops.connector_ok(rt, connector)
        conn = next((c for c in rt.snapshot().get("connectors", []) if c.get("id") == connector), {})
        return {"status": conn.get("status"), "power_w": conn.get("power_w"), "soc_pct": conn.get("soc_pct"),
                "meter_wh": conn.get("meter_wh"), "has_transaction": rt.has_transaction(connector),
                "online": rt.online, "connected": rt.connected}

    async def _expect_state(self, cid: str, ps: PlannedStep, rec: StepRecord) -> bool:
        spec = ps.step.expect.state
        deadline = self._deadline(ps.step)
        while True:
            values = self._state_values(cid, spec.connector)
            miss = match_all(values, spec.checks())
            if miss is None:
                rec.message = f"state of connector {spec.connector}: {_short(values)}"
                return True
            if not await self._until(deadline):
                rec.message = f"state of connector {spec.connector} within {self._window(ps.step)}: {miss}"
                return False

    async def _expect_not(self, cid: str, since: int, ps: PlannedStep, rec: StepRecord) -> bool:
        spec = ps.step.expect_not
        deadline = self._deadline(ps.step)
        while True:
            hit = self._find(cid, since, spec)
            if hit:
                rec.evidence = [e.seq for e in hit[:10]]
                rec.message = f"unwanted frame seq {hit[0].seq} ({hit[0].direction} {hit[0].message_type} " \
                              f"{hit[0].action})"
                return False
            if not await self._until(deadline):
                rec.message = f"no such frame within {self._window(ps.step)}"
                return True

    async def _await_csms(self, cid: str, since: int, ps: PlannedStep, rec: StepRecord, track: str) -> bool:
        a = ps.step.await_csms
        if a.prompt:
            self.prompts[track] = a.prompt
            self.engine.log_event("scenario", f"{self.scenario.name} [{track}]: {a.prompt}")
        try:
            return await self._expect_frame(cid, since, ps, rec, what=f"CSMS {a.action}",
                                            spec=FrameSpec(dir="rx", message_type="CALL", action=a.action,
                                                           payload=a.payload))
        finally:
            self.prompts.pop(track, None)

    async def _track(self, tr: PlannedTrack) -> None:
        since = self._seq0
        for ps in tr.steps:
            rec = self.records[(tr.name, ps.id)]
            s = ps.step
            if ps.at is not None:
                await self._sleep_until(self._target(ps.at))
            elif s.after:
                await self.clock.sleep_sim(s.after)
            rec.status, rec.t_wall, rec.t_sim = "running", round(self._wall(), 3), round(self._sim(), 3)
            try:
                if s.do is not None:
                    if s.do not in ("wait", "log"):
                        since = self.engine.frame_log.last_seq
                    await self._do(tr.charger, ps, rec)
                    ok = True
                elif s.expect is not None and s.expect.frame is not None:
                    ok = await self._expect_frame(tr.charger, since, ps, rec, spec=s.expect.frame,
                                                  then=s.expect.then, what=s.expect.frame.action or "frame")
                elif s.expect is not None:
                    ok = await self._expect_state(tr.charger, ps, rec)
                elif s.expect_not is not None:
                    ok = await self._expect_not(tr.charger, since, ps, rec)
                else:
                    ok = await self._await_csms(tr.charger, since, ps, rec, tr.name)
                rec.status = "pass" if ok else "fail"
            except ops.OpError as e:
                rec.status, rec.message = "error", f"{e.kind}: {e}"
            if rec.status != "pass":
                log.info("scenario %s: %s %s %s: %s", self.scenario.name, tr.name, ps.id, rec.status, rec.message)
                if s.on_fail == "stop":
                    raise _Stop(f"{tr.name} step {ps.id} {rec.status}")

    # ------------------------------------------------------------ run

    async def run(self) -> Dict[str, Any]:
        eng, sc = self.engine, self.scenario
        self.state = "running"
        self._started_at = datetime.now(timezone.utc).astimezone()
        eng.frame_listeners.append(self._on_frame)
        tasks: List[asyncio.Task] = []
        watch: List[asyncio.Task] = []
        try:
            if sc.clock.time_scale is not None:
                self._scale_before = eng.time_scale
                eng.set_running(time_scale=sc.clock.time_scale)
            if sc.clock.start_at is not None:
                eng.clock.reset(sc.clock.start_at)
            self._w0, self._s0 = time.monotonic(), eng.clock.elapsed_sim()
            self._ref = eng.clock.now_sim()
            self._seq0 = eng.frame_log.last_seq
            eng.log_event("scenario", f"scenario {sc.name} started ({len(self.tracks)} track(s))")
            tasks = [asyncio.ensure_future(self._track(t)) for t in self.tracks]
            if sc.until:
                watch.append(asyncio.ensure_future(self._sleep_until(self._target(parse_time(sc.until)))))
            pending = set(tasks) | set(watch)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if any(t in watch for t in done):
                    self._end_reason = "until"
                    break
                stop = next((t for t in done if not t.cancelled() and isinstance(t.exception(), _Stop)), None)
                if stop is not None:
                    self._end_reason = "stop"
                    break
                broken = next((t for t in done if not t.cancelled() and t.exception() is not None), None)
                if broken is not None:
                    raise broken.exception()
                if not (pending - set(watch)):
                    break
        except asyncio.CancelledError:
            if not self._aborted:
                raise
        except ScenarioError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 - an internal error is a verdict "error", not a crash
            log.exception("scenario %s failed", sc.name)
            self.error = f"{type(e).__name__}: {e}"
        finally:
            for t in tasks + watch:
                t.cancel()
            await asyncio.gather(*tasks, *watch, return_exceptions=True)
            with contextlib.suppress(ValueError):
                eng.frame_listeners.remove(self._on_frame)
            if self._scale_before is not None:
                eng.set_running(time_scale=self._scale_before)
            self._finish()
        return self.verdict()

    def _finish(self) -> None:
        self._wall_s, self._sim_s = self._wall(), self._sim()
        for r in self.records.values():
            if r.status == "running":
                if self._aborted:
                    r.status, r.message = "aborted", "scenario aborted"
                elif self._end_reason == "until":
                    r.status, r.message = "fail", "scenario until reached before the step finished"
                else:
                    r.status, r.message = "skipped", "scenario stopped"
            elif r.status == "pending":
                r.status = "skipped"
        statuses = {r.status for r in self.records.values()}
        if self._aborted:
            self.result = "aborted"
        elif self.error is not None or "error" in statuses:
            self.result = "error"
        elif "fail" in statuses:
            self.result = "fail"
        else:
            self.result = "pass"
        self.state = "done"
        self.prompts.clear()
        self.engine.log_event("scenario", f"scenario {self.scenario.name}: {self.result}")

    # ------------------------------------------------------------ reports

    def _counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.records.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    def status(self) -> Dict[str, Any]:
        running = self.state == "running"
        tracks = []
        for t in self.tracks:
            recs = [self.records[(t.name, s.id)] for s in t.steps]
            cur = next((r.id for r in recs if r.status == "running"), None)
            tracks.append({"name": t.name, "charger": t.charger, "step": cur,
                           "done": sum(r.status not in ("pending", "running") for r in recs), "total": len(recs)})
        return {"state": self.state, "name": self.scenario.name, "result": self.result, "error": self.error,
                "started": self._started_at.isoformat() if self._started_at else None,
                "wall_s": round(self._wall() if running else self._wall_s, 3),
                "sim_s": round(self._sim() if running else self._sim_s, 3),
                "sim_time": self.clock.now_sim().isoformat() if running else None,
                "steps": self._counts(), "tracks": tracks,
                "prompts": [{"track": k, "prompt": v} for k, v in self.prompts.items()]}

    def verdict(self) -> Dict[str, Any]:
        return {"format": VERDICT_FORMAT, "scenario": self.scenario.name, "result": self.result,
                "error": self.error, "seed": self.scenario.seed,
                "started": self._started_at.isoformat() if self._started_at else None,
                "wall_s": round(self._wall_s, 3), "sim_s": round(self._sim_s, 3), "tool_version": __version__,
                "trace": self.trace_path, "steps": [asdict(r) for r in self.records.values()]}

    def trace_jsonl(self) -> str:
        """The recorded frames of the scenario's chargers as JSON lines (the frame log's export format)."""
        return "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in self.frames)

