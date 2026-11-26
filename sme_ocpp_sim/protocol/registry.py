"""OCPP version registry (plan §2.3).

Only metadata lives here so that config validation can check a version without importing any adapter.
Adapter classes are imported lazily by :func:`get_adapter_class`.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Type

from .base import VersionAdapter


@dataclass(frozen=True)
class VersionInfo:
    version: str
    available: bool
    note: str = ""
    #: "module:Class" of the adapter; ``None`` while unavailable.
    adapter: Optional[str] = None


VERSIONS: Dict[str, VersionInfo] = {
    "1.6": VersionInfo("1.6", True, adapter="sme_ocpp_sim.protocol.v16.adapter:Adapter16"),
    "2.0.1": VersionInfo("2.0.1", False, note="coming in v0.2"),
    "2.1": VersionInfo("2.1", False, note="coming in v0.3"),
}


def list_versions() -> List[VersionInfo]:
    return list(VERSIONS.values())


def check_version(version: str) -> str:
    """Return ``version`` if it is registered and available, else raise ``ValueError`` with a clear message."""
    info = VERSIONS.get(version)
    if info is None:
        raise ValueError(f"unknown OCPP version {version!r} (known: {', '.join(VERSIONS)})")
    if not info.available:
        raise ValueError(f"OCPP {version} is not available yet ({info.note})")
    return version


def get_adapter_class(version: str) -> Type[VersionAdapter]:
    info = VERSIONS[check_version(version)]
    assert info.adapter is not None
    module, _, cls = info.adapter.partition(":")
    return getattr(importlib.import_module(module), cls)
