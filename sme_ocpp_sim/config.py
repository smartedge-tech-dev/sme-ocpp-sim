"""Simulator configuration (YAML, design §7).

M3 scope: top-level ``simulation`` / ``frame_log`` / ``csms_defaults`` and a flat per-charger model
(``connectors`` count instead of the ``evses`` tree, constant power placeholder). ``template`` / ``evses`` /
``ev_models`` / ``session`` follow in M4 / M5; ``behaviors`` is v0.2.

YAML pitfall: unquoted ``0123``, ``1e5``-like numbers, ``ON`` / ``OFF`` / ``yes`` / ``no`` are not strings in
YAML 1.1. Identity / text fields therefore only accept real strings and tell the user to quote the value,
instead of silently turning ``0123`` into ``83``.

The only product import is the version metadata registry (no adapter is imported).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import yaml
from pydantic import AfterValidator, BeforeValidator, Field, field_validator, model_validator
from sme_sim_common.config import StrictModel
from typing_extensions import Annotated

from .protocol.registry import check_version


def _quoted(v: Any) -> Any:
    if not isinstance(v, str):
        raise ValueError(f"must be a quoted string (YAML read {v!r} as {type(v).__name__}); write it in quotes")
    return v


QuotedStr = Annotated[str, BeforeValidator(_quoted)]
OcppVersion = Annotated[QuotedStr, AfterValidator(check_version)]


def _key_value(v: Any) -> str:
    # config_keys values may be written unquoted (``HeartbeatInterval: 300``); keep the OCPP wire form.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v
    raise ValueError(f"config key value must be a string, integer or boolean, got {type(v).__name__}")


class SimulationConfig(StrictModel):
    tick_seconds: float = Field(1.0, gt=0)
    time_scale: float = Field(1.0, gt=0)
    running: bool = True


class FrameLogConfig(StrictModel):
    size: int = Field(20000, ge=100)


class ReconnectConfig(StrictModel):
    min_s: float = Field(1.0, gt=0)
    max_s: float = Field(60.0, gt=0)
    jitter: float = Field(0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> "ReconnectConfig":
        if self.max_s < self.min_s:
            raise ValueError("reconnect.max_s must be >= min_s")
        return self


def _ws_url(v: Optional[str]) -> Optional[str]:
    if v is not None and not v.startswith(("ws://", "wss://")):
        raise ValueError("CSMS url must start with ws:// or wss://")
    return v


WsUrl = Annotated[QuotedStr, AfterValidator(_ws_url)]


class CsmsDefaults(StrictModel):
    url: Optional[WsUrl] = None
    ocpp_version: OcppVersion = "1.6"
    security_profile: int = Field(0, ge=0, le=2)  # 3 (client certificates) is out of v0.1 scope
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)


class ChargerConfig(StrictModel):
    id: QuotedStr = Field(min_length=1)
    #: ``None`` = ``csms_defaults.url``; the charger id is appended as the last path segment.
    csms_url: Optional[WsUrl] = None
    #: ``None`` = ``csms_defaults.ocpp_version``
    ocpp_version: Optional[OcppVersion] = None
    password: Optional[QuotedStr] = Field(None, repr=False)  # security profile 1/2 basic auth
    ca_file: Optional[QuotedStr] = None
    verify_tls: bool = True
    require_subprotocol: bool = True
    # BootNotification identity; lengths are the 1.6 CiString limits (chargePointVendor/Model 20,
    # chargePointSerialNumber 25, firmwareVersion 50).
    vendor: QuotedStr = Field("SmartEdge", min_length=1, max_length=20)
    model: QuotedStr = Field("SIM-AC6", min_length=1, max_length=20)
    serial: Optional[QuotedStr] = Field(None, max_length=25)
    firmware: Optional[QuotedStr] = Field("0.1.0", max_length=50)
    connectors: int = Field(1, ge=1)
    config_keys: Dict[str, Annotated[str, BeforeValidator(_key_value)]] = Field(default_factory=dict)
    auth_mode: Literal["local_only", "csms_only", "local_then_csms", "accept_all"] = "local_then_csms"
    id_tags: List[Annotated[QuotedStr, Field(min_length=1, max_length=20)]] = Field(default_factory=list)
    #: Boot retry delay when the CSMS answers Pending / Rejected with ``interval`` 0.
    boot_retry_s: int = Field(60, ge=1)
    #: M3 placeholder: constant charging power until the M4 power model.
    power_w: float = Field(6000.0, gt=0)
    #: Default for MeterValueSampleInterval when ``config_keys`` does not set it (``None`` = key table default).
    meter_sample_interval_s: Optional[int] = Field(None, ge=0)

    @field_validator("id_tags")
    @classmethod
    def _unique_tags(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate id_tags")
        return v


class SimConfig(StrictModel):
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    frame_log: FrameLogConfig = Field(default_factory=FrameLogConfig)
    csms_defaults: CsmsDefaults = Field(default_factory=CsmsDefaults)
    chargers: List[ChargerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_chargers(self) -> "SimConfig":
        seen = set()
        for c in self.chargers:
            if c.id in seen:
                raise ValueError(f"duplicate charger id {c.id!r}")
            seen.add(c.id)
            if c.csms_url is None and self.csms_defaults.url is None:
                raise ValueError(f"charger {c.id!r}: no csms_url and no csms_defaults.url")
        return self

    def resolve_csms(self, charger: ChargerConfig) -> Tuple[str, str]:
        """``(url, ocpp_version)`` of ``charger`` after applying ``csms_defaults``."""
        url = charger.csms_url or self.csms_defaults.url
        assert url is not None  # guaranteed by _check_chargers
        return url, charger.ocpp_version or self.csms_defaults.ocpp_version


def parse_text(text: str) -> SimConfig:
    return SimConfig.model_validate(yaml.safe_load(text) or {})


def load_file(path: Union[str, Path]) -> SimConfig:
    return parse_text(Path(path).read_text(encoding="utf-8"))
