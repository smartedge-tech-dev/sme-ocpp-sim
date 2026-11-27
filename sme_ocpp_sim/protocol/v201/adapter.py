"""OCPP 2.0.1 version adapter: 2.0.1 messages <-> station calls (plan §11.2, M2 / M3).

A separate stack from ``protocol/v16``: it uses only the 2.0.1 models and codec of this package.

Inbound: DataTransfer (M2), the remote-control actions (M3: RequestStartTransaction,
RequestStopTransaction, UnlockConnector, Reset, ChangeAvailability, TriggerMessage) and the Device Model
actions (M4: GetVariables, SetVariables, GetBaseReport; the NotifyReport parts go out with
:meth:`Adapter201.send_notify_report`). DataTransfer goes to the
station as the version-neutral domain command; a remote-control request is handed to the station as its parsed
2.0.1 request model and the station answers with the 2.0.1 response model (the semantics are 2.0.1-specific,
plan §11.2, so there is no domain command in between). Every other action has no handler and is answered
CALLERROR NotImplemented by the RPC layer (smart charging is M5; ISO 15118 messages such as
Get15118EVCertificate / GetCertificateStatus stay NotImplemented in v0.2, plan §11.1 Q9).

Outbound: the shell's version-neutral messages (BootNotification, Heartbeat, DataTransfer) come in as domain
events (``protocol.base``); the transaction flow is 2.0.1-specific and is sent as ready-made
:class:`~.models.TransactionEventRequest` models built by ``Station201`` (:meth:`Adapter201.send_transaction_event`).
The 1.6-shaped domain transaction calls of :class:`VersionAdapter` (``send_tx_started`` / ``send_tx_updated`` /
``send_tx_ended``) do not fit 2.0.1 (the station, not the CSMS, generates the transaction id) and raise
``NotImplementedError``.

Interpretations (not defined by the spec)
-----------------------------------------
- BootNotification ``reason`` (M3): ``RemoteReset`` for the boots after a CSMS Reset until one is accepted,
  ``Triggered`` for the one boot a TriggerMessage asked for, ``PowerUp`` otherwise (also after a reconnect: the
  simulator does not model a reboot without a Reset). The station sets it (:attr:`Adapter201.boot_reason`,
  :meth:`Adapter201.trigger_boot_reason`).
- An id token presented at the charger is sent as ``type`` ``ISO14443`` (an RFID card); a token of a
  RequestStartTransaction keeps the ``type`` the CSMS sent.
- The domain ``DomainStatus`` is projected to ``connectorStatus``: Preparing / Charging / SuspendedEV /
  SuspendedEVSE / Finishing -> ``Occupied``; the finer state travels as ``chargingState`` in TransactionEvent.
- An ``AuthorizationStatus`` the domain does not know (NoCredit, NotAllowedTypeEVSE, NotAtThisLocation,
  NotAtThisTime, Unknown) is reported to the operator as ``Invalid``; the wire value is kept in the frame log.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ...rpc.errors import OCPP201_ERRORS
from .. import base as d
from . import models as m
from .codec import dump, parse
from .enums import (AuthorizationStatusEnumType, BootReasonEnumType, ConnectorStatusEnumType,
                    DataTransferStatusEnumType, IdTokenEnumType)

log = logging.getLogger(__name__)

#: ``type`` of an id token presented at the charger (see the module docstring).
LOCAL_TOKEN_TYPE = IdTokenEnumType.iso14443

_CONNECTOR_STATUS: Dict[d.DomainStatus, ConnectorStatusEnumType] = {
    d.DomainStatus.AVAILABLE: ConnectorStatusEnumType.available,
    d.DomainStatus.PREPARING: ConnectorStatusEnumType.occupied,
    d.DomainStatus.CHARGING: ConnectorStatusEnumType.occupied,
    d.DomainStatus.SUSPENDED_EV: ConnectorStatusEnumType.occupied,
    d.DomainStatus.SUSPENDED_EVSE: ConnectorStatusEnumType.occupied,
    d.DomainStatus.FINISHING: ConnectorStatusEnumType.occupied,
    d.DomainStatus.RESERVED: ConnectorStatusEnumType.reserved,
    d.DomainStatus.UNAVAILABLE: ConnectorStatusEnumType.unavailable,
    d.DomainStatus.FAULTED: ConnectorStatusEnumType.faulted,
}

#: CSMS -> CS remote-control actions (M3) answered by the station with 2.0.1 response models.
REMOTE_ACTIONS = ("RequestStartTransaction", "RequestStopTransaction", "UnlockConnector", "Reset",
                  "ChangeAvailability", "TriggerMessage",
                  # M4 Device Model, answered the same way
                  "GetVariables", "SetVariables", "GetBaseReport")

_DOMAIN_AUTH = {a.value for a in (d.AuthResult.ACCEPTED, d.AuthResult.BLOCKED, d.AuthResult.EXPIRED,
                                  d.AuthResult.INVALID, d.AuthResult.CONCURRENT_TX)}


def connector_status(status: d.DomainStatus) -> ConnectorStatusEnumType:
    """The 2.0.1 ``connectorStatus`` of a domain status (see the module docstring)."""
    return _CONNECTOR_STATUS[status]


def id_token(token: str, token_type: IdTokenEnumType = LOCAL_TOKEN_TYPE) -> m.IdTokenType:
    return m.IdTokenType(id_token=token, type=token_type)


def auth_result(status: AuthorizationStatusEnumType) -> d.AuthResult:
    """Domain authorization outcome of a 2.0.1 ``AuthorizationStatus`` (see the module docstring)."""
    return d.AuthResult(status.value) if status.value in _DOMAIN_AUTH else d.AuthResult.INVALID


def id_token_info(info: m.IdTokenInfoType) -> d.IdTokenInfo:
    group = info.group_id_token.id_token if info.group_id_token is not None else None
    return d.IdTokenInfo(status=auth_result(info.status), expiry=info.cache_expiry_date_time,
                         parent_id_token=group)


def sampled_values(samples: Sequence[d.Sample], context: str) -> List[m.SampledValueType]:
    return [m.SampledValueType(value=s.value, context=context, measurand=s.measurand, phase=s.phase,
                               location=s.location,
                               unit_of_measure=m.UnitOfMeasureType(unit=s.unit) if s.unit else None)
            for s in samples]


def meter_value(ts: datetime, samples: Sequence[d.Sample], context: str) -> m.MeterValueType:
    """One ``MeterValueType``; ``samples`` must not be empty (``sampledValue`` has ``minItems`` 1)."""
    return m.MeterValueType(timestamp=ts, sampled_value=sampled_values(samples, context))


class Adapter201(d.VersionAdapter):
    version = "2.0.1"
    subprotocol = "ocpp2.0.1"
    error_codes = OCPP201_ERRORS
    config_keys = ()  # 2.0.1 has a Device Model instead of a key table (``device_model``)

    def __init__(self, *, strict: bool = False) -> None:
        super().__init__(strict=strict)
        #: ``reason`` of every BootNotification (see the module docstring).
        self.boot_reason = BootReasonEnumType.power_up
        self._boot_reason_once: Optional[BootReasonEnumType] = None

    def trigger_boot_reason(self, reason: BootReasonEnumType) -> None:
        """Use ``reason`` for the next BootNotification only."""
        self._boot_reason_once = reason

    def _parse(self, action: str, payload: Any, kind: str = "request") -> Any:
        model, ignored = parse(action, payload, kind, strict=self.strict)  # type: ignore[arg-type]
        if ignored:
            log.info("%s %s: ignored unknown fields %s", action, kind, ignored)
        return model

    # ------------------------------------------------------------------ inbound

    def handlers(self, runtime: d.CommandSink) -> Dict[str, d.Handler]:
        table: Dict[str, Any] = {"DataTransfer": self._data_transfer}
        for action in REMOTE_ACTIONS:
            table[action] = functools.partial(self._remote, action)
        return {a: functools.partial(h, runtime) for a, h in table.items()}

    async def _remote(self, action: str, runtime: d.CommandSink, payload: dict) -> dict:
        """A remote-control CALL: the station gets the request model and returns the response model."""
        res = await runtime.handle(self._parse(action, payload))
        if not isinstance(res, m.RESPONSES[action]):
            raise TypeError(f"{action}: the station answered {type(res).__name__}")
        return dump(res)

    async def _data_transfer(self, runtime: d.CommandSink, payload: dict) -> dict:
        req: m.DataTransferRequest = self._parse("DataTransfer", payload)
        # ``data`` is any JSON in 2.0.1; the domain carries it unchanged.
        res: d.DataTransferResult = await runtime.handle(d.DataTransfer(req.vendor_id, req.message_id, req.data))
        return dump(m.DataTransferResponse(status=DataTransferStatusEnumType(res.status.value), data=res.data))

    # ------------------------------------------------------------------ outbound

    async def _send(self, action: str, request: m.OcppModel) -> Any:
        if self._call is None:
            raise ConnectionError("not connected")
        payload = await self._call(action, dump(request))
        return self._parse(action, payload, "response")

    async def send_booting(self, ev: d.Booting) -> d.BootResult:
        cs = m.ChargingStationType(vendor_name=ev.vendor, model=ev.model, serial_number=ev.serial,
                                   firmware_version=ev.firmware)
        reason, self._boot_reason_once = self._boot_reason_once or self.boot_reason, None
        res: m.BootNotificationResponse = await self._send(
            "BootNotification", m.BootNotificationRequest(charging_station=cs, reason=reason))
        return d.BootResult(d.BootStatus(res.status.value), res.interval, res.current_time)

    async def send_heartbeat(self, ev: d.Heartbeat) -> Optional[datetime]:
        res: m.HeartbeatResponse = await self._send("Heartbeat", m.HeartbeatRequest())
        return res.current_time

    async def send_connector_status(self, ev: d.ConnectorStatus) -> None:
        """``ev.error_code`` / ``info`` are not part of a 2.0.1 StatusNotification and are not sent."""
        await self._send("StatusNotification", m.StatusNotificationRequest(
            timestamp=ev.timestamp or datetime.now(timezone.utc), connector_status=connector_status(ev.status), evse_id=ev.evse_id,
            connector_id=ev.connector_id))

    async def send_authorize(self, ev: d.AuthorizeRequest) -> d.IdTokenInfo:
        res: m.AuthorizeResponse = await self._send("Authorize", m.AuthorizeRequest(id_token=id_token(ev.id_token)))
        return id_token_info(res.id_token_info)

    async def send_transaction_event(self, req: m.TransactionEventRequest) -> m.TransactionEventResponse:
        return await self._send("TransactionEvent", req)

    async def send_meter_sample(self, ev: d.MeterSample) -> None:
        """MeterValues outside the transaction flow (2.0.1 sends transaction readings in TransactionEvent)."""
        await self._send("MeterValues", m.MeterValuesRequest(
            evse_id=ev.evse_id, meter_value=[meter_value(ev.timestamp, ev.samples, ev.context)]))

    async def send_notify_report(self, req: m.NotifyReportRequest) -> m.NotifyReportResponse:
        """One NotifyReport part of a GetBaseReport (built by ``Station201``)."""
        return await self._send("NotifyReport", req)

    async def send_data_transfer(self, cmd: d.DataTransfer) -> d.DataTransferResult:
        res: m.DataTransferResponse = await self._send(
            "DataTransfer", m.DataTransferRequest(vendor_id=cmd.vendor_id, message_id=cmd.message_id, data=cmd.data))
        return d.DataTransferResult(d.DataTransferStatus(res.status.value), res.data)

    # ------------------------------------------------------------------ 1.6-shaped transaction calls

    async def send_tx_started(self, ev: d.TxStarted) -> d.TxStartedResult:
        raise NotImplementedError("2.0.1 transactions use send_transaction_event")

    async def send_tx_updated(self, ev: d.TxUpdated) -> None:
        raise NotImplementedError("2.0.1 transactions use send_transaction_event")

    async def send_tx_ended(self, ev: d.TxEnded) -> Optional[d.IdTokenInfo]:
        raise NotImplementedError("2.0.1 transactions use send_transaction_event")
