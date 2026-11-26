"""OCPP 1.6J version adapter: 1.6 messages <-> domain commands / events (plan §2.3, §3.2).

Inbound: every handler parses with :func:`codec.parse` (lenient unless ``strict``), applies the range checks the
schemas cannot express (connectorId vs connector count), hands one domain command to the runtime and dumps the
domain result as the 1.6 response. Actions without a handler are answered CALLERROR NotImplemented by the RPC
layer (UpdateFirmware, GetDiagnostics, Reservation, LocalAuthList, ...).

Outbound: ``send_*`` build 1.6 requests from domain events and parse the responses (``kind="response"``).
Transaction ids are 1.6 integers on the wire and strings in the domain.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ...rpc.errors import OCPP16_ERRORS, CallError
from .. import base as d
from . import models as m
from .codec import dump, parse
from .config_keys import KEYS

log = logging.getLogger(__name__)

_NO_ERROR = "NoError"


def _fmt(value: float) -> str:
    v = float(value)
    if v.is_integer():
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _sampled(samples: Sequence[d.Sample], context: str) -> List[m.SampledValue]:
    return [m.SampledValue(value=_fmt(s.value), context=context, measurand=s.measurand, unit=s.unit,
                           phase=s.phase, location=s.location) for s in samples]


def _id_info(info: m.IdTagInfo) -> d.IdTokenInfo:
    return d.IdTokenInfo(status=info.status.value, expiry=info.expiry_date, parent_id_token=info.parent_id_tag)


def _accepted(status: d.CommandStatus) -> str:
    return "Accepted" if status is d.CommandStatus.ACCEPTED else "Rejected"


def _check_connector(cid: int, runtime: d.CommandSink, *, allow_zero: bool) -> None:
    lo, hi = (0 if allow_zero else 1), runtime.connector_count
    if not lo <= cid <= hi:
        raise CallError(OCPP16_ERRORS.property_constraint_violation,
                        f"connectorId: {cid} is out of range {lo}..{hi}", {"field": "connectorId"})


class Adapter16(d.VersionAdapter):
    version = "1.6"
    subprotocol = "ocpp1.6"
    error_codes = OCPP16_ERRORS
    config_keys = tuple(KEYS)

    # ------------------------------------------------------------------ inbound

    def _parse(self, action: str, payload: Any) -> Any:
        model, ignored = parse(action, payload, strict=self.strict)
        if ignored:
            log.info("%s: ignored unknown fields %s", action, ", ".join(ignored))
        return model

    def handlers(self, runtime: d.CommandSink) -> Dict[str, d.Handler]:
        p = self._parse

        async def remote_start(payload: dict) -> dict:
            req = p("RemoteStartTransaction", payload)
            if req.connector_id is not None:
                # TODO(spec): connectorId 0 on RemoteStartTransaction is treated as out of range (must be > 0).
                _check_connector(req.connector_id, runtime, allow_zero=False)
            # M4: convert chargingProfile into charging.profiles; until then the 1.6 model is passed through.
            st = await runtime.handle(d.RequestStart(req.connector_id, req.id_tag, req.charging_profile))
            return dump(m.RemoteStartTransactionResponse(status=_accepted(st)))

        async def remote_stop(payload: dict) -> dict:
            req = p("RemoteStopTransaction", payload)
            st = await runtime.handle(d.RequestStop(str(req.transaction_id)))
            return dump(m.RemoteStopTransactionResponse(status=_accepted(st)))

        async def get_configuration(payload: dict) -> dict:
            req = p("GetConfiguration", payload)
            res: d.GetConfigResult = await runtime.handle(d.GetConfig(tuple(req.key or ())))
            return dump(m.GetConfigurationResponse(
                configuration_key=[m.KeyValue(key=e.key, readonly=e.readonly, value=e.value) for e in res.known],
                unknown_key=list(res.unknown) or None))

        async def change_configuration(payload: dict) -> dict:
            req = p("ChangeConfiguration", payload)
            st: d.ConfigStatus = await runtime.handle(d.SetConfig(req.key, req.value))
            return dump(m.ChangeConfigurationResponse(status=st.value))

        async def reset(payload: dict) -> dict:
            req = p("Reset", payload)
            st = await runtime.handle(d.Reset(d.ResetKind(req.type.value)))
            return dump(m.ResetResponse(status=_accepted(st)))

        async def trigger(payload: dict) -> dict:
            req = p("TriggerMessage", payload)
            if req.connector_id is not None:
                # TODO(spec): connectorId 0 is accepted for TriggerMessage (charger-wide request).
                _check_connector(req.connector_id, runtime, allow_zero=True)
            what = d.TriggerWhat(req.requested_message.value)
            if what in (d.TriggerWhat.DIAGNOSTICS_STATUS, d.TriggerWhat.FIRMWARE_STATUS):
                return dump(m.TriggerMessageResponse(status="NotImplemented"))
            st = await runtime.handle(d.Trigger(what, req.connector_id))
            status = {d.CommandStatus.ACCEPTED: "Accepted", d.CommandStatus.REJECTED: "Rejected"}.get(
                st, "NotImplemented")
            return dump(m.TriggerMessageResponse(status=status))

        async def unlock(payload: dict) -> dict:
            req = p("UnlockConnector", payload)
            _check_connector(req.connector_id, runtime, allow_zero=False)
            st: d.UnlockStatus = await runtime.handle(d.Unlock(req.connector_id))
            return dump(m.UnlockConnectorResponse(status=st.value))

        async def change_availability(payload: dict) -> dict:
            req = p("ChangeAvailability", payload)
            _check_connector(req.connector_id, runtime, allow_zero=True)
            st: d.AvailabilityStatus = await runtime.handle(
                d.ChangeAvailability(req.connector_id, req.type.value == "Operative"))
            return dump(m.ChangeAvailabilityResponse(status=st.value))

        async def data_transfer(payload: dict) -> dict:
            req = p("DataTransfer", payload)
            res: d.DataTransferResult = await runtime.handle(d.DataTransfer(req.vendor_id, req.message_id, req.data))
            return dump(m.DataTransferResponse(status=res.status.value, data=res.data))

        def smart_charging(action: str) -> d.Handler:
            async def not_yet(payload: dict) -> dict:
                # M4: SetChargingProfile / ClearChargingProfile / GetCompositeSchedule with charging.profiles.
                raise CallError(OCPP16_ERRORS.not_implemented, f"{action} is not implemented yet")
            return not_yet

        return {
            "RemoteStartTransaction": remote_start,
            "RemoteStopTransaction": remote_stop,
            "GetConfiguration": get_configuration,
            "ChangeConfiguration": change_configuration,
            "Reset": reset,
            "TriggerMessage": trigger,
            "UnlockConnector": unlock,
            "ChangeAvailability": change_availability,
            "DataTransfer": data_transfer,
            **{a: smart_charging(a) for a in ("SetChargingProfile", "ClearChargingProfile", "GetCompositeSchedule")},
        }

    # ------------------------------------------------------------------ outbound

    async def _send(self, action: str, request: m.OcppModel) -> Any:
        if self._call is None:
            raise ConnectionError("not connected")
        payload = await self._call(action, dump(request))
        model, _ = parse(action, payload, kind="response")
        return model

    async def send_booting(self, ev: d.Booting) -> d.BootResult:
        resp = await self._send("BootNotification", m.BootNotificationRequest(
            charge_point_vendor=ev.vendor, charge_point_model=ev.model,
            charge_point_serial_number=ev.serial, firmware_version=ev.firmware))
        return d.BootResult(d.BootStatus(resp.status.value), resp.interval, resp.current_time)

    async def send_heartbeat(self, ev: d.Heartbeat) -> Optional[datetime]:
        resp = await self._send("Heartbeat", m.HeartbeatRequest())
        return resp.current_time

    async def send_connector_status(self, ev: d.ConnectorStatus) -> None:
        await self._send("StatusNotification", m.StatusNotificationRequest(
            connector_id=ev.connector_id, error_code=ev.error_code or _NO_ERROR, status=ev.status.value,
            info=ev.info, timestamp=ev.timestamp, vendor_error_code=ev.vendor_error_code))

    async def send_authorize(self, ev: d.AuthorizeRequest) -> d.IdTokenInfo:
        resp = await self._send("Authorize", m.AuthorizeRequest(id_tag=ev.id_token))
        return _id_info(resp.id_tag_info)

    async def send_tx_started(self, ev: d.TxStarted) -> d.TxStartedResult:
        resp = await self._send("StartTransaction", m.StartTransactionRequest(
            connector_id=ev.evse_id, id_tag=ev.id_token, meter_start=ev.meter_start_wh, timestamp=ev.timestamp,
            reservation_id=ev.reservation_id))
        return d.TxStartedResult(str(resp.transaction_id), _id_info(resp.id_tag_info))

    async def send_tx_updated(self, ev: d.TxUpdated) -> None:
        if not ev.samples:
            return  # 1.6 needs at least one sampledValue
        await self._send("MeterValues", m.MeterValuesRequest(
            connector_id=ev.evse_id, transaction_id=int(ev.tx_id),
            meter_value=[m.MeterValueNonEmpty(timestamp=ev.timestamp,
                                              sampled_value=_sampled(ev.samples, ev.context))]))

    async def send_tx_ended(self, ev: d.TxEnded) -> Optional[d.IdTokenInfo]:
        data = ([m.MeterValue(timestamp=ev.timestamp, sampled_value=_sampled(ev.samples, "Transaction.End"))]
                if ev.samples else None)
        resp = await self._send("StopTransaction", m.StopTransactionRequest(
            transaction_id=int(ev.tx_id), timestamp=ev.timestamp, meter_stop=ev.meter_stop_wh,
            id_tag=ev.id_token, reason=ev.reason, transaction_data=data))
        return _id_info(resp.id_tag_info) if resp.id_tag_info is not None else None

    async def send_meter_sample(self, ev: d.MeterSample) -> None:
        if not ev.samples:
            return
        await self._send("MeterValues", m.MeterValuesRequest(
            connector_id=ev.evse_id, transaction_id=int(ev.tx_id) if ev.tx_id is not None else None,
            meter_value=[m.MeterValueNonEmpty(timestamp=ev.timestamp,
                                              sampled_value=_sampled(ev.samples, ev.context))]))

    async def send_data_transfer(self, cmd: d.DataTransfer) -> d.DataTransferResult:
        resp = await self._send("DataTransfer", m.DataTransferRequest(
            vendor_id=cmd.vendor_id, message_id=cmd.message_id, data=cmd.data))
        return d.DataTransferResult(d.DataTransferStatus(resp.status.value), resp.data)
