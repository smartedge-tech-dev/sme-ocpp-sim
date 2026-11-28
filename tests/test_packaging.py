"""Packaging guards: every subpackage is listed in pyproject.toml (the list is explicit because configs/ is
mapped into the wheel as sme_ocpp_sim.configs) and every shipped config loads."""

import tomllib
from pathlib import Path

from sme_ocpp_sim.config import load_file

ROOT = Path(__file__).resolve().parents[1]


def test_all_subpackages_listed_in_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    listed = set(pyproject["tool"]["setuptools"]["packages"])
    found = {".".join(p.parent.relative_to(ROOT).parts) for p in (ROOT / "sme_ocpp_sim").rglob("__init__.py")}
    missing = found - listed
    assert not missing, f"add to [tool.setuptools] packages in pyproject.toml: {sorted(missing)}"


def test_shipped_configs_load():
    configs = sorted((ROOT / "configs").glob("*.yaml"))
    assert configs
    for path in configs:
        load_file(str(path))


def test_default_config_is_bundled_demo(monkeypatch):
    """Started without -c, the CLI loads the bundled demo config (as sme-modbus-sim / sme-bac-sim)."""
    import sme_ocpp_sim.__main__ as cli
    import sme_ocpp_sim.web as web

    assert cli.DEFAULT_CONFIG == ROOT / "configs" / "demo-site.yaml"
    seen = {}
    monkeypatch.setattr(web, "create_app", lambda engine, path, **kw: seen.update(engine=engine, path=path) or "app")
    monkeypatch.setattr(cli, "setup_logging", lambda **kw: None)  # keep the root handlers of pytest
    monkeypatch.setattr(cli, "run_web", lambda app, args, name: seen.update(app=app))
    assert cli.main(["--no-browser"]) == 0
    assert seen["app"] == "app"
    assert seen["path"] == str(cli.DEFAULT_CONFIG)
    assert seen["engine"].config.chargers, "demo config should define chargers"
