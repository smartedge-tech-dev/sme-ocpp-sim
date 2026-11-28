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
from sme_ocpp_sim.web import create_app

pytest.importorskip("mcp")

DEAD_URL = "ws://127.0.0.1:9/ocpp"  # nothing listens: chargers stay offline
ACCEPT = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
TOOLS = {
    "get_version", "get_state", "export_config", "set_simulation", "get_charger", "get_charger_settings",
    "get_charging_profiles", "get_composite_schedule", "get_frames", "get_impairments", "connect_charger",
    "disconnect_charger", "plug_in", "unplug", "authorize", "start_transaction", "stop_transaction", "set_fault",
    "clear_fault", "set_external_limit", "set_impairments", "drop_connection", "send_frame", "mini_csms_status",
    "mini_csms_call",
}


@pytest.fixture
def client():
    cfg = SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL, "reconnect": {"min_s": 0.05, "max_s": 0.2}},
                                    "chargers": [{"id": "CP001", "connectors": 2, "id_tags": ["TAG1"]}]})
    engine = OcppEngine(cfg)
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


def test_main_json_logs(monkeypatch, tmp_path):
    seen = {}

    def fake_run_web(app, args, name):
        seen["args"] = args
        seen["paths"] = {getattr(r, "path", None) for r in app.routes}

    monkeypatch.setattr(cli, "run_web", fake_run_web)
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
    assert seen["args"].log_format == "json"
    assert {"/mcp", "/healthz"} <= seen["paths"]
