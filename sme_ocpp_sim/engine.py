"""Simulation engine.

M0: an empty :class:`OcppEngine` on top of ``sme_sim_common.engine.BaseEngine``. Per-charger runtimes,
incremental reload (``_apply_diff``) and the power step (``_step``) follow in M3 / M5.
"""

from __future__ import annotations

from typing import Any, Dict, List

from sme_sim_common.engine import BaseEngine

from .config import SimConfig


class OcppEngine(BaseEngine):
    logger_name = "sme_ocpp_sim.engine"

    def __init__(self, config: SimConfig | None = None, **kwargs: Any):
        self.chargers: List[Any] = []
        super().__init__(config if config is not None else SimConfig(), **kwargs)

    def _build(self, config: SimConfig) -> List[Any]:
        return []  # no charger runtimes yet

    def _install(self, state: List[Any]) -> None:
        self.state = state
        self.chargers = state

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sim_time": self.sim_time,
            "running": self.running,
            "time_scale": self.time_scale,
            "chargers": [],
        }
