"""Simulator configuration (YAML).

M0 placeholder: only the top-level ``chargers`` list exists so the shell routes (``GET|PUT /api/config``) work.
The real charger / connector / EV models follow in M5.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SimConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chargers: List[Dict[str, Any]] = Field(default_factory=list)


def parse_text(text: str) -> SimConfig:
    return SimConfig.model_validate(yaml.safe_load(text) or {})


def load_file(path: Union[str, Path]) -> SimConfig:
    return parse_text(Path(path).read_text(encoding="utf-8"))
