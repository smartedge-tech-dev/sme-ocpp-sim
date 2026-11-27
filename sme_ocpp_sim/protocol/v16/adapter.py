"""OCPP 1.6J version adapter: 1.6 messages <-> domain commands / events (plan §2.3, §3.2).

Inbound: every handler parses with :func:`codec.parse` (lenient unless ``strict``), applies the range checks the
schemas cannot express (connectorId vs connector count), hands one domain command to the runtime and dumps the
domain result as the 1.6 response. Actions without a handler are answered CALLERROR NotImplemented by the RPC
layer.

Outbound: ``send_*`` build 1.6 requests from domain events and parse the responses (``kind="response"``).
Transaction ids are 1.6 integers on the wire and strings in the domain.

Smart charging (1.6 §3.13, §5.7, §5.16, §6.13, §7.13; errata v4.0): ``csChargingProfiles`` / ``chargingProfile``
become :class:`protocol.v16.profiles.ChargingProfile` (naive datetimes are taken as UTC). An invalid
``chargingRateUnit`` (like any other invalid enum value) is CALLERROR PropertyConstraintViolation (errata 3.24),
raised by the codec.

Local Auth List Management / Reservation (M6, §3.5, §3.11, §5.1, §5.4, §5.10, §5.13, §5.15, errata 3.52):
SendLocalList / GetLocalListVersion / ClearCache / ReserveNow / CancelReservation become the domain commands of
the same name; the station decides every status.

Firmware Management (M7, §4.4, §4.5, §5.9, §5.19): UpdateFirmware / GetDiagnostics become the domain commands of
the same name; the station runs the update / upload (``runtime.maintenance``) and sends Firmware- /
DiagnosticsStatusNotification with :meth:`Adapter16.send_firmware_status` / :meth:`Adapter16.send_diagnostics_status`.
TriggerMessage for both is handed to the station like the other triggers.

Interpretations (not defined by the spec)
-----------------------------------------
- SendLocalList ``listVersion`` < 1 is CALLERROR PropertyConstraintViolation (errata 3.52: it "SHALL NOT be -1 or
  0"), like the other range checks the schema cannot express. ReserveNow ``connectorId`` outside 0..N is CALLERROR
  PropertyConstraintViolation.
- RemoteStartTransaction.chargingProfile must be a TxProfile without transactionId (§5.16.2: it "SHALL NOT be
  set"); anything else is answered Rejected without starting a transaction. The runtime also rejects a profile
  the profile store would refuse (first period not 0, ...).
- SetChargingProfile / ClearChargingProfile with a connectorId outside 0..N are CALLERROR
  PropertyConstraintViolation like the other connector-addressed requests; GetCompositeSchedule answers Rejected
  for an unknown connector or a duration < 1 (§5.7 "not able to report the requested schedule").
- GetCompositeSchedule.conf: ``scheduleStart`` = ``chargingSchedule.startSchedule`` = the time of the request,
  ``duration`` = the requested duration, limits rounded to 1 decimal (§7.13 at most one fraction digit),
  adjacent periods that become equal after rounding are merged. The runtime reports unconstrained intervals as
  the charger's hardware maximum, so the answer is Accepted even when no profile is installed (the hardware
  limit is a real limit; 1.6 has no encoding for "no limit").
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import profiles as cp
from ...rpc.errors import OCPP16_ERRORS, CallError
from .. import base as d
from . import models as m
from .codec import dump, parse
from .config_keys import KEYS

log = logging.getLogger(__name__)

_NO_ERROR = "NoError"

#: Domain stop reason -> 1.6 ``Reason`` of StopTransaction.req.
_REASON_16: Dict[d.StopReason, str] = {
    d.StopReason.LOCAL: "Local",
    d.StopReason.REMOTE: "Remote",
    d.StopReason.EV_DISCONNECTED: "EVDisconnected",
    d.StopReason.DE_AUTHORIZED: "DeAuthorized",
    d.StopReason.UNLOCK_COMMAND: "UnlockCommand",
    d.StopReason.SOFT_RESET: "SoftReset",
    d.StopReason.HARD_RESET: "HardReset",
    d.StopReason.OTHER: "Other",
}


def _fmt(value: float) -> str:
    v = float(value)
    if v.is_integer():
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _sampled(samples: Sequence[d.Sample], context: str) -> List[m.SampledValue]:
    return [m.SampledValue(value=_fmt(s.value), context=context, measurand=s.measurand, unit=s.unit,
                           phase=s.phase, location=s.location) for s in samples]


def _id_info(info: m.IdTagInfo) -> d.IdTokenInfo:
    return d.IdTokenInfo(status=d.AuthResult(info.status.value), expiry=_utc(info.expiry_date),
                         parent_id_token=info.parent_id_tag)


def _meter_values(evse_id: int, tx_id: Optional[str], ts: datetime, samples: Sequence[d.Sample],
                  context: str) -> m.MeterValuesRequest:
    return m.MeterValuesRequest(
        connector_id=evse_id, transaction_id=int(tx_id) if tx_id is not None else None,
        meter_value=[m.MeterValueNonEmpty(timestamp=ts, sampled_value=_sampled(samples, context))])


def _utc(t: Optional[datetime]) -> Optional[datetime]:
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _profile(p: m.ChargingProfile) -> cp.ChargingProfile:
    """1.6 ChargingProfile -> domain profile."""
    sch = p.charging_schedule
    return cp.ChargingProfile(
        profile_id=p.charging_profile_id,
        stack_level=p.stack_level,
        purpose=cp.ChargingProfilePurpose(p.charging_profile_purpose.value),
        kind=cp.ChargingProfileKind(p.charging_profile_kind.value),
        schedule=cp.ChargingSchedule(
            unit=cp.ChargingRateUnit(sch.charging_rate_unit.value),
            periods=tuple(cp.SchedulePeriod(x.start_period, x.limit, x.number_phases)
                          for x in sch.charging_schedule_period),
            duration=sch.duration,
            start_schedule=_utc(sch.start_schedule),
            min_charging_rate=sch.min_charging_rate),
        transaction_id=p.transaction_id,
        recurrency_kind=cp.RecurrencyKind(p.recurrency_kind.value) if p.recurrency_kind is not None else None,
        valid_from=_utc(p.valid_from),
        valid_to=_utc(p.valid_to))


def _schedule(res: d.GetCompositeResult) -> m.ChargingSchedule:
    """Domain composite schedule -> 1.6 ChargingSchedule (1-decimal limits, equal neighbours merged)."""
    sched = res.schedule
    assert sched is not None
    periods: List[m.ChargingSchedulePeriod] = []
    for x in sched.periods:
        if x.limit is None:
            raise ValueError("composite period without limit")  # the runtime fills these in
        period = m.ChargingSchedulePeriod(start_period=x.start_period, limit=round(x.limit, 1),
                                          number_phases=x.number_phases)
        if periods and (periods[-1].limit, periods[-1].number_phases) == (period.limit, period.number_phases):
            continue
        periods.append(period)
    return m.ChargingSchedule(charging_rate_unit=sched.unit.value, charging_schedule_period=periods,
                              duration=sched.duration, start_schedule=res.schedule_start)


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
    config_keys = KEYS

    # ------------------------------------------------------------------ inbound

    def _parse(self, action: str, payload: Any) -> Any:
        model, ignored = parse(action, payload, strict=self.strict)
        if ignored:
            log.info("%s: ignored unknown fields %s", action, ", ".join(ignored))
        return model

    def handlers(self, runtime: d.CommandSink) -> Dict[str, d.Handler]:
        table = {
            "RemoteStartTransaction": self._remote_start,
            "RemoteStopTransaction": self._remote_stop,
            "GetConfiguration": self._get_configuration,
            "ChangeConfiguration": self._change_configuration,
            "Reset": self._reset,
            "TriggerMessage": self._trigger,
            "UnlockConnector": self._unlock,
            "ChangeAvailability": self._change_availability,
            "DataTransfer": self._data_transfer,
            "SetChargingProfile": self._set_profile,
            "ClearChargingProfile": self._clear_profile,
            "GetCompositeSchedule": self._get_composite,
            "SendLocalList": self._send_local_list,
            "GetLocalListVersion": self._get_local_list_version,
            "ClearCache": self._clear_cache,
            "ReserveNow": self._reserve_now,
            "CancelReservation": self._cancel_reservation,
            "UpdateFirmware": self._update_firmware,
            "GetDiagnostics": self._get_diagnostics,
        }
        return {a: functools.partial(h, runtime) for a, h in table.items()}

    async def _remote_start(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("RemoteStartTransaction", payload)
        if req.connector_id is not None:
            # TODO(spec): connectorId 0 on RemoteStartTransaction is treated as out of range (must be > 0).
            _check_connector(req.connector_id, runtime, allow_zero=False)
        profile = None
        if req.charging_profile is not None:
            profile = _profile(req.charging_profile)
            if profile.purpose is not cp.ChargingProfilePurpose.TX or profile.transaction_id is not None:
                return dump(m.RemoteStartTransactionResponse(status="Rejected"))
        st = await runtime.handle(d.RequestStart(req.connector_id, req.id_tag, profile))
        return dump(m.RemoteStartTransactionResponse(status=_accepted(st)))

    async def _remote_stop(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("RemoteStopTransaction", payload)
        st = await runtime.handle(d.RequestStop(str(req.transaction_id)))
        return dump(m.RemoteStopTransactionResponse(status=_accepted(st)))

    async def _get_configuration(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("GetConfiguration", payload)
        res: d.GetConfigResult = await runtime.handle(d.GetConfig(tuple(req.key or ())))
        return dump(m.GetConfigurationResponse(
            configuration_key=[m.KeyValue(key=e.key, readonly=e.readonly, value=e.value) for e in res.known],
            unknown_key=list(res.unknown) or None))

    async def _change_configuration(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("ChangeConfiguration", payload)
        st: d.ConfigStatus = await runtime.handle(d.SetConfig(req.key, req.value))
        return dump(m.ChangeConfigurationResponse(status=st.value))

    async def _reset(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("Reset", payload)
        st = await runtime.handle(d.Reset(d.ResetKind(req.type.value)))
        return dump(m.ResetResponse(status=_accepted(st)))

    async def _trigger(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("TriggerMessage", payload)
        if req.connector_id is not None:
            # TODO(spec): connectorId 0 is accepted for TriggerMessage (charger-wide request).
            _check_connector(req.connector_id, runtime, allow_zero=True)
        what = d.TriggerWhat(req.requested_message.value)
        st = await runtime.handle(d.Trigger(what, req.connector_id))
        status = {d.CommandStatus.ACCEPTED: "Accepted", d.CommandStatus.REJECTED: "Rejected"}.get(
            st, "NotImplemented")
        return dump(m.TriggerMessageResponse(status=status))

    async def _unlock(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("UnlockConnector", payload)
        _check_connector(req.connector_id, runtime, allow_zero=False)
        st: d.UnlockStatus = await runtime.handle(d.Unlock(req.connector_id))
        return dump(m.UnlockConnectorResponse(status=st.value))

    async def _change_availability(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("ChangeAvailability", payload)
        _check_connector(req.connector_id, runtime, allow_zero=True)
        st: d.AvailabilityStatus = await runtime.handle(
            d.ChangeAvailability(req.connector_id, req.type.value == "Operative"))
        return dump(m.ChangeAvailabilityResponse(status=st.value))

    async def _data_transfer(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("DataTransfer", payload)
        res: d.DataTransferResult = await runtime.handle(d.DataTransfer(req.vendor_id, req.message_id, req.data))
        return dump(m.DataTransferResponse(status=res.status.value, data=res.data))

    async def _set_profile(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("SetChargingProfile", payload)
        _check_connector(req.connector_id, runtime, allow_zero=True)
        st = await runtime.handle(d.SetProfile(req.connector_id, _profile(req.cs_charging_profiles)))
        status = {d.CommandStatus.ACCEPTED: "Accepted", d.CommandStatus.NOT_SUPPORTED: "NotSupported"}.get(
            st, "Rejected")
        return dump(m.SetChargingProfileResponse(status=status))

    async def _clear_profile(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("ClearChargingProfile", payload)
        if req.connector_id is not None:
            _check_connector(req.connector_id, runtime, allow_zero=True)
        purpose = req.charging_profile_purpose
        st = await runtime.handle(d.ClearProfiles(
            req.id, req.connector_id, cp.ChargingProfilePurpose(purpose.value) if purpose is not None else None,
            req.stack_level))
        return dump(m.ClearChargingProfileResponse(
            status="Accepted" if st is d.CommandStatus.ACCEPTED else "Unknown"))

    async def _get_composite(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("GetCompositeSchedule", payload)
        if not 0 <= req.connector_id <= runtime.connector_count or req.duration < 1:
            return dump(m.GetCompositeScheduleResponse(status="Rejected"))
        unit = cp.ChargingRateUnit(req.charging_rate_unit.value) if req.charging_rate_unit is not None else None
        res: d.GetCompositeResult = await runtime.handle(d.GetComposite(req.connector_id, req.duration, unit))
        if res.status is not d.CommandStatus.ACCEPTED or res.schedule is None:
            return dump(m.GetCompositeScheduleResponse(status="Rejected"))
        return dump(m.GetCompositeScheduleResponse(
            status="Accepted", connector_id=res.evse_id, schedule_start=res.schedule_start,
            charging_schedule=_schedule(res)))

    async def _send_local_list(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("SendLocalList", payload)
        if req.list_version < 1:
            raise CallError(OCPP16_ERRORS.property_constraint_violation,
                            f"listVersion: {req.list_version} must be > 0", {"field": "listVersion"})
        entries = tuple((x.id_tag, _id_info(x.id_tag_info) if x.id_tag_info is not None else None)
                        for x in req.local_authorization_list or ())
        st: d.LocalListStatus = await runtime.handle(
            d.SendLocalList(req.list_version, req.update_type.value == "Full", entries))
        return dump(m.SendLocalListResponse(status=st.value))

    async def _get_local_list_version(self, runtime: d.CommandSink, payload: dict) -> dict:
        self._parse("GetLocalListVersion", payload)
        version: int = await runtime.handle(d.GetLocalListVersion())
        return dump(m.GetLocalListVersionResponse(list_version=version))

    async def _clear_cache(self, runtime: d.CommandSink, payload: dict) -> dict:
        self._parse("ClearCache", payload)
        st = await runtime.handle(d.ClearCache())
        return dump(m.ClearCacheResponse(status=_accepted(st)))

    async def _reserve_now(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("ReserveNow", payload)
        _check_connector(req.connector_id, runtime, allow_zero=True)
        st: d.ReservationStatus = await runtime.handle(d.ReserveNow(
            req.reservation_id, req.connector_id, req.id_tag, _utc(req.expiry_date), req.parent_id_tag))
        return dump(m.ReserveNowResponse(status=st.value))

    async def _cancel_reservation(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("CancelReservation", payload)
        st = await runtime.handle(d.CancelReservation(req.reservation_id))
        return dump(m.CancelReservationResponse(status=_accepted(st)))

    async def _update_firmware(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("UpdateFirmware", payload)
        await runtime.handle(d.UpdateFirmware(req.location, _utc(req.retrieve_date), req.retries, req.retry_interval))
        return dump(m.UpdateFirmwareResponse())

    async def _get_diagnostics(self, runtime: d.CommandSink, payload: dict) -> dict:
        req = self._parse("GetDiagnostics", payload)
        name: Optional[str] = await runtime.handle(d.GetDiagnostics(
            req.location, req.retries, req.retry_interval, _utc(req.start_time), _utc(req.stop_time)))
        return dump(m.GetDiagnosticsResponse(file_name=name))

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
        await self._send("MeterValues", _meter_values(ev.evse_id, ev.tx_id, ev.timestamp, ev.samples, ev.context))

    async def send_tx_ended(self, ev: d.TxEnded) -> Optional[d.IdTokenInfo]:
        data = ([m.MeterValue(timestamp=ev.timestamp, sampled_value=_sampled(ev.samples, "Transaction.End"))]
                if ev.samples else None)
        resp = await self._send("StopTransaction", m.StopTransactionRequest(
            transaction_id=int(ev.tx_id), timestamp=ev.timestamp, meter_stop=ev.meter_stop_wh,
            id_tag=ev.id_token, reason=_REASON_16[ev.reason] if ev.reason is not None else None,
            transaction_data=data))
        return _id_info(resp.id_tag_info) if resp.id_tag_info is not None else None

    async def send_meter_sample(self, ev: d.MeterSample) -> None:
        if not ev.samples:
            return
        await self._send("MeterValues", _meter_values(ev.evse_id, ev.tx_id, ev.timestamp, ev.samples, ev.context))

    async def send_data_transfer(self, cmd: d.DataTransfer) -> d.DataTransferResult:
        resp = await self._send("DataTransfer", m.DataTransferRequest(
            vendor_id=cmd.vendor_id, message_id=cmd.message_id, data=cmd.data))
        return d.DataTransferResult(d.DataTransferStatus(resp.status.value), resp.data)

    async def send_firmware_status(self, ev: d.FirmwareStatus) -> None:
        await self._send("FirmwareStatusNotification", m.FirmwareStatusNotificationRequest(status=ev.status.value))

    async def send_diagnostics_status(self, ev: d.LogStatus) -> None:
        await self._send("DiagnosticsStatusNotification", m.DiagnosticsStatusNotificationRequest(
            status=ev.status.value))
