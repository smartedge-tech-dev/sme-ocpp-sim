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
