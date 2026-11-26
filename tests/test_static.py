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
        for asset in ("app.js", "style.css", "i18n/en.js", "i18n/ja.js", "i18n/zh.js", "favicon.svg"):
            assert c.get(f"/static/{asset}").status_code == 200, asset
        for asset in ("shell/shell.js", "shell/shell.css", "vendor/vendor.js", "i18n/common.js"):
            assert c.get(f"/sme-sim-common/{asset}").status_code == 200, asset


def test_state_and_version():
    with TestClient(create_app(OcppEngine())) as c:
        assert c.get("/api/state").json()["chargers"] == []
        assert __version__ in c.get("/api/version").text


def test_used_keys_defined():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n(?:-placeholder|-title|-aria-label)?="([^"]+)"', html))
    used |= set(re.findall(r'\bt\(\s*"([^"]+)"', js)) | set(re.findall(r'\["(about\.[^"]+)"', js))
    assert used
    product = _en_keys(STATIC / "i18n" / "en.js")
    common = _en_keys(*sorted(COMMON_STATIC.rglob("*.js")))
    missing = sorted(k for k in used if k not in (common if k.startswith(RESERVED) else product))
    assert not missing, f"missing {missing}"
