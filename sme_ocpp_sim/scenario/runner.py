"""Scenario runner (v0.3 plan §3.2 / §3.5, M3): runs the tracks of one scenario in parallel on the engine's sim
clock and judges its assertions on the charger frame stream.

- **Timing**: ``at`` / ``after`` / ``wait`` / ``within_sim`` / ``until`` are sim time (``engine.clock``, so they
  follow ``time_scale`` and pause; Q2); ``within`` is wall time. OCPP timestamps on the wire stay wall time. In a
  ``repeat`` track ``at: "+dur"`` counts from the start of the round.
- **Actions** go through :mod:`.ops`, like the web routes (same checks, same effects); ``wait`` / ``log`` are the
  runner's own.
- **Frames**: the runner listens on ``engine.frame_listeners`` and keeps, per charger and ordered by seq, the
  frames of the chargers a frame assertion can look at (independent of the frame log's ring size). Each track has
  a **cursor**: it starts at the scenario start, a passing ``expect.frame`` / ``await_csms`` moves it to the
  matched frame (with ``then``: the answer), an action step moves it to the newest frame seq when the action
  started; ``expect_not`` / ``expect.state`` leave it. A frame assertion searches after the cursor (``from:
  prev_step``, default), after the start of the previous action (``from: action``) or from the scenario start
  (``from: scenario_start``). Frames no track can need any more are dropped; at most :data:`MAX_FRAMES` per
  charger are kept, an assertion that would need a dropped one fails ("frame buffer overflowed").
- **Failure**: a failed assertion or a rejected action with ``on_fail: stop`` (default) stops the whole scenario;
  ``continue`` goes on with the next step. Steps that did not run are ``skipped``.
- **Result**: ``aborted`` (stopped from outside, also by an engine stop) > ``error`` (an action failed / setup
  failed) > ``fail`` > ``pass``. ``until`` ends the run: a step still running then fails, later ones are skipped.
- **Clock**: ``clock.time_scale`` is restored afterwards unless someone changed the speed meanwhile;
  ``clock.start_at`` restarts the engine's sim clock, which stays where the run left it.

One scenario runs at a time (``engine.scenario``); its chargers are :attr:`ScenarioRunner.chargers` (the standing
traffic skips them and ends its sessions there before the scenario starts: Q3).

**Generator tracks** (``generate:``, :mod:`.traffic`, M4) run a traffic generator per scenario track (one plan
for its ``for_each`` chargers) until ``until``; their record (kind ``generate``) passes with the session counts.
The standing traffic (``engine.traffic``) is a runner with ``standing=True``: generator tracks only, no
``until``, no frame buffer, no verdict use.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..framelog import FrameEntry
from . import ops
from . import traffic as _traffic
from .match import frame_payload, match_all
from .model import (DEFAULT_WITHIN_S, FrameSpec, PlannedStep, PlannedTrack, Scenario, ScenarioError, expand,
                    parse_time)

log = logging.getLogger("sme_ocpp_sim.scenario")

VERDICT_FORMAT = "ocppsim-verdict/1"
#: Wall seconds between state checks of an assertion (frames of its charger wake it at once).
POLL_S = 0.05
#: Frames kept per charger at most (oldest dropped first).
MAX_FRAMES = 200_000
_REPLIES = ("CALLRESULT", "CALLERROR")


class _Stop(Exception):
    """A step failed with ``on_fail: stop``: the scenario ends."""


class _Overflow(Exception):
    """A frame assertion needs frames the buffer has dropped."""


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


@dataclass
class _Buf:
    seqs: List[int] = field(default_factory=list)
    entries: List[FrameEntry] = field(default_factory=list)
    dropped_upto: int = 0            # newest seq dropped by the size cap


def _short(v: Any, n: int = 300) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def _uses_frames(s) -> bool:
    return s.expect_not is not None or s.await_csms is not None or (s.expect is not None and s.expect.frame is not None)


class ScenarioRunner:
    max_frames = MAX_FRAMES

    def __init__(self, engine: Any, scenario: Scenario, *, source: Optional[str] = None, standing: bool = False,
                 config: Any = None) -> None:
        self.engine = engine
        self.scenario = scenario
        self.source = source
        self.standing = standing
        #: The config the generator tracks read (``use: traffic``, vehicle pools); default: the engine's.
        self.config = config if config is not None else engine.config
        self.tracks: List[PlannedTrack] = expand(scenario, list(engine.chargers))
        self.generators: Dict[int, _traffic.Generator] = {}
        for t in self.tracks:
            if t.generate is None or t.group in self.generators:
                continue
            cids = [x.charger for x in self.tracks if x.group == t.group]
            try:
                tc = _traffic.merged(self.config, t.generate, scenario.seed)
                self.config.check_traffic(tc, cids)
            except ValueError as e:
                raise ScenarioError(f"tracks.{t.group}.generate: {e}") from None
            self.generators[t.group] = _traffic.Generator(self, tc, cids, standing=standing)
        self.chargers = frozenset(t.charger for t in self.tracks)
        self._frame_chargers = frozenset(t.charger for t in self.tracks if any(_uses_frames(s.step) for s in t.steps))
        self._keep_all = {t.name for t in self.tracks if any(s.step.from_ == "scenario_start" for s in t.steps)}
        self.records: Dict[Tuple[str, str], StepRecord] = {
            (t.name, s.id): StepRecord(s.id, t.name, s.step.kind) for t in self.tracks for s in t.steps}
        for t in self.tracks:
            if t.generate is not None:
                self.records[(t.name, "generate")] = StepRecord("generate", t.name, "generate")
        self.state = "pending"                      # pending | running | done
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.prompts: Dict[str, str] = {}
        self.trace_path: Optional[str] = None
        #: The scenario's own trace (format v1, only its chargers: :mod:`..trace.recorder`) while it runs.
        self._trace: Optional[Any] = None
        self._bufs: Dict[str, _Buf] = {c: _Buf() for c in self._frame_chargers}
        self._payloads: Dict[int, Any] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._floors: Dict[str, Tuple[str, int]] = {}   # track -> (charger, lowest seq it may still search after)
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
            self.state, self._w0, self._s0 = "running", time.monotonic(), self.engine.clock.total_sim()
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
        return self.clock.total_sim() - self._s0

    def _wall(self) -> float:
        return time.monotonic() - self._w0

    def _target(self, spec: Tuple[str, Any], base: float = 0.0) -> float:
        """Sim seconds since the start at which ``spec`` (a parsed ``at`` / ``until``) falls; ``base`` = start of a
        relative one."""
        kind, v = spec
        if kind == "rel":
            return base + v
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
        if e.charger not in self.chargers:
            return
        buf = self._bufs.get(e.charger)
        if buf is None:
            return
        buf.seqs.append(e.seq)
        buf.entries.append(e)
        if len(buf.entries) > self.max_frames:
            cut = max(1, self.max_frames // 10)
            buf.dropped_upto = buf.seqs[cut - 1]
            self._drop(buf, cut)
        ev = self._events.pop(e.charger, None)
        if ev is not None:
            ev.set()

    def _drop(self, buf: _Buf, n: int) -> None:
        for x in buf.entries[:n]:
            self._payloads.pop(x.seq, None)
        del buf.seqs[:n], buf.entries[:n]

    def _trim(self, cid: str) -> None:
        """Drop the frames of ``cid`` no track can search any more (none for a track with ``from:
        scenario_start``)."""
        buf = self._bufs.get(cid)
        if buf is None or any(c == cid and name in self._keep_all for name, (c, _) in self._floors.items()):
            return
        floors = [v for c, v in self._floors.values() if c == cid]
        if not floors:
            return
        self._drop(buf, bisect.bisect_right(buf.seqs, min(floors)))

    def _payload(self, e: FrameEntry) -> Any:
        p = self._payloads.get(e.seq)
        if p is None:
            p = self._payloads[e.seq] = frame_payload(e.raw)
        return p

    def _find(self, charger: str, since: int, spec: FrameSpec) -> List[FrameEntry]:
        buf = self._bufs[charger]
        if since < buf.dropped_upto:
            raise _Overflow
        out = []
        for e in buf.entries[bisect.bisect_right(buf.seqs, since):]:
            if ((spec.dir and e.direction != spec.dir) or (spec.action and e.action != spec.action)
                    or (spec.message_type and e.message_type != spec.message_type)):
                continue
            if spec.payload and match_all(self._payload(e), spec.payload) is not None:
                continue
            out.append(e)
        return out

    def _reply(self, call: FrameEntry) -> Optional[FrameEntry]:
        buf = self._bufs[call.charger]
        for e in buf.entries[bisect.bisect_right(buf.seqs, call.seq):]:
            if e.uid == call.uid and e.direction != call.direction and e.message_type in _REPLIES:
                return e
        return None

    async def _until(self, deadline: Tuple[str, float], cid: str) -> bool:
        """Wait for a new frame of ``cid`` or the next poll; False once ``deadline`` (``("wall"|"sim", t)``) has
        passed."""
        kind, t = deadline
        now = self._wall() if kind == "wall" else self._sim()
        if now >= t:
            return False
        left = t - now if kind == "wall" else self.clock.to_wall(t - now)
        ev = self._events.get(cid)
        if ev is None:
            ev = self._events[cid] = asyncio.Event()
        waiter = asyncio.ensure_future(ev.wait())   # asyncio.wait: a racing cancellation is never swallowed
        try:
            await asyncio.wait({waiter}, timeout=min(POLL_S, max(left, 0.001)))
        finally:
            waiter.cancel()
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
                    if not rec.evidence:
                        why, rec.evidence = f"no answer yet to seq {e.seq}", [e.seq]
                    continue
                if then.message_type and r.message_type != then.message_type:
                    why, rec.evidence = f"answer seq {r.seq} is a {r.message_type}", [e.seq, r.seq]
                    continue
                miss = match_all(self._payload(r), then.payload) if then.payload else None
                if miss is None:
                    rec.evidence = [e.seq, r.seq]
                    rec.message = f"{what}: seq {e.seq}, answer seq {r.seq}"
                    return True
                why, rec.evidence = f"answer seq {r.seq}: {miss}", [e.seq, r.seq]
            if not await self._until(deadline, cid):
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
            if not await self._until(deadline, cid):
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
            if not await self._until(deadline, cid):
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

    async def _generate(self, tr: PlannedTrack) -> None:
        rec = self.records[(tr.name, "generate")]
        rec.status, rec.t_wall, rec.t_sim = "running", round(self._wall(), 3), round(self._sim(), 3)
        await self.generators[tr.group].run_charger(tr.charger)

    async def _track(self, tr: PlannedTrack) -> None:
        cursor = action_mark = self._seq0
        self._floors[tr.name] = (tr.charger, cursor)
        round_no, round_start = -1, self._sim()
        for ps in tr.steps:
            rec = self.records[(tr.name, ps.id)]
            s = ps.step
            if ps.round != round_no:
                round_no, round_start = ps.round, self._sim()
            if ps.at is not None:
                await self._sleep_until(self._target(ps.at, round_start))
            elif s.after:
                await self.clock.sleep_sim(s.after)
            rec.status, rec.t_wall, rec.t_sim = "running", round(self._wall(), 3), round(self._sim(), 3)
            since = {"action": action_mark, "scenario_start": self._seq0}.get(s.from_ or "prev_step", cursor)
            try:
                if s.do is not None:
                    if s.do not in ("wait", "log"):
                        action_mark = self.engine.frame_log.last_seq
                        cursor = max(cursor, action_mark)
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
                if ok and s.do is None and s.expect_not is None and rec.evidence:
                    cursor = max(cursor, *rec.evidence)
                rec.status = "pass" if ok else "fail"
            except ops.OpError as e:
                rec.status, rec.message = "error", f"{e.kind}: {e}"
            except _Overflow:
                rec.status, rec.message = "fail", f"frame buffer overflowed (more than {self.max_frames} frames)"
            self._floors[tr.name] = (tr.charger, min(cursor, action_mark))
            self._trim(tr.charger)
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
        cancelled = False
        try:
            if self.trace_path:
                self._trace = await eng.open_recorder(self.trace_path, sorted(self.chargers))
            if sc.clock.time_scale is not None:
                self._scale_before = eng.time_scale
                eng.set_running(time_scale=sc.clock.time_scale)
            if sc.clock.start_at is not None:
                eng.clock.reset(sc.clock.start_at)
                if not self.standing:
                    await eng.traffic.restart("the scenario reset the sim clock")   # plan hours follow the clock
            if not self.standing:
                await eng.traffic.preempt(self.chargers)   # Q3: the scenario owns its chargers
            self._w0, self._s0 = time.monotonic(), eng.clock.total_sim()
            self._ref = eng.clock.now_sim()
            self._seq0 = eng.frame_log.last_seq
            for g in self.generators.values():
                g.begin(self._ref)
            if self.standing:
                seed = next(iter(self.generators.values())).t.seed if self.generators else None
                eng.log_event("traffic", f"traffic started on {len(self.tracks)} charger(s), seed {seed}")
            else:
                eng.log_event("scenario", f"scenario {sc.name} started ({len(self.tracks)} track(s))")
            tasks = [asyncio.ensure_future(self._generate(t) if t.generate is not None else self._track(t))
                     for t in self.tracks]
            if sc.until:
                watch.append(asyncio.ensure_future(self._sleep_until(self._target(parse_time(sc.until)))))
            pending = set(tasks) | set(watch)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if any(t in watch for t in done):
                    self._end_reason = "until"
                    break
                if any(not t.cancelled() and isinstance(t.exception(), _Stop) for t in done):
                    self._end_reason = "stop"
                    break
                broken = next((t for t in done if not t.cancelled() and t.exception() is not None), None)
                if broken is not None:
                    raise broken.exception()
                if not (pending - set(watch)):
                    break
        except asyncio.CancelledError:
            cancelled = not self._aborted
        except ScenarioError as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001 - an internal error is a verdict "error", not a crash
            log.exception("scenario %s failed", sc.name)
            self.error = f"{type(e).__name__}: {e}"
        finally:
            for t in tasks + watch:
                t.cancel()
            try:
                await asyncio.gather(*tasks, *watch, return_exceptions=True)
            except asyncio.CancelledError:   # cancelled again while cleaning up: finish the cleanup first
                cancelled = True
            for g in self.generators.values():
                try:
                    await g.close()
                except asyncio.CancelledError:
                    cancelled = True
            if self._trace is not None:
                rec, self._trace = self._trace, None
                try:
                    await eng.close_recorder(rec)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as e:  # noqa: BLE001 - the verdict still comes out
                    log.warning("scenario %s: closing the trace failed: %r", sc.name, e)
            self._cleanup()
        if cancelled:
            raise asyncio.CancelledError
        return self.verdict()

    def _cleanup(self) -> None:
        eng, sc = self.engine, self.scenario
        if self._on_frame in eng.frame_listeners:
            eng.frame_listeners.remove(self._on_frame)
        if self._trace is not None:   # not closed by run() (normally it is): stop feeding it
            if self._trace in eng.recorders:
                eng.recorders.remove(self._trace)
            self._trace.stop()
            self._trace = None
        if self._scale_before is not None and eng.time_scale == sc.clock.time_scale:
            eng.set_running(time_scale=self._scale_before)   # not when someone changed the speed meanwhile
        self._finish()

    def _finish(self) -> None:
        if self.state == "done":
            return
        self._wall_s, self._sim_s = self._wall(), self._sim()
        not_run = 0
        gens = {t.name: self.generators[t.group] for t in self.tracks if t.generate is not None}
        for (track, _sid), r in self.records.items():
            if r.kind == "generate":
                r.status = "aborted" if self._aborted and not self.standing else "pass"
                r.message = _short(asdict(gens[track].stats))
            elif r.status == "running":
                if self._aborted:
                    r.status, r.message = "aborted", "scenario aborted"
                elif self._end_reason == "until":
                    r.status, r.message = "fail", "scenario until reached before the step finished"
                else:
                    r.status, r.message = "skipped", "scenario stopped"
            elif r.status == "pending":
                r.status, not_run = "skipped", not_run + 1
                if self._end_reason == "until":
                    r.message = "scenario until reached before the step started"
        if self._end_reason == "until" and not_run:
            log.info("scenario %s: until reached, %d step(s) not run", self.scenario.name, not_run)
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
        self._bufs.clear()
        self._payloads.clear()
        if self.standing:
            self.engine.log_event("traffic", "traffic stopped")
            return
        self.engine.log_event("scenario", f"scenario {self.scenario.name}: {self.result}"
                              + (f" (until reached, {not_run} step(s) not run)" if self._end_reason == "until"
                                 and not_run else ""))

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
                "sim_s": round(max(0.0, self._sim()) if running else self._sim_s, 3),
                "sim_time": self.clock.now_sim().isoformat() if running else None,
                "steps": self._counts(), "tracks": tracks,
                "traffic": [g.status() for g in self.generators.values()],
                "prompts": [{"track": k, "prompt": v} for k, v in self.prompts.items()]}

    def verdict(self) -> Dict[str, Any]:
        return {"format": VERDICT_FORMAT, "scenario": self.scenario.name, "result": self.result,
                "error": self.error, "seed": self.scenario.seed,
                "started": self._started_at.isoformat() if self._started_at else None,
                "wall_s": round(self._wall_s, 3), "sim_s": round(self._sim_s, 3), "tool_version": __version__,
                "trace": self.trace_path, "steps": [asdict(r) for r in self.records.values()]}
