"""MCP endpoint POST /mcp (v0.2.2 D), /healthz and the JSON log format."""
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

import sme_ocpp_sim.__main__ as cli
from sme_ocpp_sim import __version__
from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.mcp_tools import build_registry
import sme_ocpp_sim.web as web
from sme_ocpp_sim.web import create_app

DEAD_URL = "ws://127.0.0.1:9/ocpp"  # nothing listens: chargers stay offline
ACCEPT = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
TOOLS = {
    "get_version", "get_state", "export_config", "set_simulation", "get_charger", "get_charger_settings",
    "get_charging_profiles", "get_composite_schedule", "get_frames", "get_impairments", "connect_charger",
    "disconnect_charger", "plug_in", "unplug", "authorize", "start_transaction", "stop_transaction", "set_fault",
    "clear_fault", "set_external_limit", "set_impairments", "drop_connection", "send_frame", "mini_csms_status",
    "mini_csms_call", "data_transfer", "mini_csms_actions",
}


@pytest.fixture
def client():
    cfg = SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL, "reconnect": {"min_s": 0.05, "max_s": 0.2}},
                                    "chargers": [{"id": "CP001", "connectors": 2, "id_tags": ["TAG1"]}]})
    engine = OcppEngine(cfg)
    engine.stop_grace_s = 0.2  # chargers dialling the dead port: do not wait the full grace on teardown
    with TestClient(create_app(engine), base_url="http://127.0.0.1") as c:
        yield c, engine


def rpc(c, method, params=None, **headers):
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
               headers={**ACCEPT, **headers})
    return r


def call(c, name, **arguments):
    r = rpc(c, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_healthz(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["version"] == __version__


def test_tools_list(client):
    c, _ = client
    r = rpc(c, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "pytest", "version": "0"}})
    info = r.json()["result"]["serverInfo"]
    assert info["name"] == "sme-ocpp-sim" and info["version"] == __version__
    tools = {t["name"]: t for t in rpc(c, "tools/list").json()["result"]["tools"]}
    assert set(tools) == TOOLS
    assert all(t["description"] and t["inputSchema"]["type"] == "object" for t in tools.values())
    assert tools["get_charger"]["annotations"] == {"readOnlyHint": True}
    assert tools["send_frame"]["annotations"] == {"destructiveHint": True}
    assert tools["plug_in"]["inputSchema"]["required"] == ["cid"]
    assert "OtherError" in tools["set_fault"]["inputSchema"]["properties"]["error_code"]["enum"]
    assert "block" in tools["set_impairments"]["inputSchema"]["properties"]


def test_registry_without_endpoint():
    app = create_app(OcppEngine(), mcp=False)
    assert "/mcp" not in {getattr(r, "path", None) for r in app.routes}
    assert {t["name"] for t in build_registry(app).list_tools()} == TOOLS


def test_call_tools(client):
    c, engine = client
    res = call(c, "get_charger", cid="CP001")
    assert not res.get("isError") and res["structuredContent"]["id"] == "CP001"
    state = call(c, "get_state")["structuredContent"]
    assert "CP001" in json.dumps(state)

    res = call(c, "plug_in", cid="CP001", connector=2)
    assert not res.get("isError") and res["structuredContent"] == {"ok": True}, res

    res = call(c, "set_impairments", cid="CP001", block={"Reset": "NoReply"}, latency_ms=10)
    assert not res.get("isError"), res
    assert engine.config.chargers[0].impairments.block == {"Reset": "NoReply"}
    assert call(c, "get_impairments", cid="CP001")["structuredContent"]["latency_ms"] == 10


def test_tool_errors(client):
    c, _ = client
    res = call(c, "get_charger", cid="NOPE")
    assert res["isError"] and "404" in res["content"][0]["text"]
    res = call(c, "plug_in", cid="CP001", connector=0)  # schema: minimum 1
    assert res["isError"]
    res = call(c, "send_frame", cid="CP001", action="Heartbeat", payload={})  # offline: 409 from the route
    assert res["isError"] and "409" in res["content"][0]["text"]


def test_foreign_host_refused(client):
    c, _ = client
    r = rpc(c, "tools/list", Host="evil.example")
    assert r.status_code >= 400


def test_foreign_host_accepted_on_a_lan_host():
    with TestClient(create_app(OcppEngine(), host="0.0.0.0"), base_url="http://127.0.0.1") as c:
        r = rpc(c, "tools/list", Host="evil.example")
        assert r.status_code == 200 and len(r.json()["result"]["tools"]) == len(TOOLS)


# Minimal valid arguments of every tool: the call must get past the input schema and the route's body / query
# validation; a domain error of the route (offline charger, Mini CSMS off, 1.6 ...) is fine.
MINIMAL = {
    "get_version": {}, "get_state": {}, "export_config": {}, "set_simulation": {"time_scale": 1},
    "get_charger": {"cid": "CP001"}, "get_charger_settings": {"cid": "CP001"},
    "get_charging_profiles": {"cid": "CP001"}, "get_composite_schedule": {"cid": "CP001", "duration": 60},
    "get_frames": {"dir": "tx", "limit": 5}, "get_impairments": {"cid": "CP001"},
    "connect_charger": {"cid": "CP001"}, "disconnect_charger": {"cid": "CP001"},
    "plug_in": {"cid": "CP001"}, "unplug": {"cid": "CP001"}, "authorize": {"cid": "CP001", "id_tag": "TAG1"},
    "start_transaction": {"cid": "CP001"}, "stop_transaction": {"cid": "CP001"},
    "data_transfer": {"cid": "CP001", "vendor_id": "acme"}, "set_fault": {"cid": "CP001"},
    "clear_fault": {"cid": "CP001"}, "set_external_limit": {"cid": "CP001"}, "set_impairments": {"cid": "CP001"},
    "drop_connection": {"cid": "CP001"}, "send_frame": {"cid": "CP001", "action": "Heartbeat"},
    "mini_csms_status": {}, "mini_csms_actions": {"version": "1.6"},
    "mini_csms_call": {"cid": "CP001", "action": "Reset", "payload": {"type": "Soft"}},
}
PYDANTIC_MISMATCH = ("Field required", "Extra inputs are not permitted", "Input should be", "unexpected argument")


def test_minimal_args_cover_every_tool():
    assert set(MINIMAL) == TOOLS


@pytest.mark.parametrize("name", sorted(MINIMAL))
def test_every_tool_accepts_minimal_args(client, name):
    c, _ = client
    res = call(c, name, **MINIMAL[name])
    text = res["content"][0]["text"]
    if res.get("isError"):  # only a route's own answer, never the schema check or the body / query model
        assert text.startswith("HTTP "), text
        assert not any(m in text for m in PYDANTIC_MISMATCH), text


def test_get_frames_query_string(client, monkeypatch):
    c, engine = client
    seen = {}

    def since(seq, **kw):
        seen.update(seq=seq, **kw)
        return []

    monkeypatch.setattr(engine.frame_log, "since", since)
    res = call(c, "get_frames", since=3, dir="tx", limit=5, charger="CP001", action="Heartbeat")
    assert not res.get("isError"), res
    assert res["structuredContent"]["frames"] == []
    assert seen == {"seq": 3, "limit": 6, "charger": "CP001", "action": "Heartbeat", "direction": "tx"}
    assert call(c, "get_frames", dir="up")["isError"]  # enum of the input schema


def test_send_frame_validate_alias(client):
    c, _ = client
    res = call(c, "send_frame", cid="CP001", action="NotAnAction")  # validate: true (default) checks the action
    assert res["isError"] and "HTTP 400" in res["content"][0]["text"]
    res = call(c, "send_frame", cid="CP001", action="NotAnAction", validate=False)  # unchecked: offline is next
    assert res["isError"] and "HTTP 409" in res["content"][0]["text"]


def test_start_transaction_default_id_tag(client):
    c, _ = client
    res = call(c, "start_transaction", cid="CP001")
    text = res["content"][0]["text"]
    assert "id_tag: required" not in text
    if not res.get("isError"):
        assert res["structuredContent"]["id_tag"] == "TAG1"
    assert call(c, "authorize", cid="CP001")["isError"]  # authorize: id_tag required by the schema


def test_main_json_logs(monkeypatch, tmp_path):
    seen = {}

    def fake_run_web(app, args, name):
        seen["args"] = args
        seen["paths"] = {getattr(r, "path", None) for r in app.routes}

    real_create_app = web.create_app

    def spy_create_app(engine, path, **kw):
        seen["host"] = kw.get("host")
        return real_create_app(engine, path, **kw)

    monkeypatch.setattr(cli, "run_web", fake_run_web)
    monkeypatch.setattr(web, "create_app", spy_create_app)
    monkeypatch.setattr(cli, "default_config_path", lambda: None)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        assert cli.main(["--no-browser", "--log-format", "json", "--host", "0.0.0.0"]) == 0
        handler = root.handlers[0]
        buf = io.StringIO()
        handler.setStream(buf)
        logging.getLogger("sme-ocpp-sim").info("hello %s", "json")
        line = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert line["message"] == "hello json" and line["level"] == "INFO" and line["logger"] == "sme-ocpp-sim"
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
    assert seen["args"].log_format == "json" and seen["host"] == "0.0.0.0"
    assert {"/mcp", "/healthz"} <= seen["paths"]
