"""Web API (plan §5 M6): product routes, errors, password masking, config save, /ws, bulk."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import PASSWORD_MASK, SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.web import create_app, mask_text

FAST = {"min_s": 0.05, "max_s": 0.2, "jitter": 0}
DEAD_URL = "ws://127.0.0.1:9/ocpp"  # nothing listens: chargers stay offline

CONFIG_TEXT = f"""\
# comment kept by save
csms_defaults:
  url: {DEAD_URL}
  security_profile: 1
  reconnect: {{min_s: 0.05, max_s: 0.2, jitter: 0}}
chargers:
  - id: CP001
    password: "s3cret#pw"  # inline comment
    connectors: 2
    id_tags: [TAG1]
  - id: CP002
    password: other-secret
"""

PRODUCT_ROUTES = {
    ("post", "/api/chargers/{cid}/" + a) for a in
    ("connect", "disconnect", "plug", "unplug", "swipe", "start", "stop", "fault", "clear-fault", "datatransfer")
} | {("get", "/api/chargers/{cid}/profiles"), ("get", "/api/chargers/{cid}/composite"), ("get", "/api/frames"),
     ("get", "/api/frames.jsonl"), ("post", "/api/chargers/bulk/connect"), ("post", "/api/chargers/bulk/disconnect")}


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "sim.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    cfg = SimConfig.model_validate(yaml.safe_load(CONFIG_TEXT))
    engine = OcppEngine(cfg, config_text=CONFIG_TEXT, config_path=path)
    engine.push_interval = 0.05
    engine.stop_grace_s = 0.2
    with TestClient(create_app(engine, path)) as client:
        yield client, engine, path


def _frame(engine, charger, direction, action):
    engine.frame_log.add(charger, SimpleNamespace(direction=direction, message_type=2, uid="u", action=action,
                                                  raw="[2]"))


def _conn(client, cid, n):
    ch = next(c for c in client.get("/api/state").json()["chargers"] if c["id"] == cid)
    return ch, next(c for c in ch["connectors"] if c["id"] == n)


# ------------------------------------------------------------------ OpenAPI and errors

def test_openapi_lists_all_routes(setup):
    client, _, _ = setup
    paths = client.get("/openapi.json").json()["paths"]
    listed = {(m, p) for p, ops in paths.items() for m in ops}
    assert PRODUCT_ROUTES <= listed


@pytest.mark.parametrize("method,path", sorted(r for r in PRODUCT_ROUTES if "{cid}" in r[1]))
def test_unknown_charger_is_404(setup, method, path):
    client, _, _ = setup
    body = {"vendor_id": "V"} if path.endswith("datatransfer") else None
    r = client.request(method.upper(), path.replace("{cid}", "NOPE"), json=body)
    assert r.status_code == 404 and "NOPE" in r.json()["error"]


@pytest.mark.parametrize("path,body", [
    ("plug", {"connector": 3}), ("unplug", {"connector": 0}), ("swipe", {"connector": 1}),
    ("stop", {"connector": 1}), ("fault", {"error_code": "NoError"}), ("fault", {"error_code": "Bogus"}),
    ("clear-fault", {"connector": 1}), ("datatransfer", {"vendor_id": "V"}), ("datatransfer", {}),
    ("plug", {"connector": "x"}), ("start", {"connector": 9}),
])
def test_bad_input_or_state_is_400(setup, path, body):
    client, _, _ = setup
    r = client.post(f"/api/chargers/CP001/{path}", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]


def test_start_without_tags_is_400(setup):
    client, _, _ = setup
    r = client.post("/api/chargers/CP002/start")
    assert r.status_code == 400 and "id_tag" in r.json()["error"]


def test_invalid_query_is_400(setup):
    client, _, _ = setup
    assert client.get("/api/frames?dir=up").status_code == 400
    assert client.get("/api/frames?since=-1").status_code == 400
    assert client.get("/api/chargers/CP001/composite?duration=0").status_code == 400
    assert client.get("/api/chargers/CP001/composite?connector=5").status_code == 400
    assert client.get("/api/chargers/CP001/composite?unit=kW").status_code == 400
    assert client.get("/api/chargers/CP001/profiles?connector=7").status_code == 400
    assert client.post("/api/simulation", json={"bogus": 1}).status_code == 400


# ------------------------------------------------------------------ connector operations (offline)

def test_plug_fault_clear_unplug(setup):
    client, _, _ = setup
    assert client.post("/api/chargers/CP001/plug", json={"connector": 2}).json() == {"ok": True}
    assert _conn(client, "CP001", 2)[1]["plugged"] is True
    assert client.post("/api/chargers/CP001/fault", json={"connector": 2}).status_code == 200
    c = _conn(client, "CP001", 2)[1]
    assert c["status"] == "Faulted" and c["error_code"] == "OtherError"
    assert client.post("/api/chargers/CP001/start", json={"connector": 2}).status_code == 400  # faulted
    assert client.post("/api/chargers/CP001/clear-fault", json={"connector": 2}).status_code == 200
    assert _conn(client, "CP001", 2)[1]["status"] == "Preparing"
    assert client.post("/api/chargers/CP001/unplug", json={"connector": 2}).status_code == 200
    assert _conn(client, "CP001", 2)[1]["status"] == "Available"
    assert client.post("/api/chargers/CP001/plug").status_code == 200  # connector defaults to 1
    assert _conn(client, "CP001", 1)[1]["plugged"] is True


def test_profiles_and_composite(setup):
    client, _, _ = setup
    assert client.get("/api/chargers/CP001/profiles").json() == {"profiles": []}
    r = client.get("/api/chargers/CP001/composite?connector=1&duration=60&unit=W").json()
    assert r["connector"] == 1 and r["unit"] == "W" and r["duration"] == 60
    assert r["periods"][0]["limit"] == 6000.0
    whole = client.get("/api/chargers/CP001/composite?evse=0&duration=60").json()
    assert whole["connector"] == 0 and whole["periods"][0]["limit"] == 12000.0  # 2 connectors
    amps = client.get("/api/chargers/CP001/composite?connector=1&duration=60&unit=A").json()
    assert amps["unit"] == "A" and amps["periods"][0]["limit"] == pytest.approx(30.0)


# ------------------------------------------------------------------ frames

def test_frames_filters_and_jsonl(setup):
    client, engine, _ = setup
    for i in range(5):
        _frame(engine, "CP001", "tx", "Heartbeat")
    _frame(engine, "CP002", "rx", "Reset")
    r = client.get("/api/frames").json()
    assert [f["seq"] for f in r["frames"]] == [1, 2, 3, 4, 5, 6] and r["last_seq"] == 6 and not r["truncated"]
    assert [f["seq"] for f in client.get("/api/frames?since=4").json()["frames"]] == [5, 6]
    assert [f["seq"] for f in client.get("/api/frames?charger=CP002").json()["frames"]] == [6]
    assert [f["seq"] for f in client.get("/api/frames?dir=rx&action=Reset").json()["frames"]] == [6]
    lim = client.get("/api/frames?limit=2").json()
    assert [f["seq"] for f in lim["frames"]] == [5, 6] and lim["truncated"]
    j = client.get("/api/frames.jsonl?charger=CP001&since=3")
    assert j.headers["content-type"].startswith("application/x-ndjson")
    assert [int(line.split('"seq": ')[1].split(",")[0]) for line in j.text.splitlines()] == [4, 5]


# ------------------------------------------------------------------ connect / disconnect / bulk

def test_connect_disconnect_and_bulk(setup):
    client, engine, _ = setup

    def enabled():
        return {c["id"]: c["enabled"] for c in client.get("/api/state").json()["chargers"]}

    assert enabled() == {"CP001": True, "CP002": True}
    assert client.post("/api/chargers/CP001/disconnect").json() == {"ok": True, "changed": True}
    assert client.post("/api/chargers/CP001/disconnect").json()["changed"] is False
    assert enabled() == {"CP001": False, "CP002": True}
    assert client.post("/api/chargers/CP001/connect").json()["changed"] is True
    assert enabled()["CP001"] is True

    assert client.post("/api/chargers/bulk/disconnect", json={"all": True}).json()["changed"] == \
        {"CP001": True, "CP002": True}
    assert enabled() == {"CP001": False, "CP002": False}
    # the disconnect survives a config reload
    assert client.put("/api/config", content=client.get("/api/config").text).status_code == 200
    assert enabled() == {"CP001": False, "CP002": False} and not engine._runs
    assert client.post("/api/chargers/bulk/connect", json=["CP002"]).json()["changed"] == {"CP002": True}
    assert client.post("/api/chargers/bulk/connect", json={"ids": ["CP001", "CP002"]}).json()["changed"] == \
        {"CP001": True, "CP002": False}
    assert client.post("/api/chargers/bulk/connect", json={"ids": ["CP001", "X"]}).status_code == 404
    assert client.post("/api/chargers/bulk/connect", json={"ids": []}).status_code == 400
    assert client.post("/api/chargers/bulk/connect", json=5).status_code == 400


# ------------------------------------------------------------------ secrets and config

def test_password_masking(setup):
    client, engine, _ = setup
    assert "secret" not in client.get("/api/state").text
    text = client.get("/api/config").text
    assert "s3cret" not in text and "other-secret" not in text and text.count(PASSWORD_MASK) == 2
    assert "# comment kept by save" in text
    dev = client.get("/api/devices/CP001/config").text
    assert "s3cret" not in dev and PASSWORD_MASK in dev

    # the masked text sent back keeps the stored passwords
    assert client.put("/api/config", content=text).status_code == 200
    assert [c.password for c in engine.config.chargers] == ["s3cret#pw", "other-secret"]
    assert client.put("/api/devices/CP002/config", content=dev.replace("CP001", "CP002")).status_code == 200
    assert engine.config.chargers[1].password == "other-secret"
    # a new charger cannot inherit a mask
    bad = text + f"  - id: CP003\n    password: \"{PASSWORD_MASK}\"\n"
    assert client.put("/api/config", content=bad).status_code == 400

    dup = client.post("/api/devices/CP001/duplicate", json={}).json()
    assert dup["name"] == "CP003" and dup["device"]["password"] == PASSWORD_MASK
    assert engine.config.chargers[-1].password == "s3cret#pw"


def test_mask_text_falls_back_for_flow_style():
    out = mask_text("chargers: [{id: A, password: pw}]\n")
    assert "pw" not in out.replace(PASSWORD_MASK, "") and PASSWORD_MASK in out
    assert mask_text("chargers:\n  - id: A\n    password: null\n") == "chargers:\n  - id: A\n    password: null\n"


def test_config_save_keeps_comments_and_password(setup):
    client, engine, path = setup
    assert client.post("/api/simulation", json={"time_scale": 2}).status_code == 200
    r = client.post("/api/config/save")
    assert r.status_code == 200 and r.json()["path"] == str(path)
    saved = path.read_text(encoding="utf-8")
    assert "# comment kept by save" in saved and "s3cret#pw" in saved and "time_scale: 2" in saved
    assert "s3cret" not in client.get("/api/config").text


def test_config_save_without_file_is_400():
    engine = OcppEngine(SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [{"id": "A"}]}))
    with TestClient(create_app(engine)) as client:
        assert client.post("/api/config/save").status_code == 400
        assert client.post("/api/devices/A/duplicate", json={}).json()["name"] == "A-2"


# ------------------------------------------------------------------ /ws

def test_ws_snapshot_then_increments(setup):
    client, _, _ = setup
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and {c["id"] for c in first["data"]["chargers"]} == {"CP001", "CP002"}
        seqs, msgs = [], []

        def push():  # engine pushes carry push_seq; "log" events (own event seq) are not counted
            while True:
                m = ws.receive_json()
                if "push_seq" in m or m["type"] in ("charger_state", "values", "frame"):
                    seqs.append(m["push_seq"])
                    return m
                assert m["type"] in ("log", "stats"), m

        while len(msgs) < 2:
            msgs.append(push())
        assert [m["type"] for m in msgs] == ["charger_state", "values"] and all(m["full"] for m in msgs)
        client.post("/api/chargers/CP001/plug", json={"connector": 1})
        m = push()
        while m["type"] != "charger_state":
            m = push()
        assert not m["full"] and [c["id"] for c in m["chargers"]] == ["CP001"]
        assert m["chargers"][0]["connectors"][0]["status"] == "Preparing"
        assert seqs == list(range(1, len(seqs) + 1))  # per-connection, no gaps, no duplicates


# ------------------------------------------------------------------ online: start / stop / swipe / datatransfer

def test_online_operations():
    async def main():
        peer = CsmsPeer(hb_interval=30)
        await peer.start()
        cfg = SimConfig.model_validate({"csms_defaults": {"url": peer.url, "reconnect": FAST},
                                        "chargers": [{"id": "CP", "auth_mode": "accept_all", "id_tags": ["T1"]}]})
        engine = OcppEngine(cfg)
        await engine.start()
        rt = engine.charger("CP")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)),
                                         base_url="http://t") as c:
                async with asyncio.timeout(10):
                    while not rt.online:
                        await asyncio.sleep(0.02)
                r = (await c.post("/api/chargers/CP/start")).json()
                assert r == {"ok": True, "status": "Accepted", "id_tag": "T1"}
                assert (await c.post("/api/chargers/CP/start")).status_code == 400  # already running
                assert (await c.post("/api/chargers/CP/stop")).json() == {"ok": True}
                assert (await c.post("/api/chargers/CP/stop")).status_code == 400
                assert (await c.post("/api/chargers/CP/swipe", json={"id_tag": "T2"})).json()["status"] == "Accepted"
                assert (await c.post("/api/chargers/CP/swipe", json={"id_tag": "T2"})).json()["status"] == "Stopped"
                r = (await c.post("/api/chargers/CP/datatransfer", json={"vendor_id": "V", "data": "ping"})).json()
                assert r == {"ok": True, "status": "Accepted", "data": "pong"}
                assert peer.sessions["CP"].data_transfers[-1]["vendor_id"] == "V"
                assert (await c.post("/api/chargers/CP/disconnect")).json()["changed"] is True
                assert not rt.connected
                assert (await c.post("/api/chargers/CP/datatransfer", json={"vendor_id": "V"})).status_code == 400
                assert (await c.post("/api/chargers/CP/connect")).json()["changed"] is True
                async with asyncio.timeout(10):
                    while not rt.online:
                        await asyncio.sleep(0.02)
        finally:
            await engine.stop()
            await peer.stop()

    asyncio.run(main())


def test_operator_connect_needs_running_engine():
    engine = OcppEngine(SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [{"id": "A"}]}))
    app = create_app(engine)

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/chargers/A/connect")
            assert r.status_code == 400 and "not running" in r.json()["error"]

    asyncio.run(main())
