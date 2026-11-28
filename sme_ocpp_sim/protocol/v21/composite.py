"""OCPP 2.1 composite schedule and live setting (2.1-M2): the 2.0.1 evaluator (``protocol.v201.composite``)
extended with operationMode, dischargeLimit, setpoint, PriorityCharging, LocalGeneration, ``limitAtSoC`` and
``maxOfflineDuration``.

As in 2.0.1, :func:`setting_at` (what the power model applies each tick) is the evaluation that
:func:`composite_schedule` reports per period, so GetCompositeSchedule and the simulated power cannot diverge.

Rules (OCPP 2.1 Part 2 K 3.6 "Combining charging profile purposes", K21, K27, Q 2.x; simplest subset)
-------------------------------------------------------------------------------------------------
- Per purpose the leading profile is the valid one with the highest stackLevel that has a period at that time
  (as 2.0.1); a profile is not valid while :meth:`ProfileStore21.valid_offline` says so (K01.FR.101).
- The transaction purpose of an EVSE is PriorityCharging while priority charging is active for its transaction
  (K21.FR.04, profiles of the EVSE and of evseId 0), else TxProfile, else TxDefaultProfile.
  ChargingStationMaxProfile and the external constraints (evseId 0 and the EVSE) always apply.
- ``limit`` = the lowest limit of the leading periods (W after unit conversion); a LocalGeneration limit (EVSE or
  evseId 0) is added on top of it when both exist (K 3.6, K27).
- ``operationMode`` = the mode of the leading transaction period (absent field = ChargingOnly); without one, an
  external period in ExternalSetpoint mode sets it, else it is not reported.
- ``dischargeLimit`` (<= 0) = the one closest to 0 among the leading periods, only in a mode that may discharge
  (every mode but ChargingOnly and Idle).
- ``setpoint``: an external constraints period with operationMode ExternalSetpoint and a setpoint takes
  precedence (K "ExternalSetpoint"; SmartChargingCtrlr.SetpointPriority is not modelled); else the transaction
  period's setpoint in CentralSetpoint / ExternalSetpoint / CentralFrequency. It is capped to [dischargeLimit,
  limit] (V2X.06 / V2X.07) and to >= 0 in a mode that may not discharge.
- Idle (Q10): limit, dischargeLimit and setpoint are absent from the period; the power model gets setpoint 0.
- ``limitAtSoC`` (K01.FR.100): when the EV's current SoC is >= ``soc``, the limit and setpoint of that schedule
  are capped at ``limit``. The current SoC is used for every period of a composite schedule (no SoC forecast).
- evseId 0: only ``limit`` = min(station-level limits, sum of the EVSE limits), as 2.0.1.
- ``numberPhases`` = the lowest of the leading periods (K01.FR.140).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Collection, List, Mapping, Optional, Sequence, Tuple

from ..v201.composite import (DEFAULT_PHASES, DEFAULT_VOLTAGE, EXTERNAL, ActiveTransaction, _boundaries, _Context,
                              _period_at, convert)
from ..v201.profiles import ChargingProfile, ChargingProfilePurpose, ChargingRateUnit, SchedulePeriod
from .profiles import DISCHARGE_MODES, LOCAL_GENERATION, MODE, PRIORITY, ProfileStore21, mode_of

W = ChargingRateUnit.w
_TX = ChargingProfilePurpose.tx_profile
_TX_DEFAULT = ChargingProfilePurpose.tx_default_profile
_MAX = ChargingProfilePurpose.charging_station_max_profile
_SETPOINT_MODES = frozenset({MODE.central_setpoint, MODE.external_setpoint, MODE.central_frequency})

__all__ = ["CompositePeriod21", "CompositeSchedule21", "Setting", "composite_schedule", "setting_at"]


@dataclass(frozen=True)
class Setting:
    """What applies to an EVSE at one instant, in W (``None`` = not constrained / not given)."""

    limit: Optional[float] = None
    discharge_limit: Optional[float] = None  # <= 0
    setpoint: Optional[float] = None  # signed
    operation_mode: Optional[Any] = None  # OperationModeEnumType
    phases: Optional[int] = None  # numberPhases of the leading periods


@dataclass(frozen=True)
class CompositePeriod21:
    start_period: int
    limit: Optional[float]
    number_phases: Optional[int] = None
    discharge_limit: Optional[float] = None
    setpoint: Optional[float] = None
    operation_mode: Optional[Any] = None


@dataclass(frozen=True)
class CompositeSchedule21:
    evse_id: int
    start: datetime
    duration: int
    unit: ChargingRateUnit
    periods: Tuple[CompositePeriod21, ...]


@dataclass
class _Ctx(_Context):
    priority: Collection[int] = ()  # EVSEs whose transaction uses priority charging
    soc: Mapping[int, float] = field(default_factory=dict)

    def candidates(self, evse_id: int, purpose: Any) -> List[ChargingProfile]:
        if purpose not in (PRIORITY, LOCAL_GENERATION) or evse_id == 0:
            return super().candidates(evse_id, purpose)
        key = (evse_id, purpose)
        if key not in self._cache:
            own = [ip.profile for e in (evse_id, 0) for ip in self.store.profiles(e) if ip.profile.purpose is purpose]
            self._cache[key] = sorted(own, key=lambda p: p.stack_level, reverse=True)
        return self._cache[key]


@dataclass(frozen=True)
class _Values:
    limit: Optional[float]
    discharge_limit: Optional[float]
    setpoint: Optional[float]
    mode: Any
    phases: int


def _lead(ctx: _Ctx, profiles: Sequence[ChargingProfile], t: datetime, rel: datetime,
          evse_id: int) -> Optional[_Values]:
    store = ctx.store
    for profile in profiles:
        if isinstance(store, ProfileStore21) and not store.valid_offline(profile, t):
            continue
        period = _period_at(profile, t, rel)
        if period is not None:
            return _values(ctx, profile, period, evse_id)
    return None


def _values(ctx: _Ctx, profile: ChargingProfile, p: SchedulePeriod, evse_id: int) -> _Values:
    phases = min(p.number_phases or DEFAULT_PHASES, ctx.phases)
    limit, setpoint = p.limit, getattr(p, "setpoint", None)
    cap = getattr(profile.schedule, "limit_at_soc", None)
    soc = ctx.soc.get(evse_id)
    if cap is not None and soc is not None and soc >= cap[0]:  # K01.FR.100
        limit = cap[1] if limit is None else min(limit, cap[1])
        setpoint = None if setpoint is None else min(setpoint, cap[1])
    unit = profile.schedule.unit

    def w(v: Optional[float]) -> Optional[float]:
        return None if v is None else convert(v, unit, W, phases, ctx.voltage)

    return _Values(w(limit), w(getattr(p, "discharge_limit", None)), w(setpoint), mode_of(p), phases)


def _min(values: Sequence[Optional[float]]) -> Optional[float]:
    given = [v for v in values if v is not None]
    return min(given) if given else None


def _evse_setting(ctx: _Ctx, evse_id: int, t: datetime) -> Setting:
    rel = ctx.relative_origin(evse_id)
    tx = None
    if evse_id in ctx.priority:
        tx = _lead(ctx, ctx.candidates(evse_id, PRIORITY), t, rel, evse_id)
    for purpose in (_TX, _TX_DEFAULT):
        tx = tx or _lead(ctx, ctx.candidates(evse_id, purpose), t, rel, evse_id)
    ws = ctx.window_start
    externals = [x for x in (_lead(ctx, ctx.candidates(0, EXTERNAL), t, ws, evse_id),
                             _lead(ctx, ctx.candidates(evse_id, EXTERNAL), t, ws, evse_id)) if x]
    station = _lead(ctx, ctx.candidates(0, _MAX), t, ws, evse_id)
    leads = [x for x in (tx, station, *externals) if x]
    lg = _lead(ctx, ctx.candidates(evse_id, LOCAL_GENERATION), t, rel, evse_id)
    if not leads:
        return Setting()
    ext_sp = next((x.setpoint for x in externals if x.mode is MODE.external_setpoint and x.setpoint is not None),
                  None)
    mode = tx.mode if tx else (MODE.external_setpoint if ext_sp is not None else None)
    phases = min(x.phases for x in leads)
    if mode is MODE.idle:
        return Setting(operation_mode=mode, phases=phases, setpoint=0.0)
    limit = _min([x.limit for x in leads])
    if lg is not None and limit is not None and lg.limit is not None:
        limit += lg.limit  # K 3.6 / K27: local generation adds on top
    discharging = mode in DISCHARGE_MODES
    dlimit = None
    if discharging:
        given = [x.discharge_limit for x in leads if x.discharge_limit is not None]
        dlimit = max(given) if given else None
    setpoint = ext_sp if ext_sp is not None else (tx.setpoint if tx and mode in _SETPOINT_MODES else None)
    if setpoint is not None:
        if limit is not None:
            setpoint = min(setpoint, limit)  # V2X.06
        if dlimit is not None:
            setpoint = max(setpoint, dlimit)  # V2X.07
        if not discharging:
            setpoint = max(setpoint, 0.0)
    return Setting(limit, dlimit, setpoint, mode, phases)


def _setting(ctx: _Ctx, evse_id: int, t: datetime) -> Setting:
    if evse_id != 0:
        return _evse_setting(ctx, evse_id, t)
    ws = ctx.window_start
    station = [x for x in (_lead(ctx, ctx.candidates(0, _MAX), t, ws, 0),
                           _lead(ctx, ctx.candidates(0, EXTERNAL), t, ws, 0)) if x]
    parts = [_evse_setting(ctx, e, t).limit for e in range(1, ctx.store.evse_count + 1)]
    limits = [x.limit for x in station]
    if parts and all(p is not None for p in parts):
        limits.append(sum(parts))  # type: ignore[arg-type]
    phases = min((x.phases for x in station), default=None)
    return Setting(limit=_min(limits), phases=phases)


def _ctx(store: ProfileStore21, start: datetime, transactions: Optional[Mapping[int, ActiveTransaction]],
         number_phases: int, voltage: float, priority: Collection[int],
         soc: Optional[Mapping[int, float]]) -> _Ctx:
    return _Ctx(store, transactions or {}, number_phases, voltage, start, priority=priority, soc=soc or {})


def setting_at(store: ProfileStore21, evse_id: int, now: datetime, *,
               transactions: Optional[Mapping[int, ActiveTransaction]] = None, number_phases: int = DEFAULT_PHASES,
               voltage: float = DEFAULT_VOLTAGE, priority: Collection[int] = (),
               soc: Optional[Mapping[int, float]] = None) -> Setting:
    """The setting in force at ``now`` (the first period of a composite schedule starting now)."""
    return _setting(_ctx(store, now, transactions, number_phases, voltage, priority, soc), evse_id, now)


def composite_schedule(store: ProfileStore21, evse_id: int, start: datetime, duration: int,
                       unit: Optional[ChargingRateUnit] = None, *,
                       transactions: Optional[Mapping[int, ActiveTransaction]] = None,
                       number_phases: int = DEFAULT_PHASES, voltage: float = DEFAULT_VOLTAGE,
                       priority: Collection[int] = (), soc: Optional[Mapping[int, float]] = None
                       ) -> CompositeSchedule21:
    """Composite schedule over [start, start + duration) seconds; ``ValueError`` as the 2.0.1 one."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if duration < 1:
        raise ValueError("duration must be >= 1 s")
    if not 0 <= evse_id <= store.evse_count:
        raise ValueError(f"unknown EVSE {evse_id}")
    out_unit = unit or W
    ctx = _ctx(store, start, transactions, number_phases, voltage, priority, soc)
    end = start + timedelta(seconds=duration)
    rel_origins = [start, *(tx.start for tx in ctx.transactions.values())]
    offsets = {0}
    for ip in store.profiles():
        for b in _boundaries(ip.profile, rel_origins, start, end):
            if start < b < end:
                offsets.add(math.ceil((b - start).total_seconds()))
    periods: List[CompositePeriod21] = []
    for offset in sorted(o for o in offsets if o < duration):
        s = _setting(ctx, evse_id, start + timedelta(seconds=offset))
        n = s.phases or number_phases

        def out(v: Optional[float]) -> Optional[float]:
            return None if v is None else round(convert(v, W, out_unit, n, voltage), 6)

        period = CompositePeriod21(offset, out(s.limit), n if out_unit is ChargingRateUnit.a else None,
                                   out(s.discharge_limit), out(s.setpoint) if s.operation_mode is not MODE.idle
                                   else None, s.operation_mode)
        if periods and _same(periods[-1], period):
            continue
        periods.append(period)
    return CompositeSchedule21(evse_id, start, duration, out_unit, tuple(periods))


def _same(a: CompositePeriod21, b: CompositePeriod21) -> bool:
    return (a.limit, a.number_phases, a.discharge_limit, a.setpoint, a.operation_mode) == \
        (b.limit, b.number_phases, b.discharge_limit, b.setpoint, b.operation_mode)
