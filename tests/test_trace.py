"""Trace format v1, recorder and analyze (v0.3 M5)."""

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sme_ocpp_sim.__main__ import main
from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.framelog import FrameEntry
from sme_ocpp_sim.trace import format as fmt
from sme_ocpp_sim.trace.analyze import analyze, percentile, to_text
from sme_ocpp_sim.trace.recorder import TraceRecorder
from sme_ocpp_sim.web import create_app

DATA = Path(__file__).resolve().parent / "data" / "traces"
DEAD_URL = "ws://127.0.0.1:9/ocpp"


def _entry(seq: int, raw: str = '[2,"u","Heartbeat",{}]', charger: str = "CP1") -> FrameEntry:
    return FrameEntry(seq=seq, ts=1000.0 + seq, charger=charger, direction="tx", message_type="CALL", uid="u",
                      action="Heartbeat", raw=raw)


def _rec(path, **kw) -> TraceRecorder:
    t = [100.0]

    def sim_now():
        t[0] += 10.0
        return t[0]
    return TraceRecorder(path, tool_version="9.9", time_scale=60.0, sim_start="2026-01-01T00:00:00+00:00",
                         chargers_info=[{"id": "CP1", "ocpp_version": "1.6", "connectors": 2,
                                         "csms_url": "ws://h/ocpp"}], sim_now=sim_now, **kw)


# ------------------------------------------------------------------ format / recorder

def test_round_trip(tmp_path):
    p = tmp_path / "t.jsonl"
    rec = _rec(p).start()
    rec.event("CP1", "connected", {"session": 1})
    rec.frame(_entry(1))
    rec.frame(_entry(2, charger="CP2"))
    rec.event(None, "clock", {"time_scale": 2.0, "running": True})
    rec.stop()
    rec.stop()  # idempotent
    lines = [json.loads(x) for x in p.read_text("utf-8").splitlines()]
    assert [x["type"] for x in lines] == ["header", "event", "frame", "frame", "event", "footer"]
    h = lines[0]
    assert h["format"] == "ocppsim-trace" and h["version"] == 1 and h["masked"] is True and h["part"] == 0
    assert h["time_scale"] == 60.0 and h["chargers"][0]["connectors"] == 2
    t = fmt.load(p)
    assert t.version == 1 and len(t.frames) == 2 and len(t.events) == 2 and t.footer["dropped"] == 0
    assert t.frames[0]["raw"] == '[2,"u","Heartbeat",{}]' and t.frames[0]["seq"] == 1
    assert t.frames[0]["sim_t"] > 0 and t.ocpp_version("CP1") == "1.6"
    assert rec.status()["recording"] is False and rec.status()["frames"] == 2


def test_charger_filter(tmp_path):
    rec = _rec(tmp_path / "t.jsonl", chargers=["CP1"]).start()
    rec.frame(_entry(1))
    rec.frame(_entry(2, charger="CP2"))
    rec.event("CP2", "plug")
    rec.event(None, "clock")
    rec.stop()
    t = fmt.load(tmp_path / "t.jsonl")
    assert [f["charger"] for f in t.frames] == ["CP1"] and [e["event"] for e in t.events] == ["clock"]


def test_v0_file_is_readable():
    t = fmt.load(DATA / "v0-ocpp201.jsonl")
    assert t.version == 0 and t.header is None and len(t.frames) == 8 and t.frames[0]["type"] == "frame"
    assert t.events == [] and t.bad_lines == []


def test_bad_header_and_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"type":"header","format":"other","version":1}\n', encoding="utf-8")
    with pytest.raises(fmt.TraceError):
        fmt.load(p)
    p.write_text('[1,2]\n{"seq":1,"ts":1,"charger":"A","direction":"tx","raw":"x"}\n', encoding="utf-8")
    t = fmt.load(p)
    assert t.version == 0 and t.bad_lines == [1] and len(t.frames) == 1


def test_rotate(tmp_path):
    p = tmp_path / "run.jsonl"
    rec = _rec(p, rotate_mb=2000 / (1024 * 1024)).start()
    for i in range(1, 61):
        rec.frame(_entry(i))
    rec.stop()
    parts = sorted(tmp_path.glob("run*.jsonl"))
    assert len(parts) > 2 and rec.parts[0] == str(p) and len(rec.parts) == len(parts)
    second = [json.loads(x) for x in Path(rec.parts[1]).read_text("utf-8").splitlines()]
    assert second[0]["type"] == "header" and second[0]["part"] == 1 and second[0]["previous"] == "run.jsonl"
    first = [json.loads(x) for x in p.read_text("utf-8").splitlines()]
    assert first[-1]["type"] == "footer" and first[-1]["next"] == "run.1.jsonl"
    t = fmt.load(p)
    assert [f["seq"] for f in t.frames] == list(range(1, 61)) and t.footer["next"] is None
    assert len(t.parts) == len(parts)
    assert len(fmt.load(p, follow=False).frames) < 60


def test_full_queue_counts_dropped(tmp_path):
    rec = _rec(tmp_path / "t.jsonl", queue_size=2)
    for i in range(1, 6):    # writer not started yet: the queue fills up
        rec.frame(_entry(i))
    assert rec.dropped == 3 and rec.frames == 2
    rec.start()
    rec.stop()
    t = fmt.load(tmp_path / "t.jsonl")
    assert len(t.frames) == 2 and t.footer["dropped"] == 3 and t.dropped == 3


def test_strip_credentials():
    assert fmt.strip_credentials("ws://cp:secret@h:9000/ocpp") == "ws://h:9000/ocpp"
    assert fmt.strip_credentials("wss://h/ocpp") == "wss://h/ocpp"


# ------------------------------------------------------------------ analyze

def test_percentile():
    assert percentile([], 50) is None
    assert percentile([3, 1, 2], 50) == 2 and percentile([1, 2], 95) == 2 and percentile([5], 95) == 5


def test_analyze_golden_ocpp16():
    r = analyze(fmt.load(DATA / "v1-ocpp16.jsonl"))
    assert r["format"] == "ocppsim-analysis/1"
    assert r["trace"]["version"] == 1 and r["trace"]["frames"] == 19 and r["trace"]["complete"] is True
    c = r["chargers"]["CP1"]
    assert c["ocpp_version"] == "1.6"
    boot = c["actions"]["tx"]["BootNotification"]
    assert boot["calls"] == 2 and boot["results"] == 2 and boot["latency_ms"] == {"p50": 50.0, "p95": 100.0,
                                                                                   "max": 100.0}
    hb = c["actions"]["tx"]["Heartbeat"]
    assert hb["calls"] == 3 and hb["results"] == 2 and hb["unanswered"] == 1
    assert c["actions"]["tx"]["MeterValues"]["errors"] == {"FormationViolation": 1}
    assert c["actions"]["rx"]["SetChargingProfile"]["results"] == 1
    cn = c["connection"]
    assert cn["connected"] == 1 and cn["disconnected"] == 1 and cn["connected_s"]["p50"] == 220.0
    assert [b["status"] for b in cn["boots"]] == ["Pending", "Accepted"]
    assert cn["heartbeat_interval_s"]["min"] == 60.0 and cn["heartbeat_interval_s"]["max"] == 140.0
    [tx] = c["transactions"]
    assert tx["transaction_id"] == 42 and tx["delta_wh"] == 7500 and tx["duration_s"] == 3600
    assert tx["stop_reason"] == "EVDisconnected" and tx["complete"] and tx["id_tag"] == "TAG1"
    an = c["anomalies"]
    assert an["unparseable"] == [19] and [u["uid"] for u in an["unpaired"]] == ["zz"]
    assert [u["seq"] for u in an["duplicate_uid"]] == [18]
    assert [u["action"] for u in an["concurrent_call"]] == ["MeterValues"]
    assert [u["seq"] for u in an["unanswered"]] == [18]
    [p] = c["smart_charging"]
    assert (p["profile_id"], p["purpose"], p["stack_level"], p["status"]) == (7, "TxProfile", 0, "Accepted")
    text = to_text(r)
    assert "== CP1 (OCPP 1.6" in text and "boot: Pending -> Accepted" in text
    assert "transaction 42 (connector 1): 3600 s, 7500 Wh, reason EVDisconnected" in text
    assert "FormationViolation x1" in text and "unpaired 1" in text


def test_analyze_golden_ocpp201_seqno():
    r = analyze(fmt.load(DATA / "v0-ocpp201.jsonl"))
    c = r["chargers"]["CP2"]
    assert c["ocpp_version"] == "2.0.1" and r["trace"]["version"] == 0
    [tx] = c["transactions"]
    assert tx["transaction_id"] == "T1" and tx["connector"] == 1 and tx["id_tag"] == "TAG2"
    assert tx["meter_start_wh"] == 1000.0 and tx["meter_stop_wh"] == 5000.0 and tx["delta_wh"] == 4000.0
    assert tx["duration_s"] == 3600 and tx["stop_reason"] == "EVDisconnected"
    assert tx["seq_no"] == {"count": 4, "gaps": [2], "duplicates": [1]}
    assert c["actions"]["tx"]["TransactionEvent"]["latency_ms"]["max"] == 500.0
    assert "seqNo gaps [2] duplicates [1]" in to_text(r)
    assert analyze(fmt.load(DATA / "v0-ocpp201.jsonl"), charger="CPX")["chargers"] == {}


def test_cli_analyze(tmp_path, capsys):
    assert main(["analyze", str(DATA / "v1-ocpp16.jsonl")]) == 0
    assert "== CP1" in capsys.readouterr().out
    assert main(["analyze", str(DATA / "v0-ocpp201.jsonl"), "--json", "--ocpp-version", "2.1"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["chargers"]["CP2"]["ocpp_version"] == "2.1"
    assert main(["analyze", str(tmp_path / "missing.jsonl")]) == 2


# ------------------------------------------------------------------ engine / web

def _engine(**cfg) -> OcppEngine:
    return OcppEngine(SimConfig.model_validate({
        "csms_defaults": {"url": "ws://cp1:pw@127.0.0.1:9/ocpp", "reconnect": {"min_s": 0.05, "max_s": 0.2}},
        "chargers": [{"id": "CP1", "id_tags": ["TAG1"]}], **cfg}))


def test_engine_events_and_header(tmp_path):
    async def go():
        eng = _engine()
        rec = await eng.open_recorder(tmp_path / "t.jsonl")
        eng.set_running(time_scale=30)
        eng.trace_event("CP1", "plug", {"connector": 1})
        await eng.close_recorder(rec)
        assert eng.recorders == []
    asyncio.run(go())
    t = fmt.load(tmp_path / "t.jsonl")
    [ch] = t.header["chargers"]
    assert {k: ch[k] for k in ("id", "ocpp_version", "connectors", "csms_url", "vendor", "model", "serial",
                               "security_profile", "subprotocol", "path", "ev_model", "seed")} == {
        "id": "CP1", "ocpp_version": "1.6", "connectors": 1, "csms_url": "ws://127.0.0.1:9/ocpp",
        "vendor": "SmartEdge", "model": "SIM-AC6", "serial": None, "security_profile": 0,
        "subprotocol": "ocpp1.6", "path": "/ocpp/CP1", "ev_model": "compact-40kwh", "seed": 0}
    assert ch["impairments"]["latency_ms"] == 0 and "pw" not in json.dumps(t.header)
    assert [(e["event"], e["detail"]) for e in t.events] == [
        ("clock", {"time_scale": 30, "running": True}), ("plug", {"connector": 1})]


def test_config_trace_path_records_from_start(tmp_path):
    p = tmp_path / "boot.jsonl"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    async def go():
        eng = _engine(trace={"path": str(p)}, mini_csms={"enabled": True, "port": port})
        eng.config.csms_defaults.url = f"ws://127.0.0.1:{port}/ocpp"
        eng = OcppEngine(eng.config)
        eng.stop_grace_s = 0.2
        await eng.start()
        assert eng.trace is not None and eng.trace.recording and eng.trace_dir() == tmp_path
        for _ in range(200):
            if eng.chargers["CP1"].online:
                break
            await asyncio.sleep(0.02)
        await eng.stop()
        assert not eng.trace.recording
    asyncio.run(go())
    t = fmt.load(p)
    assert t.footer is not None
    assert [e["event"] for e in t.events if e["charger"] == "CP1"][:1] == ["connected"]
    assert any(e["event"] == "disconnected" and e["detail"]["reason"] == "stopped" for e in t.events)
    assert any(f["action"] == "BootNotification" for f in t.frames)
    assert next(e for e in t.events if e["event"] == "connected")["detail"]["path"] == "/ocpp/CP1"
    assert analyze(t)["chargers"]["CP1"]["connection"]["connected"] == 1


def test_web_trace_api(tmp_path):
    eng = _engine(trace={"path": str(tmp_path / "unused" / "x.jsonl")})
    eng.config.trace.path = None
    eng.stop_grace_s = 0.2
    eng.trace_dir = lambda: tmp_path   # type: ignore[method-assign]
    with TestClient(create_app(eng), base_url="http://127.0.0.1") as c:
        assert c.get("/api/trace").json() == {"recording": False, "path": None, "dir": str(tmp_path)}
        assert c.post("/api/trace/stop").status_code == 409
        assert c.post("/api/trace/start", json={"file": "../evil.jsonl"}).status_code == 400
        assert c.post("/api/trace/start", json={"file": "a.txt"}).status_code == 400
        r = c.post("/api/trace/start", json={"file": "run1.jsonl"})
        assert r.status_code == 200 and r.json()["recording"] is True, r.text
        assert c.post("/api/trace/start", json={}).status_code == 409
        assert c.post("/api/chargers/CP1/disconnect").status_code == 200   # an ops event
        r = c.post("/api/trace/stop")
        assert r.status_code == 200 and r.json()["recording"] is False
        files = c.get("/api/trace/files").json()["files"]
        assert [f["name"] for f in files] == ["run1.jsonl"]
        body = c.get("/api/trace/files/run1.jsonl").text
        assert body.startswith('{"type":"header"') and '"event":"disconnect"' in body
        assert c.get("/api/trace/files/none.jsonl").status_code == 404
        r = c.get("/api/trace/files/run1.jsonl/analyze", params={"format": "text"})
        assert r.status_code == 200 and r.text.startswith("trace v1")
        v1 = c.get("/api/frames.jsonl", params={"format": "v1"}).text.splitlines()
        assert json.loads(v1[0])["format"] == "ocppsim-trace"
        v0 = c.get("/api/frames.jsonl").text
        assert '"type"' not in v0
        r = c.get("/api/frames/analyze")
        assert r.status_code == 200 and r.json()["format"] == "ocppsim-analysis/1"


def test_web_scenario_records_a_trace(tmp_path):
    eng = _engine()
    eng.stop_grace_s = 0.2
    eng.trace_dir = lambda: tmp_path   # type: ignore[method-assign]
    text = yaml.safe_dump({"format": "ocppsim-scenario/1", "name": "t r", "tracks": [
        {"charger": "CP1", "steps": [{"do": "log", "message": "hi"}]}]})
    with TestClient(create_app(eng), base_url="http://127.0.0.1") as c:
        assert c.post("/api/scenarios/run", content=text).status_code == 200
        for _ in range(100):
            if c.get("/api/scenarios/current").json()["state"] == "done":
                break
            time.sleep(0.05)
        v = c.get("/api/scenarios/result").json()
    assert Path(v["trace"]).parent == tmp_path and Path(v["trace"]).name.startswith("scenario-t_r-")
    t = fmt.load(v["trace"])
    assert t.version == 1 and t.footer is not None and [c["id"] for c in t.header["chargers"]] == ["CP1"]


# ------------------------------------------------------------------ review fixes (M5)

def _call(seq, ts, uid, action="Heartbeat", d="tx", charger="CP1"):
    return {"type": "frame", "seq": seq, "ts": ts, "charger": charger, "direction": d, "message_type": "CALL",
            "uid": uid, "action": action, "raw": json.dumps([2, uid, action, {}]), "note": "", "sim_t": None}


def _result(seq, ts, uid, d="rx", charger="CP1"):
    return {"type": "frame", "seq": seq, "ts": ts, "charger": charger, "direction": d, "message_type": "CALLRESULT",
            "uid": uid, "action": None, "raw": json.dumps([3, uid, {}]), "note": "", "sim_t": None}


def test_rotation_footers_are_cumulative_and_dropped_is_not_summed(tmp_path):
    p = tmp_path / "run.jsonl"
    rec = _rec(p, rotate_mb=2000 / (1024 * 1024), queue_size=40)
    for i in range(1, 100):    # writer not started: 40 queued, the rest dropped
        rec.frame(_entry(i))
    assert rec.dropped == 59
    rec.start()
    rec.stop()
    assert len(rec.parts) >= 2
    footers = [json.loads(Path(x).read_text("utf-8").splitlines()[-1]) for x in rec.parts]
    assert all(f["dropped"] == 59 for f in footers) and footers[-1]["frames"] == 40
    assert [f["frames"] for f in footers] == sorted(f["frames"] for f in footers)
    t = fmt.load(p)
    assert t.dropped == 59 and len(t.frames) == 40 and analyze(t)["trace"]["dropped"] == 59


def test_rotation_open_failure_ends_with_error_footer(tmp_path, monkeypatch):
    p = tmp_path / "run.jsonl"
    rec = _rec(p, rotate_mb=2000 / (1024 * 1024))
    orig = rec._open

    def failing(part):
        if part >= 1:
            raise OSError("disk full")
        return orig(part)
    monkeypatch.setattr(rec, "_open", failing)
    rec.start()
    for i in range(1, 40):
        rec.frame(_entry(i))
    for _ in range(100):
        if not rec.recording:
            break
        time.sleep(0.02)
    rec.stop()
    last = json.loads(p.read_text("utf-8").splitlines()[-1])
    assert last["type"] == "footer" and last["next"] is None and "disk full" in last["error"]
    assert rec.error and not (tmp_path / "run.1.jsonl").exists()
    t = fmt.load(p)
    assert t.incomplete and analyze(t)["trace"]["complete"] is False


def test_missing_next_part_is_incomplete(tmp_path):
    p = tmp_path / "run.jsonl"
    rec = _rec(p, rotate_mb=2000 / (1024 * 1024)).start()
    for i in range(1, 40):
        rec.frame(_entry(i))
    rec.stop()
    assert fmt.load(p).incomplete is False and analyze(fmt.load(p))["trace"]["complete"] is True
    (tmp_path / "run.1.jsonl").unlink()
    t = fmt.load(p)
    assert t.incomplete and analyze(t)["trace"]["complete"] is False
    assert "part is missing" in to_text(analyze(t))


def test_writer_flushes_when_idle(tmp_path):
    p = tmp_path / "t.jsonl"
    rec = _rec(p).start()
    rec.frame(_entry(1))
    for _ in range(100):
        if '"seq":1' in p.read_text("utf-8"):
            break
        time.sleep(0.02)
    assert '"seq":1' in p.read_text("utf-8")    # readable while still recording
    rec.stop()


def test_strip_credentials_query_fragment_and_invalid():
    assert fmt.strip_credentials("wss://u:p@h:9000/ocpp/CP1?token=abc&x=1#frag") == "wss://h:9000/ocpp/CP1"
    assert fmt.strip_credentials("ws://h/ocpp?key=secret") == "ws://h/ocpp"
    assert fmt.strip_credentials("ws://u:topsecret@h:notaport/x") == fmt.INVALID_URL
    assert "topsecret" not in fmt.strip_credentials("ws://u:topsecret@[bad/x")
    assert fmt.url_path("wss://u:p@h/ocpp/CP1?t=1") == "/ocpp/CP1"


def test_analyze_orders_by_ts_and_closes_calls_on_disconnect():
    tr = fmt.Trace(version=1, header={"chargers": []})
    # frames deliberately out of ts order; the disconnect leaves a unanswered; b is stale (>60 s) when c comes
    tr.frames = [_call(2, 12.0, "b"), _call(1, 10.0, "a"), _call(3, 100.0, "c"), _result(4, 100.5, "c")]
    tr.events = [{"type": "event", "ts": 11.0, "sim_t": None, "charger": "CP1", "event": "disconnected",
                  "detail": {}}]
    c = analyze(tr)["chargers"]["CP1"]
    assert c["anomalies"]["concurrent_call"] == []
    assert [u["uid"] for u in c["anomalies"]["unanswered"]] == ["a", "b"]
    assert c["actions"]["tx"]["Heartbeat"]["unanswered"] == 2


def test_analyze_flags_concurrent_but_not_stale_calls():
    tr = fmt.Trace(version=1)
    tr.frames = [_call(1, 10.0, "a"), _call(2, 12.0, "b")]
    assert len(analyze(tr)["chargers"]["CP1"]["anomalies"]["concurrent_call"]) == 1
    tr.frames = [_call(1, 10.0, "a"), _call(2, 100.0, "b"), _result(3, 100.1, "b", d="rx")]
    c = analyze(tr)["chargers"]["CP1"]
    assert c["anomalies"]["concurrent_call"] == []
    assert [u["uid"] for u in c["anomalies"]["unanswered"]] == ["a"]


def test_analyze_survives_frame_lines_missing_fields():
    tr = fmt.Trace(version=1)
    tr.frames = [_call(1, 10.0, "a"), {"type": "frame", "raw": '[2,"x","Heartbeat",{}]'},
                 {**_call(3, 11.0, "c"), "ts": None}, {k: v for k, v in _call(4, 12.0, "d").items() if k != "seq"},
                 {k: v for k, v in _call(5, 13.0, "e").items() if k != "direction"}]
    tr.events = [{"type": "event", "event": "disconnected", "charger": "CP1"}]
    r = analyze(tr)
    assert r["trace"]["bad_frames"] == 4 and r["trace"]["frames"] == 1
    assert "4 bad line(s)" in to_text(r)


def test_seqno_gaps_without_started_count_from_first_seen():
    def ev(seq_no, kind="Updated"):
        p = {"eventType": kind, "seqNo": seq_no, "timestamp": "2026-01-01T00:00:00Z",
             "transactionInfo": {"transactionId": "T9"}, "evse": {"id": 1}}
        return {"type": "frame", "seq": seq_no, "ts": 10.0 + seq_no, "charger": "CP2", "direction": "tx",
                "message_type": "CALL", "uid": f"u{seq_no}", "action": "TransactionEvent",
                "raw": json.dumps([2, f"u{seq_no}", "TransactionEvent", p]), "note": "", "sim_t": None}
    tr = fmt.Trace(version=1)
    tr.frames = [ev(5), ev(6), ev(8)]
    [tx] = analyze(tr)["chargers"]["CP2"]["transactions"]
    assert tx["seq_no"]["gaps"] == [7]
    tr.frames = [ev(0, "Started"), ev(2)]       # a Started event: counted from 0
    [tx] = analyze(tr)["chargers"]["CP2"]["transactions"]
    assert tx["seq_no"]["gaps"] == [1]


def test_masking_of_results_and_notify_report():
    from sme_ocpp_sim.framelog import SECRET_MASK, FrameLog, mask_frame
    gv = json.dumps([3, "u1", {"getVariableResult": [
        {"attributeStatus": "Accepted", "attributeValue": "hunter2", "variable": {"name": "BasicAuthPassword"}},
        {"attributeValue": "x", "variable": {"name": "Other"}}]}])
    out = json.loads(mask_frame(gv, "GetVariables"))
    assert out[2]["getVariableResult"][0]["attributeValue"] == SECRET_MASK
    assert out[2]["getVariableResult"][1]["attributeValue"] == "x"
    assert mask_frame(gv, "Heartbeat") == gv
    gc = json.dumps([3, "u2", {"configurationKey": [{"key": "AuthorizationKey", "readonly": False, "value": "s3cret"}]}])
    assert json.loads(mask_frame(gc, "GetConfiguration"))[2]["configurationKey"][0]["value"] == SECRET_MASK
    nr = json.dumps([2, "u3", "NotifyReport", {"reportData": [
        {"variable": {"name": "BasicAuthPassword"}, "variableAttribute": [{"value": "pw"}]}]}])
    assert "pw" not in mask_frame(nr, "NotifyReport") and SECRET_MASK in mask_frame(nr)

    class Info:
        direction, message_type, uid, action, note = "tx", 3, "u1", "GetVariables", ""
        raw = gv
    assert "hunter2" not in FrameLog().add("CP1", Info).raw


def test_ops_send_event_detail_is_masked():
    import inspect

    from sme_ocpp_sim.scenario import ops
    eng = _engine()
    seen = []

    class Rec:
        def event(self, charger, event, detail=None):
            seen.append((event, detail))
    eng.recorders.append(Rec())
    sig = inspect.signature(ops.send.__wrapped__)
    ops._trace(eng, "send", sig, ("CP1",), {"action": "ChangeConfiguration",
                                            "payload": {"key": "AuthorizationKey", "value": "topsecret"}}, 0)
    raw = json.dumps([2, "u", "ChangeConfiguration", {"key": "AuthorizationKey", "value": "topsecret"}])
    ops._trace(eng, "send", sig, ("CP1",), {"raw": raw}, 0)
    assert "topsecret" not in json.dumps(seen) and "********" in json.dumps(seen)


def test_web_trace_start_refuses_existing_file(tmp_path):
    eng = _engine()
    eng.stop_grace_s = 0.2
    eng.trace_dir = lambda: tmp_path   # type: ignore[method-assign]
    (tmp_path / "old.jsonl").write_text("keep me\n", "utf-8")
    with TestClient(create_app(eng), base_url="http://127.0.0.1") as c:
        r = c.post("/api/trace/start", json={"file": "old.jsonl"})
        assert r.status_code == 409 and "already exists" in r.text
    assert (tmp_path / "old.jsonl").read_text("utf-8") == "keep me\n"


def test_cli_trace_path_is_kept_out_of_the_config(tmp_path):
    eng = _engine()
    eng.stop_grace_s = 0.2
    eng.trace_start_path = str(tmp_path / "cli.jsonl")

    async def go():
        await eng.start()
        assert eng.trace is not None and eng.trace.recording and eng.trace_dir() == tmp_path
        await eng.stop()
    asyncio.run(go())
    assert eng.config.trace.path is None and (tmp_path / "cli.jsonl").exists()
