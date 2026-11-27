"""Mini CSMS: per-version answers to station CALLs and the check of outbound CALLs.

Every CP -> CSMS action a version defines gets a valid minimal answer (``CP_ACTIONS``); anything else is answered
by the RPC layer with CALLERROR NotImplemented. The answers that carry a decision:

* BootNotification - ``mini_csms.boot`` (status, interval) and ``currentTime``;
* Authorize / StartTransaction / StopTransaction (1.6) and TransactionEvent with an idToken (2.0.1) -
  ``mini_csms.authorize`` (default status, per tag overrides);
* StartTransaction (1.6) - a transactionId from one counter per Mini CSMS;
* DataTransfer - ``UnknownVendorId`` (the Mini CSMS knows no vendor extension);
* SignCertificate - ``Rejected`` (it has no CA to sign with);
* 2.0.1 Get15118EVCertificate / GetCertificateStatus - ``Failed``; NotifyEVChargingNeeds - ``Rejected`` (no
  charging schedule will follow); NotifyEVChargingSchedule - ``Accepted``.

All other answers are empty objects (their response schemas have no required field). Inbound payloads of actions
with a model are checked with the version's codec: a violation is a CALLERROR with ``strict: true`` and a recorded
warning otherwise (tolerant towards real stations, plan §7).

Only ``rpc`` and ``protocol.<version>.models / codec`` are imported (no ``runtime`` / ``engine``, plan §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Tuple

from ..protocol.v16 import codec as codec16
from ..protocol.v16 import models as models16
from ..protocol.v201 import codec as codec201
from ..protocol.v201 import models as models201
from ..rpc.errors import OCPP16_ERRORS, OCPP201_ERRORS, CallError, ErrorCodes

if TYPE_CHECKING:
    from .session import CpSession


@dataclass(frozen=True)
class VersionSpec:
    version: str
    subprotocol: str
    codec: ModuleType
    errors: ErrorCodes
    #: Every CP -> CSMS action the version defines (answered by the Mini CSMS).
    cp_actions: Tuple[str, ...]
    #: CSMS -> CP actions the Mini CSMS can send (those with a request model, so they can be checked).
    csms_actions: Tuple[str, ...]


_CP16 = ("Authorize", "BootNotification", "DataTransfer", "DiagnosticsStatusNotification",
         "FirmwareStatusNotification", "Heartbeat", "MeterValues", "StartTransaction", "StatusNotification",
         "StopTransaction",
         # Security Whitepaper (edition 4)
         "LogStatusNotification", "SecurityEventNotification", "SignCertificate", "SignedFirmwareStatusNotification")
_CP201 = ("Authorize", "BootNotification", "ClearedChargingLimit", "DataTransfer", "FirmwareStatusNotification",
          "Get15118EVCertificate", "GetCertificateStatus", "Heartbeat", "LogStatusNotification", "MeterValues",
          "NotifyChargingLimit", "NotifyCustomerInformation", "NotifyDisplayMessages", "NotifyEVChargingNeeds",
          "NotifyEVChargingSchedule", "NotifyEvent", "NotifyMonitoringReport", "NotifyReport",
          "PublishFirmwareStatusNotification", "ReportChargingProfiles", "ReservationStatusUpdate",
          "SecurityEventNotification", "SignCertificate", "StatusNotification", "TransactionEvent")


def _csms_actions(models: ModuleType, cp: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(sorted(a for a in models.ACTIONS if a not in cp or a == "DataTransfer"))


SPECS: Dict[str, VersionSpec] = {
    "1.6": VersionSpec("1.6", "ocpp1.6", codec16, OCPP16_ERRORS, _CP16, _csms_actions(models16, _CP16)),
    "2.0.1": VersionSpec("2.0.1", "ocpp2.0.1", codec201, OCPP201_ERRORS, _CP201, _csms_actions(models201, _CP201)),
}
BY_SUBPROTOCOL = {s.subprotocol: s for s in SPECS.values()}

#: 1.6 AuthorizationStatus values; a 2.0.1-only status is answered as Invalid to a 1.6 station.
_AUTH16 = {"Accepted", "Blocked", "Expired", "Invalid", "ConcurrentTx"}


def now_iso() -> str:
    v = datetime.now(timezone.utc)
    return v.strftime("%Y-%m-%dT%H:%M:%S") + f".{v.microsecond // 1000:03d}Z"


def check_outbound(version: str, action: str, payload: Any) -> None:
    """``ValueError`` unless ``action`` is a CSMS -> CP action of ``version`` and ``payload`` matches its schema
    (the version's request model, strict: unknown fields are errors too)."""
    spec = SPECS.get(version)
    if spec is None:
        raise ValueError(f"OCPP {version} is not supported by the Mini CSMS")
    if action not in spec.csms_actions:
        raise ValueError(f"{action!r} is not a CSMS -> CP action of OCPP {version}")
    try:
        spec.codec.parse(action, payload, "request", strict=True)
    except CallError as e:
        raise ValueError(f"{e.code}: {e.description}") from None


Handler = Callable[[dict], Awaitable[dict]]


class Ctx:
    """What the handlers need from the Mini CSMS (duck-typed): the live config and a transaction id counter."""

    cfg: Any

    def next_tx_id(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError


def build_handlers(spec: VersionSpec, session: "CpSession", ctx: Ctx) -> Dict[str, Handler]:
    answers = _answers16 if spec.version == "1.6" else _answers201
    table = answers(session, ctx)
    out: Dict[str, Handler] = {}
    for action in spec.cp_actions:
        out[action] = _wrap(spec, session, ctx, action, table.get(action))
    return out


def _wrap(spec: VersionSpec, session: "CpSession", ctx: Ctx, action: str, answer) -> Handler:
    async def handler(payload: dict) -> dict:
        if _has_model(spec, action):
            try:
                spec.codec.parse(action, payload, "request", strict=False)
            except CallError as e:
                if ctx.cfg.strict:
                    raise
                session.warn(f"{action}: {e.code}: {e.description}")
        session.count(action, payload)
        return answer(payload) if answer is not None else {}
    return handler


def _has_model(spec: VersionSpec, action: str) -> bool:
    models = models16 if spec.version == "1.6" else models201
    return action in models.REQUESTS


def _auth_status(ctx: Ctx, tag: Any, v16: bool) -> str:
    a = ctx.cfg.authorize
    status = a.tags.get(tag, a.default) if isinstance(tag, str) else a.default
    return status if not v16 or status in _AUTH16 else "Invalid"


def _boot(session: "CpSession", ctx: Ctx, payload: dict) -> dict:
    b = ctx.cfg.boot
    session.on_boot(payload, b.status)
    return {"currentTime": now_iso(), "interval": b.interval, "status": b.status}


def _answers16(session: "CpSession", ctx: Ctx) -> Dict[str, Callable[[dict], dict]]:
    def authorize(p: dict) -> dict:
        return {"idTagInfo": {"status": _auth_status(ctx, p.get("idTag"), True)}}

    def start(p: dict) -> dict:
        tx_id = ctx.next_tx_id()
        status = _auth_status(ctx, p.get("idTag"), True)
        session.tx_start(str(tx_id), p.get("connectorId"), p.get("idTag"), p.get("meterStart"), p.get("timestamp"))
        return {"transactionId": tx_id, "idTagInfo": {"status": status}}

    def stop(p: dict) -> dict:
        session.tx_stop(str(p.get("transactionId")), p.get("meterStop"), p.get("timestamp"), p.get("reason"))
        session.meter_values(None, p.get("transactionId"), p.get("transactionData") or [])
        if "idTag" in p:
            return {"idTagInfo": {"status": _auth_status(ctx, p.get("idTag"), True)}}
        return {}

    def status(p: dict) -> dict:
        session.status(str(p.get("connectorId")), p.get("status"), p.get("errorCode"), p.get("timestamp"))
        return {}

    def meter(p: dict) -> dict:
        session.meter_values(p.get("connectorId"), p.get("transactionId"), p.get("meterValue") or [])
        return {}

    def heartbeat(p: dict) -> dict:
        session.on_heartbeat()
        return {"currentTime": now_iso()}

    return {
        "BootNotification": lambda p: _boot(session, ctx, p),
        "Heartbeat": heartbeat,
        "Authorize": authorize,
        "StartTransaction": start,
        "StopTransaction": stop,
        "StatusNotification": status,
        "MeterValues": meter,
        "DataTransfer": lambda p: {"status": "UnknownVendorId"},
        "SignCertificate": lambda p: {"status": "Rejected"},
    }


def _answers201(session: "CpSession", ctx: Ctx) -> Dict[str, Callable[[dict], dict]]:
    def token(p: dict) -> dict:
        tok = p.get("idToken")
        return {"status": _auth_status(ctx, tok.get("idToken") if isinstance(tok, dict) else None, False)}

    def tx_event(p: dict) -> dict:
        info = p.get("transactionInfo") or {}
        tx_id, evse = str(info.get("transactionId")), (p.get("evse") or {}).get("id")
        tok = p.get("idToken")
        tag = tok.get("idToken") if isinstance(tok, dict) else None
        kind = p.get("eventType")
        if kind == "Started" or not session.has_tx(tx_id):
            session.tx_start(tx_id, evse, tag, None, p.get("timestamp"))
        session.tx_update(tx_id, tag=tag, state=info.get("chargingState"), trigger=p.get("triggerReason"))
        session.meter_values(evse, tx_id, p.get("meterValue") or [])
        if kind == "Ended":
            session.tx_stop(tx_id, None, p.get("timestamp"), info.get("stoppedReason"))
        return {"idTokenInfo": token(p)} if tok is not None else {}

    def status(p: dict) -> dict:
        session.status(f"{p.get('evseId')}/{p.get('connectorId')}", p.get("connectorStatus"), None,
                       p.get("timestamp"))
        return {}

    def meter(p: dict) -> dict:
        session.meter_values(p.get("evseId"), None, p.get("meterValue") or [])
        return {}

    def heartbeat(p: dict) -> dict:
        session.on_heartbeat()
        return {"currentTime": now_iso()}

    return {
        "BootNotification": lambda p: _boot(session, ctx, p),
        "Heartbeat": heartbeat,
        "Authorize": lambda p: {"idTokenInfo": token(p)},
        "TransactionEvent": tx_event,
        "StatusNotification": status,
        "MeterValues": meter,
        "DataTransfer": lambda p: {"status": "UnknownVendorId"},
        "SignCertificate": lambda p: {"status": "Rejected"},
        "Get15118EVCertificate": lambda p: {"status": "Failed", "exiResponse": ""},
        "GetCertificateStatus": lambda p: {"status": "Failed"},
        "NotifyEVChargingNeeds": lambda p: {"status": "Rejected"},
        "NotifyEVChargingSchedule": lambda p: {"status": "Accepted"},
    }
