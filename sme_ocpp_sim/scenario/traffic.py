"""Traffic generator (ATG, v0.3 plan §3.3, M4): seeded random charging sessions on idle connectors.

The ATG is a **generator track** of the one scenario runner (:mod:`.runner`): a scenario track with ``generate:``
instead of ``steps``, and the standing traffic of the config's ``traffic:`` block is a scenario of generator tracks
only, without ``until`` and without a verdict (:class:`StandingTraffic`). Its actions go through :mod:`.ops` like
every other caller.

- **Plan** (:class:`TrafficPlan`): every ``interval_s`` (sim) each connector of the group checks for an arrival
  with ``probability`` x ``hourly_weights[hour of the sim clock]``. An arrival brings a vehicle (drawn from the
  vehicle pool, ``traffic.vehicles`` narrowing it), an id tag (``id_tag_strategy``), a charging duration and an
  idle time (uniform in ``duration_s`` / ``idle_after_s``).
- **Reproducibility**: the same seed, config and start time give the same plan. Each connector has its own random
  streams (``{seed}:traffic:{charger}:{connector}`` for arrivals / durations / tags, the vehicle pool stream
  ``vehicle:{charger}:{connector}``), and every check takes the same random numbers whatever happens at run time,
  so adding a charger does not change the others' plans (except ``round_robin``, which counts arrivals across the
  group). The plan does not depend on the CSMS: an arrival on a connector that is not idle (a session still
  running, a fault, offline, a scenario on the charger) is **skipped**, not postponed. Frame-level timing is not
  part of the promise.
- **Session**: ``auth_order`` plug + swipe; charging until ``stop_after`` (``duration``, the vehicle's target SoC,
  or the first of both; a transaction that ends by itself, e.g. a CSMS RemoteStop, ends it too; ``max_session_s``
  caps it); then ``stop_by`` (``swipe`` with the same tag, ``unplug``, or ``remote_only``: wait for the CSMS to stop
  it, a local stop after ``max_session_s``); then the car stays plugged ``idle_after_s`` and leaves (unplug). With
  ``stop_by: unplug`` the transaction lasts until the car leaves.
- **Priority**: a running scenario owns its chargers; standing traffic skips arrivals there and stops its own
  sessions on them (stop + unplug) before the scenario starts (Q3).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from ..charging.ev import Vehicle
from ..charging.vehicles import stream_rng
from ..config import SimConfig, TrafficConfig
from . import ops

log = logging.getLogger("sme_ocpp_sim.traffic")

#: **Wall** seconds a session waits for its transaction to start after plug + swipe (CSMS answer time).
TX_START_TIMEOUT_S = 30.0
#: Shortest wall interval between the state checks of a charging session.
POLL_WALL_S = 0.25
#: Sim seconds between the state checks of a charging session (at least).
POLL_SIM_S = 60.0
#: Wall seconds a session's cleanup (stop + unplug) may take when it is cancelled.
CLEANUP_S = 3.0
#: Recent events kept for the status.
RECENT = 50
#: Most arrivals a plan preview returns, and most ticks it computes.
MAX_PREVIEW = 10_000
MAX_PREVIEW_TICKS = 200_000


@dataclass
class Arrival:
    tick: int
    t_s: float                       # sim seconds since the traffic start
    charger: str
    connector: int
    id_tag: str
    duration_s: float
    idle_s: float
    vehicle: Vehicle

    def to_dict(self, start: Optional[datetime] = None) -> Dict[str, Any]:
        d = {"tick": self.tick, "t_s": self.t_s, "charger": self.charger, "connector": self.connector,
             "id_tag": self.id_tag, "duration_s": self.duration_s, "idle_s": self.idle_s,
             "ev_model": self.vehicle.ev.id, "initial_soc": self.vehicle.initial_soc_pct,
             "target_soc": self.vehicle.target_soc_pct}
        if start is not None:
            d["at"] = (start + timedelta(seconds=self.t_s)).isoformat()
        return d


def merged(config: SimConfig, generate: Dict[str, Any], seed: Optional[int] = None) -> TrafficConfig:
    """The traffic settings of a ``generate:`` block: ``use: traffic`` starts from the config's ``traffic:``
    block, else from the defaults; then the scenario ``seed``, then the block's own fields."""
    base = config.traffic.model_dump() if generate.get("use") == "traffic" else {}
    if seed is not None:
        base["seed"] = seed
    own = {k: v for k, v in generate.items() if k != "use"}
    return TrafficConfig.model_validate({**base, **own, "enabled": True, "chargers": "*"})


class TrafficPlan:
    """The arrivals of a group of chargers, made lazily tick by tick (memory: the arrivals not yet taken)."""

    def __init__(self, t: TrafficConfig, config: SimConfig, connectors: Dict[str, int], start: datetime) -> None:
        self.t, self.start = t, start
        self.chargers = list(connectors)
        cfgs = {c.id: c for c in config.chargers}
        self._conns = [(cid, n) for cid in self.chargers for n in range(1, connectors[cid] + 1)]
        self._rng = {k: random.Random(f"{t.seed}:traffic:{k[0]}:{k[1]}") for k in self._conns}
        self._vrng = {k: stream_rng(t.seed, *k) for k in self._conns}
        self._pools = {cid: config.traffic_pool(cfgs[cid], t) for cid in self.chargers}
        self._tags = {cid: list(t.id_tags or cfgs[cid].id_tags) for cid in self.chargers}
        self._homes = self._affinity(t, config)
        self._rr = 0
        self.tick = 0
        self._queue: Dict[str, Deque[Arrival]] = {cid: deque() for cid in self.chargers}

    def _affinity(self, t: TrafficConfig, config: SimConfig) -> Dict[str, List[str]]:
        """charger -> the tags that call it home (``affinity_map``, else one home charger per tag by seed)."""
        homes: Dict[str, List[str]] = {cid: [] for cid in self.chargers}
        if t.affinity_map is not None:
            for tag, cids in t.affinity_map.items():
                for cid in cids:
                    if cid in homes:
                        homes[cid].append(tag)
            return homes
        # Rendezvous hashing: a tag's home is the charger with the highest score, so adding a charger moves only
        # the tags it wins and removing one only the tags it had.
        def score(tag: str, cid: str) -> bytes:
            return hashlib.sha256(f"{t.seed}:affinity:{tag}:{cid}".encode()).digest()
        for tag in sorted({tag for tags in self._tags.values() for tag in tags}):
            homes[max(self.chargers, key=lambda cid: score(tag, cid))].append(tag)
        return homes

    def probability(self, t_s: float) -> float:
        p = self.t.probability
        if self.t.hourly_weights is not None:
            p *= self.t.hourly_weights[(self.start + timedelta(seconds=t_s)).hour]
        return min(p, 1.0)

    def _tag(self, cid: str, u1: float, u2: float) -> str:
        tags, s = self._tags[cid], self.t.id_tag_strategy
        if s == "round_robin":
            self._rr += 1
            return tags[(self._rr - 1) % len(tags)]
        if s == "affinity":
            home = [x for x in self._homes[cid] if x in tags]
            if home and u1 < self.t.affinity_prob:
                return home[int(u2 * len(home))]
        return tags[int(u2 * len(tags))]

    def _advance(self) -> List[Arrival]:
        self.tick += 1
        t_s = self.tick * self.t.interval_s
        p = self.probability(t_s)
        (d0, d1), (i0, i1) = self.t.duration_s, self.t.idle_after_s
        out = []
        for key in self._conns:
            rng = self._rng[key]
            if rng.random() >= p:
                continue
            # Every arrival takes the same four numbers, whatever the strategy (the stream never shifts).
            dur, idle, u1, u2 = rng.random(), rng.random(), rng.random(), rng.random()
            a = Arrival(self.tick, t_s, key[0], key[1], self._tag(key[0], u1, u2), round(d0 + (d1 - d0) * dur, 1),
                        round(i0 + (i1 - i0) * idle, 1), self._pools[key[0]].draw(self._vrng[key]))
            out.append(a)
        return out

    def take(self, cid: str, tick: int) -> List[Arrival]:
        """The arrivals of ``cid`` up to ``tick`` not taken yet."""
        while self.tick < tick:
            for a in self._advance():
                self._queue[a.charger].append(a)
        q, out = self._queue[cid], []
        while q and q[0].tick <= tick:
            out.append(q.popleft())
        return out

    def preview(self, until_s: float, limit: int = MAX_PREVIEW) -> Tuple[List[Arrival], bool]:
        """``(arrivals up to until_s, truncated)`` of a fresh plan (do not mix with :meth:`take`)."""
        out: List[Arrival] = []
        while (self.tick + 1) * self.t.interval_s <= until_s:
            if self.tick >= MAX_PREVIEW_TICKS:
                return out, True
            out.extend(self._advance())
            if len(out) > limit:
                return out[:limit], True
        return out, False


@dataclass
class Stats:
    arrivals: int = 0
    skipped: int = 0
    started: int = 0
    rejected: int = 0
    completed: int = 0
    aborted: int = 0                 # the transaction ended by itself (CSMS stop, reset, fault) before the stop
    failed: int = 0
    preempted: int = 0               # ended for a scenario on the charger
    cancelled: int = 0               # ended by a traffic stop / restart


class Generator:
    """One ``generate:`` block of a running scenario (a group of generator tracks, one per charger)."""

    def __init__(self, runner: Any, t: TrafficConfig, chargers: List[str], *, standing: bool = False) -> None:
        self.runner, self.engine, self.t = runner, runner.engine, t
        self.chargers = chargers
        self.standing = standing
        self.plan: Optional[TrafficPlan] = None
        self.stats = Stats()
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=RECENT)
        self.active: Dict[Tuple[str, int], asyncio.Task] = {}
        self.cleanup = True
        self._why = "cancelled"          # counter of the sessions :meth:`close` cancels

    def begin(self, start: datetime) -> None:
        conns = {cid: ops.runtime(self.engine, cid).connector_count for cid in self.chargers}
        self.plan = TrafficPlan(self.t, self.runner.config, conns, start)

    def _event(self, a: Arrival, what: str, note: str = "") -> None:
        self.recent.append({"t_s": a.t_s, "charger": a.charger, "connector": a.connector, "event": what,
                            "id_tag": a.id_tag, "ev_model": a.vehicle.ev.id, **({"note": note} if note else {})})

    def paused(self, cid: str) -> bool:
        """Standing traffic yields to a running scenario on the same charger."""
        sc = self.engine.scenario
        return self.standing and sc is not None and sc is not self.runner and sc.state == "running" \
            and cid in sc.chargers

    def _idle(self, cid: str, c: int) -> Optional[str]:
        """``None`` when ``cid`` / ``c`` can take a car, else why not."""
        if (cid, c) in self.active:
            return "busy"
        if self.paused(cid):
            return "scenario"
        rt = self.engine.chargers.get(cid)
        if rt is None or not rt.online:
            return "offline"
        if rt.faulted(c) or rt.has_transaction(c):
            return "not available"
        conn = next((x for x in rt.station.snapshot().get("connectors", []) if x.get("id") == c), {})
        return None if conn.get("status") == "Available" else f"status {conn.get('status')}"

    async def run_charger(self, cid: str) -> None:
        """The generator track of ``cid``: check for arrivals every ``interval_s`` until cancelled."""
        assert self.plan is not None
        tick = 0
        while True:
            tick += 1
            await self.runner._sleep_until(tick * self.t.interval_s)
            await asyncio.sleep(0)       # behind schedule (long pause, slow loop): still let others run
            for a in self.plan.take(cid, tick):
                self.stats.arrivals += 1
                why = self._idle(cid, a.connector)
                if why is not None:
                    self.stats.skipped += 1
                    self._event(a, "skipped", why)
                    continue
                self._event(a, "arrival")
                task = asyncio.ensure_future(self._session(a))
                self.active[(cid, a.connector)] = task
                task.add_done_callback(lambda _t, k=(cid, a.connector): self.active.pop(k, None))

    # ------------------------------------------------------------ one session

    async def _poll(self, step_sim: float) -> None:
        scale = max(self.engine.clock.to_wall(1.0), 1e-9)   # wall s per sim s
        await self.engine.clock.sleep_sim(min(step_sim, max(POLL_SIM_S, POLL_WALL_S / scale)))

    def _soc(self, rt, c: int) -> Optional[float]:
        conn = next((x for x in rt.station.snapshot().get("connectors", []) if x.get("id") == c), {})
        return conn.get("soc_pct")

    async def _session(self, a: Arrival) -> None:
        eng, cid, c, t = self.engine, a.charger, a.connector, self.t
        rt = ops.runtime(eng, cid)
        try:
            rt.next_vehicle[c] = a.vehicle
            if t.auth_order == "plug_first":
                await ops.plug(eng, cid, c)
                r = await ops.swipe(eng, cid, c, a.id_tag)
            else:
                r = await ops.swipe(eng, cid, c, a.id_tag)
                await ops.plug(eng, cid, c)
            if r.get("status") != "Accepted":
                self.stats.rejected += 1
                self._event(a, "rejected", str(r.get("status")))
                rt.next_vehicle.pop(c, None)
                await self._clear(rt, cid, c)   # best effort: never counted twice
                return
            w0 = time.monotonic()
            while not rt.has_transaction(c):
                if time.monotonic() - w0 > TX_START_TIMEOUT_S:
                    raise ops.OpError("timeout", "no transaction started")
                await asyncio.sleep(0.05)
            self.stats.started += 1
            self._event(a, "started")
            s0 = eng.clock.total_sim()
            limit = t.max_session_s
            if t.stop_by != "remote_only" and t.stop_after in ("duration", "first"):
                limit = min(limit, a.duration_s)
            want_soc = t.stop_by != "remote_only" and t.stop_after in ("soc_target", "first")
            while rt.has_transaction(c):
                if eng.chargers.get(cid) is not rt:
                    raise ops.OpError("conflict", "the charger was restarted by a config change")
                left = limit - (eng.clock.total_sim() - s0)
                if left <= 0:
                    break
                if want_soc and (self._soc(rt, c) or 0) >= a.vehicle.target_soc_pct - 0.01:
                    break
                await self._poll(left)
            ended = not rt.has_transaction(c) and t.stop_by != "remote_only"
            if rt.has_transaction(c) and t.stop_by != "unplug":
                if t.stop_by == "swipe":
                    await ops.swipe(eng, cid, c, a.id_tag)
                else:
                    await ops.stop(eng, cid, c)
            if a.idle_s > 0:
                await eng.clock.sleep_sim(a.idle_s)
            if eng.chargers.get(cid) is not rt:
                raise ops.OpError("conflict", "the charger was restarted by a config change")
            await ops.unplug(eng, cid, c)
            if ended:
                self.stats.aborted += 1
                self._event(a, "aborted", "the transaction ended before the stop condition")
            else:
                self.stats.completed += 1
                self._event(a, "completed")
        except asyncio.CancelledError:
            setattr(self.stats, self._why, getattr(self.stats, self._why) + 1)
            self._event(a, self._why)
            if self.cleanup:
                await self._clear(rt, cid, c)
            raise
        except Exception as e:  # noqa: BLE001 - one broken session must not stop the generator
            self.stats.failed += 1
            msg = f"{e.kind}: {e}" if isinstance(e, ops.OpError) else f"{type(e).__name__}: {e}"
            self._event(a, "failed", msg)
            if isinstance(e, ops.OpError):
                log.info("traffic %s/%s: %s", cid, c, msg)
            else:
                log.exception("traffic %s/%s session failed", cid, c)
            await self._clear(rt, cid, c)
        finally:
            rt.next_vehicle.pop(c, None)

    async def _clear(self, rt, cid: str, c: int) -> None:
        """Best effort: end our transaction and unplug (a cancelled / failed / preempted session); nothing when
        the charger has been rebuilt meanwhile (never touch the new runtime)."""
        if self.engine.chargers.get(cid) is not rt:
            return

        async def go() -> None:
            if rt.has_transaction(c):
                await ops.stop(self.engine, cid, c)
            await ops.unplug(self.engine, cid, c)
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(go(), CLEANUP_S)

    async def close(self, chargers: Optional[Iterable[str]] = None, *, cleanup: Optional[bool] = None,
                    why: str = "cancelled") -> int:
        """Cancel the sessions (of ``chargers``, default all; counted as ``why``) and wait for them; their count."""
        if cleanup is not None:
            self.cleanup = cleanup
        self._why = why
        want = None if chargers is None else set(chargers)
        tasks = [task for (cid, _), task in list(self.active.items()) if want is None or cid in want]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._why = "cancelled"
        return len(tasks)

    def power_w(self) -> float:
        total = 0.0
        for cid in self.chargers:
            rt = self.engine.chargers.get(cid)
            if rt is not None:
                total += sum(x.get("power_w") or 0 for x in rt.station.snapshot().get("connectors", []))
        return round(total, 1)

    def status(self) -> Dict[str, Any]:
        return {"seed": self.t.seed, "chargers": list(self.chargers), "stats": asdict(self.stats),
                "active": len(self.active), "power_w": self.power_w(), "recent": list(self.recent)}


# ------------------------------------------------------------------ standing traffic

def _generate_block(t: TrafficConfig) -> Dict[str, Any]:
    return {k: v for k, v in t.model_dump(mode="json").items() if k not in ("enabled", "chargers")}


class StandingTraffic:
    """The config's ``traffic:`` block, run as a scenario of generator tracks only (``engine.traffic``).

    Started with the engine when ``traffic.enabled``, or by :meth:`start` (``POST /api/traffic/start``, not
    saved). A config reload that changes ``traffic:`` or the charger list restarts it (a new plan from the
    reload time); toggling ``enabled`` starts / stops it."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.runner: Optional[Any] = None
        self.error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.runner is not None and not self.runner.done

    def _scenario(self, config: SimConfig, chargers: List[str]):
        from .model import load_data
        return load_data({"format": "ocppsim-scenario/1", "name": "traffic", "seed": config.traffic.seed,
                          "tracks": [{"name": "traffic", "for_each": {"chargers": chargers},
                                      "generate": _generate_block(config.traffic)}]}, standing=True)

    def start(self, config: Optional[SimConfig] = None) -> Dict[str, Any]:
        """Start it (no-op while running); ``ValueError`` when the config cannot run it."""
        from .runner import ScenarioRunner
        if self.running:
            return self.status()
        config = config or self.engine.config
        chargers = config.traffic_chargers(config.traffic)
        if not chargers:
            raise ValueError("traffic: no chargers")
        config.check_traffic(config.traffic)
        self.error = None
        self.runner = ScenarioRunner(self.engine, self._scenario(config, chargers), standing=True, config=config)
        self.runner.start().add_done_callback(lambda _t, r=self.runner: self._ended(r))
        return self.status()

    def _ended(self, r: Any) -> None:
        if r.error is not None and r is self.runner:
            self.error = r.error
            self.engine.log_event("error", f"traffic stopped: {r.error}")

    def start_quiet(self, config: Optional[SimConfig] = None) -> None:
        """:meth:`start` for the engine / a reload: a config that cannot run (no chargers, no tag pool) leaves it
        stopped with :attr:`error` set and a log event, instead of raising."""
        try:
            self.start(config)
        except ValueError as e:
            self.error = str(e)
            self.engine.log_event("warning", f"traffic not started: {e}")

    async def restart(self, why: str) -> None:
        """Stop (sessions stopped and unplugged) and start again with a new plan from now, if running."""
        if self.running:
            await self.stop()
            self.engine.log_event("traffic", f"traffic restarted: {why}")
            self.start_quiet()

    async def stop(self, *, cleanup: bool = True) -> bool:
        r = self.runner
        if r is None or r.done:
            return False
        for g in r.generators.values():
            g.cleanup = cleanup
            g._why = "cancelled"
        r.abort()
        with contextlib.suppress(Exception):
            await r.wait()
        return True

    async def preempt(self, chargers: Iterable[str]) -> None:
        """A scenario takes ``chargers``: end our sessions there first."""
        r = self.runner
        if r is None or r.done:
            return
        for g in r.generators.values():
            await g.close(chargers, cleanup=True, why="preempted")

    async def reconfigure(self, old: SimConfig, new: SimConfig, rebuilt: Iterable[str] = ()) -> None:
        """After a reload: restart when ``traffic:``, the charger list or a target charger (rebuilt runtime:
        connectors, pool, id tags ...) changed. Toggling ``enabled`` starts / stops it; otherwise it keeps its run
        state (also one set by :meth:`start` / :meth:`stop`)."""
        ids = lambda c: [x.id for x in c.chargers]  # noqa: E731
        hit = set(rebuilt) & set(new.traffic_chargers(new.traffic))
        if old.traffic == new.traffic and ids(old) == ids(new) and not hit:
            return
        was = self.running
        await self.stop()
        if new.traffic.enabled if old.traffic.enabled != new.traffic.enabled else was:
            self.start_quiet(new)

    def preview(self, hours: float = 1.0, charger: Optional[str] = None, limit: int = 1000,
                start: Optional[datetime] = None) -> Dict[str, Any]:
        """The plan of the config's ``traffic:`` block for the next ``hours`` (sim) from ``start`` (default: the
        start of the running traffic, so the preview is its plan, else the sim clock now)."""
        config = self.engine.config
        t = config.traffic
        chargers = config.traffic_chargers(t)
        config.check_traffic(t)
        if start is None:
            start = self.runner._ref if self.running and self.runner._ref is not None else \
                self.engine.clock.now_sim()
        conns = {cid: ops.runtime(self.engine, cid).connector_count for cid in chargers}
        plan = TrafficPlan(t, config, conns, start)
        arrivals, truncated = plan.preview(hours * 3600.0, MAX_PREVIEW)
        if charger is not None:
            arrivals = [a for a in arrivals if a.charger == charger]
        cut = len(arrivals) > limit
        per: Dict[str, int] = {}
        for a in arrivals:
            per[a.charger] = per.get(a.charger, 0) + 1
        return {"seed": t.seed, "start": start.isoformat(), "hours": hours, "interval_s": t.interval_s,
                "count": len(arrivals), "per_charger": per, "truncated": truncated or cut,
                "arrivals": [a.to_dict(start) for a in arrivals[:limit]]}

    def brief(self) -> Dict[str, Any]:
        """Active sessions and total power (the UI indicator, in the stats push)."""
        gens = list(self.runner.generators.values()) if self.runner is not None else []
        return {"active": sum(len(g.active) for g in gens), "power_w": sum(g.power_w() for g in gens)}

    def status(self) -> Dict[str, Any]:
        r = self.runner
        t = self.engine.config.traffic
        out: Dict[str, Any] = {"state": "running" if self.running else "stopped", "enabled": t.enabled,
                               "error": self.error, "config": t.model_dump(mode="json")}
        if r is not None:
            st = r.status()
            out.update({"started": st["started"], "sim_s": st["sim_s"], "sim_time": st["sim_time"]})
            gens = [g.status() for g in r.generators.values()]
            if gens:
                out.update(gens[0])
        return out
