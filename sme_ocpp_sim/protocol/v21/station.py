"""OCPP 2.1 station: :class:`~sme_ocpp_sim.protocol.v201.station.Station201` with the 2.1 models injected.

All 2.0.1 behaviour is inherited unchanged (2.1 = 2.0.1 + optional fields + new messages, Part 0 Ch.1). On top:

- Reset ``ImmediateAndResume`` (B13) is accepted and handled as ``Immediate``; resuming the transactions after the
  reboot is not simulated (E17).
- TriggerMessage ``CustomTrigger`` answers ``NotImplemented`` (no custom triggers are declared).
- Store-and-ack 2.1 messages (tier (c) of the 2.1 delta): the request is kept in memory and answered schema-valid.
  Nothing acts on it yet (no local cost calculation, no DER behaviour, no frequency response):

  - AFRRSignal -> ``Accepted`` (the last signal is kept).
  - NotifyAllowedEnergyTransfer -> ``Accepted`` for an ongoing transaction, else ``Rejected`` (kept per
    transaction).
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

Transaction limits, tariffs and cost, availability (2.1-M3; calculation in :mod:`.tariff`):

- Tariffs (I07 - I11). With TariffCostCtrlr.Enabled[Tariff] false every tariff message is CALLERROR
  ``NotSupported`` (I07.FR.01 / I11.FR.01). SetDefaultTariff: ``Rejected`` ``UnknownEVSE``, ``TooManyElements``
  (more prices than TariffCostCtrlr.MaxElements), ``ConditionNotSupported`` (conditions; ConditionsSupported is
  false), ``DuplicateTariffId`` (the tariffId is installed on an EVSE not addressed), else ``Accepted``; evseId 0 =
  every EVSE (I07.FR.12). An EVSE keeps one tariff without validFrom plus the ones with validFrom; the one in force
  is the latest validFrom already reached, else the one without (I07.FR.19 / FR.20). GetTariffs (``DefaultTariff``
  per EVSE, ``DriverTariff`` for the transactions), ClearTariffs (``NoTariff`` when nothing matches).
  ChangeTransactionTariff: ``TxNotFound``, ``TooManyElements``, ``ConditionNotSupported``, ``NoCurrencyChange``
  (I11.FR.05), else ``Accepted`` and a TransactionEvent ``Updated`` ``TariffChanged`` with ``tariffId`` (I11.FR.07).
- Driver tariff (I08): the ``tariff`` of an Accepted AuthorizeResponse applies to the next transaction of that
  idToken (implicitly accepted, I08.FR.22); one that cannot be processed is ignored (HandleFailedTariff
  ``UseDefault`` behaviour, TariffCostCtrlr.Problem is not modelled). A transaction otherwise uses the default
  tariff of its EVSE in force when it starts; its ``tariffId`` is sent in the Started event.
- Cost (I12, plan Q19): with Enabled[Tariff] and Enabled[Cost] true the Ended event of a transaction with a tariff
  carries ``costDetails`` (rough local calculation, see :mod:`.tariff`). Running cost updates are not sent
  (Enabled[RunningCost] false).
- Transaction limits (E16 / F07): a ``transactionLimit`` in a TransactionEventResponse sets the limits named in
  TxCtrlr.SupportedLimits (all four by default); they are confirmed once by an ``Updated`` ``LimitSet`` event with
  the supported values in ``transactionInfo.transactionLimit`` (E16.FR.03 / FR.13, F07.FR.03; not for the answer to
  an ``offline`` event, E16.FR.18). :meth:`Station21.set_transaction_limit` is the local (driver) limit (E16.FR.01;
  never above a CSMS limit, E16.FR.04). Checked every tick: a reached limit suspends the energy transfer with an
  ``Updated`` event (``SuspendedEVSE``, triggerReason ``CostLimitReached`` / ``EnergyLimitReached`` /
  ``TimeLimitReached`` / ``SoCLimitReached``, E16.FR.05), or ends the transaction when TxStopPoint contains
  ``EnergyTransfer`` (E16.FR.06). A raised limit resumes it (E16.FR.14). The cost limit uses the local cost with a
  tariff (E16.FR.16), else the last ``totalCost`` of a TransactionEventResponse (E16.FR.15).
- Availability (G01): ``availability_report`` (``status`` by default) reports a connector status change as
  StatusNotification, NotifyEvent (Connector.AvailabilityState, trigger ``Delta``) or both. TriggerMessage
  StatusNotification still sends StatusNotification.

Not modelled: frequency response (CentralFrequency follows its setpoint, LocalFrequency and LocalLoadBalancing
charge within the limits), SetpointPriority, setpointReactive and the _L2 / _L3 values (accepted, not applied),
evseSleep, ISO 15118-20 renegotiation after NotifyAllowedEnergyTransfer. A RequestStartTransaction profile is
converted and checked with the 2.1 rules (as SetChargingProfile), then with the 2.0.1 RequestStart rules.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from ...charging import power
from ...rpc import CallError
from .. import base as d
from ..station import StationHost
from ..v201 import device_model as dm
from ..v201.adapter import connector_status
from ..v201.composite import convert
from ..v201.enums import (ChargingProfileStatusEnumType, ChargingRateUnitEnumType, GenericStatusEnumType,
                          ReasonEnumType, TransactionEventEnumType, TriggerReasonEnumType)
from ..v201.profiles import REASON_INVALID, REASON_UNSUPPORTED_RATE_UNIT
from ..v201.station import (CURRENT, CURRENT_DECIMALS, ENERGY, LOCATION, POWER, POWER_DECIMALS,
                            REASON_INVALID_DURATION, REASON_UNKNOWN_EVSE, Station201, _Evse, _Tx)
from . import models as m
from .adapter import Adapter21
from .composite import CompositeSchedule21, Setting, composite_schedule, setting_at
from .profiles import (DYNAMIC, MODE, PRIORITY, ProfileStore21, UpdateRejected, charging_profile21,
                       reject_reason_21)
from .tariff import (LIMIT_FIELDS, CostMeter, has_conditions, limits_of, reached, tariff_key, too_many_elements,
                     with_utc)

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
TARIFF = "TariffCostCtrlr"
TARIFF_ENABLED = (TARIFF, "Enabled")  # instances Tariff / Cost / RunningCost
TARIFF_MAX_ELEMENTS = (TARIFF, "MaxElements")  # instance Tariff
TARIFF_CURRENCY = (TARIFF, "Currency")
TX_SUPPORTED_LIMITS = (dm.TX, "SupportedLimits")
TX_STOP_POINT = (dm.TX, "TxStopPoint")
SUPPORTED_LIMITS = tuple(LIMIT_FIELDS.values())
DEFAULT_MAX_ELEMENTS = 10
#: Reached limit -> (triggerReason, stoppedReason when the transaction ends, E16.FR.05 / FR.06).
_LIMIT_REACHED = {
    "max_cost": (m.TriggerReasonEnumTypeExt21.cost_limit_reached, ReasonEnumType.other),
    "max_energy": (TriggerReasonEnumType.energy_limit_reached, ReasonEnumType.energy_limit_reached),
    "max_time": (TriggerReasonEnumType.time_limit_reached, ReasonEnumType.time_limit_reached),
    "max_so_c": (m.TriggerReasonEnumTypeExt21.so_c_limit_reached, ReasonEnumType.soc_limit_reached),
}
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
        self.default_tariffs: Dict[int, List[m.TariffType]] = {}  # by EVSE id (several with validFrom, I07.FR.20)
        self.tx_tariffs: Dict[str, m.TariffType] = {}  # driver-specific tariffs by transactionId (I08 / I11)
        #: Transaction limits (E16) and cost (I12) by transactionId.
        self.tx_limits: Dict[str, Dict[str, float]] = {}
        #: ``tx_limits`` is the effective limit: per key the lower of the local (driver) and the CSMS limit (E16.FR.04).
        self._local_limits: Dict[str, Dict[str, float]] = {}
        self._csms_limits: Dict[str, Dict[str, float]] = {}
        #: transactionId -> the limit (``LIMIT_FIELDS`` key) that suspended it. Lifecycle: set by ``_check_limit``
        #: (E16.FR.05, the EVSE is held at 0 W) -> cleared by ``_set_limits`` once the raised limit is no longer
        #: reached (E16.FR.14) -> the next tick resumes the charge; dropped with the transaction.
        self._limit_reached: Dict[str, str] = {}
        #: transactionId -> [simulated seconds since the transaction started, monotonic time of the last update].
        self._tx_time: Dict[str, List[float]] = {}
        self._costs: Dict[str, CostMeter] = {}
        #: transactionInfo fields sent once in the next TransactionEvent of a transaction (I08.FR.22).
        self._tx_info_once: Dict[str, Dict[str, Any]] = {}
        self._event_id = 0  # NotifyEvent eventId
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

    def _tariffs_enabled(self) -> bool:
        return self.dm.bool_value(TARIFF_ENABLED, "Tariff")

    def _require_tariffs(self, action: str) -> None:
        if not self._tariffs_enabled():  # I07.FR.01 / I11.FR.01 (GetTariffs / ClearTariffs alike)
            raise CallError(self._adapter.error_codes.not_supported,
                            f"{action}: TariffCostCtrlr.Enabled[Tariff] is false")

    def _tariff_problem(self, tariff: m.TariffType) -> Optional[str]:
        """The status for a tariff the station cannot process (I07.FR.02 / FR.03, I11.FR.02 / FR.03)."""
        if too_many_elements(tariff, self.dm.int_value(TARIFF_MAX_ELEMENTS, "Tariff")):
            return "TooManyElements"
        if has_conditions(tariff):  # TariffCostCtrlr.ConditionsSupported is false
            return "ConditionNotSupported"
        return None

    def default_tariff(self, evse_id: int, now: Optional[datetime] = None) -> Optional[m.TariffType]:
        """The default tariff in force on an EVSE (I07.FR.19 / FR.20)."""
        now = now or self.host.clock()
        tariffs = self.default_tariffs.get(evse_id, [])
        dated = [t for t in tariffs if t.valid_from is not None and t.valid_from <= now]
        if dated:
            return max(dated, key=lambda t: t.valid_from)
        return next((t for t in tariffs if t.valid_from is None), None)

    def _set_default_tariff(self, req: m.SetDefaultTariffRequest) -> m.SetDefaultTariffResponse:
        self._require_tariffs("SetDefaultTariff")
        R = self.models.SetDefaultTariffResponse
        req = req.model_copy(update={"tariff": with_utc(req.tariff)})
        if req.evse_id != 0 and req.evse_id not in self.evses:  # I07.FR.06
            return R(status="Rejected", status_info=self._info("UnknownEVSE"))
        problem = self._tariff_problem(req.tariff)
        if problem is not None:
            return R(status=problem)
        evses = list(self.evses) if req.evse_id == 0 else [req.evse_id]
        tid = req.tariff.tariff_id
        if any(t.tariff_id == tid for i, ts in self.default_tariffs.items() if i not in evses for t in ts):
            return R(status="DuplicateTariffId")  # I07.FR.04
        for i in evses:  # the same id is replaced; one without validFrom replaces the other one without
            kept = [t for t in self.default_tariffs.get(i, []) if t.tariff_id != tid
                    and not (req.tariff.valid_from is None and t.valid_from is None)]
            self.default_tariffs[i] = kept + [req.tariff]
        return R(status="Accepted")

    def _get_tariffs(self, req: m.GetTariffsRequest) -> m.GetTariffsResponse:
        self._require_tariffs("GetTariffs")
        by_id: Dict[str, Tuple[m.TariffType, List[int]]] = {}
        for i, ts in sorted(self.default_tariffs.items()):
            if req.evse_id in (0, i):
                for t in ts:
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
        self._require_tariffs("ClearTariffs")

        def hit(i: int, t: m.TariffType) -> bool:
            return (req.evse_id in (None, 0, i)) and (req.tariff_ids is None or t.tariff_id in req.tariff_ids)
        cleared = sorted({t.tariff_id for i, ts in self.default_tariffs.items() for t in ts if hit(i, t)})
        self.default_tariffs = {i: [t for t in ts if not hit(i, t)] for i, ts in self.default_tariffs.items()}
        R = self.models.ClearTariffsResultType
        results = [R(tariff_id=tid, status="Accepted") for tid in cleared]
        results += [R(tariff_id=tid, status="NoTariff") for tid in (req.tariff_ids or ()) if tid not in cleared]
        return self.models.ClearTariffsResponse(clear_tariffs_result=results or [R(status="NoTariff")])

    def _change_transaction_tariff(
            self, req: m.ChangeTransactionTariffRequest) -> m.ChangeTransactionTariffResponse:
        self._require_tariffs("ChangeTransactionTariff")
        R = self.models.ChangeTransactionTariffResponse
        e = next((x for x in self.evses.values() if x.tx is not None and x.tx.tx_id == req.transaction_id), None)
        if e is None or e.tx is None:  # I11.FR.04
            return R(status="TxNotFound")
        req = req.model_copy(update={"tariff": with_utc(req.tariff)})
        problem = self._tariff_problem(req.tariff)
        if problem is not None:
            return R(status=problem)
        cost = self._costs.get(e.tx.tx_id)
        if cost is not None and cost.tariff is not None and cost.tariff.currency != req.tariff.currency:
            return R(status="NoCurrencyChange")  # I11.FR.05
        self.tx_tariffs[req.transaction_id] = req.tariff
        self.host.after_reply(functools.partial(self._tariff_changed, e, e.tx, req.tariff))
        return R(status="Accepted")

    def _tariff_changed(self, e: _Evse, tx: _Tx, tariff: m.TariffType) -> None:
        """I11.FR.07 / FR.08: the new tariff applies from now on, reported with TariffChanged."""
        if e.tx is not tx:
            return
        self.settle()
        self._costs.setdefault(tx.tx_id, CostMeter(None)).switch(tariff, self._tx_energy(e))
        self._queue_event(e, tx, TransactionEventEnumType.updated,
                          self.models.TriggerReasonEnumTypeExt21.tariff_changed,
                          self.models.TransactionType(transaction_id=tx.tx_id, tariff_id=tariff.tariff_id))

    def _start_tariff(self, e: _Evse, tx: _Tx) -> Optional[m.TariffType]:
        """The tariff of a new transaction: the driver tariff of its idToken (I08.FR.04 / FR.22), else the default
        tariff of its EVSE (I07.FR.14)."""
        if not self._tariffs_enabled():
            return None
        driver = self._adapter.driver_tariffs.pop(tariff_key(tx.id_token, tx.token_type), None)
        if driver is not None:
            problem = self._tariff_problem(driver)
            if problem is None:
                self.tx_tariffs[tx.tx_id] = driver
                return driver
            log.warning("%s: driver tariff %s of %s ignored (%s), the default tariff applies", self.cfg.id,
                        driver.tariff_id, tx.id_token, problem)
        return self.default_tariff(e.id)

    # ------------------------------------------------------------------ E16 / F07 transaction limits

    @staticmethod
    def _tx_energy(e: _Evse) -> float:
        return max(0.0, e.meter_wh - e.tx.meter_start_wh) if e.tx is not None else 0.0

    def _supported_limits(self) -> Tuple[str, ...]:
        return self.dm.list_value(TX_SUPPORTED_LIMITS)

    def _local_cost(self) -> bool:
        return self._tariffs_enabled() and self.dm.bool_value(TARIFF_ENABLED, "Cost")

    def _set_limits(self, e: _Evse, tx: _Tx, *, local: Optional[Dict[str, float]] = None,
                    csms: Optional[Dict[str, float]] = None, confirm: bool) -> None:
        """Merge local / CSMS limits into the transaction's, confirm the effective ones with LimitSet (E16.FR.01 /
        FR.03), check them again. The effective limit is the lower of the two per key (E16.FR.04)."""
        if not local and not csms:
            return
        self.settle()
        mine, theirs = self._local_limits.setdefault(tx.tx_id, {}), self._csms_limits.setdefault(tx.tx_id, {})
        mine.update(local or {})
        theirs.update(csms or {})
        limits = self.tx_limits[tx.tx_id] = {k: min(x[k] for x in (mine, theirs) if k in x)
                                             for k in {**mine, **theirs}}
        if "max_cost" in limits:  # the cost limit needs a meter even when the transaction has no tariff
            self._costs.setdefault(tx.tx_id, CostMeter(None))
        if confirm:
            self._queue_event(e, tx, TransactionEventEnumType.updated,
                              self.models.TriggerReasonEnumTypeExt21.limit_set,
                              self.models.TransactionType(transaction_id=tx.tx_id,
                                                          transaction_limit=self.models.TransactionLimitType(**limits)))
        if tx.tx_id in self._limit_reached and self._reached(e) is None:  # E16.FR.14: resumes with the next tick
            del self._limit_reached[tx.tx_id]
        self._check_limit(e)  # E16.FR.10

    def set_transaction_limit(self, connector: int, **limits: Optional[float]) -> Dict[str, float]:
        """Local (EV driver) transaction limits of EVSE ``connector``: keywords ``max_energy`` (Wh), ``max_time``
        (s), ``max_cost``, ``max_so_c`` (%); ``None`` and unsupported ones are ignored (E16.FR.13). A CSMS limit is
        never exceeded, nor replaced by a later one (E16.FR.04). Returns the limits in force; ``ValueError`` without a transaction."""
        e = self._evse(connector)
        if e.tx is None:
            raise ValueError(f"EVSE {connector} has no transaction")
        unknown = set(limits) - set(LIMIT_FIELDS)
        if unknown:
            raise ValueError(f"unknown transaction limits: {', '.join(sorted(unknown))}")
        supported = set(self._supported_limits())
        values = {k: v for k, v in limits.items() if v is not None and LIMIT_FIELDS[k] in supported}
        self._set_limits(e, e.tx, local=values, confirm=True)
        return dict(self.tx_limits.get(e.tx.tx_id, {}))

    def _tx_elapsed(self, e: _Evse) -> float:
        """Simulated seconds since the transaction of ``e`` started: wall time x ``time_scale``, frozen while the
        simulation is paused (``maxTime``, E16.FR.09). Brought up to now on every call."""
        assert e.tx is not None
        now = self.host.monotonic()
        t = self._tx_time.setdefault(e.tx.tx_id, [0.0, now])
        if not self.host.paused:
            t[0] += max(0.0, now - t[1]) * self.host.time_scale
        t[1] = now
        return t[0]

    def _advance(self, e: _Evse) -> None:
        if e.tx is not None:  # also while not charging: maxTime counts from the start of the transaction
            self._tx_elapsed(e)
        super()._advance(e)

    def _reached(self, e: _Evse) -> Optional[str]:
        tx = e.tx
        if tx is None or not self.tx_limits.get(tx.tx_id):
            return None
        cost = self._costs.get(tx.tx_id)
        energy = self._tx_energy(e)
        return reached(self.tx_limits[tx.tx_id], energy_wh=energy, elapsed_s=self._tx_elapsed(e),
                       soc_pct=e.pw.soc_pct if e.pw is not None else None,
                       cost=cost.cost(energy, self._local_cost()) if cost is not None else None)

    def _check_limit(self, e: _Evse) -> None:
        """E16.FR.05 / FR.06: act on a newly reached limit."""
        tx = e.tx
        if tx is None or tx.tx_id in self._limit_reached:
            return
        which = self._reached(e)
        if which is None:
            return
        trigger, reason = _LIMIT_REACHED[which]
        log.info("%s: EVSE %d transaction %s reached %s", self.cfg.id, e.id, tx.tx_id, LIMIT_FIELDS[which])
        if "EnergyTransfer" in self.dm.list_value(TX_STOP_POINT):
            self._end_tx(e, trigger, reason)
            return
        self._advance(e)  # up to now at the old setting
        self._limit_reached[tx.tx_id] = which
        self._advance(e)  # zero-length step at 0 W: SuspendedEVSE
        tx.charging_state = self._charging_state(e)
        self._queue_event(e, tx, TransactionEventEnumType.updated, trigger,
                          self.models.TransactionType(transaction_id=tx.tx_id, charging_state=tx.charging_state))
        self._refresh(e)

    def _on_tx_answer(self, item: Any, res: m.TransactionEventResponse) -> None:
        super()._on_tx_answer(item, res)
        e, tx, req = item.evse, item.tx, item.req
        if e is None or tx is None or e.tx is not tx:
            return
        if res.total_cost is not None:  # E16.FR.15: the CSMS's running cost
            self._costs.setdefault(tx.tx_id, CostMeter(None)).central = res.total_cost
        values = limits_of(res.transaction_limit, self._supported_limits())
        if values:  # E16.FR.02 / F07.FR.02; unsupported limits are dropped (E16.FR.13)
            self._set_limits(e, tx, csms=values, confirm=not (req is not None and req.offline))  # E16.FR.18
        elif res.total_cost is not None:
            self._check_limit(e)

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
            # E16 / I (2.1-M3)
            var(TX_SUPPORTED_LIMITS, dm.DT.member_list, ",".join(SUPPORTED_LIMITS), values_list=SUPPORTED_LIMITS,
                **ro),
            var(TARIFF_ENABLED, dm.DT.boolean, "true", variable_instance="Tariff"),
            var(TARIFF_ENABLED, dm.DT.boolean, "true", variable_instance="Cost"),
            var(TARIFF_ENABLED, dm.DT.boolean, "false", variable_instance="RunningCost", derived=True, **ro),
            var((TARIFF, "Available"), dm.DT.boolean, "true", variable_instance="Tariff", derived=True, **ro),
            var((TARIFF, "Available"), dm.DT.boolean, "true", variable_instance="Cost", derived=True, **ro),
            var(TARIFF_CURRENCY, dm.DT.string, "JPY", max_limit=3),
            var(TARIFF_MAX_ELEMENTS, dm.DT.integer, str(DEFAULT_MAX_ELEMENTS), variable_instance="Tariff", **ro),
            var((TARIFF, "ConditionsSupported"), dm.DT.boolean, "false", variable_instance="Tariff", derived=True,
                **ro),
            var((TARIFF, "TariffFallbackMessage"), dm.DT.string, "", variable_instance="en", max_limit=255),
            var((TARIFF, "TotalCostFallbackMessage"), dm.DT.string, "", variable_instance="en", max_limit=255),
        ]

    def _bidirectional(self) -> bool:
        return self.dm.bool_value(V2X_ENABLED)

    # ------------------------------------------------------------------ K01 / Q: profiles, setting, physics

    def _socs(self) -> Dict[int, float]:
        return {e.id: e.pw.soc_pct for e in self.evses.values() if e.pw is not None}

    def _setting(self, e: _Evse) -> Setting:
        if e.tx is not None and (e.tx.deauthorized or e.tx.tx_id in self._limit_reached):
            return Setting(limit=0.0)  # E05.FR.02 / E16.FR.05
        return setting_at(self.profiles, e.id, self.host.clock(), transactions=self._transactions(),
                          number_phases=self._phases(e.id), voltage=self.supply.phase_voltage_v(),
                          priority=set(self.priority), soc=self._socs())

    def _limit_w(self, e: _Evse) -> Optional[float]:
        s = self._setting(e)
        return 0.0 if s.operation_mode is MODE.idle else s.limit

    def _step_power(self, e: _Evse, dt: float) -> None:
        s = self._setting(e)
        setpoint = s.setpoint
        if setpoint is not None and setpoint < 0 and not self._bidirectional():
            setpoint = 0.0
        if setpoint is None:
            e.pw = self.host.step_power(e.pw, dt, s.limit, e.vehicle)
        else:
            e.pw = self.host.step_power(e.pw, dt, s.limit, e.vehicle, setpoint_w=setpoint)
        cost = self._costs.get(e.tx.tx_id) if e.tx is not None and dt > 0 else None
        if cost is not None:  # totalUsage.chargingTime / idleTime (I12)
            sim = dt * self.host.time_scale  # simulated seconds
            if e.pw.power_w != 0:
                cost.charging_s += sim
            else:
                cost.idle_s += sim

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
            self._check_limit(e)
        self._pull_dynamic(self.host.clock())

    def _end_tx(self, e: _Evse, *args: Any, **kw: Any) -> None:
        tx = e.tx
        super()._end_tx(e, *args, **kw)
        self.priority.pop(e.id, None)
        self._modes.pop(e.id, None)
        if tx is not None:
            for store in (self.tx_tariffs, self.tx_limits, self._local_limits, self._csms_limits, self._limit_reached,
                      self._tx_time, self._costs,
                          self._tx_info_once, self.allowed_energy_transfer):
                store.pop(tx.tx_id, None)  # type: ignore[attr-defined]

    def _queue_event(self, e: _Evse, tx: _Tx, event: TransactionEventEnumType, trigger: Any, info: Any,
                     **kw: Any) -> Any:
        """The 2.0.1 event plus the 2.1 transaction fields: the tariff of a new transaction (Started, I08.FR.22),
        pending once-only ``transactionInfo`` values and the final ``costDetails`` (Ended, I12.FR.01)."""
        push = kw.pop("push", True)
        if event is TransactionEventEnumType.started and tx.tx_id not in self._costs:
            tariff = self._start_tariff(e, tx)
            self._costs[tx.tx_id] = CostMeter(tariff)
            self._tx_time.setdefault(tx.tx_id, [0.0, self.host.monotonic()])
            if tariff is not None:
                self._tx_info_once.setdefault(tx.tx_id, {})["tariff_id"] = tariff.tariff_id
        once = self._tx_info_once.pop(tx.tx_id, None)
        if once:
            info = info.model_copy(update={k: v for k, v in once.items() if getattr(info, k) is None})
        out = super()._queue_event(e, tx, event, trigger, info, push=False, **kw)
        cost = self._costs.get(tx.tx_id)
        if (event is TransactionEventEnumType.ended and cost is not None and cost.tariff is not None
                and self._local_cost()):
            out.req.cost_details = cost.details(max(0.0, e.meter_wh - tx.meter_start_wh), self.models)
        if push:
            self._push(out)
        return out

    def _remote_start_profile(self, req: Any) -> Tuple[Any, Any, Optional[str]]:
        """F01 / K05 with the 2.1 profile rules: converted as SetChargingProfile (setpoint, operationMode, Dynamic),
        checked by :func:`.profiles.reject_reason_21` first, then by the 2.0.1 RequestStart rules."""
        wire = req.charging_profile
        if wire is None:
            return None, None, None
        profile, wire = charging_profile21(wire, self.host.clock())
        reason = reject_reason_21(profile, modes=self.dm.list_value(V2X_OPERATION_MODES),
                                  purposes=self.dm.list_value(SC_ADDITIONAL_PURPOSES),
                                  bidirectional=self._bidirectional())
        if reason is None:
            reason = self.profiles.reject_reason(req.evse_id or 1, profile, for_remote_start=True,
                                                 limits=self._limits())
        return profile, wire, reason

    # ------------------------------------------------------------------ G01 availability

    def _availability_event(self, ev: d.ConnectorStatus) -> m.NotifyEventRequest:
        mm = self.models
        self._event_id += 1
        at = ev.timestamp or self.host.clock()
        return mm.NotifyEventRequest(generated_at=at, seq_no=0, event_data=[mm.EventDataType(
            event_id=self._event_id, timestamp=at, trigger="Delta", actual_value=connector_status(ev.status).value,
            event_notification_type="HardWiredNotification",
            component=mm.ComponentType(name="Connector", evse=mm.EVSEType(id=ev.evse_id, connector_id=ev.connector_id)),
            variable=mm.VariableType(name="AvailabilityState"))])

    def _status_sends(self, ev: d.ConnectorStatus) -> List[Tuple[str, Any]]:
        """G01: StatusNotification and / or NotifyEvent(Connector.AvailabilityState) by ``availability_report``;
        ``both`` is two outbox items, so a reconnect never resends the one already delivered."""
        mode = getattr(self.cfg, "availability_report", "status")
        out: List[Tuple[str, Any]] = []
        if mode in ("status", "both"):
            out += super()._status_sends(ev)
        if mode in ("event", "both"):
            out.append(("NotifyEvent", lambda: self._adapter.send_notify_event(self._availability_event(ev))))
        return out

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
        """K28.FR.06 / FR.08 / FR.09; ``KeyError`` / ``ValueError`` / ``UpdateRejected`` from
        :meth:`ProfileStore21.update_dynamic` (the profile is left unchanged then)."""
        self.settle()
        now = self.host.clock()
        ip = self.profiles.update_dynamic(profile_id, update, now, modes=self.dm.list_value(V2X_OPERATION_MODES),
                                          purposes=self.dm.list_value(SC_ADDITIONAL_PURPOSES),
                                          bidirectional=self._bidirectional())
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
        except UpdateRejected as exc:  # the merged period is invalid (V2X.05, K01.FR.111 ...)
            return R(status=ChargingProfileStatusEnumType.rejected, status_info=self._info(exc.reason))
        self.host.after_reply(self.tick)
        return R(status=ChargingProfileStatusEnumType.accepted)

    def _pull_dynamic(self, now: datetime) -> None:
        """K28.FR.10: a PullDynamicScheduleUpdate per due Dynamic profile with a dynUpdateInterval."""
        if not self.host.online:
            return
        # K28.FR.10 does not require a transaction: a Dynamic profile is pulled while it exists and is in its validity.
        dynamic = {ip.profile.profile_id for ip in self.profiles.profiles() if ip.profile.kind is DYNAMIC}
        for stale in set(self._pull_due) - dynamic:  # replaced by another kind, cleared or dropped meanwhile
            del self._pull_due[stale]
        for ip in self.profiles.profiles():
            p = ip.profile
            interval = getattr(p, "dyn_update_interval", None)
            if p.kind is not DYNAMIC or not interval or interval <= 0 or p.profile_id in self._pulling:
                continue
            if (p.valid_from and now < p.valid_from) or (p.valid_to and now >= p.valid_to):
                continue  # outside validFrom / validTo the profile does not apply: nothing to update
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
        except (KeyError, ValueError, UpdateRejected):  # replaced or cleared meanwhile, or the update is invalid
            return
        # Runs in a session task on the same event loop as the CALL handlers: settle() / tick() are synchronous, so
        # nothing interleaves with them and they are as safe here as in a handler (the shell cancels the task with
        # the session).
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
        self._pulling.clear()  # the session tasks (and so the in-flight pulls) are cancelled
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
            tx = e.tx
            cost = self._costs.get(tx.tx_id) if tx is not None else None
            c["tx_limit"] = dict(self.tx_limits.get(tx.tx_id, {})) or None if tx is not None else None
            c["limit_reached"] = LIMIT_FIELDS.get(self._limit_reached.get(tx.tx_id, "")) if tx is not None else None
            c["tariff_id"] = cost.tariff.tariff_id if cost is not None and cost.tariff is not None else None
            c["cost"] = None
            if cost is not None and cost.tariff is not None and self._local_cost():
                c["cost"] = {"amount": cost.total(self._tx_energy(e))[0], "currency": cost.tariff.currency}
            elif cost is not None and cost.central is not None:
                c["cost"] = {"amount": cost.central, "currency": self.dm.value(TARIFF_CURRENCY)}
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
