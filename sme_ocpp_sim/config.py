"""Simulator configuration (YAML, design §7).

Scope: top-level ``simulation`` / ``frame_log`` / ``csms_defaults`` / ``ev_models`` / ``mini_csms`` (built-in CSMS,
M9) and a flat per-charger model
(``connectors`` count instead of the ``evses`` tree; every connector has the same supply and ``max_power_w``).
A 2.0.1 charger takes Device Model ``variables`` (:class:`VariableConfig`) instead of the 1.6 ``config_keys``.
The EV plugged into a connector is ``session.ev_model`` (a built-in preset of :mod:`.charging.ev` or an
``ev_models`` entry). A charger may take its model defaults from a charger template (``template`` / ``params``,
:mod:`.templates_builtin`). Not supported: the ``evses`` tree (plan Q11: ``connectors: N``) and ``behaviors`` (v0.3).

YAML pitfall: unquoted ``0123``, ``1e5``-like numbers, ``ON`` / ``OFF`` / ``yes`` / ``no`` are not strings in
YAML 1.1. Identity / text fields therefore only accept real strings and tell the user to quote the value,
instead of silently turning ``0123`` into ``83``.

YAML write-back (:func:`write_back` / :func:`save_file`): every top-level block whose value changed is replaced
as a whole (``sme_sim_common.config.replace_yaml_block``). Comments outside the replaced blocks are kept;
comments *inside* a replaced block - e.g. on a single ``chargers:`` entry - are lost (accepted v0.1 trade-off;
per-item write-back needs sme-sim-common 1.2 ``replace_yaml_list_item``). Blocks are dumped with
``exclude_defaults``: a value equal to its default is not written out, which keeps the file short but means a
default changed in a later version applies to such a file too. A templated charger is compared with its template
instead (a field set to the model default but differing from the template is written). If ``replace_yaml_block`` cannot splice a block
(unusual layout) it re-dumps the whole document and *all* comments are lost; :func:`write_back_checked` reports
that and :func:`write_back` / :func:`save_file` log a warning.

Product imports: the version metadata registry (no adapter) and the pure EV model of :mod:`.charging.ev`.
"""

from __future__ import annotations

import codecs
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

import yaml
from pydantic import (AfterValidator, BeforeValidator, Field, SerializationInfo, field_validator, model_serializer,
                      model_validator)
from pydantic_core import to_jsonable_python
from sme_sim_common.config import StrictModel, atomic_write_text, replace_yaml_block, splice_yaml_block
from typing_extensions import Annotated

from .charging.ev import PRESETS, EvModel, validate_curve
from .protocol.registry import check_version

log = logging.getLogger(__name__)


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


def _variable_value(v: Any) -> str:
    # variables values may be unquoted (``value: 300`` / ``value: true`` / ``value: 1.5``); keep the wire form.
    if isinstance(v, float):
        return repr(v)
    return _key_value(v)


VariableValue = Annotated[str, BeforeValidator(_variable_value)]
Name50 = Annotated[QuotedStr, Field(min_length=1, max_length=50)]


class VariableConfig(StrictModel):
    """One ``variables:`` entry of a 2.0.1 charger (OCPP 2.0.1 Device Model, ``protocol.v201.device_model``).

    An entry that addresses a standard variable (same component / instance / EVSE / connector / variable /
    instance) overrides its values; any other entry is a user-defined variable and needs ``data_type``. The
    entries are checked against the standard set when the whole config is validated (:class:`SimConfig`)."""

    component: Name50
    component_instance: Optional[Name50] = None
    evse: Optional[int] = Field(None, ge=1)
    connector: Optional[int] = Field(None, ge=1)
    variable: Name50
    variable_instance: Optional[Name50] = None
    #: The ``Actual`` attribute value; ``target`` / ``min_set`` / ``max_set`` are the other attributes.
    value: Optional[VariableValue] = None
    target: Optional[VariableValue] = None
    min_set: Optional[VariableValue] = None
    max_set: Optional[VariableValue] = None
    # Definition of a user-defined variable (not allowed for a standard one).
    data_type: Optional[Literal["string", "decimal", "integer", "dateTime", "boolean", "OptionList",
                                "SequenceList", "MemberList"]] = None
    mutability: Optional[Literal["ReadOnly", "WriteOnly", "ReadWrite"]] = None
    unit: Optional[Annotated[QuotedStr, Field(max_length=16)]] = None
    min_limit: Optional[float] = None
    max_limit: Optional[float] = None
    values_list: Optional[List[QuotedStr]] = None
    reboot_required: Optional[bool] = None
    persistent: Optional[bool] = None
    constant: Optional[bool] = None
    #: Part of a SummaryInventory report.
    summary: Optional[bool] = None

    @model_validator(mode="after")
    def _shape(self) -> "VariableConfig":
        if self.connector is not None and self.evse is None:
            raise ValueError("variables: connector needs evse")
        if self.data_type is not None and self.value is None:
            raise ValueError(f"variables: {self.component}.{self.variable} needs a value")
        return self


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
    #: Consecutive failed connections (a failed handshake, or a session that ended before an accepted boot)
    #: after which the charger stops reconnecting until the operator connects it again; ``None`` = no limit.
    max_retries: Optional[int] = Field(None, ge=0)

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
    #: OCPP security profile every charger must match (checked per charger): 0 = no password + ws://,
    #: 1 = password + ws://, 2 = password + wss://, 3 = wss:// + TLS client certificate (``client_cert_file`` /
    #: ``client_key_file``) and no password (Basic Auth is not part of profile 3, OCPP 2.0.1 A00.FR.401).
    security_profile: int = Field(0, ge=0, le=3)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)


class EvModelConfig(StrictModel):
    """One ``ev_models`` entry (design §7); see :class:`.charging.ev.EvModel` for the semantics."""

    id: QuotedStr = Field(min_length=1)
    capacity_kwh: float = Field(gt=0)
    max_ac_kw: float = Field(gt=0)
    max_current_a: float = Field(32.0, gt=0)
    #: Accepted for the design §7 shape; DC charging is not modelled in v0.1.
    max_dc_kw: Optional[float] = Field(None, gt=0)
    phases: Literal[1, 2, 3] = 1
    min_current_a: float = Field(6.0, ge=0)
    efficiency: float = Field(0.9, gt=0, le=1)
    curve: Optional[List[Tuple[float, float]]] = None  # SoC% -> fraction of max; None = ev.DEFAULT_CURVE

    @field_validator("curve")
    @classmethod
    def _curve(cls, v: Optional[List[Tuple[float, float]]]) -> Optional[List[Tuple[float, float]]]:
        if v is not None:
            validate_curve(tuple(v))
        return v

    @model_validator(mode="after")
    def _currents(self) -> "EvModelConfig":
        if self.min_current_a > self.max_current_a:
            raise ValueError("min_current_a must be <= max_current_a")
        return self

    def to_model(self) -> EvModel:
        extra = {"curve": tuple(tuple(p) for p in self.curve)} if self.curve is not None else {}
        return EvModel(self.id, capacity_kwh=self.capacity_kwh, max_ac_w=self.max_ac_kw * 1000.0,
                       max_current_a=self.max_current_a, phases=self.phases, min_current_a=self.min_current_a,
                       efficiency=self.efficiency, **extra)


class SessionConfig(StrictModel):
    """The EV that is plugged in (design §7 ``session``); every new transaction starts at ``initial_soc``."""

    ev_model: QuotedStr = "compact-40kwh"
    initial_soc: float = Field(30.0, ge=0, le=100)
    target_soc: float = Field(90.0, ge=0, le=100)


class MaintenanceConfig(StrictModel):
    """Firmware update / diagnostics (log) upload simulation (M7). No file is downloaded or uploaded: each step
    only waits its duration (wall-clock seconds). ``fail_*`` makes every attempt of that step fail (failure
    injection); ``retry_interval_s`` is used when the CSMS request has no retryInterval."""

    download_s: float = Field(2.0, ge=0)
    install_s: float = Field(2.0, ge=0)
    upload_s: float = Field(2.0, ge=0)
    retry_interval_s: float = Field(5.0, ge=0)
    fail_download: bool = False
    fail_install: bool = False
    fail_upload: bool = False


class MeterOptions(StrictModel):
    """Meter value options OCPP has no configuration key for (simulator-only; v0.2.2 B).

    - ``begin_end_messages`` (OCPP 1.6 only): a MeterValues with context ``Transaction.Begin`` right after the
      accepted StartTransaction and one with ``Transaction.End`` right before the StopTransaction, both with the
      MeterValuesSampledData measurands. OCPP 2.0.1 always reports them in the Started / Ended events
      (SampledDataCtrlr.TxStartedMeasurands / TxEndedMeasurands).
    - ``phase_values``: ``off`` = one Voltage and one balanced Current.Import without a phase; ``L-N`` / ``L-L`` =
      one Voltage per supply phase (line-to-neutral ``L1-N``.., or line-to-line ``L1-L2``.. on a 3-phase supply)
      and one Current.Import per supply phase (``L1``..), each with its ``phase``.
    - ``post_tx_delay_s``: seconds between the end of a transaction and sending its StopTransaction / Ended
      TransactionEvent; the outbox is strictly ordered, so the messages queued after it wait as well.
    - ``tx_data_max_samples``: at most this many sampled and as many clock-aligned readings are kept for one
      transaction's StopTransaction ``transactionData`` / Ended ``meterValue`` (the oldest is dropped first, with
      one warning per transaction).
    """

    begin_end_messages: bool = False
    phase_values: Literal["off", "L-N", "L-L"] = "off"
    post_tx_delay_s: float = Field(0.0, ge=0)
    tx_data_max_samples: int = Field(1000, ge=1, le=10000)


#: What the Web API shows instead of a charger password; sent back unchanged it keeps the stored password.
PASSWORD_MASK = "********"

#: Upper bound for ``connectors`` (simulator limit, not an OCPP one).
MAX_CONNECTORS = 32


def _charger_id(v: str) -> str:
    if ":" in v:
        raise ValueError("charger id must not contain ':' (it is the Basic Auth user-id, RFC 7617)")
    if v == "bulk":
        raise ValueError("charger id 'bulk' is reserved (the Web API uses /api/chargers/bulk/... for bulk operations)")
    return v


class ChargerConfig(StrictModel):
    id: Annotated[QuotedStr, AfterValidator(_charger_id)] = Field(min_length=1)
    #: ``None`` = ``csms_defaults.url``; the charger id is appended as the last path segment.
    csms_url: Optional[WsUrl] = None
    #: ``None`` = ``csms_defaults.ocpp_version``
    ocpp_version: Optional[OcppVersion] = None
    password: Optional[QuotedStr] = Field(None, repr=False)  # security profile 1/2 basic auth
    ca_file: Optional[QuotedStr] = None
    #: Security profile 3: the Charging Station certificate (PEM, chain allowed) and its private key (PEM, not
    #: encrypted). A certificate signed later through SignCertificate / CertificateSigned replaces them in memory.
    client_cert_file: Optional[QuotedStr] = None
    client_key_file: Optional[QuotedStr] = None
    verify_tls: bool = True
    require_subprotocol: bool = True
    # BootNotification identity; lengths are the 1.6 CiString limits (chargePointVendor/Model 20,
    # chargePointSerialNumber 25, firmwareVersion 50).
    vendor: QuotedStr = Field("SmartEdge", min_length=1, max_length=20)
    model: QuotedStr = Field("SIM-AC6", min_length=1, max_length=20)
    serial: Optional[QuotedStr] = Field(None, max_length=25)
    firmware: Optional[QuotedStr] = Field("0.1.0", max_length=50)
    connectors: int = Field(1, ge=1, le=MAX_CONNECTORS)
    config_keys: Dict[str, Annotated[str, BeforeValidator(_key_value)]] = Field(default_factory=dict)
    auth_mode: Literal["local_only", "csms_only", "local_then_csms", "accept_all"] = "local_then_csms"
    id_tags: List[Annotated[QuotedStr, Field(min_length=1, max_length=20)]] = Field(default_factory=list)
    #: Boot retry delay when the CSMS answers Pending / Rejected with ``interval`` 0.
    boot_retry_s: float = Field(60.0, gt=0)
    #: Non-Accepted BootNotifications (Pending, Rejected or no answer; counted across reconnects until Accepted)
    #: after which the charger closes the connection and stops until the operator connects it again;
    #: ``None`` = no limit (the OCPP behaviour). A triggered BootNotification is not counted.
    boot_max_retries: Optional[int] = Field(None, ge=0)
    # Supply and hardware limit of each connector (design §3.2 / §7). ``voltage_v`` is the nominal supply
    # voltage; ``line_to_line`` marks a 3-phase voltage given line-to-line (Japanese 3-phase 200 V delta).
    max_power_w: float = Field(6000.0, gt=0)
    voltage_v: float = Field(200.0, gt=0)
    phases: Literal[1, 2, 3] = 1
    line_to_line: bool = False
    #: Power ramp in W per simulated second (``None`` = step change).
    ramp_up_w_per_s: Optional[float] = Field(None, gt=0)
    ramp_down_w_per_s: Optional[float] = Field(None, gt=0)
    session: SessionConfig = Field(default_factory=SessionConfig)
    #: Default for MeterValueSampleInterval when ``config_keys`` does not set it (``None`` = key table default).
    meter_sample_interval_s: Optional[int] = Field(None, ge=0)
    #: OCPP 2.0.1 Device Model overrides and user-defined variables (2.0.1 chargers only).
    variables: List[VariableConfig] = Field(default_factory=list)
    #: Firmware / diagnostics simulation timings and failure injection (M7).
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    #: Simulator-only meter value options (v0.2.2 B).
    meter: MeterOptions = Field(default_factory=MeterOptions)
    #: Charger template (:mod:`.templates_builtin`): the template fields this charger does not set come from it.
    template: Optional[Annotated[QuotedStr, Field(min_length=1)]] = None
    #: Template parameters (``{param}`` placeholders of a user template), overriding the template's defaults.
    params: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _expand_template(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("template") is not None:
            from .templates_builtin import expand_charger  # lazy: the templates module imports this one

            data = expand_charger(data)
        return data

    @model_serializer(mode="wrap")
    def _without_template_values(self, handler, info: SerializationInfo):
        # A dump keeps only the charger's own overrides of its template (module docstring of templates_builtin).
        # The template fields are compared with the template's value, not the model default: ``exclude_defaults``
        # drops e.g. ``phases: 1`` before this runs, which would silently revert an override of a 3-phase
        # template to the template's value on the next load. Dict / model fields (``config_keys``, ``session``,
        # ``maintenance``) are diffed per key, so only the keys the charger changed are written.
        out = handler(self)
        if self.template is None or not isinstance(out, dict):
            return out
        from .templates_builtin import template_values

        for k, tv in template_values(self.template, self.params).items():
            if (info.include is not None and k not in info.include) or (info.exclude is not None
                                                                         and k in info.exclude):
                continue
            own, ref = to_jsonable_python(getattr(self, k)), to_jsonable_python(tv)
            if isinstance(own, dict) and isinstance(ref, dict):
                own = {key: v for key, v in own.items() if key not in ref or ref[key] != v}
            # an empty value is not an override (expand_extras lets the template fill it in)
            if own in (None, "", {}, []) or own == ref:
                out.pop(k, None)
            else:
                out[k] = own
        return out

    @field_validator("id_tags")
    @classmethod
    def _unique_tags(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate id_tags")
        return v


#: OCPP versions the Mini CSMS speaks, and their OCPP-J subprotocols.
MINI_CSMS_VERSIONS = {"1.6": "ocpp1.6", "2.0.1": "ocpp2.0.1"}
_AUTH_STATUSES = ("Accepted", "Blocked", "Expired", "Invalid", "Unknown", "NoCredit", "NotAllowedTypeEVSE",
                  "NotAtThisLocation", "NotAtThisTime", "ConcurrentTx")


class MiniCsmsBoot(StrictModel):
    """The BootNotification answer of the Mini CSMS."""

    status: Literal["Accepted", "Pending", "Rejected"] = "Accepted"
    #: Heartbeat interval (Accepted) or retry delay (Pending / Rejected), seconds.
    interval: int = Field(300, ge=0)


class MiniCsmsCredential(StrictModel):
    id: QuotedStr = Field(min_length=1)
    password: QuotedStr = Field(repr=False)


class MiniCsmsAuth(StrictModel):
    """Security profile 1 check (HTTP Basic Auth over ws://). ``basic: false`` = profile 0, any station id."""

    basic: bool = False
    #: Known stations and their passwords; with ``basic: true`` any other id or a wrong password is HTTP 401.
    credentials: List[MiniCsmsCredential] = Field(default_factory=list)

    @field_validator("credentials")
    @classmethod
    def _unique(cls, v: List[MiniCsmsCredential]) -> List[MiniCsmsCredential]:
        ids = [c.id for c in v]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate mini_csms.auth.credentials id")
        return v


class MiniCsmsAuthorize(StrictModel):
    """The authorization status answered for an id tag / id token (Authorize, StartTransaction, TransactionEvent)."""

    default: Literal[_AUTH_STATUSES] = "Accepted"  # type: ignore[valid-type]
    #: Per id tag overrides, e.g. ``{"BAD-TAG": "Blocked"}``. 1.6 knows only Accepted / Blocked / Expired /
    #: Invalid / ConcurrentTx: another status is answered as Invalid to a 1.6 station.
    tags: Dict[str, Literal[_AUTH_STATUSES]] = Field(default_factory=dict)  # type: ignore[valid-type]


class MiniCsmsPlanItem(StrictModel):
    """One scheduled CALL: ``action`` with ``payload`` sent ``at_s`` seconds after the trigger ``after``."""

    at_s: float = Field(0.0, ge=0)
    #: ``boot`` = the first Accepted BootNotification of a connection; ``connect`` = the WebSocket connection.
    after: Literal["boot", "connect"] = "boot"
    action: QuotedStr = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    #: Only stations with this id (shell-style pattern, e.g. ``"CP-*"``); ``None`` = every station.
    charger: Optional[QuotedStr] = None
    #: Only stations speaking this version; ``None`` = every version (the payload must fit each of them).
    ocpp_version: Optional[Literal["1.6", "2.0.1"]] = None


class MiniCsmsConfig(StrictModel):
    """Built-in simple CSMS (``mini_csms:``, plan M9): a WebSocket server in the same process, Security Profile 0 / 1.
    ``host`` / ``port`` / ``enabled`` changes restart it (stations reconnect); the other fields apply at once."""

    enabled: bool = False
    host: QuotedStr = "127.0.0.1"
    port: int = Field(9000, ge=0, le=65535)
    versions: List[Literal["1.6", "2.0.1"]] = Field(default_factory=lambda: ["1.6", "2.0.1"], min_length=1)
    boot: MiniCsmsBoot = Field(default_factory=MiniCsmsBoot)
    auth: MiniCsmsAuth = Field(default_factory=MiniCsmsAuth)
    authorize: MiniCsmsAuthorize = Field(default_factory=MiniCsmsAuthorize)
    #: Answer a station CALL whose payload does not match the version's schema with CALLERROR (else: answer it
    #: anyway and record a warning - tolerant towards real stations).
    strict: bool = False
    call_timeout_s: float = Field(30.0, gt=0)
    frame_log_size: int = Field(5000, ge=100)
    plan: List[MiniCsmsPlanItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_plan(self) -> "MiniCsmsConfig":
        from .csms.handlers import check_outbound  # lazy: config imports no protocol stack at module level

        for i, item in enumerate(self.plan):
            for version in [item.ocpp_version] if item.ocpp_version else self.versions:
                try:
                    check_outbound(version, item.action, item.payload)
                except ValueError as e:
                    raise ValueError(f"mini_csms.plan[{i}] ({item.action}, OCPP {version}): {e}") from None
        return self


class SimConfig(StrictModel):
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)
    frame_log: FrameLogConfig = Field(default_factory=FrameLogConfig)
    csms_defaults: CsmsDefaults = Field(default_factory=CsmsDefaults)
    ev_models: List[EvModelConfig] = Field(default_factory=list)
    chargers: List[ChargerConfig] = Field(default_factory=list)
    mini_csms: MiniCsmsConfig = Field(default_factory=MiniCsmsConfig)

    @model_validator(mode="after")
    def _check_chargers(self) -> "SimConfig":
        ev_ids = [e.id for e in self.ev_models]
        if len(set(ev_ids)) != len(ev_ids):
            raise ValueError("duplicate ev_models id")
        seen = set()
        for c in self.chargers:
            if c.id in seen:
                raise ValueError(f"duplicate charger id {c.id!r}")
            seen.add(c.id)
            if c.csms_url is None and self.csms_defaults.url is None:
                raise ValueError(f"charger {c.id!r}: no csms_url and no csms_defaults.url")
            self._check_profile(c)
            self._check_variables(c)
            if c.session.ev_model not in ev_ids and c.session.ev_model not in PRESETS:
                raise ValueError(f"charger {c.id!r}: unknown session.ev_model {c.session.ev_model!r} "
                                 f"(not in ev_models nor presets {sorted(PRESETS)})")
        return self

    def _check_variables(self, c: ChargerConfig) -> None:
        if not c.variables:
            return
        version = c.ocpp_version or self.csms_defaults.ocpp_version
        if version != "2.0.1":
            raise ValueError(f"charger {c.id!r}: variables is for OCPP 2.0.1 chargers (this one is {version}; "
                             "use config_keys)")
        from .protocol.v201.device_model import check_charger  # lazy: config imports no protocol stack

        try:
            check_charger(c)
        except ValueError as e:
            raise ValueError(f"charger {c.id!r}: {e}") from None

    def resolve_ev(self, charger: ChargerConfig) -> EvModel:
        """The EV model of ``charger``'s session; an ``ev_models`` entry overrides a preset of the same id."""
        for e in self.ev_models:
            if e.id == charger.session.ev_model:
                return e.to_model()
        return PRESETS[charger.session.ev_model]

    def _check_profile(self, c: ChargerConfig) -> None:
        profile = self.csms_defaults.security_profile
        url = c.csms_url or self.csms_defaults.url or ""
        want_password, want_tls, want_cert = profile in (1, 2), profile >= 2, profile == 3
        problems = []
        if want_password != (c.password is not None):
            problems.append("a password" if want_password else "no password")
        if want_tls != url.startswith("wss://"):
            problems.append("a wss:// url" if want_tls else "a ws:// url")
        has_cert = (c.client_cert_file is not None, c.client_key_file is not None)
        if want_cert and has_cert != (True, True):
            problems.append("client_cert_file and client_key_file")
        if not want_cert and any(has_cert):
            problems.append("no client_cert_file / client_key_file")
        if problems:
            raise ValueError(f"charger {c.id!r}: security_profile {profile} needs {' and '.join(problems)}")

    def resolve_csms(self, charger: ChargerConfig) -> Tuple[str, str]:
        """``(url, ocpp_version)`` of ``charger`` after applying ``csms_defaults``."""
        url = charger.csms_url or self.csms_defaults.url
        assert url is not None  # guaranteed by _check_chargers
        return url, charger.ocpp_version or self.csms_defaults.ocpp_version


def parse_text(text: str) -> SimConfig:
    return SimConfig.model_validate(yaml.safe_load(text) or {})


def load_file(path: Union[str, Path]) -> SimConfig:
    return parse_text(Path(path).read_text(encoding="utf-8"))


#: Top-level blocks :func:`write_back` may rewrite, and their value when everything in them is default.
WRITE_BACK_BLOCKS: Dict[str, Any] = {"simulation": {}, "frame_log": {}, "csms_defaults": {}, "ev_models": [],
                                     "chargers": [], "mini_csms": {}}


def _blocks(config: SimConfig) -> Dict[str, Any]:
    return config.model_dump(mode="json", exclude_defaults=True)


def write_back_checked(text: str, config: SimConfig) -> Tuple[str, bool]:
    """``(new_text, redumped)``: ``text`` with every changed top-level block replaced by ``config``'s value (see
    the module docstring); ``redumped`` is ``True`` when a block could not be spliced and the whole document was
    re-dumped, losing every comment."""
    try:
        old = _blocks(parse_text(text))
    except Exception:  # noqa: BLE001 - unreadable / invalid text: rewrite every block
        old = None
    new = _blocks(config)
    redumped = False
    for key, empty in WRITE_BACK_BLOCKS.items():
        value = new.get(key, empty)
        if old is None or old.get(key, empty) != value:
            spliced = splice_yaml_block(text, key, yaml.safe_dump({key: value}, allow_unicode=True,
                                                                   sort_keys=False))
            text = replace_yaml_block(text, key, value)
            redumped = redumped or text != spliced
    return text, redumped


def write_back(text: str, config: SimConfig) -> str:
    """:func:`write_back_checked` without the flag (a full re-dump is logged as a warning)."""
    out, redumped = write_back_checked(text, config)
    if redumped:
        log.warning("config write-back re-dumped the whole YAML document: comments were lost")
    return out


def save_file(path: Union[str, Path], config: SimConfig) -> str:
    """Write ``config`` back into ``path`` (atomic), keeping comments outside the changed blocks.

    The file's newline style (LF / CRLF) and a UTF-8 BOM are kept. Returns the new text with ``\n`` newlines and
    no BOM. Blocking file I/O: async callers run it via ``asyncio.to_thread`` and serialise it against reloads
    (the web layer does both)."""
    p = Path(path)
    raw = p.read_bytes() if p.exists() else b""
    bom = raw.startswith(codecs.BOM_UTF8)
    text = raw[len(codecs.BOM_UTF8) if bom else 0:].decode("utf-8")
    crlf = "\r\n" in text
    text = write_back(text.replace("\r\n", "\n"), config)
    out = text.replace("\n", "\r\n") if crlf else text
    atomic_write_text(p, ("\ufeff" if bom else "") + out)
    return text
