"""FastAPI app.

The generic routes (``/``, ``/api/version``, ``/api/state``, ``/api/config*``, ``/api/simulation``, ``/ws``, the
``/sme-sim-common`` and ``/static`` mounts) come from ``sme_sim_common.web.make_base_app``. The OCPP routes
(``/api/chargers/*``, ``/api/frames``, profiles) are added in M6 through ``product_routers``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from sme_sim_common.web import make_base_app

from . import __version__
from .config import SimConfig
from .engine import OcppEngine

STATIC = Path(__file__).resolve().parent / "static"


def create_app(engine: OcppEngine, config_path: Optional[str | Path] = None) -> FastAPI:
    return make_base_app(engine, config_path=config_path, static_dir=STATIC, title="SME OCPPSim",
                         version=__version__, config_model=SimConfig)
