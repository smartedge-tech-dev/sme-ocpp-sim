"""Built-in scenarios: the examples of the repository's ``scenarios/`` folder, shipped with every build.

Installed wheel: ``sme_ocpp_sim/scenarios`` (``pyproject.toml`` maps ``scenarios/`` to the package
``sme_ocpp_sim.scenarios``); source tree, Docker image and Windows exe (PyInstaller puts the package under
``sys._MEIPASS``): ``scenarios/`` beside the package. A built-in is referenced as ``builtin:<name>`` (the file
name without ``.yaml``) by ``scenario run``, ``POST /api/scenarios/run?builtin=<name>`` and the MCP
``run_scenario`` tool.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .model import ScenarioError

PREFIX = "builtin:"
_PKG = Path(__file__).resolve().parents[1]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def folder() -> Optional[Path]:
    """The folder of the built-in scenarios, or None when the build has none."""
    for p in (_PKG / "scenarios", _PKG.parent / "scenarios"):
        if p.is_dir():
            return p
    return None


def path(name: str) -> Path:
    """The file of the built-in ``name``; :class:`ScenarioError` when there is no such built-in."""
    if name.startswith(PREFIX):
        name = name[len(PREFIX):]
    d = folder()
    p = d / f"{name}.yaml" if d is not None and _NAME.fullmatch(name) else None
    if p is None or not p.is_file():
        known = ", ".join(e["name"] for e in listing()) or "none"
        raise ScenarioError(f"unknown built-in scenario {name!r} (built-in: {known})")
    return p


def text(name: str) -> str:
    return path(name).read_text(encoding="utf-8")


def listing() -> List[Dict[str, Optional[str]]]:
    """``[{"name", "title", "description"}]`` of every built-in, sorted by name (``title``: the scenario's
    ``name:``; an unreadable file is listed with ``description`` None)."""
    d = folder()
    out = []
    for p in sorted(d.glob("*.yaml")) if d is not None else []:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        data = data if isinstance(data, dict) else {}
        out.append({"name": p.stem, "title": data.get("name"), "description": data.get("description")})
    return out
