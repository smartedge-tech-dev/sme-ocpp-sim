"""v0.3 M6: trace replay against a CSMS (trace/replay.py, rewrite_v16.py, rewrite_v201.py)."""

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from ocpp.v16 import call as call16

from peers.ocpp16_peer import CsmsPeer
from peers.ocpp201_peer import CsmsPeer201
from sme_ocpp_sim.__main__ import main
from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.mcp_tools import build_registry
from sme_ocpp_sim.trace import format as fmt
from sme_ocpp_sim.trace.replay import (Replayer, ReplayError, build_plan, compare, exit_code, parse_timing,
                                       to_text)
from sme_ocpp_sim.trace.rewrite import earliest_datetime, shift_datetime
from sme_ocpp_sim.trace.rewrite_v16 import Rewriter16
from sme_ocpp_sim.trace.rewrite_v201 import MAX_TX_ID, Rewriter201
from sme_ocpp_sim.web import create_app


# ---------------------------------------------------------------- trace builders

class TraceBuilder:
    def __init__(self, charger="CP1", version="1.6", gap=0.2):
        self.charger, self.version, self.gap = charger, version, gap
        self.lines = []
        self.seq = 0
        self.ts = 1000.0
        self.n = 0

    def _frame(self, d, mtype, uid, action, raw):
        self.seq += 1
        self.lines.append({"type": "frame", "seq": self.seq, "ts": self.ts, "charger": self.charger,
                           "direction": d, "message_type": mtype, "uid": uid, "action": action,
                           "raw": json.dumps(raw), "note": "", "sim_t": None})
        self.ts += 0.01

    def call(self, action, payload, answer=None, error=None):
        """A charger CALL and the CSMS answer (``None`` = unanswered)."""
        self.n += 1
        uid = f"u{self.n}"
        self._frame("tx", "CALL", uid, action, [2, uid, action, payload])
        if error is not None:
            self._frame("rx", "CALLERROR", uid, action, [4, uid, error, "", {}])
        elif answer is not None:
            self._frame("rx", "CALLRESULT", uid, action, [3, uid, answer])
        self.ts += self.gap

    def csms_call(self, action, payload, answer=None, error=None):
        self.n += 1
        uid = f"c{self.n}"
        self._frame("rx", "CALL", uid, action, [2, uid, action, payload])
        if error is not None:
            self._frame("tx", "CALLERROR", uid, action, [4, uid, error, "", {}])
        elif answer is not None:
            self._frame("tx", "CALLRESULT", uid, action, [3, uid, answer])

    def trace(self):
        hdr = fmt.header(tool_version="t", created="2026-01-01T00:00:00Z", time_scale=1.0, sim_start=None,
                         chargers=[{"id": self.charger, "ocpp_version": self.version, "connectors": 1}])
        return fmt.parse_lines([fmt.dumps(hdr)] + [fmt.dumps(x) for x in self.lines])

    def write(self, path):
        hdr = fmt.header(tool_version="t", created="2026-01-01T00:00:00Z", time_scale=1.0, sim_start=None,
                         chargers=[{"id": self.charger, "ocpp_version": self.version, "connectors": 1}])
        path.write_text("".join([fmt.dumps(hdr)] + [fmt.dumps(x) for x in self.lines]), encoding="utf-8")
        return path


def trace16(gap=0.2, tag="TAG1"):
    b = TraceBuilder(gap=gap)
    b.call("BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"},
           {"status": "Accepted", "interval": 60, "currentTime": "2026-01-01T00:00:00Z"})
    b.call("Authorize", {"idTag": tag}, {"idTagInfo": {"status": "Accepted"}})
    b.call("StartTransaction", {"connectorId": 1, "idTag": tag, "meterStart": 0,
                                "timestamp": "2026-01-01T00:00:10Z"},
           {"transactionId": 777, "idTagInfo": {"status": "Accepted"}})
    b.csms_call("GetConfiguration", {}, {"configurationKey": [{"key": "HeartbeatInterval", "readonly": False,
                                                               "value": "60"}]})
    b.call("MeterValues", {"connectorId": 1, "transactionId": 777, "meterValue": [
        {"timestamp": "2026-01-01T00:00:20Z", "sampledValue": [{"value": "100"}]}]}, {})
    b.call("StopTransaction", {"transactionId": 777, "meterStop": 100, "timestamp": "2026-01-01T00:00:30.000Z",
                               "idTag": tag}, {"idTagInfo": {"status": "Accepted"}})
    return b


def trace201(gap=0.05):
    b = TraceBuilder(charger="CS1", version="2.0.1", gap=gap)
    b.call("BootNotification", {"chargingStation": {"model": "M", "vendorName": "V"}, "reason": "PowerUp"},
           {"status": "Accepted", "interval": 60, "currentTime": "2026-01-01T00:00:00Z"})
    tok = {"idToken": "T1", "type": "ISO14443"}
    b.call("TransactionEvent", {"eventType": "Started", "timestamp": "2026-01-01T00:00:10Z",
                                "triggerReason": "Authorized", "seqNo": 0,
                                "transactionInfo": {"transactionId": "tx-1"}, "idToken": tok},
           {"idTokenInfo": {"status": "Accepted"}})
    b.call("TransactionEvent", {"eventType": "Ended", "timestamp": "2026-01-01T00:01:10Z",
                                "triggerReason": "StopAuthorized", "seqNo": 1,
                                "transactionInfo": {"transactionId": "tx-1", "stoppedReason": "Local"}}, {})
    return b


def _run(coro_fn, timeout=30):
    return asyncio.run(asyncio.wait_for(coro_fn(), timeout))


# ---------------------------------------------------------------- unit

def test_parse_timing():
    assert parse_timing("original") == 1.0 and parse_timing(None) == 1.0
    assert parse_timing("asap") is None and parse_timing("scale:4") == 4.0
    for bad in ("scale:0", "scale:-1", "scale:x", "fast", "scale:inf"):
        with pytest.raises(ReplayError):
            parse_timing(bad)


def test_shift_datetime_keeps_style():
    d = timedelta(days=1, seconds=1)
    assert shift_datetime("2026-01-01T00:00:00Z", d) == "2026-01-02T00:00:01Z"
    assert shift_datetime("2026-01-01T00:00:00.5+09:00", d) == "2026-01-02T00:00:01.500+09:00"
    assert shift_datetime("not a time", d) == "not a time"
    assert earliest_datetime([{"a": "2026-01-02T00:00:00Z"}, {"b": [{"c": "2026-01-01T00:00:00Z"}]}]) == \
        datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_rewriter16_maps_transaction_id():
    rw = Rewriter16(shift=None)
    rw.learn("StartTransaction", {"transactionId": 777}, {"transactionId": 5})
    assert rw.call("StopTransaction", {"transactionId": 777, "meterStop": 1}) == {"transactionId": 5, "meterStop": 1}
    assert rw.call("MeterValues", {"transactionId": 8}) == {"transactionId": 8}
    rw.learn("StartTransaction", {"transactionId": 9}, None)
    assert rw.notes and "9" in rw.notes[0]


def test_rewriter201_suffix_and_length():
    rw = Rewriter201(shift=None, run_id="abc123")
    p = rw.call("TransactionEvent", {"transactionInfo": {"transactionId": "tx-1"}})
    assert p["transactionInfo"]["transactionId"] == "tx-1-abc123"
    long = "x" * 36
    assert len(rw.tx_id(long)) == MAX_TX_ID and rw.tx_id(long).endswith("-abc123")
    assert rw.reply("RequestStartTransaction", {"status": "Accepted", "transactionId": "tx-1"})["transactionId"] \
        == "tx-1-abc123"
    off = Rewriter201(shift=None, run_id="abc123", tx_suffix=False)
    assert off.call("TransactionEvent", {"transactionId": "tx-1"}) == {"transactionId": "tx-1"}


def test_build_plan_pairs_answers_and_detects_version():
    plan = build_plan(trace16().trace())
    assert plan.charger == "CP1" and plan.ocpp_version == "1.6"
    assert [c.action for c in plan.calls] == ["BootNotification", "Authorize", "StartTransaction", "MeterValues",
                                               "StopTransaction"]
    assert plan.calls[2].response[2]["transactionId"] == 777
    assert plan.answers["GetConfiguration"][0][0] == 3
    b = trace201()
    v0 = fmt.Trace(version=0, frames=b.trace().frames)
    assert build_plan(v0).ocpp_version == "2.0.1"
    two = trace16().trace()
    two.frames.append({**two.frames[0], "charger": "CP2"})
    with pytest.raises(ReplayError, match="CP1, CP2"):
        build_plan(two)
    with pytest.raises(ReplayError, match="no frames"):
        build_plan(trace16().trace(), "CPX")


def test_compare():
    rec = {"kind": "result", "status": {"idTagInfo.status": "Accepted"}}
    assert compare(rec, {"kind": "result", "status": {"idTagInfo.status": "Accepted"}}) == (True, [])
    assert compare(rec, {"kind": "result", "status": {"idTagInfo.status": "Invalid"}}) == \
        (False, ["idTagInfo.status: Accepted -> Invalid"])
    assert compare(rec, {"kind": "timeout"}) == (False, ["result -> timeout"])
    assert compare({"kind": "error", "code": "NotImplemented"}, {"kind": "error", "code": "NotSupported"})[0] is False
    assert compare({"kind": "none"}, {"kind": "timeout"}) == (None, [])
    assert exit_code({"result": "diff"}) == 0 and exit_code({"result": "diff"}, fail_on_diff=True) == 1
    assert exit_code({"result": "error"}) == 2


# ---------------------------------------------------------------- against the test peers

def test_replay16_against_peer(tmp_path):
    async def go():
        peer = CsmsPeer(tag_statuses={"TAG1": "Invalid"})
        await peer.start()
        try:
            rp = Replayer(trace16(gap=0.02).trace(), csms_url=peer.url, timing="asap",
                          out_trace=tmp_path / "new.jsonl")

            return peer, await rp.run()
        finally:
            await peer.stop()

    peer, report = _run(go)
    s = peer.history[0]
    assert report["format"] == "ocppsim-replay/1" and report["result"] == "diff", report
    assert report["summary"]["calls"] == 5 and report["summary"]["sent"] == 5
    auth = next(e for e in report["calls"] if e["action"] == "Authorize")
    assert auth["match"] is False and auth["diff"] == ["idTagInfo.status: Accepted -> Invalid"]
    # 1.6 transactionId re-mapped from the new StartTransaction.conf
    assert s.starts[0]["transaction_id"] == 1
    assert s.stops[0]["transaction_id"] == 1 and s.meter_values[0]["transaction_id"] == 1
    # timestamps shifted to now, new uniqueIds
    calls = [json.loads(raw) for _d, raw in s.frames]
    start = next(f for f in calls if f[0] == 2 and f[2] == "StartTransaction")
    ts = datetime.fromisoformat(start[3]["timestamp"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 60
    stop = next(f for f in calls if f[0] == 2 and f[2] == "StopTransaction")
    assert stop[3]["timestamp"].endswith("Z")
    stop_ts = datetime.fromisoformat(stop[3]["timestamp"].replace("Z", "+00:00"))
    assert abs((stop_ts - ts).total_seconds() - 20) < 1.0   # the start time has no fraction (cut)
    assert not {f[1] for f in calls if f[0] == 2} & {"u1", "u2", "u3", "u4", "u5"}
    # new trace of the run
    new = fmt.load(tmp_path / "new.jsonl")
    assert new.header["chargers"][0]["replay_of"]["charger"] == "CP1"
    assert sum(1 for f in new.frames if f["direction"] == "tx" and f["message_type"] == "CALL") >= 5
    assert "replay" in to_text(report)


def test_replay16_answers_csms_calls_from_the_trace():
    async def go():
        peer = CsmsPeer()
        await peer.start()
        try:
            b = TraceBuilder(gap=0.3)
            b.call("BootNotification", {"chargePointVendor": "V", "chargePointModel": "M"},
                   {"status": "Accepted", "interval": 60, "currentTime": "2026-01-01T00:00:00Z"})
            b.csms_call("GetConfiguration", {}, {"configurationKey": [{"key": "K", "readonly": True, "value": "1"}]})
            b.csms_call("ChangeConfiguration", {"key": "K", "value": "2"}, error="NotSupported")
            b.call("Heartbeat", {}, {"currentTime": "2026-01-01T00:00:01Z"})
            b.call("Heartbeat", {}, {"currentTime": "2026-01-01T00:00:02Z"})
            rp = Replayer(b.trace(), csms_url=peer.url, timing="original")
            run = asyncio.create_task(rp.run())
            while "CP1" not in peer.sessions or not peer.sessions["CP1"].booted.is_set():
                await asyncio.sleep(0.01)
            got = await peer.request("CP1", call16.GetConfiguration())
            got2 = await peer.request("CP1", call16.GetConfiguration())   # 2nd: the last answer is re-used
            unknown = await peer.request("CP1", call16.ClearCache(), suppress=True)
            report = await run
            return got, got2, unknown, report
        finally:
            await peer.stop()

    got, got2, unknown, report = _run(go)
    assert got.configuration_key[0]["key"] == "K" and got2.configuration_key[0]["key"] == "K"
    assert unknown is None   # CALLERROR NotImplemented
    inbound = report["inbound"]
    assert [i["action"] for i in inbound] == ["GetConfiguration", "GetConfiguration", "ClearCache"]
    assert inbound[1]["reused"] is True and inbound[2] == {"action": "ClearCache", "occurrence": 1,
                                                           "answer": "not_implemented", "in_trace": False}
    assert report["result"] == "match" and report["summary"]["inbound_not_in_trace"] == 1


def test_timing_modes():
    async def go(timing):
        peer = CsmsPeer()
        await peer.start()
        try:
            b = TraceBuilder(gap=0.4)
            for i in range(4):
                b.call("Heartbeat", {}, {"currentTime": "2026-01-01T00:00:00Z"})
            t0 = time.monotonic()
            report = await Replayer(b.trace(), csms_url=peer.url, timing=timing).run()
            return time.monotonic() - t0, report
        finally:
            await peer.stop()

    original, r1 = _run(lambda: go("original"))
    scaled, r2 = _run(lambda: go("scale:4"))
    asap, r3 = _run(lambda: go("asap"))
    assert all(r["result"] == "match" and r["summary"]["sent"] == 4 for r in (r1, r2, r3))
    assert original >= 1.2 * 0.95 and scaled < original and asap < scaled
    assert scaled >= 0.3 * 0.9
    assert r2["timing"] == "scale:4"


def test_replay201_suffix_and_as(tmp_path):
    async def go(**kw):
        peer = CsmsPeer201()
        await peer.start()
        try:
            report = await Replayer(trace201().trace(), csms_url=peer.url, timing="asap", run_id="r00001",
                                    **kw).run()
            return peer, report
        finally:
            await peer.stop()

    peer, report = _run(go)
    ev = peer.history[0].transaction_events
    assert [e["transaction_info"]["transaction_id"] for e in ev] == ["tx-1-r00001"] * 2
    assert report["result"] == "match" and report["ocpp_version"] == "2.0.1"
    peer, report = _run(lambda: go(tx_suffix=False, as_id="CS900"))
    assert peer.history[0].cs_id == "CS900" and report["as"] == "CS900"
    assert peer.history[0].transaction_events[0]["transaction_info"]["transaction_id"] == "tx-1"


def test_security_profile1_password_is_injected():
    async def go(password):
        peer = CsmsPeer(passwords={"CP1": "s3cret-pw"})
        await peer.start()
        try:
            return await Replayer(trace16(gap=0.01).trace(), csms_url=peer.url, timing="asap",
                                  password=password).run()
        finally:
            await peer.stop()

    ok = _run(lambda: go("s3cret-pw"))
    assert ok["result"] == "match" and ok["security_profile"] == 1
    assert "s3cret-pw" not in json.dumps(ok)
    bad = _run(lambda: go(None))
    assert bad["result"] == "error" and "connect failed" in bad["error"]
    assert bad["summary"]["not_sent"] == 5 and exit_code(bad) == 2


def test_connection_lost_marks_the_rest_not_sent():
    async def go():
        peer = CsmsPeer()
        await peer.start()
        rp = Replayer(trace16(gap=0.01).trace(), csms_url=peer.url, timing="asap", call_timeout=2)
        gate = asyncio.Event()
        peer.gates["StartTransaction"] = gate
        run = asyncio.create_task(rp.run())
        while "CP1" not in peer.sessions:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        for ws in list(peer._server.connections):   # the CSMS drops the connection mid-transaction
            ws.transport.abort()
        report = await run
        gate.set()
        await peer.stop()
        return report

    report = _run(go)
    assert report["result"] == "error"
    kinds = [e["replayed"]["kind"] for e in report["calls"]]
    assert kinds[0] == "result" and "not_sent" in kinds


# ---------------------------------------------------------------- CLI / web / MCP

class ThreadPeer:
    """A CsmsPeer on its own event loop thread (for the CLI and the web app, which run their own loops)."""

    def __init__(self, **kw):
        self.loop = asyncio.new_event_loop()
        self.peer = CsmsPeer(**kw)
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.peer.start(), self.loop).result(10)
        return self.peer

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.peer.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def test_cli_replay(tmp_path, capsys):
    path = trace16(gap=0.01).write(tmp_path / "t.jsonl")
    with ThreadPeer(tag_statuses={"TAG1": "Blocked"}) as peer:
        out = tmp_path / "r.json"
        code = main(["replay", str(path), "--csms", peer.url, "--timing", "asap", "--out", str(out),
                     "--trace", str(tmp_path / "new.jsonl")])
        assert code == 0
        assert "differed 2" in capsys.readouterr().out   # Authorize + StartTransaction idTagInfo
        assert json.loads(out.read_text(encoding="utf-8"))["result"] == "diff"
        assert main(["replay", str(path), "--csms", peer.url, "--timing", "asap", "--fail-on-diff", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["summary"]["differed"] == 2
    assert (tmp_path / "new.jsonl").is_file()
    assert main(["replay", str(path), "--csms", "ws://127.0.0.1:1/ocpp", "--timing", "asap"]) == 2
    assert main(["replay", str(path), "--csms", "ws://x/ocpp", "--timing", "fast"]) == 2
    assert main(["replay", str(tmp_path / "missing.jsonl"), "--csms", "ws://x/ocpp"]) == 2


def _engine(tmp_path) -> OcppEngine:
    eng = OcppEngine(SimConfig.model_validate({"chargers": []}))
    eng.trace_start_path = str(tmp_path / "unused.jsonl")   # trace_dir() = tmp_path
    return eng


def test_web_replay_api_and_mcp_tools(tmp_path):
    eng = _engine(tmp_path)
    trace16(gap=0.01).write(eng.trace_dir() / "rec.jsonl")
    with ThreadPeer() as peer, TestClient(create_app(eng), base_url="http://127.0.0.1") as c:
        assert c.post("/api/replay", json={"file": "nope.jsonl", "csms_url": peer.url}).status_code == 404
        assert c.post("/api/replay", json={"file": "../x.jsonl", "csms_url": peer.url}).status_code == 400
        assert c.post("/api/replay", json={"file": "rec.jsonl", "csms_url": peer.url,
                                           "timing": "fast"}).status_code == 400
        r = c.post("/api/replay", json={"file": "rec.jsonl", "csms_url": peer.url, "timing": "asap",
                                        "password": "pw-not-echoed"})
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        deadline = time.time() + 15
        while True:
            st = c.get(f"/api/replay/{rid}").json()
            if st["state"] == "done" or time.time() > deadline:
                break
            time.sleep(0.05)
        assert st["state"] == "done" and st["report"]["result"] == "match", st
        assert "pw-not-echoed" not in json.dumps(st)
        assert (eng.trace_dir() / f"replay-{rid}.jsonl").is_file()
        assert [x["id"] for x in c.get("/api/replay").json()["replays"]] == [rid]
        assert c.get("/api/replay/zzz").status_code == 404
        assert c.post(f"/api/replay/{rid}/stop").json()["state"] == "done"
        app = c.app
    tools = {t["name"]: t for t in build_registry(app).list_tools()}
    assert {"start_replay", "get_replay"} <= set(tools)
