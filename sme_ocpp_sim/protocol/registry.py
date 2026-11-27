"""OCPP version registry (plan §2.3).

Only metadata lives here so that config validation can check a version without importing any adapter.
Adapter and station classes are imported lazily by :func:`get_adapter_class` / :func:`get_station_class`.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional, Type

from .base import VersionAdapter

if TYPE_CHECKING:
    from .station import VersionStation


@dataclass(frozen=True)
class VersionInfo:
    version: str
    available: bool
    note: str = ""
    #: "module:Class" of the adapter; ``None`` while unavailable.
    adapter: Optional[str] = None
    #: "module:Class" of the station (``protocol.station.VersionStation``); ``None`` while unavailable.
    station: Optional[str] = None


VERSIONS: Dict[str, VersionInfo] = {
    "1.6": VersionInfo("1.6", True, adapter="sme_ocpp_sim.protocol.v16.adapter:Adapter16",
                         station="sme_ocpp_sim.protocol.v16.station:Station16"),
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


def _load(version: str, path: Optional[str]) -> type:
    if path is None:
        raise ValueError(f"OCPP {version} has no implementation registered")
    module, _, cls = path.partition(":")
    return getattr(importlib.import_module(module), cls)


def get_adapter_class(version: str) -> Type[VersionAdapter]:
    return _load(version, VERSIONS[check_version(version)].adapter)


def get_station_class(version: str) -> Type[VersionStation]:
    return _load(version, VERSIONS[check_version(version)].station)
