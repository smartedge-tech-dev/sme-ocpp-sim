"""Traffic generator (v0.3 M4): plan determinism, id tag strategies, arrival statistics, vehicle seed wiring,
scenario-over-traffic priority and an accelerated multi-hour run against the ``tests/peers`` CSMS."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201

from sme_ocpp_sim.config import SimConfig, TrafficConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.scenario.model import ScenarioError, load_data
from sme_ocpp_sim.scenario.runner import ScenarioRunner
from sme_ocpp_sim.scenario.traffic import TrafficPlan
from sme_ocpp_sim.web import create_app

T0 = datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)


def _cfg(n: int = 3, traffic=None, url: str = "ws://127.0.0.1:9/ocpp", version: str = "1.6", **sim) -> SimConfig:
    return SimConfig.model_validate({
        "simulation": sim or {},
        "csms_defaults": {"url": url, "ocpp_version": version,
                          "reconnect": {"min_s": 0.05, "max_s": 0.2, "jitter": 0}},
        "chargers": [{"id": f"CP{i}", "id_tags": [f"T{i}a", f"T{i}b"]} for i in range(1, n + 1)],
        "traffic": traffic or {}})


def _plan(cfg: SimConfig, hours: float = 24, conns=None, start=T0):
    ids = cfg.traffic_chargers(cfg.traffic)
    plan = TrafficPlan(cfg.traffic, cfg, conns or {c: 1 for c in ids}, start)
    return [a.to_dict() for a in plan.preview(hours * 3600)[0]]


# ------------------------------------------------------------------ plan

def test_plan_is_deterministic_per_seed_and_per_charger():
    t = {"seed": 7, "probability": 0.3, "interval_s": 600}
    a, b = _plan(_cfg(3, t)), _plan(_cfg(3, t))
    assert a == b and len(a) > 20
    assert _plan(_cfg(3, {**t, "seed": 8})) != a
    # Adding a charger leaves the plans of the others unchanged (own random streams).
    more = _plan(_cfg(4, t))
    assert [x for x in more if x["charger"] != "CP4"] == a
    # Vehicles come from the pool, durations / idle times from the ranges.
    assert {x["ev_model"] for x in a} > {"compact-40kwh"}
    assert all(1800 <= x["duration_s"] <= 7200 and 0 <= x["idle_s"] <= 900 for x in a)


def test_vehicles_narrow_the_pool():
    got = _plan(_cfg(2, {"seed": 1, "probability": 0.5, "vehicles": {"kei-20kwh": 1, "suv-90kwh": 0}}))
    assert got and {x["ev_model"] for x in got} == {"kei-20kwh"}
    with pytest.raises(ValueError, match="unknown vehicle"):
        _cfg(1, {"vehicles": {"nope": 1}})


def test_arrival_probability_and_hourly_weights():
    cfg = _cfg(1, {"seed": 3, "probability": 0.3, "interval_s": 60})
    n_checks = 24 * 60 * 7
    got = _plan(cfg, hours=24 * 7)
    assert abs(len(got) / n_checks - 0.3) < 0.02
    assert all(x["t_s"] % 60 == 0 for x in got)
    weights = [0.0] * 12 + [1.0] * 12          # nothing before noon (sim clock hour)
    got = _plan(_cfg(1, {"seed": 3, "probability": 0.3, "interval_s": 60, "hourly_weights": weights}), hours=48)
    hours = {int(x["t_s"] // 3600) % 24 for x in got}
    assert got and all(h >= 12 for h in hours)
    with pytest.raises(ValueError):
        _cfg(1, {"hourly_weights": [1.0] * 23})


def test_id_tag_strategies():
    base = {"seed": 5, "probability": 0.4, "interval_s": 300, "id_tags": ["A", "B", "C"]}
    rr = _plan(_cfg(3, {**base, "id_tag_strategy": "round_robin"}))
    assert [x["id_tag"] for x in rr] == [["A", "B", "C"][i % 3] for i in range(len(rr))]
    rnd = _plan(_cfg(3, {**base, "id_tag_strategy": "random"}), hours=72)
    counts = {tag: sum(x["id_tag"] == tag for x in rnd) for tag in "ABC"}
    assert min(counts.values()) > len(rnd) / 3 * 0.7
    aff = _plan(_cfg(3, {**base, "id_tag_strategy": "affinity", "affinity_prob": 1.0,
                         "affinity_map": {"A": ["CP1"], "B": ["CP2"], "C": ["CP3"]}}))
    assert all(x["id_tag"] == {"CP1": "A", "CP2": "B", "CP3": "C"}[x["charger"]] for x in aff)
    half = _plan(_cfg(3, {**base, "id_tag_strategy": "affinity", "affinity_prob": 0.5,
                          "affinity_map": {"A": ["CP1"]}}), hours=96)
    cp1 = [x["id_tag"] for x in half if x["charger"] == "CP1"]
    assert 0.55 < cp1.count("A") / len(cp1) < 0.8      # 0.5 + 0.5 / 3
    # Default tag pool: the charger's own id_tags; default affinity binding: from the seed, stable.
    own = _plan(_cfg(2, {"seed": 5, "probability": 0.4, "id_tag_strategy": "affinity"}))
    assert all(x["id_tag"].startswith(f"T{x['charger'][2:]}") for x in own)
    assert own == _plan(_cfg(2, {"seed": 5, "probability": 0.4, "id_tag_strategy": "affinity"}))


def test_config_checks():
    with pytest.raises(ValueError, match="unknown charger"):
        _cfg(1, {"chargers": ["CPX"]})
    cfg = SimConfig.model_validate({"csms_defaults": {"url": "ws://x"}, "chargers": [{"id": "A"}],
                                    "traffic": {"enabled": False}})
    with pytest.raises(ValueError, match="no id_tags"):
        cfg.check_traffic(cfg.traffic)
    with pytest.raises(ValueError, match="no id_tags"):
        SimConfig.model_validate({"csms_defaults": {"url": "ws://x"}, "chargers": [{"id": "A"}],
                                  "traffic": {"enabled": True}})
    with pytest.raises(ValueError):
        TrafficConfig.model_validate({"duration_s": [10, 5]})


def test_vehicle_seed_follows_traffic_seed():
    async def go():
        eng = OcppEngine(_cfg(1, {"seed": 11}))
        rt = eng.chargers["CP1"]
        assert rt.vehicle_seed == 11
        await eng.reload(_cfg(1, {"seed": 12}))
        assert eng.chargers["CP1"] is rt and rt.vehicle_seed == 12   # applied live, no restart
    asyncio.run(go())


# ------------------------------------------------------------------ HTTP / preview

def test_http_traffic_routes_and_preview():
    eng = OcppEngine(_cfg(2, {"seed": 4, "probability": 0.5}))
    with TestClient(create_app(eng, mcp=False)) as c:
        st = c.get("/api/traffic").json()
        assert st["state"] == "stopped" and st["config"]["seed"] == 4
        q = {"hours": 6, "start": "2026-12-01T00:00:00+00:00"}
        p1, p2 = c.get("/api/traffic/plan", params=q).json(), c.get("/api/traffic/plan", params=q).json()
        assert p1 == p2 and p1["count"] > 0 and set(p1["per_charger"]) == {"CP1", "CP2"}
        assert p1["arrivals"][0]["at"].startswith("2026-12-01")
        one = c.get("/api/traffic/plan", params={**q, "charger": "CP2"}).json()
        assert one["arrivals"] == [a for a in p1["arrivals"] if a["charger"] == "CP2"]
        assert c.get("/api/traffic/plan", params={"charger": "NOPE"}).status_code == 404
        r = c.put("/api/traffic", json={"seed": 9, "probability": 0.1, "id_tag_strategy": "round_robin"})
        assert r.status_code == 200 and eng.config.traffic.seed == 9
        assert c.put("/api/traffic", json={"chargers": ["NOPE"]}).status_code == 400
        assert c.post("/api/traffic/stop").status_code == 409
        r = c.post("/api/traffic/start")
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert c.post("/api/traffic/stop").status_code == 200
        assert c.get("/api/traffic").json()["state"] == "stopped"


# ------------------------------------------------------------------ against the peers

async def _with_peer(version: str, fn, n: int = 2, traffic=None):
    peer = CsmsPeer() if version == "1.6" else CsmsPeer201()
    await peer.start()
    eng = OcppEngine(_cfg(n, traffic, url=peer.url, version=version, time_scale=3600))
    eng.stop_grace_s = 0.2
    await eng.start()
    try:
        for _ in range(200):
            if all(rt.online for rt in eng.chargers.values()):
                break
            await asyncio.sleep(0.05)
        return await fn(eng)
    finally:
        await eng.stop()
        await peer.stop()


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_accelerated_multi_hour_standing_traffic(version):
    traffic = {"enabled": True, "seed": 2, "interval_s": 300, "probability": 0.5, "duration_s": [600, 1800],
               "idle_after_s": [0, 300], "stop_after": "first"}

    async def fn(eng):
        tr = eng.traffic
        assert tr.running
        await asyncio.sleep(7)          # ~7 h of sim time at time_scale 3600
        st = tr.status()
        assert st["sim_s"] > 5 * 3600
        assert not tr.runner._bufs      # no frame buffer for generator tracks (bounded memory)
        return st

    st = asyncio.run(_with_peer(version, fn, traffic=traffic))
    s = st["stats"]
    assert s["completed"] >= 6 and s["failed"] == 0 and s["rejected"] == 0, st
    # Every arrival is skipped or started, except sessions still waiting for their transaction.
    assert 0 <= s["arrivals"] - s["skipped"] - s["started"] <= st["active"]
    assert s["started"] - s["completed"] <= st["active"]
    assert {e["event"] for e in st["recent"]} >= {"arrival", "started", "completed"}


def test_scenario_takes_priority_over_traffic():
    traffic = {"enabled": True, "seed": 1, "interval_s": 60, "probability": 1.0, "duration_s": [36000, 36000],
               "stop_by": "swipe"}

    async def fn(eng):
        tr = eng.traffic
        for _ in range(100):            # traffic sessions on both chargers
            if len(tr.runner.generators[0].active) == 2 and all(rt.has_transaction(1) for rt in eng.chargers.values()):
                break
            await asyncio.sleep(0.05)
        assert all(rt.has_transaction(1) for rt in eng.chargers.values())
        sc = load_data({"format": "ocppsim-scenario/1", "name": "own", "tracks": [{"charger": "CP1", "steps": [
            {"expect": {"state": {"status": "Available", "has_transaction": False}}, "within": 5},
            {"do": "start", "id_tag": "T1a"},
            {"do": "wait", "duration": "2h"},
            {"expect": {"state": {"has_transaction": True}}, "within": 5},
            {"do": "unplug"}]}]})
        runner = ScenarioRunner(eng, sc)
        eng.scenario = runner
        v = await asyncio.wait_for(runner.start(), 30)
        g = tr.runner.generators[0]
        assert v["result"] == "pass", v
        assert g.stats.preempted == 1 and eng.chargers["CP2"].has_transaction(1)   # CP2 kept its session
        assert any(e["charger"] == "CP1" and e.get("note") == "scenario" for e in g.recent)
        return v

    asyncio.run(_with_peer("1.6", fn, traffic=traffic))


def test_scenario_generate_track():
    async def fn(eng):
        sc = load_data({"format": "ocppsim-scenario/1", "name": "gen", "seed": 3, "until": "+3h", "tracks": [
            {"for_each": {"chargers": "*"},
             "generate": {"use": "traffic", "probability": 0.6, "duration_s": [600, 1200], "id_tags": ["X"]}}]})
        r = ScenarioRunner(eng, sc)
        assert r.generators[0].t.seed == 3 and r.generators[0].t.id_tags == ["X"]
        v = await asyncio.wait_for(r.run(), 30)
        assert v["result"] == "pass" and {s["kind"] for s in v["steps"]} == {"generate"}
        assert '"completed"' in v["steps"][0]["message"]
        assert not any(rt.has_transaction(1) for rt in eng.chargers.values())   # cleaned up at until
        with pytest.raises(ScenarioError, match="unknown vehicle"):
            ScenarioRunner(eng, load_data({"format": "ocppsim-scenario/1", "name": "x", "until": "+1h", "tracks": [
                {"charger": "CP1", "generate": {"vehicles": {"nope": 1}}}]}))

    asyncio.run(_with_peer("1.6", fn, traffic={"interval_s": 300}))
