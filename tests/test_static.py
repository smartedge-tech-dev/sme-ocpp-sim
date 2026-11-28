"""Static web UI: served by the app (product files under /static, shared shell under /sme-sim-common) and every
i18n key it uses exists. Completeness of en / ja / zh and the reserved prefixes are checked in test_i18n.py."""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sme_sim_common import __version__ as common_version
from sme_sim_common.testing import iter_i18n_keys
from sme_sim_common.web import STATIC_DIR as COMMON_STATIC, VERSION_TOKEN

from sme_ocpp_sim import __version__
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "sme_ocpp_sim" / "static"
RESERVED = ("shell.", "vendor.", "common.")


def _en_keys(*paths: Path) -> set:
    return {k.key for p in paths for k in iter_i18n_keys(p) if k.lang == "en"}


def test_index_and_assets_served():
    with TestClient(create_app(OcppEngine())) as c:
        r = c.get("/")
        assert r.status_code == 200 and "SME OCPPSim" in r.text and 'data-storage="sme-ocpp-sim"' in r.text
        assert VERSION_TOKEN not in r.text
        common_refs = re.findall(r'"/sme-sim-common/[^"]+"', r.text)
        assert len(common_refs) == 4
        assert all(ref.endswith(f'?v={common_version}"') for ref in common_refs), common_refs
        for asset in ("app.js", "csms.js", "panels.js", "style.css", "i18n/en.js", "i18n/ja.js", "i18n/zh.js", "favicon.svg"):
            assert c.get(f"/static/{asset}").status_code == 200, asset
        for asset in ("shell/shell.js", "shell/shell.css", "vendor/vendor.js", "i18n/common.js"):
            assert c.get(f"/sme-sim-common/{asset}").status_code == 200, asset


def test_state_and_version():
    with TestClient(create_app(OcppEngine())) as c:
        assert c.get("/api/state").json()["chargers"] == []
        assert __version__ in c.get("/api/version").text


def test_used_keys_defined():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = "".join((STATIC / f).read_text(encoding="utf-8") for f in ("app.js", "csms.js", "panels.js"))
    used = set(re.findall(r'data-i18n(?:-placeholder|-title|-aria-label)?="([^"]+)"', html))
    used |= set(re.findall(r'\bt\(\s*"([^"]+)"', js)) | set(re.findall(r'\["(about\.[^"]+)"', js))
    assert used
    product = _en_keys(STATIC / "i18n" / "en.js")
    common = _en_keys(*sorted(COMMON_STATIC.rglob("*.js")))
    missing = sorted(k for k in used if k not in (common if k.startswith(RESERVED) else product))
    assert not missing, f"missing {missing}"


def test_literal_and_enumerated_keys_defined():
    # Keys passed as plain string literals to helpers (btn("op.plug"), kv("detail.power"), menu labels) and the
    # enumerated keys built with tk(prefix, value) for OCPP 1.6 connector statuses and the connection badge.
    js = "".join((STATIC / f).read_text(encoding="utf-8") for f in ("app.js", "csms.js", "panels.js"))
    prefixes = ("add", "bulk", "chargers", "conn", "csms", "detail", "dt", "frames", "keys", "menu", "op", "profiles",
                "sc", "scn", "settings", "status", "trf")
    used = set(re.findall(r'"((?:%s)\.[A-Za-z0-9_.]+)"' % "|".join(prefixes), js))
    used -= set(re.findall(r'\btk\(\s*"([^"]+)"', js))  # tk() prefixes, completed below
    used -= {k for k in used if k.endswith(".jsonl")}     # file names (frames.jsonl)
    used |= {f"status.{s}" for s in ("Available", "Preparing", "Charging", "SuspendedEVSE", "SuspendedEV",
                                     "Finishing", "Reserved", "Unavailable", "Faulted")}
    used |= {f"conn.{s}" for s in ("online", "pending", "rejected", "booting", "connecting", "error", "held",
                                   "gave_up")}
    used |= {f"frames.dir.{d}" for d in ("tx", "rx")}
    # csms.js: t(`csms.boot.${boot_status || "none"}`) and t(`csms.plan.${status}`)
    used |= {f"csms.boot.{s}" for s in ("Accepted", "Pending", "Rejected", "none")}
    used |= {f"csms.plan.{s}" for s in ("waiting", "sent", "ok", "error", "cancelled")}
    # panels.js: scenario state / result / step status, traffic state and counters (template literals)
    used |= {f"scn.state.{s}" for s in ("pending", "running", "done")}
    used |= {f"scn.result.{s}" for s in ("pass", "fail", "error", "aborted")}
    used |= {f"scn.status.{s}" for s in ("pending", "running", "pass", "fail", "error", "skipped", "aborted")}
    used |= {f"trf.state.{s}" for s in ("running", "stopped")}
    used |= {f"trf.stat.{s}" for s in ("arrivals", "started", "completed", "rejected", "skipped", "aborted", "failed",
                                       "preempted", "cancelled")}
    product = _en_keys(STATIC / "i18n" / "en.js")
    missing = sorted(used - product)
    assert not missing, f"missing {missing}"


def test_global_dialog_functions_exposed():
    # Plan §9: the main dialogs can be opened from the console / Playwright for screenshots.
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r"Object\.assign\(window, \{([^}]+)\}\)", js)
    assert m
    names = {n.strip() for n in m.group(1).split(",")}
    assert names == {"openChargerDialog", "openSettingsDialog", "openProfileDialog", "openFrameDetail",
                     "openAddChargerDialog"}
    assert "vendor: Shell.vendors.smartEdge" in js
    assert "window.openCsmsPanel = openCsmsPanel" in (STATIC / "csms.js").read_text(encoding="utf-8")
    assert "Object.assign(window, { openScenarioPanel, openTrafficPanel })" in (STATIC / "panels.js").read_text(
        encoding="utf-8")


def test_settings_use_json_routes():
    # The settings dialog and the key table go through the JSON routes (validated server side); no client-side YAML
    # editing is left in the UI.
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "yaml" not in js.lower()
    for route in ("/api/csms-defaults", 'chargerUrl(SET.id, "settings")', 'chargerUrl(id, "configuration")'):
        assert route in js, route
