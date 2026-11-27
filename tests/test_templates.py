"""Charger templates (plan M10): built-ins, expansion in the config, dumps without template values, user
templates, id counting and the bulk create route."""

import pytest
import yaml
from fastapi.testclient import TestClient

from sme_ocpp_sim import templates_builtin
from sme_ocpp_sim.config import SimConfig, parse_text, write_back
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.templates_builtin import REGISTRY, TEMPLATE_FIELDS, next_ids, registry
from sme_ocpp_sim.web import create_app

DEAD_URL = "ws://127.0.0.1:9/ocpp"


def _cfg(*chargers, **top):
    return SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": list(chargers), **top})


@pytest.fixture
def user_dir(tmp_path):
    yield tmp_path
    REGISTRY.clear_user_templates()
    REGISTRY._user_dir = None  # back to the default directory for the other tests


@pytest.mark.parametrize("name", REGISTRY.names())
@pytest.mark.parametrize("version", ["1.6", "2.0.1"])
def test_builtin_templates_make_valid_chargers(name, version):
    cfg = _cfg({"id": "CP1", "template": name, "ocpp_version": version})
    c = cfg.chargers[0]
    fields = REGISTRY.builtins[name]["extras"]
    assert set(fields) <= set(TEMPLATE_FIELDS)
    assert all(getattr(c, k) == v for k, v in fields.items())
    assert c.max_power_w <= c.voltage_v * c.phases * 32  # a plausible AC rating (<= 32 A per phase)


def test_builtin_catalog():
    names = registry().names()
    assert {"jp-ac-3kw", "jp-ac-6kw", "jp-ac-6kw-dual", "eu-ac-11kw", "eu-ac-22kw"} <= set(names)
    entry = next(e for e in registry().catalog() if e["name"] == "jp-ac-6kw")
    assert entry["source"] == "builtin" and "200 V" in entry["title"] and entry["description"]


def test_explicit_fields_override_template_and_dump_keeps_only_overrides():
    cfg = _cfg({"id": "A", "template": "jp-ac-6kw-dual", "max_power_w": 3000, "model": "SIM-AC6D"},
               {"id": "B", "template": "eu-ac-22kw", "config_keys": {"HeartbeatInterval": 60}})
    a, b = cfg.chargers
    assert (a.connectors, a.max_power_w, a.model) == (2, 3000.0, "SIM-AC6D")
    assert (b.phases, b.voltage_v, b.max_power_w) == (3, 230.0, 22000.0)
    dumped = cfg.model_dump(mode="json", exclude_defaults=True)["chargers"]
    # model equals the template's value: left out; max_power_w differs: kept
    assert dumped[0] == {"id": "A", "template": "jp-ac-6kw-dual", "max_power_w": 3000.0}
    assert dumped[1] == {"id": "B", "template": "eu-ac-22kw", "config_keys": {"HeartbeatInterval": "60"}}
    assert SimConfig.model_validate(cfg.model_dump()) == cfg


def test_write_back_keeps_template_reference_short():
    text = f"csms_defaults:\n  url: {DEAD_URL}\nchargers: []\n"
    cfg = _cfg({"id": "CP1", "template": "jp-ac-6kw-dual"})
    out = write_back(text, cfg)
    assert yaml.safe_load(out)["chargers"] == [{"id": "CP1", "template": "jp-ac-6kw-dual"}]
    assert parse_text(out).chargers[0].connectors == 2


def test_unknown_template_and_bad_params_rejected():
    with pytest.raises(ValueError, match="unknown template 'nope'"):
        _cfg({"id": "A", "template": "nope"})
    with pytest.raises(ValueError, match="quoted string"):
        _cfg({"id": "A", "template": 5})


def test_user_template_with_params(user_dir):
    (user_dir / "wallbox.yaml").write_text(
        "title: My wallbox\nparams: {kw: 7}\nmodel: WB-{kw}\nmax_power_w: '{kw}'\n"
        "config_keys: {MeterValueSampleInterval: '60'}\n", encoding="utf-8")
    (user_dir / "bad.yaml").write_text("max_power_w: -1\n", encoding="utf-8")
    (user_dir / "secret.yaml").write_text("password: x\n", encoding="utf-8")
    errors = REGISTRY.load_user_templates(user_dir)
    assert sorted(e["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for e in errors) == ["bad.yaml", "secret.yaml"]
    assert "max_power_w" in next(e["error"] for e in errors if e["path"].endswith("bad.yaml"))
    cfg = _cfg({"id": "A", "template": "wallbox"}, {"id": "B", "template": "wallbox", "params": {"kw": 11}})
    a, b = cfg.chargers
    assert (a.model, a.max_power_w, a.config_keys) == ("WB-7", 7.0, {"MeterValueSampleInterval": "60"})
    assert (b.model, b.max_power_w) == ("WB-11", 11.0)
    assert cfg.model_dump(exclude_defaults=True)["chargers"][1] == {"id": "B", "template": "wallbox",
                                                                     "params": {"kw": 11}}


def test_user_template_overrides_builtin(user_dir):
    (user_dir / "jp-ac-6kw.yaml").write_text("model: OWN-6\n", encoding="utf-8")
    assert REGISTRY.load_user_templates(user_dir) == []
    assert _cfg({"id": "A", "template": "jp-ac-6kw"}).chargers[0].model == "OWN-6"
    REGISTRY.clear_user_templates()
    assert _cfg({"id": "A", "template": "jp-ac-6kw"}).chargers[0].model == "SIM-AC6"


def test_next_ids():
    assert next_ids("CP001", 3) == ["CP001", "CP002", "CP003"]
    assert next_ids("CP-0099", 2) == ["CP-0099", "CP-0100"]
    assert next_ids("X9", 2) == ["X9", "X10"]
    assert next_ids("CP-", 3) == ["CP-1", "CP-2", "CP-3"]
    assert next_ids("solo", 1) == ["solo"]


# ------------------------------------------------------------------ web API

@pytest.fixture
def client(user_dir, monkeypatch):
    monkeypatch.setenv(REGISTRY.env_var, str(user_dir))
    cfg = SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL, "security_profile": 1,
                                                      "reconnect": {"min_s": 0.05, "max_s": 0.2, "jitter": 0}},
                                    "chargers": [{"id": "CP001", "password": "pw"}]})
    engine = OcppEngine(cfg)
    engine.stop_grace_s = 0.2
    REGISTRY.load_user_templates(user_dir)
    with TestClient(create_app(engine)) as c:
        yield c, engine


def test_bulk_create_from_template(client):
    c, engine = client
    r = c.post("/api/chargers/bulk/create", json={"id": "CP002", "count": 3, "template": "jp-ac-6kw-dual",
                                                  "password": "pass01", "fields": {"id_tags": ["T1"]}})
    assert r.status_code == 200, r.text
    assert r.json()["ids"] == ["CP002", "CP003", "CP004"]
    new = {ch.id: ch for ch in engine.config.chargers}
    assert [new[i].password for i in ("CP002", "CP003", "CP004")] == ["pass01", "pass02", "pass03"]
    assert all(new[i].connectors == 2 and new[i].template == "jp-ac-6kw-dual" and new[i].id_tags == ["T1"]
               for i in ("CP002", "CP003", "CP004"))
    assert new["CP001"].password == "pw"


def test_bulk_create_conflicts_and_errors(client):
    c, engine = client
    r = c.post("/api/chargers/bulk/create", json={"id": "CP000", "count": 2, "password": "x"})
    assert r.status_code == 409 and "CP001" in r.json()["error"]
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "count": 2, "template": "nope", "password": "x"})
    assert r.status_code == 400 and "unknown template" in r.json()["error"]
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "count": 2})  # profile 1 needs a password
    assert r.status_code == 400 and "password" in r.json()["error"]
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "fields": {"id": "Z"}, "password": "x"})
    assert r.status_code == 400
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "count": 0})
    assert r.status_code == 400
    assert [ch.id for ch in engine.config.chargers] == ["CP001"]


def test_templates_routes_and_save_from_charger(client, user_dir):
    c, engine = client
    names = [t["name"] for t in c.get("/api/templates").json()]
    assert "jp-ac-6kw" in names
    assert c.post("/api/chargers/bulk/create", json={"id": "S1", "template": "eu-ac-11kw", "password": "p",
                                                     "fields": {"max_power_w": 7000}}).status_code == 200
    r = c.post("/api/templates/from-device/S1", json={"name": "my-11kw"})
    assert r.status_code == 200, r.text
    saved = yaml.safe_load((user_dir / "my-11kw.yaml").read_text(encoding="utf-8"))
    assert saved["max_power_w"] == 7000.0 and saved["phases"] == 3 and "password" not in saved
    assert "id" not in saved and "csms_url" not in saved
    assert "my-11kw" in [t["name"] for t in c.get("/api/templates").json()]
    r = c.post("/api/chargers/bulk/create", json={"id": "U1", "template": "my-11kw", "password": "p"})
    assert r.status_code == 200
    u = next(ch for ch in engine.config.chargers if ch.id == "U1")
    assert (u.max_power_w, u.phases, u.model) == (7000.0, 3, "SIM-AC11")
    assert c.post("/api/templates/reload").json()["loaded"] == ["my-11kw"]


def test_template_values_cache_follows_reload(user_dir):
    (user_dir / "t1.yaml").write_text("model: ONE\n", encoding="utf-8")
    REGISTRY.load_user_templates(user_dir)
    assert templates_builtin.template_values("t1", {})["model"] == "ONE"
    (user_dir / "t1.yaml").write_text("model: TWO\n", encoding="utf-8")
    REGISTRY.load_user_templates(user_dir)
    assert templates_builtin.template_values("t1", {})["model"] == "TWO"


# ------------------------------------------------------------------ review fixes (M10)

def test_next_ids_width_overflow_and_prefix():
    assert next_ids("CP09", 12) == [f"CP{i:02d}" for i in range(9, 21)]
    assert next_ids("CP9", 3) == ["CP9", "CP10", "CP11"]
    assert next_ids("CP", 3) == ["CP1", "CP2", "CP3"]  # no trailing digits: a prefix
    assert next_ids("CP", 1) == ["CP"]


def test_bulk_create_ids_passwords_and_bad_fields(client):
    c, engine = client
    r = c.post("/api/chargers/bulk/create", json={"id": "CP09", "count": 12, "password": "pw9"})
    assert r.status_code == 200, r.text
    assert r.json()["ids"] == [f"CP{i:02d}" for i in range(9, 21)]
    got = {ch.id: ch.password for ch in engine.config.chargers}
    assert (got["CP09"], got["CP10"], got["CP20"]) == ("pw9", "pw10", "pw20")
    r = c.post("/api/chargers/bulk/create", json={"id": "X", "count": 3, "password": "p"})
    assert r.status_code == 200 and r.json()["ids"] == ["X1", "X2", "X3"]
    assert {ch.password for ch in engine.config.chargers if ch.id.startswith("X")} == {"p"}
    n = len(engine.config.chargers)
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "password": "p", "fields": {"nonsense": 1}})
    assert r.status_code == 400 and "nonsense" in r.json()["error"]
    r = c.post("/api/chargers/bulk/create", json={"id": "X3", "count": 2, "password": "p"})  # X3 exists
    assert r.status_code == 409 and "X3" in r.json()["error"]
    r = c.post("/api/chargers/bulk/create", json={"id": "N1", "count": 2, "password": "********"})
    assert r.status_code == 400 and "mask" in r.json()["error"]
    assert len(engine.config.chargers) == n


def test_deleted_user_template_fails_with_clear_error(user_dir):
    (user_dir / "gone.yaml").write_text("model: GONE\nphases: 3\n", encoding="utf-8")
    REGISTRY.load_user_templates(user_dir)
    cfg = _cfg({"id": "CPG", "template": "gone"})
    (user_dir / "gone.yaml").unlink()
    REGISTRY.load_user_templates(user_dir)
    dumped = cfg.chargers[0].model_dump(mode="json", exclude_defaults=True)
    assert dumped["model"] == "GONE" and dumped["phases"] == 3  # nothing to compare with: written in full
    with pytest.raises(ValueError, match=r"charger 'CPG': unknown template 'gone'.*restore the user template"):
        parse_text(write_back("", cfg))
