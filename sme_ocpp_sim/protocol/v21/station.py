"""OCPP 2.1 station: :class:`~sme_ocpp_sim.protocol.v201.station.Station201` with the 2.1 models injected.

All 2.0.1 behaviour is inherited unchanged (2.1 = 2.0.1 + optional fields + new messages, Part 0 Ch.1). On top:

- Reset ``ImmediateAndResume`` (B13) is accepted and handled as ``Immediate``; resuming the transactions after the
  reboot is not simulated yet (2.1-M3 / B13).
- TriggerMessage ``CustomTrigger`` answers ``NotImplemented`` (no custom triggers are declared).
- Store-and-ack 2.1 messages (tier (c) of the 2.1 delta): the request is kept in memory and answered schema-valid.
  Nothing acts on it yet (no local cost calculation, no DER behaviour, no frequency response):

  - AFRRSignal -> ``Accepted`` (the last signal is kept).
  - NotifyAllowedEnergyTransfer -> ``Accepted`` for an ongoing transaction, else ``Rejected`` (kept per
    transaction).
  - SetDefaultTariff -> ``Accepted`` / ``DuplicateTariffId`` (same tariffId on another EVSE); evseId 0 = every EVSE.
    GetTariffs reads them back (``DefaultTariff`` assignments, or the transaction tariffs as ``DriverTariff``),
    ClearTariffs removes them (``NoTariff`` when nothing matches), ChangeTransactionTariff -> ``Accepted`` for an
    ongoing transaction, else ``TxNotFound``.
  - SetDERControl -> ``Accepted`` (kept by controlId; same controlId replaces); ClearDERControl -> ``Accepted`` /
    ``NotFound``; GetDERControl -> ``NotFound`` or ``Accepted`` followed by one ReportDERControl with the matching
    controls (R04). ``isSuperseded`` is always false: priorities are not evaluated.

Smart charging 2.1 and V2X basics (2.1-M2; rules in :mod:`.profiles` / :mod:`.composite`):

- SetChargingProfile: the 2.1 checks (:func:`.profiles.reject_reason_21`) run before the 2.0.1 ones. The power
  model follows :func:`.composite.setting_at`: the limit as in 2.0.1, plus the signed setpoint (negative =
  discharge, ``charging.power``) in a setpoint mode, setpoint 0 in Idle. While V2XChargingCtrlr.Enabled is false a
  negative setpoint is applied as 0 (no discharge). GetCompositeSchedule reports ``operationMode``,
  ``dischargeLimit`` and ``setpoint`` per period, capped at +/- the hardware maximum like the limit.
- operationMode change of a transaction (K01.FR.110): TransactionEvent ``Updated`` ``OperationModeChanged`` with
  ``transactionInfo.operationMode`` (checked every tick; a transaction starts as ChargingOnly).
- UpdateDynamicSchedule (K28): ``Accepted`` for a Dynamic profile (the values present replace those of its period,
  ``dynUpdateTime`` = now), ``Rejected`` ``InvalidProfile`` for another kind (K28.FR.11), ``Rejected`` ``NotFound``
  for an unknown id. PullDynamicScheduleUpdate: sent for a Dynamic profile with ``dynUpdateInterval`` > 0 at
  ``dynUpdateTime + dynUpdateInterval`` (then every interval until an update arrives), only while online; an
  ``Accepted`` answer with a ``scheduleUpdate`` is applied the same way.
- UsePriorityCharging (K21): ``Rejected`` ``TxNotFound`` (no such transaction), ``NoProfile`` ``NotFound`` (no
  PriorityCharging profile on its EVSE or evseId 0), else ``Accepted``; a change sends NotifyPriorityCharging
  after the answer. :meth:`Station21.set_priority_charging` is the local request (K22). Priority charging ends with
  the transaction. NotifyPriorityCharging is not queued while offline.
- maxOfflineDuration (Q11 / Q12): offline = from the end of a session to the next ``on_online``.
- Meter values: Power.Active.Export, Energy.Active.Export.Register and Current.Export are served when configured;
  once a transaction has discharged they are also added next to their Import counterparts in every reading, so
  the import and export registers are always reported together. Import values stay >= 0 while discharging.
- Device Model: V2XChargingCtrlr.Enabled (ReadWrite, true), SupportedEnergyTransferModes, SupportedOperationModes
  (ReadOnly; the YAML may narrow them), SmartChargingCtrlr.SupportedAdditionalPurposes / SupportsDynamicProfiles /
  SupportsLimitAtSoC (true) / SupportsEvseSleep (false).

Not modelled: frequency response (CentralFrequency follows its setpoint, LocalFrequency and LocalLoadBalancing
charge within the limits), SetpointPriority, setpointReactive and the _L2 / _L3 values (accepted, not applied),
evseSleep, ISO 15118-20 renegotiation after NotifyAllowedEnergyTransfer, the profile of a RequestStartTransaction
with 2.1 fields (converted with the 2.0.1 rules).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from ...charging import power
from .. import base as d
from ..station import StationHost
from ..v201 import device_model as dm
from ..v201.composite import convert
from ..v201.enums import (ChargingProfileStatusEnumType, ChargingRateUnitEnumType, GenericStatusEnumType,
                          TransactionEventEnumType)
from ..v201.profiles import REASON_INVALID, REASON_UNSUPPORTED_RATE_UNIT
from ..v201.station import (CURRENT, CURRENT_DECIMALS, ENERGY, LOCATION, POWER, POWER_DECIMALS,
                            REASON_INVALID_DURATION, REASON_UNKNOWN_EVSE, Station201, _Evse)
from . import models as m
from .adapter import Adapter21
from .composite import CompositeSchedule21, Setting, composite_schedule, setting_at
from .profiles import DYNAMIC, MODE, PRIORITY, ProfileStore21, charging_profile21, reject_reason_21

log = logging.getLogger(__name__)

V2X = "V2XChargingCtrlr"
V2X_ENABLED = (V2X, "Enabled")
V2X_TRANSFER_MODES = (V2X, "SupportedEnergyTransferModes")
V2X_OPERATION_MODES = (V2X, "SupportedOperationModes")
SC_ADDITIONAL_PURPOSES = (dm.SMART_CHARGING, "SupportedAdditionalPurposes")
#: The operation modes the simulator accepts (see the module docstring for what each does).
OPERATION_MODES = tuple(x.value for x in MODE)
ADDITIONAL_PURPOSES = ("PriorityCharging", "LocalGeneration")
_AC = {1: "AC_single_phase", 2: "AC_two_phase", 3: "AC_three_phase"}
ENERGY_TRANSFER_MODES = (*_AC.values(), "AC_BPT")
EXPORT_POWER, EXPORT_ENERGY, EXPORT_CURRENT = "Power.Active.Export", "Energy.Active.Export.Register", "Current.Export"
EXPORT_MEASURANDS = (EXPORT_ENERGY, EXPORT_POWER, EXPORT_CURRENT)
_COMPANION = {ENERGY: EXPORT_ENERGY, POWER: EXPORT_POWER, CURRENT: EXPORT_CURRENT}
REASON_NOT_FOUND = "NotFound"
REASON_TX_NOT_FOUND = "TxNotFound"
_MEASURAND_LISTS = (dm.TX_STARTED_MEASURANDS, dm.TX_UPDATED_MEASURANDS, dm.TX_ENDED_MEASURANDS,
                    dm.ALIGNED_MEASURANDS, dm.ALIGNED_TX_ENDED_MEASURANDS)

#: SetDERControlRequest field that holds the setting of a controlType (R04), by DERControlEnumType value.
_DER_FIELD = {
    "EnterService": "enter_service", "FixedPFAbsorb": "fixed_pf_absorb", "FixedPFInject": "fixed_pf_inject",
    "FixedVar": "fixed_var", "FreqDroop": "freq_droop", "Gradients": "gradient",
    "LimitMaxDischarge": "limit_max_discharge",
}

#: ReportDERControlRequest list item type per SetDERControlRequest field.
_DER_GET = {
    "curve": "DERCurveGetType", "enter_service": "EnterServiceGetType", "fixed_pf_absorb": "FixedPFGetType",
    "fixed_pf_inject": "FixedPFGetType", "fixed_var": "FixedVarGetType", "freq_droop": "FreqDroopGetType",
    "gradient": "GradientGetType", "limit_max_discharge": "LimitMaxDischargeGetType",
}


class Station21(Station201):
    models: Any = m
    adapter_cls: Any = Adapter21

    def __init__(self, host: StationHost, *, strict: bool = False) -> None:
        super().__init__(host, strict=strict)
        #: Store-and-ack state (see the module docstring); in memory, lost on restart.
        self.afrr_signal: Optional[m.AFRRSignalRequest] = None
        self.allowed_energy_transfer: Dict[str, List[str]] = {}
        self.default_tariffs: Dict[int, m.TariffType] = {}  # by EVSE id
        self.tx_tariffs: Dict[str, m.TariffType] = {}  # by transactionId
        self.der_controls: Dict[str, m.SetDERControlRequest] = {}  # by controlId
        #: Smart charging 2.1 / V2X state (2.1-M2).
        self.profiles: ProfileStore21 = ProfileStore21(self.cfg.connectors)
        self.priority: Dict[int, str] = {}  # EVSE id -> transactionId using priority charging (K21 / K22)
        self._modes: Dict[int, Any] = {}  # EVSE id -> operationMode last reported for its transaction
        self._pull_due: Dict[int, datetime] = {}  # Dynamic profile id -> next PullDynamicScheduleUpdate
        self._pulling: Set[int] = set()
        for name in _MEASURAND_LISTS:  # the export measurands are valid values in 2.1
            var = self.dm.lookup(*name)
            if var is not None and var.values_list is not None:
                var.values_list = tuple(dict.fromkeys((*var.values_list, *EXPORT_MEASURANDS)))
                var.max_limit = len(",".join(var.values_list))
        mm = self.models
        self._remote.update({
            mm.AFRRSignalRequest: self._afrr_signal,
            mm.NotifyAllowedEnergyTransferRequest: self._notify_allowed_energy_transfer,
            mm.SetDefaultTariffRequest: self._set_default_tariff,
            mm.GetTariffsRequest: self._get_tariffs,
            mm.ClearTariffsRequest: self._clear_tariffs,
            mm.ChangeTransactionTariffRequest: self._change_transaction_tariff,
            mm.SetDERControlRequest: self._set_der_control,
            mm.GetDERControlRequest: self._get_der_control,
            mm.ClearDERControlRequest: self._clear_der_control,
            mm.UpdateDynamicScheduleRequest: self._update_dynamic_schedule,
            mm.UsePriorityChargingRequest: self._use_priority_charging,
        })

    def _tx_ids(self) -> set:
        return {e.tx.tx_id for e in self.evses.values() if e.tx is not None}

    # ------------------------------------------------------------------ Q08 / Q01

    def _afrr_signal(self, req: m.AFRRSignalRequest) -> m.AFRRSignalResponse:
        self.afrr_signal = req
        return self.models.AFRRSignalResponse(status="Accepted")

    def _notify_allowed_energy_transfer(
            self, req: m.NotifyAllowedEnergyTransferRequest) -> m.NotifyAllowedEnergyTransferResponse:
        if req.transaction_id not in self._tx_ids():
            return self.models.NotifyAllowedEnergyTransferResponse(status="Rejected", status_info=self._info("TxNotFound"))
        self.allowed_energy_transfer[req.transaction_id] = [getattr(x, "value", x) for x in req.allowed_energy_transfer]
        return self.models.NotifyAllowedEnergyTransferResponse(status="Accepted")

    # ------------------------------------------------------------------ I07 - I11 tariffs

    def _set_default_tariff(self, req: m.SetDefaultTariffRequest) -> m.SetDefaultTariffResponse:
        evses = list(self.evses) if req.evse_id == 0 else [req.evse_id]
        if req.evse_id != 0 and req.evse_id not in self.evses:
            return self.models.SetDefaultTariffResponse(status="Rejected", status_info=self._info("UnknownEvse"))
        tid = req.tariff.tariff_id
        if any(t.tariff_id == tid for i, t in self.default_tariffs.items() if i not in evses):
            return self.models.SetDefaultTariffResponse(status="DuplicateTariffId")
        for i in evses:
            self.default_tariffs[i] = req.tariff
        return self.models.SetDefaultTariffResponse(status="Accepted")

    def _get_tariffs(self, req: m.GetTariffsRequest) -> m.GetTariffsResponse:
        by_id: Dict[str, Tuple[m.TariffType, List[int]]] = {}
        for i, t in sorted(self.default_tariffs.items()):
            if req.evse_id in (0, i):
                by_id.setdefault(t.tariff_id, (t, []))[1].append(i)
        out = [self.models.TariffAssignmentType(tariff_id=tid, tariff_kind="DefaultTariff", valid_from=t.valid_from,
                                                evse_ids=evses)
               for tid, (t, evses) in by_id.items()]
        out += [self.models.TariffAssignmentType(tariff_id=t.tariff_id, tariff_kind="DriverTariff")
                for t in self.tx_tariffs.values()]
        if not out:
            return self.models.GetTariffsResponse(status="NoTariff")
        return self.models.GetTariffsResponse(status="Accepted", tariff_assignments=out)

    def _clear_tariffs(self, req: m.ClearTariffsRequest) -> m.ClearTariffsResponse:
        def hit(i: int, t: m.TariffType) -> bool:
            return (req.evse_id in (None, 0, i)) and (req.tariff_ids is None or t.tariff_id in req.tariff_ids)
        cleared = sorted({t.tariff_id for i, t in self.default_tariffs.items() if hit(i, t)})
        self.default_tariffs = {i: t for i, t in self.default_tariffs.items() if not hit(i, t)}
        R = self.models.ClearTariffsResultType
        results = [R(tariff_id=tid, status="Accepted") for tid in cleared]
        results += [R(tariff_id=tid, status="NoTariff") for tid in (req.tariff_ids or ()) if tid not in cleared]
        return self.models.ClearTariffsResponse(clear_tariffs_result=results or [R(status="NoTariff")])

    def _change_transaction_tariff(
            self, req: m.ChangeTransactionTariffRequest) -> m.ChangeTransactionTariffResponse:
        if req.transaction_id not in self._tx_ids():
            return self.models.ChangeTransactionTariffResponse(status="TxNotFound")
        self.tx_tariffs[req.transaction_id] = req.tariff
        return self.models.ChangeTransactionTariffResponse(status="Accepted")

    # ------------------------------------------------------------------ R04 DER control

    def _der_matches(self, ctl: m.SetDERControlRequest, is_default: Optional[bool], control_type: Any,
                     control_id: Optional[str]) -> bool:
        return ((is_default is None or ctl.is_default == is_default)
                and (control_type is None or ctl.control_type == control_type)
                and (control_id is None or ctl.control_id == control_id))

    def _set_der_control(self, req: m.SetDERControlRequest) -> m.SetDERControlResponse:
        self.der_controls[req.control_id] = req
        return self.models.SetDERControlResponse(status="Accepted")

    def _clear_der_control(self, req: m.ClearDERControlRequest) -> m.ClearDERControlResponse:
        hits = [k for k, c in self.der_controls.items()
                if self._der_matches(c, req.is_default, req.control_type, req.control_id)]
        for k in hits:
            del self.der_controls[k]
        return self.models.ClearDERControlResponse(status="Accepted" if hits else "NotFound")

    def _get_der_control(self, req: m.GetDERControlRequest) -> m.GetDERControlResponse:
        hits = [c for c in self.der_controls.values()
                if self._der_matches(c, req.is_default, req.control_type, req.control_id)]
        if not hits:
            return self.models.GetDERControlResponse(status="NotFound")
        self.host.after_reply(functools.partial(self._send_der_report, req.request_id, hits))
        return self.models.GetDERControlResponse(status="Accepted")

    def _der_report(self, request_id: int, controls: List[m.SetDERControlRequest]) -> m.ReportDERControlRequest:
        lists: Dict[str, list] = {}
        for c in controls:
            ctype = getattr(c.control_type, "value", c.control_type)
            name = _DER_FIELD.get(ctype, "curve")
            setting = getattr(c, name)
            if setting is None:
                log.warning("ReportDERControl: control %s (%s) skipped, its %s setting is None", c.control_id, ctype,
                            name)
                continue
            get_cls = getattr(self.models, _DER_GET[name])
            entry: Dict[str, Any] = {"id": c.control_id, "isDefault": c.is_default, "isSuperseded": False,
                                     "curveType": ctype}
            keep = {f.alias for f in get_cls.model_fields.values()}
            entry = {k: v for k, v in entry.items() if k in keep}
            setting_key = next(f.alias for k, f in get_cls.model_fields.items()
                               if f.alias not in ("id", "isDefault", "isSuperseded", "curveType", "customData"))
            entry[setting_key] = setting.model_dump(mode="json", by_alias=True, exclude_none=True)
            lists.setdefault(_wire(name), []).append(entry)
        payload: Dict[str, Any] = {"requestId": request_id, **lists}
        return self.models.ReportDERControlRequest.model_validate(payload)

    async def _send_der_report(self, request_id: int, controls: List[m.SetDERControlRequest]) -> None:
        # Built here, after the GetDERControl reply went out: a model error must not turn that reply into a CALLERROR.
        try:
            report = self._der_report(request_id, controls)
        except Exception:  # noqa: BLE001 - the reply is already sent; only the follow-up report is lost
            log.exception("ReportDERControl for request %s could not be built", request_id)
            return
        await self.host.safe(self._adapter.send_report_der_control(report), "ReportDERControl")


    # ------------------------------------------------------------------ Device Model (V2XChargingCtrlr)

    def _extra_variables(self, cfg: Any) -> List[dm.Variable]:
        def var(name: Tuple[str, str], dt: Any, value: str, **kw: Any) -> dm.Variable:
            return dm.Variable(name[0], name[1], dt, {dm.AT.actual: value}, **kw)

        ro = {"mutability": dm.MU.read_only}
        transfer = (_AC.get(cfg.phases, _AC[1]), "AC_BPT")
        return [
            var(V2X_ENABLED, dm.DT.boolean, "true"),
            var(V2X_TRANSFER_MODES, dm.DT.member_list, ",".join(transfer), values_list=ENERGY_TRANSFER_MODES, **ro),
            var(V2X_OPERATION_MODES, dm.DT.member_list, ",".join(OPERATION_MODES), values_list=OPERATION_MODES, **ro),
            var(SC_ADDITIONAL_PURPOSES, dm.DT.member_list, ",".join(ADDITIONAL_PURPOSES),
                values_list=ADDITIONAL_PURPOSES, **ro),
            var((dm.SMART_CHARGING, "SupportsDynamicProfiles"), dm.DT.boolean, "true", derived=True, **ro),
            var((dm.SMART_CHARGING, "SupportsLimitAtSoC"), dm.DT.boolean, "true", derived=True, **ro),
            var((dm.SMART_CHARGING, "SupportsEvseSleep"), dm.DT.boolean, "false", derived=True, **ro),
        ]

    def _bidirectional(self) -> bool:
        return self.dm.bool_value(V2X_ENABLED)

    # ------------------------------------------------------------------ K01 / Q: profiles, setting, physics

    def _socs(self) -> Dict[int, float]:
        return {e.id: e.pw.soc_pct for e in self.evses.values() if e.pw is not None}

    def _setting(self, e: _Evse) -> Setting:
        if e.tx is not None and e.tx.deauthorized:
            return Setting(limit=0.0)
        return setting_at(self.profiles, e.id, self.host.clock(), transactions=self._transactions(),
                          number_phases=self._phases(e.id), voltage=self.supply.phase_voltage_v(),
                          priority=set(self.priority), soc=self._socs())

    def _limit_w(self, e: _Evse) -> Optional[float]:
        s = self._setting(e)
        return 0.0 if s.operation_mode is MODE.idle else s.limit

    def _advance(self, e: _Evse) -> None:
        now = self.host.monotonic()
        if e.pw is not None and not self.host.paused:
            dt = max(0.0, now - e.last_mono) if e.last_mono is not None else 0.0
            s = self._setting(e)
            setpoint = s.setpoint
            if setpoint is not None and setpoint < 0 and not self._bidirectional():
                setpoint = 0.0
            if setpoint is None:
                e.pw = self.host.step_power(e.pw, dt, s.limit, e.vehicle)
            else:
                e.pw = self.host.step_power(e.pw, dt, s.limit, e.vehicle, setpoint_w=setpoint)
            e.meter_wh = e.pw.energy_wh
        e.last_mono = now

    def tick(self) -> None:
        super().tick()
        for e in self.evses.values():
            if e.tx is None:
                continue
            mode = self._setting(e).operation_mode or MODE.charging_only
            if mode is not self._modes.get(e.id, MODE.charging_only):  # K01.FR.110
                self._modes[e.id] = mode
                self._queue_event(e, e.tx, TransactionEventEnumType.updated,
                                  self.models.TriggerReasonEnumTypeExt21.operation_mode_changed,
                                  self.models.TransactionType(transaction_id=e.tx.tx_id, operation_mode=mode))
        self._pull_dynamic(self.host.clock())

    def _end_tx(self, e: _Evse, *args: Any, **kw: Any) -> None:
        super()._end_tx(e, *args, **kw)
        self.priority.pop(e.id, None)
        self._modes.pop(e.id, None)

    def _samples(self, e: _Evse, measurands: Any) -> List[d.Sample]:
        out = super()._samples(e, measurands)
        r = power.meter_reading(e.pw, self._ev(e), self.supply) if e.pw is not None else None
        wanted = [x for x in measurands if x in EXPORT_MEASURANDS]
        if r is not None and (r.energy_active_export_register_wh > 0 or r.power_active_export_w > 0):
            wanted += [_COMPANION[x] for x in measurands if x in _COMPANION]
        extra: List[d.Sample] = []
        for name in dict.fromkeys(wanted):
            if name == EXPORT_ENERGY:
                extra.append(d.Sample(name, round(r.energy_active_export_register_wh) if r else 0, "Wh",
                                      location=LOCATION))
            elif name == EXPORT_POWER:
                extra.append(d.Sample(name, round(r.power_active_export_w, POWER_DECIMALS) if r else 0.0, "W",
                                      location=LOCATION))
            else:
                extra.append(d.Sample(name, round(r.current_export_a, CURRENT_DECIMALS) if r else 0.0, "A",
                                      location=LOCATION))
        return out + (self._faults(e).noisy(extra) if extra else [])

    def _set_charging_profile(self, req: m.SetChargingProfileRequest) -> m.SetChargingProfileResponse:
        self.settle()
        profile, wire = charging_profile21(req.charging_profile, self.host.clock())
        reason = reject_reason_21(profile, modes=self.dm.list_value(V2X_OPERATION_MODES),
                                  purposes=self.dm.list_value(SC_ADDITIONAL_PURPOSES),
                                  bidirectional=self._bidirectional())
        if reason is None:
            reason = self._install(req.evse_id, profile, wire)
        if reason is not None:
            return self.models.SetChargingProfileResponse(status=ChargingProfileStatusEnumType.rejected,
                                                          status_info=self._info(reason))
        self._pull_due.pop(profile.profile_id, None)
        self.host.after_reply(self.tick)
        return self.models.SetChargingProfileResponse(status=ChargingProfileStatusEnumType.accepted)

    # ------------------------------------------------------------------ K08 composite schedule (2.1 fields)

    def _composite(self, evse_id: int, duration: int,
                   unit: Optional[ChargingRateUnitEnumType]) -> d.GetCompositeResult:
        if not 0 <= evse_id <= self.connector_count or duration < 1:
            return d.GetCompositeResult(d.CommandStatus.REJECTED)
        now = self.host.clock()
        phases, volts = self._phases(evse_id), self.supply.phase_voltage_v()
        sched = composite_schedule(self.profiles, evse_id, now, duration, unit, transactions=self._transactions(),
                                   number_phases=phases, voltage=volts, priority=set(self.priority),
                                   soc=self._socs())
        max_w = self.cfg.max_power_w * (self.connector_count if evse_id == 0 else 1)
        W = ChargingRateUnitEnumType.w

        def cap(p: Any) -> Any:
            # The hardware maximum caps every value and stands for "no limit" (as 2.0.1); Idle has no values.
            if p.operation_mode is MODE.idle:
                return p
            n = p.number_phases or phases
            hw = max_w if sched.unit is W else convert(max_w, W, ChargingRateUnitEnumType.a, n, volts)
            return replace(p, limit=hw if p.limit is None else min(p.limit, hw),
                           discharge_limit=None if p.discharge_limit is None else max(p.discharge_limit, -hw),
                           setpoint=None if p.setpoint is None else max(-hw, min(p.setpoint, hw)),
                           number_phases=None if sched.unit is W else n)

        return d.GetCompositeResult(d.CommandStatus.ACCEPTED, evse_id, now,
                                    replace(sched, periods=tuple(cap(p) for p in sched.periods)))

    def _get_composite_schedule(self, req: m.GetCompositeScheduleRequest) -> m.GetCompositeScheduleResponse:
        R = self.models.GetCompositeScheduleResponse
        unit = req.charging_rate_unit
        if unit is not None and unit.value not in self.dm.list_value(dm.RATE_UNIT):  # K08.FR.07
            return R(status=GenericStatusEnumType.rejected, status_info=self._info(REASON_UNSUPPORTED_RATE_UNIT))
        res = self._composite(req.evse_id, req.duration, unit)
        if res.schedule is None:
            code = REASON_UNKNOWN_EVSE if not 0 <= req.evse_id <= self.connector_count else REASON_INVALID_DURATION
            return R(status=GenericStatusEnumType.rejected, status_info=self._info(code))
        return R(status=GenericStatusEnumType.accepted, schedule=composite_schedule_type21(res.schedule, self.models))

    # ------------------------------------------------------------------ K28 dynamic profiles

    def _apply_update(self, profile_id: int, update: m.ChargingScheduleUpdateType) -> None:
        """K28.FR.06 / FR.08 / FR.09; ``KeyError`` / ``ValueError`` from :meth:`ProfileStore21.update_dynamic`."""
        self.settle()
        now = self.host.clock()
        ip = self.profiles.update_dynamic(profile_id, update, now)
        interval = getattr(ip.profile, "dyn_update_interval", None)
        if interval:
            self._pull_due[profile_id] = now + timedelta(seconds=interval)

    def _update_dynamic_schedule(self, req: m.UpdateDynamicScheduleRequest) -> m.UpdateDynamicScheduleResponse:
        R = self.models.UpdateDynamicScheduleResponse
        try:
            self._apply_update(req.charging_profile_id, req.schedule_update)
        except KeyError:
            return R(status=ChargingProfileStatusEnumType.rejected, status_info=self._info(REASON_NOT_FOUND))
        except ValueError:  # K28.FR.11
            return R(status=ChargingProfileStatusEnumType.rejected, status_info=self._info(REASON_INVALID))
        self.host.after_reply(self.tick)
        return R(status=ChargingProfileStatusEnumType.accepted)

    def _pull_dynamic(self, now: datetime) -> None:
        """K28.FR.10: a PullDynamicScheduleUpdate per due Dynamic profile with a dynUpdateInterval."""
        if not self.host.online:
            return
        for ip in self.profiles.profiles():
            p = ip.profile
            interval = getattr(p, "dyn_update_interval", None)
            if p.kind is not DYNAMIC or not interval or interval <= 0 or p.profile_id in self._pulling:
                continue
            due = self._pull_due.get(p.profile_id) or p.dyn_update_time + timedelta(seconds=interval)
            if now < due:
                continue
            self._pull_due[p.profile_id] = now + timedelta(seconds=interval)
            self._pulling.add(p.profile_id)
            if self.host.spawn(self._pull(p.profile_id)) is None:
                self._pulling.discard(p.profile_id)

    async def _pull(self, profile_id: int) -> None:
        try:
            res = await self.host.safe(self._adapter.send_pull_dynamic_schedule_update(profile_id),
                                       "PullDynamicScheduleUpdate")
        finally:
            self._pulling.discard(profile_id)
        if res is None or res.status is not ChargingProfileStatusEnumType.accepted or res.schedule_update is None:
            return
        try:
            self._apply_update(profile_id, res.schedule_update)
        except (KeyError, ValueError):  # replaced or cleared meanwhile
            return
        self.tick()

    # ------------------------------------------------------------------ K21 / K22 priority charging

    def _has_priority_profile(self, e: _Evse) -> bool:
        return any(ip.profile.purpose is PRIORITY for i in (0, e.id) for ip in self.profiles.profiles(i))

    def _set_priority(self, e: _Evse, on: bool) -> bool:
        """Switch priority charging of e's transaction; ``True`` when it changed (the caller sends the notice)."""
        assert e.tx is not None
        if (e.id in self.priority) == on:
            return False
        self.settle()
        if on:
            self.priority[e.id] = e.tx.tx_id
        else:
            del self.priority[e.id]
        return True

    def _notify_priority(self, tx_id: str, on: bool) -> None:
        self._notify(lambda: self._adapter.send_notify_priority_charging(tx_id, on), "NotifyPriorityCharging")

    def _use_priority_charging(self, req: m.UsePriorityChargingRequest) -> m.UsePriorityChargingResponse:
        R = self.models.UsePriorityChargingResponse
        e = next((x for x in self.evses.values() if x.tx is not None and x.tx.tx_id == req.transaction_id), None)
        if e is None:  # K21.FR.02
            return R(status="Rejected", status_info=self._info(REASON_TX_NOT_FOUND))
        if req.activate and not self._has_priority_profile(e):  # K21.FR.01
            return R(status="NoProfile", status_info=self._info(REASON_NOT_FOUND))
        if self._set_priority(e, req.activate):  # K21.FR.04 / FR.05 / FR.07 / FR.08
            self.host.after_reply(functools.partial(self._notify_priority, req.transaction_id, req.activate))
            self.host.after_reply(self.tick)
        return R(status="Accepted")

    def set_priority_charging(self, connector: int, on: bool) -> bool:
        """Local priority-charging request (K22) for the transaction of EVSE ``connector``: ``False`` when it is
        refused (no PriorityCharging profile) or nothing changed. ``ValueError`` without a transaction."""
        e = self._evse(connector)
        if e.tx is None:
            raise ValueError(f"EVSE {connector} has no transaction")
        if on and not self._has_priority_profile(e):
            return False
        if not self._set_priority(e, on):
            return False
        self._notify_priority(e.tx.tx_id, on)
        self.tick()
        return True

    # ------------------------------------------------------------------ Q11 / Q12 offline validity

    def on_session_ended(self) -> None:
        super().on_session_ended()
        self.profiles.go_offline(self.host.clock())

    async def on_online(self) -> None:
        dropped = self.profiles.go_online(self.host.clock())
        if dropped:
            log.info("%s: charging profiles %s dropped (invalidAfterOfflineDuration)", self.cfg.id,
                     ",".join(map(str, dropped)))
        await super().on_online()

    def snapshot(self) -> Dict[str, Any]:
        snap = super().snapshot()
        for c in snap["connectors"]:
            e = self.evses[c["id"]]
            c["energy_export_wh"] = round(e.pw.energy_export_wh, 3) if e.pw is not None else 0.0
            c["operation_mode"] = (self._modes.get(e.id) or MODE.charging_only).value if e.tx is not None else None
            c["priority_charging"] = e.id in self.priority
        return snap


def composite_schedule_type21(sched: CompositeSchedule21, models: Any = m) -> m.CompositeScheduleType:
    """The 2.1 wire model of a composite schedule: absent values (Idle, no setpoint ...) are left out, and so is
    operationMode ChargingOnly (its default), so a 2.0.1-style schedule stays as 2.0.1 reports it."""
    P = models.ChargingSchedulePeriodType
    only = MODE.charging_only
    return models.CompositeScheduleType(
        evse_id=sched.evse_id, duration=sched.duration, schedule_start=sched.start, charging_rate_unit=sched.unit,
        charging_schedule_period=[P(start_period=p.start_period, limit=p.limit, number_phases=p.number_phases,
                                    discharge_limit=p.discharge_limit, setpoint=p.setpoint,
                                    operation_mode=None if p.operation_mode is only else p.operation_mode)
                                  for p in sched.periods])


def _wire(attr: str) -> str:

    return m.ReportDERControlRequest.model_fields[attr].alias or attr
