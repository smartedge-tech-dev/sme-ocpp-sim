"""Scenario runner and verdict (v0.3 M3): model / loader, expansion, matchers, the runner on accelerated sim
time against the independent ``tests/peers`` CSMS (1.6 / 2.0.1), the HTTP API, JUnit and the headless CLI."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201

from sme_ocpp_sim.__main__ import main
from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.scenario import builtin, ops
from sme_ocpp_sim.scenario.match import check_matcher, get_path, is_missing, match_all, matches, MatcherError
from sme_ocpp_sim.scenario.model import ScenarioError, expand, load_data, load_text, parse_duration
from sme_ocpp_sim.scenario.runner import ScenarioRunner
from sme_ocpp_sim.scenario.verdict import exit_code, to_junit
from sme_ocpp_sim.web import create_app

REPO = Path(__file__).resolve().parents[1]
DEAD_URL = "ws://127.0.0.1:9/ocpp"


def _sc(tracks, **kw) -> dict:
    return {"format": "ocppsim-scenario/1", "name": "t", "tracks": tracks, **kw}


def _err(data) -> str:
    with pytest.raises(ScenarioError) as e:
        load_data(data) if isinstance(data, dict) else load_text(data)
    return str(e.value)


# ------------------------------------------------------------------ model

def test_model_errors_name_the_position():
    assert "tracks.0.steps.1" in _err(_sc([{"charger": "A", "steps": [{"do": "plug"}, {"do": "jump"}]}]))
    assert "bogus" in _err(_sc([{"charger": "A", "steps": [{"do": "plug", "bogus": 1}]}]))
    assert "does not take ['id_tag']" in _err(_sc([{"charger": "A", "steps": [{"do": "plug", "id_tag": "X"}]}]))
    assert "needs ['id_tag']" in _err(_sc([{"charger": "A", "steps": [{"do": "swipe"}]}]))
    assert "exactly one of do" in _err(_sc([{"charger": "A", "steps": [{"do": "plug", "expect_not": {}}]}]))
    assert "at or after" in _err(_sc([{"charger": "A", "steps": [{"do": "plug", "at": "+1h", "after": 3}]}]))
    assert "belong to do steps" in _err(_sc([{"charger": "A", "steps": [
        {"expect": {"state": {"online": True}}, "id_tag": "x"}]}]))
    assert "unknown matcher operator" in _err(_sc([{"charger": "A", "steps": [
        {"expect": {"frame": {"payload": {"a": {"eq": 1}}}}}]}]))
    assert "takes no steps" in _err(_sc([{"charger": "A", "generate": {}, "steps": [{"do": "plug"}]}]))
    assert "until: required" in _err(_sc([{"charger": "A", "generate": {"probability": 0.5}}]))
    assert "generate: probability" in _err(_sc([{"charger": "A", "generate": {"probability": 2}}], until="+1h"))
    assert "not allowed" in _err(_sc([{"charger": "A", "generate": {"chargers": "*"}}], until="+1h"))
    assert "format" in _err({"format": "x", "name": "n", "tracks": []})
    assert "YAML" in _err("format: [")
    assert "charger / for_each" in _err(_sc([{"steps": [{"do": "plug"}]}]))
    assert "within (wall) or within_sim" in _err(_sc([{"charger": "A", "steps": [
        {"expect_not": {"action": "X"}, "within": 1, "within_sim": 2}]}]))


def test_durations_and_times():
    assert parse_duration("1h30m") == 5400 and parse_duration("500ms") == 0.5 and parse_duration(7) == 7.0
    assert parse_duration("2.5") == 2.5
    for bad in ("5 parsecs", "-1", True, ""):
        with pytest.raises(ValueError):
            parse_duration(bad)
    # unquoted 8:00 / 8:00:30 is YAML 1.1 sexagesimal (480 / 28830): ambiguous, so it must be quoted
    assert load_text("format: ocppsim-scenario/1\nname: n\ntracks:\n- charger: A\n  steps:\n"
                     "  - {at: 08:00, do: plug}\n").tracks[0].steps[0].at == "08:00"   # a string in YAML
    for t in ("8:00", "8:00:30"):
        assert "quote clock times" in _err(f"format: ocppsim-scenario/1\nname: n\ntracks:\n- charger: A\n"
                                           f"  steps:\n  - {{at: {t}, do: plug}}\n")
    assert "HH:MM" in _err(_sc([{"charger": "A", "steps": [{"at": "noon", "do": "plug"}]}]))


def test_expand_for_each_repeat_and_params():
    sc = load_data(_sc([
        {"for_each": {"chargers": "*"}, "steps": [{"do": "swipe", "id_tag": "T-${i}-${charger}"}]},
        {"name": "r", "charger": "${cp}", "repeat": 2, "steps": [{"id": "s", "do": "plug", "connector": 1},
                                                                  {"do": "send", "action": "DataTransfer",
                                                                   "payload": {"n": "${n}", "t": "x${n}"}}]},
    ], params={"cp": "B", "n": 5}))
    tracks = expand(sc, ["A", "B"])
    assert [t.name for t in tracks] == ["track1:A", "track1:B", "r"]
    assert tracks[1].steps[0].step.id_tag == "T-1-B"
    assert [s.id for s in tracks[2].steps] == ["s#1", "2#1", "s#2", "2#2"]
    assert tracks[2].steps[1].step.payload == {"n": 5, "t": "x5"}  # a whole-string reference keeps its type
    with pytest.raises(ScenarioError, match="unknown charger 'B'"):
        expand(sc, ["A"])
    with pytest.raises(ScenarioError, match=r"tracks.0.steps.0.*\$\{nope\}"):
        expand(load_data(_sc([{"charger": "A", "steps": [{"do": "log", "message": "${nope}"}]}])), ["A"])


def test_example_scenarios_load():
    ids = [c["id"] for c in yaml.safe_load((REPO / "configs/mini-csms-demo.yaml").read_text("utf-8"))["chargers"]]
    files = sorted((REPO / "scenarios").glob("*.yaml"))
    assert len(files) >= 3
    for f in files:
        expand(load_text(f.read_text("utf-8")), ids)


# ------------------------------------------------------------------ matchers

def test_matchers():
    obj = {"a": {"b": [{"c": 3}, {"c": "12.5"}]}, "s": "Accepted", "t": True}
    assert get_path(obj, "a.b.1.c") == "12.5" and is_missing(get_path(obj, "a.x")) and is_missing(get_path(obj, "s.x"))
    assert matches(3, 3.0) and not matches(True, 1) and not matches(1, True)
    assert matches("12.5", {"gt": 12, "le": 12.5}) and not matches("abc", {"gt": 0})
    assert matches("Accepted", {"in": ["Accepted", "ConcurrentTx"]}) and matches("CP-7", {"re": r"^CP-\d$"})
    assert matches(get_path(obj, "nope"), {"exists": False}) and not matches(get_path(obj, "nope"), "x")
    assert match_all(obj, {"s": "Accepted", "a.b.0.c": {"ge": 3}}) is None
    assert "a.b.0.c: 3 does not match" in match_all(obj, {"a.b.0.c": {"lt": 3}})
    for bad in ({}, {"gt": "x"}, {"in": 1}, {"re": "("}, {"exists": 1}):
        with pytest.raises(MatcherError):
            check_matcher(bad)


# ------------------------------------------------------------------ runner against the peers

def _engine(url: str, version: str, **charger) -> OcppEngine:
    return OcppEngine(SimConfig.model_validate({
        "simulation": {"time_scale": 3600},
        "csms_defaults": {"url": url, "ocpp_version": version, "reconnect": {"min_s": 0.05, "max_s": 0.2,
                                                                              "jitter": 0}},
        "chargers": [{"id": "CP1", "id_tags": ["TAG1"], **charger}]}))


async def _with_peer(version: str, fn, **charger):
    peer = CsmsPeer() if version == "1.6" else CsmsPeer201()
    await peer.start()
    engine = _engine(peer.url, version, **charger)
    engine.stop_grace_s = 0.2
    await engine.start()
    try:
        return await fn(engine)
    finally:
        await engine.stop()
        await peer.stop()


def _run(version: str, data: dict, **charger) -> dict:
    async def fn(engine):
        r = ScenarioRunner(engine, load_data(data))
        return await asyncio.wait_for(r.run(), 30)
    return asyncio.run(_with_peer(version, fn, **charger))


# Sim-time steps may fire late, never early: a wake-up lateness of L wall seconds shows as L * time_scale sim
# seconds (0.14 s on a busy CI runner = 500 sim s at 3600x). Upper bounds allow 1 s of wall lateness at the
# engine's 3600x, still far below a mis-scheduling such as a day wrap (86400 s).
LATE_SIM = 3600 * 1.0


START = {"1.6": {"dir": "tx", "action": "StartTransaction"},
         "2.0.1": {"dir": "tx", "action": "TransactionEvent", "payload": {"eventType": "Started"}}}
STOP = {"1.6": {"dir": "tx", "action": "StopTransaction"},
        "2.0.1": {"dir": "tx", "action": "TransactionEvent", "payload": {"eventType": "Ended"}}}
ONLINE = {"expect": {"state": {"online": True}}, "within": 20}


@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_two_hour_session_in_seconds(version):
    v = _run(version, _sc([{"charger": "CP1", "steps": [
        ONLINE,
        {"do": "set_ev", "ev_model": "compact-40kwh", "initial_soc": 20, "target_soc": 100},
        {"do": "plug"},
        {"after": "1m", "do": "swipe", "id_tag": "TAG1"},
        {"do": "wait", "duration": "10m"},  # the answer arrives while waiting: matched from the swipe on
        {"expect": {"frame": START[version], "then": {"message_type": "CALLRESULT"}}, "within": 5},
        {"expect": {"state": {"status": "Charging", "has_transaction": True, "power_w": {"gt": 0}}}, "within": 5},
        {"id": "charge", "do": "wait", "duration": "2h"},
        {"expect": {"state": {"meter_wh": {"gt": 5000}, "soc_pct": {"gt": 20}}}, "within": 5},
        {"do": "unplug"},
        {"expect": {"frame": STOP[version]}, "within": 5},
    ]}], clock={"start_at": "2026-12-01T07:55:00+09:00"}), )
    assert v["result"] == "pass", json.dumps(v, indent=1)
    assert v["format"] == "ocppsim-verdict/1" and v["sim_s"] >= 2 * 3600 and v["wall_s"] < 10
    steps = {s["id"]: s for s in v["steps"]}
    assert steps["6"]["kind"] == "expect" and len(steps["6"]["evidence"]) == 2
    assert steps["charge"]["t_sim"] >= 660


def test_absolute_at_and_until():
    v = _run("1.6", _sc([{"charger": "CP1", "steps": [
        {"id": "eight", "at": "08:00", "do": "log", "message": "eight"},
        {"id": "long", "do": "wait", "duration": "10h"}]}],
        clock={"start_at": "2026-12-01T07:59:00+09:00"}, until="+30m"))
    steps = {s["id"]: s for s in v["steps"]}
    assert 60 <= steps["eight"]["t_sim"] < 60 + LATE_SIM
    assert v["result"] == "fail" and steps["long"]["status"] == "fail" and "until" in steps["long"]["message"]
    assert 1800 <= v["sim_s"] < 1800 + LATE_SIM


def test_fail_stop_continue_and_expect_not():
    v = _run("1.6", _sc([{"charger": "CP1", "steps": [
        ONLINE,
        {"id": "nope", "expect": {"frame": {"dir": "tx", "action": "StartTransaction"}}, "within": 0.2,
         "on_fail": "continue"},
        {"id": "quiet", "expect_not": {"dir": "tx", "action": "StartTransaction"}, "within_sim": "10m"},
        {"do": "start"},
        {"id": "loud", "expect_not": {"dir": "tx", "action": "StartTransaction"}, "within": 5},
        {"id": "never", "do": "stop"},
    ]}, {"name": "other", "charger": "CP1", "steps": [{"id": "w", "do": "wait", "duration": "100h"}]}]))
    steps = {(s["track"], s["id"]): s for s in v["steps"]}
    t = "track1:CP1"
    assert v["result"] == "fail"
    assert steps[(t, "nope")]["status"] == "fail" and "no match within 0.2 s" in steps[(t, "nope")]["message"]
    assert steps[(t, "quiet")]["status"] == "pass"
    assert steps[(t, "loud")]["status"] == "fail" and steps[(t, "loud")]["evidence"]
    assert steps[(t, "never")]["status"] == "skipped" and steps[("other", "w")]["status"] == "skipped"
    junit = ET.fromstring(to_junit(v))
    assert junit.tag == "testsuites" and junit.get("failures") == "2"
    assert junit.find("testsuite/testcase/failure") is not None


def test_action_error_and_then_mismatch():
    v = _run("1.6", _sc([{"charger": "CP1", "steps": [
        ONLINE,
        {"do": "send", "action": "Heartbeat"},
        {"id": "hb", "expect": {"frame": {"dir": "tx", "action": "Heartbeat"},
                                "then": {"payload": {"currentTime": {"exists": False}}}}, "within": 0.3,
         "on_fail": "continue"},
        {"id": "bad", "do": "plug", "connector": 9},
    ]}]))
    steps = {s["id"]: s for s in v["steps"]}
    assert steps["hb"]["status"] == "fail" and "currentTime" in steps["hb"]["message"]
    assert steps["bad"]["status"] == "error" and v["result"] == "error" and exit_code(v["result"]) == 2


def test_set_ev_is_used_once():
    engine = _engine(DEAD_URL, "1.6", session={"ev_model": "pool"})
    out = ops.set_ev(engine, "CP1", 1, "suv-90kwh", initial_soc=33)
    rt = engine.charger("CP1")
    assert out["ev_model"] == "suv-90kwh" and rt.vehicle(1).initial_soc_pct == 33
    assert 1 not in rt.next_vehicle
    with pytest.raises(ops.OpError):
        ops.set_ev(engine, "CP1", 1, "no-such-car")


# ------------------------------------------------------------------ HTTP API / MCP / CLI

def test_http_api_run_status_abort_result(tmp_path):
    engine = _engine(DEAD_URL, "1.6")
    engine.trace_dir = lambda: tmp_path   # web-started scenarios record a trace there
    engine.stop_grace_s = 0.2
    text = yaml.safe_dump(_sc([{"charger": "CP1", "steps": [{"do": "log", "message": "hi"},
                                                            {"do": "wait", "duration": "1000h"}]}]))
    with TestClient(create_app(engine), base_url="http://127.0.0.1") as c:
        assert c.get("/api/scenarios/current").json() == {"state": "idle"}
        assert c.get("/api/scenarios/result").status_code == 404
        assert c.post("/api/scenarios/abort").status_code == 409
        r = c.post("/api/scenarios/run", content="format: ocppsim-scenario/1\nname: x\ntracks: []\n")
        assert r.status_code == 400 and "tracks" in r.text
        r = c.post("/api/scenarios/run", content=text.replace("CP1", "CP9"))
        assert r.status_code == 400 and "unknown charger 'CP9'" in r.text
        r = c.post("/api/scenarios/run", content=text)
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert c.post("/api/scenarios/run", content=text).status_code == 409
        assert c.get("/api/scenarios/result").status_code == 409
        st = c.get("/api/scenarios/current").json()
        assert st["tracks"][0]["charger"] == "CP1" and st["state"] == "running"
        r = c.post("/api/scenarios/abort")
        assert r.status_code == 200 and r.json()["result"] == "aborted"
        v = c.get("/api/scenarios/result").json()
        assert v["result"] == "aborted" and [s["status"] for s in v["steps"]] == ["pass", "aborted"]
        x = c.get("/api/scenarios/result", params={"format": "junit"})
        assert x.headers["content-type"].startswith("application/xml") and ET.fromstring(x.text).tag == "testsuites"
        assert 'name="scenario"' in x.text and 'message="aborted"' in x.text
        r = c.post("/api/frames/expect", json={"action": "Heartbeat", "within": 0.1})
        assert r.json()["matched"] is False
        assert c.post("/api/frames/expect", json={"payload": {"a": {"bad": 1}}}).status_code == 400


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cli_headless_exit_codes(tmp_path, capsys):
    port = _free_port()
    cfg = tmp_path / "sim.yaml"
    cfg.write_text(yaml.safe_dump({
        "csms_defaults": {"url": f"ws://127.0.0.1:{port}/ocpp", "reconnect": {"min_s": 0.05, "max_s": 0.2}},
        "chargers": [{"id": "CP1", "id_tags": ["TAG1"], "auth_mode": "accept_all"}],
        "mini_csms": {"enabled": True, "port": port}}), encoding="utf-8")
    sc = tmp_path / "s.yaml"
    sc.write_text(yaml.safe_dump(_sc([{"charger": "CP1", "steps": [
        ONLINE, {"do": "start"}, {"expect": {"frame": START["1.6"], "then": {
            "payload": {"idTagInfo.status": "Accepted"}}}, "within": 5}, {"do": "wait", "duration": "1h"}]}])),
        encoding="utf-8")
    out, junit, trace = tmp_path / "v.json", tmp_path / "j.xml", tmp_path / "t.jsonl"
    rc = main(["scenario", "run", str(sc), "-c", str(cfg), "--out", str(out), "--junit", str(junit),
               "--trace", str(trace), "--time-scale", "3600"])
    v = json.loads(out.read_text("utf-8"))
    assert rc == 0 and v["result"] == "pass" and v["trace"] == str(trace), v
    assert ET.parse(junit).getroot().get("failures") == "0"
    lines = [json.loads(line) for line in trace.read_text("utf-8").splitlines()]
    assert lines[0]["format"] == "ocppsim-trace" and lines[-1]["type"] == "footer"
    assert any(x.get("action") == "StartTransaction" for x in lines)
    assert any(x.get("event") == "start" for x in lines)   # the scenario's action, recorded as an event
    sc.write_text(yaml.safe_dump(_sc([{"charger": "CP1", "steps": [
        {"expect": {"state": {"status": "Faulted"}}, "within": 0.2}]}])), encoding="utf-8")
    assert main(["scenario", "run", str(sc), "-c", str(cfg), "--out", str(out)]) == 1
    assert main(["scenario", "validate", str(sc), "-c", str(cfg)]) == 0
    sc.write_text("format: ocppsim-scenario/1\nname: x\ntracks: [{charger: NOPE, steps: [{do: plug}]}]\n",
                  encoding="utf-8")
    assert main(["scenario", "validate", str(sc), "-c", str(cfg)]) == 2
    assert "unknown charger 'NOPE'" in capsys.readouterr().err


# ------------------------------------------------------------------ review follow-ups

DT = {"do": "send", "action": "DataTransfer", "payload": {"vendorId": "acme"}}
DT_FRAME = {"frame": {"dir": "tx", "action": "DataTransfer"}}


def test_frame_cursor_and_from():
    v = _run("1.6", _sc([{"charger": "CP1", "steps": [
        ONLINE, DT,
        {"id": "a", "expect": DT_FRAME, "within": 10},
        {"id": "b", "expect": DT_FRAME, "within": 0.5, "on_fail": "continue"},   # the one frame is used up
        {"id": "c", "expect": DT_FRAME, "from": "action", "within": 10},
        {"id": "d", "expect": {"frame": {"dir": "tx", "action": "BootNotification"}}, "from": "scenario_start",
         "within": 10},
    ]}]))
    st = {s["id"]: s["status"] for s in v["steps"]}
    assert st == {"1": "pass", "2": "pass", "a": "pass", "b": "fail", "c": "pass", "d": "pass"}, v
    assert "from: only on frame" in _err(_sc([{"charger": "A", "steps": [{"do": "plug", "from": "action"}]}]))


def test_params_in_typed_fields():
    sc = load_data(_sc([{"charger": "A", "repeat": "${n}", "steps": [
        {"do": "plug", "connector": "${conn}"},
        {"expect": {"state": {"online": True, "connector": "${conn}"}}, "within": "${w}"}]}],
        params={"conn": 2, "w": "5s", "n": 3}))
    t = expand(sc, ["A"])[0]
    assert len(t.steps) == 6 and t.steps[0].step.connector == 2 and t.steps[1].step.within == 5.0
    sc = load_data(_sc([{"charger": "A", "steps": [{"do": "plug", "connector": "${i}"}]}]))
    with pytest.raises(ScenarioError, match=r"tracks.0.steps.0.*connector"):
        expand(sc, ["A"])   # ${i} = 0 is not a connector
    assert "tracks.0.steps.0.connector" in _err(_sc([{"charger": "A", "steps": [{"do": "plug",
                                                                                 "connector": "${zz}"}]}]))


def test_repeat_rounds_and_until_pending():
    assert "repeat track takes only relative" in _err(_sc([{"charger": "A", "repeat": 2, "steps": [
        {"at": "08:00", "do": "log", "message": "x"}]}]))
    assert "repeat" in _err(_sc([{"charger": "A", "repeat": 1001, "steps": [{"do": "log", "message": "x"}]}]))
    engine = _engine(DEAD_URL, "1.6")
    sc = load_data(_sc([{"charger": "CP1", "repeat": 2, "steps": [
        {"id": "l", "at": "+10m", "do": "log", "message": "r${round}"}, {"do": "wait", "duration": "1h"}]},
        {"name": "u", "charger": "CP1", "steps": [{"id": "w", "do": "wait", "duration": "10h"},
                                                  {"id": "p", "do": "log", "message": "never"}]}], until="+3h"))
    v = asyncio.run(ScenarioRunner(engine, sc).run())
    st = {(s["track"], s["id"]): s for s in v["steps"]}
    assert 4800 <= st[("track1:CP1", "l#2")]["t_sim"] < 4800 + LATE_SIM
    assert st[("u", "w")]["status"] == "fail" and st[("u", "p")]["status"] == "skipped"
    assert "until" in st[("u", "p")]["message"] and v["result"] == "fail"


def test_time_scale_restore():
    async def main_():
        engine = _engine(DEAD_URL, "1.6")   # not started: only its clock is used
        long = load_data(_sc([{"charger": "CP1", "steps": [{"do": "wait", "duration": "1000h"}]}],
                             clock={"time_scale": 1800}))
        r = ScenarioRunner(engine, long)
        r.start()
        await asyncio.sleep(0.1)
        assert engine.time_scale == 1800
        r.abort()
        await r.wait()
        assert engine.time_scale == 3600 and r.result == "aborted"
        bad = load_data(_sc([{"charger": "CP1", "steps": [{"do": "plug", "connector": 9}]}],
                            clock={"time_scale": 1800}))
        assert (await ScenarioRunner(engine, bad).run())["result"] == "error" and engine.time_scale == 3600
        r = ScenarioRunner(engine, long)
        r.start()
        await asyncio.sleep(0.1)
        engine.set_running(time_scale=10)   # changed meanwhile: kept
        r.abort()
        await r.wait()
        assert engine.time_scale == 10
    asyncio.run(main_())


def test_frame_buffer_overflow():
    async def fn(engine):
        sc = load_data(_sc([{"charger": "CP1", "steps": [
            ONLINE, {"do": "send", "action": "Heartbeat"}, {"do": "send", "action": "Heartbeat"},
            {"do": "send", "action": "Heartbeat"},
            {"id": "x", "expect": {"frame": {"dir": "tx", "action": "BootNotification"}},
             "from": "scenario_start", "within": 1}]}]))
        r = ScenarioRunner(engine, sc)
        r.max_frames = 3
        return await r.run()
    v = asyncio.run(_with_peer("1.6", fn))
    x = next(s for s in v["steps"] if s["id"] == "x")
    assert x["status"] == "fail" and "overflowed" in x["message"]


def test_engine_stop_aborts_running_scenario():
    async def main_():
        engine = _engine(DEAD_URL, "1.6")
        engine.stop_grace_s = 0.2
        await engine.start()
        r = engine.scenario = ScenarioRunner(engine, load_data(_sc([{"charger": "CP1", "steps": [
            {"do": "wait", "duration": "1000h"}]}])))
        r.start()
        await asyncio.sleep(0.1)
        await engine.stop()
        assert r.done and r.result == "aborted" and r._on_frame not in engine.frame_listeners
    asyncio.run(main_())


def test_http_limits_and_expect_oldest(tmp_path):
    from sme_ocpp_sim.web import MAX_SCENARIO_BYTES
    engine = _engine(DEAD_URL, "1.6")
    engine.trace_dir = lambda: tmp_path
    engine.stop_grace_s = 0.2
    with TestClient(create_app(engine), base_url="http://127.0.0.1") as c:
        assert c.post("/api/scenarios/run", content=b"x" * (MAX_SCENARIO_BYTES + 1)).status_code == 413
        r = c.post("/api/scenarios/run", content=b"format: \xff\xfe")
        assert r.status_code == 400 and "UTF-8" in r.text
        r = c.post("/api/frames/expect", json={"since": 0, "within": 0}).json()
        assert "oldest_seq" in r and "warning" not in r


def test_junit_strips_control_chars():
    v = {"scenario": "s\x01", "result": "fail", "steps": [
        {"id": "1", "track": "t", "kind": "expect", "status": "fail", "message": "bad\x01\x1fchar", "evidence": [3]}]}
    x = to_junit(v)
    root = ET.fromstring(x)
    assert "\x01" not in x and root.find("testsuite/testcase/failure").get("message") == "badchar [frames 3]"


def test_cli_output_error_is_exit_2(tmp_path, capsys):
    cfg = tmp_path / "sim.yaml"
    cfg.write_text(yaml.safe_dump({"csms_defaults": {"url": DEAD_URL}, "chargers": [{"id": "CP1"}]}), "utf-8")
    sc = tmp_path / "s.yaml"
    sc.write_text(yaml.safe_dump(_sc([{"charger": "CP1", "steps": [{"do": "log", "message": "x"}]}])), "utf-8")
    assert main(["scenario", "run", str(sc), "-c", str(cfg), "--out", str(tmp_path)]) == 2  # a directory
    assert "scenario error" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot deliver SIGINT to one child process: "
                    "CTRL_C_EVENT is ignored by a CREATE_NEW_PROCESS_GROUP child and CTRL_BREAK_EVENT is SIGBREAK, "
                    "not the SIGINT the handler of scenario run listens for")
def test_cli_sigint_aborts_and_writes_the_verdict(tmp_path):
    """Ctrl+C on ``scenario run``: the run is aborted, the verdict is still written, exit code 2."""
    with socket.socket() as listener:  # a fake CSMS: the charger's TCP connect means the run has started
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(30)
        port = listener.getsockname()[1]
        cfg = tmp_path / "sim.yaml"
        cfg.write_text(yaml.safe_dump({
            "csms_defaults": {"url": f"ws://127.0.0.1:{port}/ocpp", "reconnect": {"min_s": 0.05, "max_s": 0.2}},
            "chargers": [{"id": "CP1"}]}), encoding="utf-8")
        sc = tmp_path / "s.yaml"
        sc.write_text(yaml.safe_dump(_sc([{"charger": "CP1", "steps": [{"do": "wait", "duration": "1h"}]}])),
                      encoding="utf-8")
        out = tmp_path / "v.json"
        proc = subprocess.Popen([sys.executable, "-m", "sme_ocpp_sim", "scenario", "run", str(sc), "-c", str(cfg),
                                 "--out", str(out)], cwd=str(Path(__file__).resolve().parents[1]),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            conn, _ = listener.accept()
            conn.close()
            # The connect can come just before the runner starts (an abort then is a no-op): repeat the signal
            # until the process ends; the handler stays installed until the run is over.
            deadline = time.monotonic() + 30
            while proc.poll() is None and time.monotonic() < deadline:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            _stdout, stderr = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert proc.returncode == 2, stderr
    v = json.loads(out.read_text("utf-8"))
    assert v["result"] == "aborted" and v["scenario"] == "t", v


def test_builtin_scenarios(tmp_path):
    names = [e["name"] for e in builtin.listing()]
    assert {"both-versions", "plug-charge-unplug", "remote-start-no-tx"} <= set(names)
    assert all(e["title"] for e in builtin.listing())
    assert load_text(builtin.text("builtin:plug-charge-unplug")).name == "plug-charge-unplug"
    for bad in ("nope", "../configs/demo-site", ""):
        with pytest.raises(ScenarioError, match="unknown built-in"):
            builtin.path(bad)
    engine = OcppEngine(SimConfig.model_validate({"csms_defaults": {"url": "ws://127.0.0.1:9/ocpp"},
                                                  "chargers": [{"id": "DEMO-16"}, {"id": "DEMO-201"}]}))
    with TestClient(create_app(engine)) as c:
        assert [e["name"] for e in c.get("/api/scenarios/builtin").json()["scenarios"]] == names
        assert "plug-charge-unplug" in c.get("/api/scenarios/builtin/plug-charge-unplug").text
        assert c.get("/api/scenarios/builtin/nope").status_code == 404
        assert c.post("/api/scenarios/builtin/nope/run").status_code == 404
        assert c.post("/api/scenarios/run", content="builtin:nope").status_code == 404
        r = c.post("/api/scenarios/run", content="builtin:plug-charge-unplug\n")
        assert r.status_code == 200 and r.json()["name"] == "plug-charge-unplug", r.text
        assert c.post("/api/scenarios/builtin/plug-charge-unplug/run").status_code == 409  # one at a time
        assert c.post("/api/scenarios/abort").status_code == 200
    rc = main(["scenario", "validate", "builtin:plug-charge-unplug", "-c", "configs/mini-csms-demo.yaml"])
    assert rc == 0
    assert main(["scenario", "validate", "builtin:nope"]) == 2
