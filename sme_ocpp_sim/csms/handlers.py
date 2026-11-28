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
* 2.1 TransactionEvent - also ``mini_csms.transaction_limit`` in the answer to a Started event; 2.1 NotifyEvent
  with Connector.AvailabilityState - recorded as the connector status (G01).

All other answers are empty objects (their response schemas have no required field). Inbound payloads of actions
with a model are checked with the version's codec: a violation is a CALLERROR with ``strict: true`` and a recorded
warning otherwise (tolerant towards real stations, plan §7).

Only ``rpc`` and ``protocol.<version>.models / codec`` are imported (no ``runtime`` / ``engine``, plan §3).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Tuple

from ..protocol.v16 import codec as codec16
from ..protocol.v16 import models as models16
from ..protocol.v201 import codec as codec201
from ..protocol.v201 import models as models201
from ..protocol.v21 import codec as codec21
from ..rpc.errors import OCPP16_ERRORS, OCPP21_ERRORS, OCPP201_ERRORS, CallError, ErrorCodes

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


#: 2.1 = the 2.0.1 CP actions plus the new CS -> CSMS 2.1 messages (NotifyPeriodicEventStream is a SEND).
_CP21 = tuple(sorted(_CP201 + (
    "BatterySwap", "ClosePeriodicEventStream", "GetCertificateChainStatus", "NotifyDERAlarm", "NotifyDERStartStop",
    "NotifyPeriodicEventStream", "NotifyPriorityCharging", "NotifySettlement", "OpenPeriodicEventStream",
    "PullDynamicScheduleUpdate", "ReportDERControl", "VatNumberValidation")))


def _csms_actions(models: ModuleType, cp: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(sorted(a for a in models.ACTIONS if a not in cp or a == "DataTransfer"))


SPECS: Dict[str, VersionSpec] = {
    "1.6": VersionSpec("1.6", "ocpp1.6", codec16, OCPP16_ERRORS, _CP16, _csms_actions(models16, _CP16)),
    "2.0.1": VersionSpec("2.0.1", "ocpp2.0.1", codec201, OCPP201_ERRORS, _CP201, _csms_actions(models201, _CP201)),
    # Minimal 2.1 set: the 2.0.1 answers (2.1 payloads parse with the 2.1 models) and schema-valid answers to the
    # new 2.1 CS -> CSMS messages; CSMS -> CS: every action with a 2.1 model in protocol.v21.codec.
    "2.1": VersionSpec("2.1", "ocpp2.1", codec21, OCPP21_ERRORS, _CP21, _csms_actions(codec21, _CP21)),
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
    if spec.version == "2.1":
        table.update(_ANSWERS21)
        table["PullDynamicScheduleUpdate"] = functools.partial(_pull_dynamic, session)
        table["TransactionEvent"] = functools.partial(_tx_event21, session, ctx, table["TransactionEvent"])
        table["NotifyEvent"] = functools.partial(_notify_event21, session)
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
    if spec.version == "2.1":
        return action in codec21.REQUESTS
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


#: Schema-valid answers to the new 2.1 CS -> CSMS messages (the others answer an empty payload, which is what
#: their response schemas require). GetCertificateChainStatus is answered with one ``Unknown`` status entry per
#: requested certificate (the Mini CSMS has no certificate status source). NotifyPeriodicEventStream is a SEND:
#: never answered.
_ANSWERS21: Dict[str, Callable[[dict], dict]] = {
    "BatterySwap": lambda p: {},
    "GetCertificateChainStatus": lambda p: {"certificateStatus": [
        {"certificateHashData": c.get("certificateHashData"), "source": c.get("source"), "status": "Unknown",
         "nextUpdate": "1970-01-01T00:00:00Z"} for c in p.get("certificateStatusRequests") or ()]},
    "OpenPeriodicEventStream": lambda p: {"status": "Accepted"},
    "VatNumberValidation": lambda p: {"vatNumber": p.get("vatNumber", ""), "status": "Rejected"},
}


#: mini_csms.transaction_limit field -> TransactionLimitType wire name (2.1, E16 / F07).
_TX_LIMIT = {"max_energy_wh": "maxEnergy", "max_time_s": "maxTime", "max_cost": "maxCost", "max_soc": "maxSoC"}


def _tx_event21(session: "CpSession", ctx: Ctx, base: Callable[[dict], dict], p: dict) -> dict:
    """2.1 TransactionEvent: the 2.0.1 answer, plus ``mini_csms.transaction_limit`` in the answer to a Started event
    (E16.FR.02 / F07.FR.02); the reported tariffId, transactionLimit and costDetails total are kept."""
    out = base(p)
    info = p.get("transactionInfo") or {}
    tx_id = str(info.get("transactionId"))
    cost = ((p.get("costDetails") or {}).get("totalCost") or {})
    session.tx_extra(tx_id, tariff_id=info.get("tariffId"), transaction_limit=info.get("transactionLimit"),
                     total_cost=(cost.get("total") or {}).get("exclTax"), currency=cost.get("currency"))
    limit = getattr(ctx.cfg, "transaction_limit", None)
    if p.get("eventType") == "Started" and limit is not None:
        values = {wire: getattr(limit, k) for k, wire in _TX_LIMIT.items() if getattr(limit, k) is not None}
        if values:
            out = {**out, "transactionLimit": values}
    return out


def _notify_event21(session: "CpSession", p: dict) -> dict:
    """G01: a Connector.AvailabilityState event updates the connector status like a StatusNotification."""
    for ev in p.get("eventData") or ():
        if not isinstance(ev, dict):
            continue
        comp, var = ev.get("component") or {}, ev.get("variable") or {}
        if comp.get("name") == "Connector" and var.get("name") == "AvailabilityState":
            evse = comp.get("evse") or {}
            session.status(f"{evse.get('id')}/{evse.get('connectorId')}", ev.get("actualValue"), None,
                           ev.get("timestamp"))
    return {}


#: ChargingScheduleUpdateType fields (the values a Dynamic profile period can change, K28).
_UPDATE_FIELDS = ("limit", "limit_L2", "limit_L3", "dischargeLimit", "dischargeLimit_L2", "dischargeLimit_L3",
                  "setpoint", "setpoint_L2", "setpoint_L3", "setpointReactive", "setpointReactive_L2",
                  "setpointReactive_L3")


def remember_dynamic(session: "CpSession", action: str, payload: Any, result: Any) -> None:
    """Keep the values of the Dynamic profiles the Mini CSMS sent and the station accepted (SetChargingProfile /
    UpdateDynamicSchedule), so that PullDynamicScheduleUpdate can answer with them (K28.FR.07)."""
    if not isinstance(payload, dict) or not isinstance(result, dict) or result.get("status") != "Accepted":
        return
    if action == "SetChargingProfile":
        profile = payload.get("chargingProfile") or {}
        pid = profile.get("id")
        if profile.get("chargingProfileKind") != "Dynamic":
            _forget_dynamic(session, pid)  # the same id replaced by another kind
            return
        try:
            period = profile["chargingSchedule"][0]["chargingSchedulePeriod"][0]
        except (KeyError, IndexError, TypeError):
            return
        session.dynamic_profiles[pid] = {k: period[k] for k in _UPDATE_FIELDS if k in period}
        session.dynamic_meta[pid] = (payload.get("evseId"), profile.get("chargingProfilePurpose"),
                                     profile.get("stackLevel"))
    elif action == "UpdateDynamicSchedule":
        pid = payload.get("chargingProfileId")
        if pid in session.dynamic_profiles:
            session.dynamic_profiles[pid].update(
                {k: v for k, v in (payload.get("scheduleUpdate") or {}).items() if k in _UPDATE_FIELDS})
    elif action == "ClearChargingProfile":
        if payload.get("chargingProfileId") is not None:
            _forget_dynamic(session, payload["chargingProfileId"])
            return
        crit = payload.get("chargingProfileCriteria") or {}  # the criteria are ANDed; an absent one matches all
        for pid, (evse, purpose, stack) in list(session.dynamic_meta.items()):
            if all(want is None or want == have for want, have in
                   ((crit.get("evseId"), evse), (crit.get("chargingProfilePurpose"), purpose),
                    (crit.get("stackLevel"), stack))):
                _forget_dynamic(session, pid)


def _forget_dynamic(session: "CpSession", pid: Any) -> None:
    session.dynamic_profiles.pop(pid, None)
    session.dynamic_meta.pop(pid, None)


def _pull_dynamic(session: "CpSession", p: dict) -> dict:
    """K28.FR.07: ``Accepted`` with the current values of a Dynamic profile the Mini CSMS sent; K28.FR.12:
    ``Rejected`` ``InvalidProfile`` for another id."""
    values = session.dynamic_profiles.get(p.get("chargingProfileId"))
    if values is None:
        return {"status": "Rejected", "statusInfo": {"reasonCode": "InvalidProfile"}}
    return {"status": "Accepted", "scheduleUpdate": dict(values)}
