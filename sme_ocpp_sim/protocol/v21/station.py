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
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..station import StationHost
from ..v201.station import Station201
from . import models as m
from .adapter import Adapter21

log = logging.getLogger(__name__)

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


def _wire(attr: str) -> str:
    return m.ReportDERControlRequest.model_fields[attr].alias or attr
