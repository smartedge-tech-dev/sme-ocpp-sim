"""Excel import / export of the charger list (plan M10): round trip, text cells, row / column errors, masked
passwords, the web routes and the per-charger reload."""

import io

import openpyxl
import yaml
import pytest
from fastapi.testclient import TestClient

from sme_ocpp_sim.config import PASSWORD_MASK, SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.web import create_app
from sme_ocpp_sim.xlsx_io import SCHEMA, export_xlsx, import_xlsx

DEAD_URL = "ws://127.0.0.1:9/ocpp"
FAST = {"min_s": 0.05, "max_s": 0.2, "jitter": 0}


def _config(**extra):
    return SimConfig.model_validate({
        "csms_defaults": {"url": DEAD_URL, "security_profile": 1, "reconnect": FAST},
        "ev_models": [{"id": "van", "capacity_kwh": 75, "max_ac_kw": 11, "phases": 3,
                       "curve": [[0, 1.0], [80, 0.5], [100, 0.1]]}],
        "chargers": [
            {"id": "0123", "password": "pw1", "connectors": 2, "id_tags": ["TAG1", "TAG2"], "verify_tls": False,
             "config_keys": {"HeartbeatInterval": "300", "MeterValuesSampledData": "Energy.Active.Import.Register"},
             "session": {"ev_model": "van", "initial_soc": 20}, "maintenance": {"fail_install": True},
             "meter": {"phase_values": "L-L", "post_tx_delay_s": 2.5, "begin_end_messages": True}},
            {"id": "1e5", "password": "pw2", "template": "jp-ac-6kw-dual", "max_power_w": 3000},
            {"id": "V2", "password": "pw3", "ocpp_version": "2.0.1", "serial": "000777",
             "variables": [{"component": "OCPPCommCtrlr", "variable": "HeartbeatInterval", "value": "120"},
                           {"component": "MyCtrlr", "variable": "Mode", "value": "eco", "data_type": "string",
                            "mutability": "ReadWrite"}]},
        ], **extra})


def _book(data: bytes):
    return openpyxl.load_workbook(io.BytesIO(data))


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _set(ws, row: int, column: str, value):
    headers = [c.value for c in ws[1]]
    ws.cell(row, headers.index(column) + 1).value = value


def test_round_trip_equal():
    cfg = _config()
    data, warnings = export_xlsx(cfg)
    assert warnings == []
    back, msgs = import_xlsx(data, cfg)
    assert back is not None, msgs
    assert msgs == []
    # passwords come back as the mask (the engine keeps the stored ones); everything else is identical
    assert [c.password for c in back.chargers] == [PASSWORD_MASK] * 3
    restore = {c.id: c.password for c in cfg.chargers}
    back.chargers = [c.model_copy(update={"password": restore[c.id]}) for c in back.chargers]
    assert back == cfg


def test_export_layout():
    wb = _book(export_xlsx(_config())[0])
    assert wb.sheetnames == ["chargers", "config_keys", "variables", "ev_models"]
    ws = wb["chargers"]
    headers = [c.value for c in ws[1]]
    assert headers[:3] == ["id", "template", "ocpp_version"] and str(ws.cell(2, 1).value).startswith("#")
    rows = {r[0]: dict(zip(headers, r)) for r in ws.iter_rows(min_row=3, values_only=True)}
    assert set(rows) == {"0123", "1e5", "V2"}  # text ids stay text
    assert rows["0123"]["password"] == PASSWORD_MASK and rows["0123"]["id_tags"] == "TAG1,TAG2"
    assert rows["0123"]["model"] == "SIM-AC6" and rows["0123"]["ev_model"] == "van"  # effective values filled
    assert "fail_install" in rows["0123"]["extra"] and "verify_tls" in rows["0123"]["extra"]
    # a templated charger: template-provided columns stay blank, overrides are filled
    assert rows["1e5"]["template"] == "jp-ac-6kw-dual" and rows["1e5"]["max_power_w"] == 3000
    assert rows["1e5"]["connectors"] is None and rows["1e5"]["model"] is None
    keys = [r for r in wb["config_keys"].iter_rows(min_row=3, values_only=True)]
    assert ("0123", "HeartbeatInterval", "300") in keys
    var = [r for r in wb["variables"].iter_rows(min_row=3, values_only=True)]
    assert len(var) == 2 and var[1][0] == "V2" and "data_type" in var[1][-1]


def test_blank_template_workbook():
    wb = _book(SCHEMA.blank_template())
    assert wb.sheetnames == ["chargers", "config_keys", "variables", "ev_models"]
    assert wb["chargers"].max_row == 2


def test_import_new_charger_and_numbers_as_text():
    cfg = _config()
    wb = _book(export_xlsx(cfg)[0])
    ws = wb["chargers"]
    ws.append(["0456", "eu-ac-11kw", 2.0, None, "new-pw"])
    _set(ws, ws.max_row, "serial", 778.0)  # a cell Excel stores as a number
    _set(ws, ws.max_row, "ocpp_version", "2.0.1")
    wb["config_keys"].append(["0123", "WebSocketPingInterval", 30])
    back, msgs = import_xlsx(_bytes(wb), cfg)
    assert back is not None, msgs
    new = back.chargers[-1]
    assert (new.id, new.serial, new.phases, new.password, new.ocpp_version) == ("0456", "778", 3, "new-pw", "2.0.1")
    assert back.chargers[0].config_keys["WebSocketPingInterval"] == "30"


def test_errors_name_sheet_row_and_column():
    cfg = _config()
    wb = _book(export_xlsx(cfg)[0])
    ws = wb["chargers"]
    _set(ws, 3, "connectors", "two")          # bad type
    _set(ws, 4, "ocpp_version", "3.0")       # unknown version
    ws.append(["0123", None, None, None, "x"])  # duplicate id (row 6)
    ws.append(["N1", "nope", None, None, "x"])  # unknown template (row 7)
    ws.append(["N2", None, None, None, PASSWORD_MASK])  # mask without a stored password (row 8)
    ws.append(["N3", None, None, None, None])  # profile 1 without a password (row 9)
    wb["config_keys"].append(["ghost", "HeartbeatInterval", "1"])
    wb["variables"].append(["V2", "OCPPCommCtrlr", None, None, None, "NoSuchVariable", None, "1"])
    wb["ev_models"].append(["van", 10, 3])
    back, errors = import_xlsx(_bytes(wb), cfg)
    assert back is None
    text = "\n".join(errors)
    assert "chargers row 3, column 'connectors': not an integer" in text
    assert "chargers row 4, column 'ocpp_version':" in text
    assert "chargers row 6, column 'id': duplicate charger id '0123' (also row 3)" in text
    assert "chargers row 7, column 'template': unknown template 'nope'" in text
    assert "chargers row 8, column 'password':" in text
    assert "chargers row 9: " in text and "security_profile 1 needs a password" in text
    assert "config_keys row 5, column 'charger': unknown charger 'ghost'" in text
    assert "ev_models row 4, column 'id': duplicate ev_models id 'van'" in text
    assert "chargers row 5: " in text and "NoSuchVariable" in text  # V2 (row 5): unknown standard variable


def test_missing_sheet_or_column():
    wb = openpyxl.Workbook()
    wb.active.title = "other"
    assert import_xlsx(_bytes(wb), _config()) == (None, ["sheet 'chargers' is missing"])
    wb = _book(SCHEMA.blank_template())
    ws = wb["chargers"]
    ws.delete_cols(1)  # no "id" column
    back, errors = import_xlsx(_bytes(wb), _config())
    assert back is None and errors == ["chargers row 1: missing column(s) id"]
    assert import_xlsx(b"not a zip", None)[0] is None


def test_variable_row_errors_located():
    cfg = _config()
    wb = _book(export_xlsx(cfg)[0])
    ws = wb["variables"]
    ws.append(["V2", "OCPPCommCtrlr", None, None, 1, "X", None, "1"])  # connector without evse (row 5)
    back, errors = import_xlsx(_bytes(wb), cfg)
    assert back is None
    assert any(e.startswith("variables row 5") and "connector needs evse" in e for e in errors), errors


# ------------------------------------------------------------------ web routes

@pytest.fixture
def client():
    engine = OcppEngine(_config())
    engine.stop_grace_s = 0.2
    with TestClient(create_app(engine)) as c:
        yield c, engine


def test_web_export_import_reloads_only_changed(client):
    c, engine = client
    r = c.get("/api/config/xlsx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "sme-ocpp-sim-chargers.xlsx" in r.headers["content-disposition"]
    assert b"pw1" not in r.content  # xlsx is a zip; check the decoded cells too
    wb = _book(r.content)
    assert "pw1" not in {v for row in wb["chargers"].iter_rows(values_only=True) for v in row}
    before = {cid: run.runtime for cid, run in engine._runs.items()}
    _set(wb["chargers"], 4, "max_power_w", 2000)  # change 1e5 only
    r = c.post("/api/config/import", files={"file": ("chargers.xlsx", _bytes(wb))})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    got = {ch.id: ch for ch in engine.config.chargers}
    assert got["1e5"].max_power_w == 2000 and got["0123"].password == "pw1" and got["1e5"].password == "pw2"
    assert engine._runs["0123"].runtime is before["0123"] and engine._runs["V2"].runtime is before["V2"]
    assert engine._runs["1e5"].runtime is not before["1e5"]


def test_web_import_errors_and_template(client):
    c, engine = client
    wb = _book(c.get("/api/config/xlsx").content)
    _set(wb["chargers"], 3, "phases", 7)
    r = c.post("/api/config/import", files={"file": ("bad.xlsx", _bytes(wb))})
    assert r.status_code == 400
    assert any(e.startswith("chargers row 3, column 'phases'") for e in r.json()["errors"])
    assert engine.config.chargers[0].phases == 1
    r = c.get("/api/config/xlsx-template")
    assert r.status_code == 200 and _book(r.content)["chargers"].max_row == 2


# ------------------------------------------------------------------ review fixes (M10)

@pytest.fixture
def user_template(tmp_path):
    from sme_ocpp_sim.templates_builtin import REGISTRY

    (tmp_path / "delta.yaml").write_text("name: delta\nphases: 3\nline_to_line: true\nvoltage_v: 200\n"
                                         "config_keys: {HeartbeatInterval: '300', ClockAlignedDataInterval: '900'}\n"
                                         "session: {initial_soc: 50}\n", encoding="utf-8")
    REGISTRY.load_user_templates(tmp_path)
    yield tmp_path
    REGISTRY.clear_user_templates()
    REGISTRY._user_dir = None


def _overrides():
    return SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [
        {"id": "A", "template": "eu-ac-11kw", "phases": 1},
        {"id": "B", "template": "jp-ac-6kw-dual", "connectors": 1},
        {"id": "C", "template": "delta", "line_to_line": False, "config_keys": {"HeartbeatInterval": "60"},
         "session": {"target_soc": 80}},
    ]})


def _effective(cfg):
    return {c.id: (c.phases, c.connectors, c.line_to_line, c.config_keys, c.session.model_dump())
            for c in cfg.chargers}


def test_override_with_model_default_survives_yaml_and_excel(user_template):
    from sme_ocpp_sim.config import parse_text, write_back

    cfg = _overrides()
    want = _effective(cfg)
    assert want["A"][0] == 1 and want["B"][1] == 1 and want["C"][2] is False
    assert want["C"][3] == {"HeartbeatInterval": "60", "ClockAlignedDataInterval": "900"}
    assert _effective(parse_text(write_back("", cfg))) == want
    got, msgs = import_xlsx(export_xlsx(cfg)[0], cfg)
    assert got is not None, msgs
    assert _effective(got) == want
    c = cfg.chargers[2].model_dump(mode="json", exclude_defaults=True)
    # dict fields: only the charger's own keys are written, the others keep following the template
    assert c["config_keys"] == {"HeartbeatInterval": "60"} and c["session"] == {"target_soc": 80.0}
    assert c["line_to_line"] is False and "phases" not in c


def test_override_with_model_default_in_device_config_get(user_template):
    engine = OcppEngine(_overrides())
    engine.stop_grace_s = 0.2
    with TestClient(create_app(engine)) as c:
        for cid, key in (("A", "phases"), ("B", "connectors"), ("C", "line_to_line")):
            r = c.get(f"/api/devices/{cid}/config")
            assert r.status_code == 200, r.text
            body = yaml.safe_load(r.text)
            body = body.get("config", body) if isinstance(body, dict) else body
            assert key in body, body
        assert yaml.safe_load(c.get("/api/devices/C/config").text).get("line_to_line", False) is False


def test_export_writes_no_formula_cells():
    cfg = SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [
        {"id": "F1", "vendor": "=1+1", "id_tags": ["=1+1", "@SUM(A1)"], "config_keys": {"K": "=1+1", "L": "+5"}}]})
    data, _ = export_xlsx(cfg)
    wb = _book(data)
    texts = [c for ws in wb.worksheets for row in ws.iter_rows(min_row=3) for c in row
             if isinstance(c.value, str) and c.value[:1] in "=+-@"]
    assert texts and all(c.data_type == "s" and c.quotePrefix for c in texts)
    assert {c.value for c in texts} >= {"=1+1", "+5"}
    got, msgs = import_xlsx(data, cfg)
    assert got is not None, msgs
    ch = got.chargers[0]
    assert ch.vendor == "=1+1" and ch.id_tags == ["=1+1", "@SUM(A1)"] and ch.config_keys == {"K": "=1+1", "L": "+5"}


def test_layout_rows_match_the_template():
    from sme_ocpp_sim.xlsx_io import LAYOUT_ROWS

    wb = _book(SCHEMA.blank_template())
    assert {ws.title: ws.max_row for ws in wb.worksheets} == {ws.title: LAYOUT_ROWS for ws in wb.worksheets}


@pytest.mark.parametrize("extra", [0, 1])
def test_import_row_cap_boundary(extra):
    """Exactly MAX_IMPORT_ROWS charger data rows pass the row cap, one more is refused."""
    from sme_ocpp_sim.xlsx_io import CHARGERS_ROW_CAP, MAX_IMPORT_ROWS

    wb = _book(SCHEMA.blank_template())
    for i in range(MAX_IMPORT_ROWS + extra):
        wb["chargers"].append([f"R{i}"])
    cfg, errors = import_xlsx(_bytes(wb))  # sme-sim-common's per-sheet row cap (Sheet.max_rows)
    capped = f"chargers: more than {CHARGERS_ROW_CAP} rows (row limit), import refused"
    assert (capped in errors) == bool(extra), errors[:3]
    if extra:
        assert cfg is None


def test_import_row_cap_other_sheets():
    from sme_ocpp_sim.xlsx_io import LAYOUT_ROWS, MAX_IMPORT_ROWS

    assert SCHEMA.max_rows == 10 * MAX_IMPORT_ROWS + LAYOUT_ROWS  # config_keys / variables / ev_models
    wb = _book(SCHEMA.blank_template())
    wb["chargers"].append(["A"])
    for i in range(MAX_IMPORT_ROWS + 1):  # more than chargers allows, within the other sheets' cap
        wb["config_keys"].append(["A", f"K{i}", "1"])
    cfg, errors = import_xlsx(_bytes(wb))
    assert not any("row limit" in e for e in errors), errors[:3]


def test_web_import_size_cap(client):
    from sme_ocpp_sim.web import MAX_IMPORT_BYTES

    c, engine = client
    r = c.post("/api/config/import", files={"file": ("big.xlsx", b"x" * (MAX_IMPORT_BYTES + 1))})
    assert r.status_code == 413 and "import size limit" in r.text  # make_base_app(max_import_bytes=)
    assert [ch.id for ch in engine.config.chargers] == ["0123", "1e5", "V2"]


def test_web_import_password_mask(client):
    c, engine = client
    wb = _book(c.get("/api/config/xlsx").content)
    _set(wb["chargers"], 3, "max_power_w", 5000)  # a change, so the stored passwords must be carried over
    r = c.post("/api/config/import", files={"file": ("ok.xlsx", _bytes(wb))})
    assert r.status_code == 200, r.text
    assert [ch.password for ch in engine.config.chargers] == ["pw1", "pw2", "pw3"]
    wb = _book(c.get("/api/config/xlsx").content)
    wb["chargers"].append(["NEW", None, None, None, PASSWORD_MASK])  # unknown id with the mask
    assert wb["chargers"].cell(1, 5).value == "password"
    r = c.post("/api/config/import", files={"file": ("m.xlsx", _bytes(wb))})
    assert r.status_code == 400
    assert any("column 'password'" in e and "'NEW' has none" in e for e in r.json()["errors"])
    assert [ch.id for ch in engine.config.chargers] == ["0123", "1e5", "V2"]


def test_web_import_mask_without_stored_password():
    engine = OcppEngine(SimConfig.model_validate({"csms_defaults": {"url": DEAD_URL}, "chargers": [{"id": "P0"}]}))
    engine.stop_grace_s = 0.2
    with TestClient(create_app(engine)) as c:
        wb = _book(c.get("/api/config/xlsx").content)
        _set(wb["chargers"], 3, "password", PASSWORD_MASK)
        r = c.post("/api/config/import", files={"file": ("m.xlsx", _bytes(wb))})
        assert r.status_code == 400 and "'P0' has none" in r.text
        assert engine.config.chargers[0].password is None


def test_web_failed_import_leaves_config_untouched(client):
    c, engine = client
    before = engine.config.model_dump()
    runs = {cid: run.runtime for cid, run in engine._runs.items()}
    wb = _book(c.get("/api/config/xlsx").content)
    _set(wb["chargers"], 3, "max_power_w", 1234)  # a valid change ...
    wb["config_keys"].append(["nope", "HeartbeatInterval", "1"])  # ... and an error in another sheet
    r = c.post("/api/config/import", files={"file": ("bad.xlsx", _bytes(wb))})
    assert r.status_code == 400 and any("unknown charger 'nope'" in e for e in r.json()["errors"])
    assert engine.config.model_dump() == before
    assert {cid: run.runtime for cid, run in engine._runs.items()} == runs
