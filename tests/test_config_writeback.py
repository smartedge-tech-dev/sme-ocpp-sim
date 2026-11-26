"""SimConfig string coercion pitfalls and YAML write-back (plan §5 M5 / §8)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sme_ocpp_sim.config import load_file, parse_text, save_file, write_back, write_back_checked

DEMO = Path(__file__).resolve().parent.parent / "configs" / "demo-site.yaml"
URL = 'csms_defaults: {url: "ws://h/ocpp"}\n'


@pytest.mark.parametrize("raw", ["0123", "1e5", "ON", "OFF", "yes", "no"])
def test_quoted_ids_and_tags_stay_strings(raw):
    cfg = parse_text(URL + f'chargers:\n  - id: "{raw}"\n    id_tags: ["{raw}"]\n    vendor: "{raw}"\n')
    c = cfg.chargers[0]
    assert (c.id, c.id_tags, c.vendor) == (raw, [raw], raw)
    back = parse_text(write_back(URL, cfg)).chargers[0]  # dumped YAML must quote them again
    assert (back.id, back.id_tags, back.vendor) == (raw, [raw], raw)


@pytest.mark.parametrize("raw", ["0123", "ON", "OFF", "yes", "no"])
@pytest.mark.parametrize("field", ["id", "vendor", "id_tags"])
def test_unquoted_non_strings_are_rejected(raw, field):
    value = f"[{raw}]" if field == "id_tags" else raw
    item = f"id: {value}" if field == "id" else f'id: "A"\n    {field}: {value}'
    with pytest.raises(ValidationError, match="quoted string"):
        parse_text(URL + f"chargers:\n  - {item}\n")


def test_demo_config_loads():
    cfg = load_file(DEMO)
    assert [c.id for c in cfg.chargers] == ["CP-0001", "CP-0002", "CP-0003"]
    assert cfg.resolve_ev(cfg.chargers[2]).id == "demo-60kwh"


def test_write_back_keeps_comments_outside_changed_blocks(tmp_path):
    text = DEMO.read_text(encoding="utf-8")
    cfg = load_file(DEMO)
    assert write_back(text, cfg) == text  # nothing changed, nothing rewritten
    cfg.chargers[1].vendor = "Other"
    cfg.chargers.pop(0)
    path = tmp_path / "site.yaml"
    path.write_text(text, encoding="utf-8")
    out = save_file(path, cfg)
    assert path.read_text(encoding="utf-8") == out
    for comment in ("# SME OCPPSim demo site", "# speeds up the power model", "# frames kept in memory",
                    "# 0 = ws:// without password"):
        assert comment in out
    again = parse_text(out)
    assert again == cfg
    assert [c["id"] for c in yaml.safe_load(out)["chargers"]] == ["CP-0002", "CP-0003"]
    assert "vendor: SmartEdge" not in out  # defaults are not written out


def test_save_file_keeps_crlf_and_bom(tmp_path):
    text = DEMO.read_text(encoding="utf-8")
    cfg = load_file(DEMO)
    cfg.chargers[1].vendor = "Other"
    path = tmp_path / "site.yaml"
    path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
    out = save_file(path, cfg)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw[3:].decode("utf-8") == out.replace("\n", "\r\n")
    assert "\r" not in out and "# SME OCPPSim demo site" in out
    assert parse_text(out) == cfg


def test_write_back_reports_full_redump():
    cfg = load_file(DEMO)
    text = DEMO.read_text(encoding="utf-8")
    assert write_back_checked(text, cfg) == (text, False)
    cfg.chargers[1].vendor = "Other"
    assert write_back_checked(text, cfg)[1] is False
    flow = '{csms_defaults: {url: "ws://h/ocpp"}, chargers: [{id: "A"}]}  # flow style\n'
    cfg2 = parse_text(flow)
    cfg2.chargers[0].vendor = "Other"
    out, redumped = write_back_checked(flow, cfg2)
    assert redumped and parse_text(out) == cfg2
