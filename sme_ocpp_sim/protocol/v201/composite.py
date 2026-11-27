"""Composite charging schedule and live power limit from one evaluator (OCPP 2.0.1, plan §11.2, M5).

A copy of ``protocol/v16/composite.py`` adapted to 2.0.1 (the versions share no smart-charging code).
:func:`composite_schedule` answers GetCompositeSchedule; :func:`limit_at` is the limit the power model applies
each tick and is literally the first period of a one-second composite schedule, so the schedule reported to the
CSMS and the power actually drawn cannot diverge.

Rules kept from the 1.6 evaluator (the 2.0.1 spec text was not available locally; UNVERIFIED that 2.0.1 states
them the same way): per purpose the prevailing profile is the valid one with the highest stackLevel, falling back
to the next lower stackLevel while its schedule is not running; TxProfile overrules TxDefaultProfile for the
current transaction; Absolute starts at ``startSchedule``, Relative (and Absolute without ``startSchedule``) at the
transaction start, Recurring restarts every day / week with ``duration`` cut to the recurrence; W = A x phase
voltage x phases, phases = ``numberPhases`` or 3, capped at the phases the station has.

Interpretations (not defined by the schemas)
--------------------------------------------
- The limit of an EVSE is min(ChargingStationMaxProfile, ChargingStationExternalConstraints of evseId 0,
  ChargingStationExternalConstraints of that EVSE, TxProfile-else-TxDefaultProfile). External constraints of
  evseId 0 apply to the whole station like ChargingStationMaxProfile; those of an EVSE only to it.
- evseId 0 = min(ChargingStationMax, external constraints of evseId 0, sum over EVSEs 1..N of their own limit
  without the station-level parts); if any EVSE is unconstrained the sum is unconstrained. Idle EVSEs are
  included, so the value is the most the station *may* draw, not what it draws now. A single EVSE gets the full
  ChargingStationMax value (no sharing between EVSEs is modelled here).
- An EVSE-specific TxDefaultProfile replaces the evseId-0 one with the same stackLevel for that EVSE only.
- Relative schedules (and Absolute without startSchedule) with no transaction on the EVSE use the start of the
  requested window as origin; station-level purposes always use the window start. Hence :func:`limit_at` without
  a transaction always sees the first period of a Relative schedule.
- Recurring without ``startSchedule`` is anchored at 00:00 UTC (Daily) or Monday 00:00 UTC (Weekly); the
  recurrence also extends before ``startSchedule`` (which only fixes the time of day / week).
- validFrom is inclusive, validTo is exclusive.
- ``minChargingRate`` and ``phaseToUse`` do not alter the limit (the EV model does not refuse low rates and does
  not model which phase is used).
- An unconstrained interval is reported as ``limit=None``; the station decides how to encode it. No unit given
  -> W. Limits are returned unrounded except for float noise (6 decimals).
- Period boundaries falling between whole seconds start at the next whole second (startPeriod is an integer).
- Only the first ``chargingSchedule`` of a profile is evaluated (see ``profiles``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Mapping, Optional, Sequence, Tuple, cast

from .profiles import (
    ChargingProfile,
    ChargingProfileKind,
    ChargingProfilePurpose,
    ChargingRateUnit,
    ProfileStore,
    RecurrencyKind,
    SchedulePeriod,
)

DEFAULT_VOLTAGE = 230.0  # phase (line-to-neutral) voltage
DEFAULT_PHASES = 3  # numberPhases absent -> 3

EXTERNAL = ChargingProfilePurpose.charging_station_external_constraints
_ZERO = timedelta(0)
_RECURRENCE = {RecurrencyKind.daily: timedelta(days=1), RecurrencyKind.weekly: timedelta(weeks=1)}
_ANCHOR = {
    RecurrencyKind.daily: datetime(1970, 1, 1, tzinfo=timezone.utc),
    RecurrencyKind.weekly: datetime(1970, 1, 5, tzinfo=timezone.utc),  # a Monday
}

_Limit = Tuple[float, int]  # (watts, effective phases)


@dataclass(frozen=True)
class ActiveTransaction:
    transaction_id: str
    start: datetime


@dataclass(frozen=True)
class CompositePeriod:
    start_period: int  # seconds from CompositeSchedule.start
    limit: Optional[float]  # None = unconstrained
    number_phases: Optional[int] = None  # set for unit A only


@dataclass(frozen=True)
class CompositeSchedule:
    evse_id: int
    start: datetime
    duration: int
    unit: ChargingRateUnit
    periods: Tuple[CompositePeriod, ...]


def convert(value: float, src: ChargingRateUnit, dst: ChargingRateUnit, phases: int, voltage: float) -> float:
    """A (per phase) <-> W: P = U x I x phases."""
    if src is dst:
        return value
    if src is ChargingRateUnit.a:
        return value * voltage * phases
    return value / (voltage * phases)


@dataclass
class _Context:
    store: ProfileStore
    transactions: Mapping[int, ActiveTransaction]
    phases: int
    voltage: float
    window_start: datetime
    _cache: dict = field(default_factory=dict)

    def relative_origin(self, evse_id: int) -> datetime:
        tx = self.transactions.get(evse_id)
        return tx.start if tx else self.window_start

    def candidates(self, evse_id: int, purpose: ChargingProfilePurpose) -> List[ChargingProfile]:
        key = (evse_id, purpose)
        if key not in self._cache:
            own = [ip.profile for ip in self.store.profiles(evse_id) if ip.profile.purpose is purpose]
            if purpose is ChargingProfilePurpose.tx_profile:
                tx = self.transactions.get(evse_id)
                own = [p for p in own if tx and p.transaction_id in (None, tx.transaction_id)]
            elif purpose is ChargingProfilePurpose.tx_default_profile and evse_id != 0:
                levels = {p.stack_level for p in own}
                own += [
                    ip.profile
                    for ip in self.store.profiles(0)
                    if ip.profile.purpose is purpose and ip.profile.stack_level not in levels
                ]
            self._cache[key] = sorted(own, key=lambda p: p.stack_level, reverse=True)
        return self._cache[key]


def _origin(profile: ChargingProfile, t: datetime, rel_origin: datetime) -> Tuple[datetime, Optional[timedelta]]:
    """Schedule origin in force at ``t`` and the effective schedule duration."""
    sched = profile.schedule
    duration = timedelta(seconds=sched.duration) if sched.duration is not None else None
    if profile.kind is ChargingProfileKind.recurring:
        kind = profile.recurrency_kind or RecurrencyKind.daily
        period = _RECURRENCE[kind]
        anchor = sched.start_schedule or _ANCHOR[kind]
        origin = anchor + ((t - anchor) // period) * period
        return origin, min(duration, period) if duration is not None else period
    if profile.kind is ChargingProfileKind.absolute and sched.start_schedule is not None:
        return sched.start_schedule, duration
    return rel_origin, duration


def _period_at(profile: ChargingProfile, t: datetime, rel_origin: datetime) -> Optional[SchedulePeriod]:
    if (profile.valid_from and t < profile.valid_from) or (profile.valid_to and t >= profile.valid_to):
        return None
    origin, duration = _origin(profile, t, rel_origin)
    offset = t - origin
    if offset < _ZERO or (duration is not None and offset >= duration):
        return None
    seconds = offset.total_seconds()
    current = None
    for period in profile.schedule.periods:
        if period.start_period > seconds:
            break
        current = period
    return current


def _prevailing(ctx: _Context, profiles: Sequence[ChargingProfile], t: datetime, rel: datetime) -> Optional[_Limit]:
    for profile in profiles:
        period = _period_at(profile, t, rel)
        if period is not None:
            phases = min(period.number_phases or DEFAULT_PHASES, ctx.phases)
            watts = convert(period.limit, profile.schedule.unit, ChargingRateUnit.w, phases, ctx.voltage)
            return watts, phases
    return None


def _min(a: Optional[_Limit], b: Optional[_Limit]) -> Optional[_Limit]:
    if a is None or b is None:
        return a if b is None else b
    return a if a[0] <= b[0] else b


def _tx_limit(ctx: _Context, evse_id: int, t: datetime) -> Optional[_Limit]:
    rel = ctx.relative_origin(evse_id)
    tx = _prevailing(ctx, ctx.candidates(evse_id, ChargingProfilePurpose.tx_profile), t, rel)
    if tx is not None:
        return tx
    return _prevailing(ctx, ctx.candidates(evse_id, ChargingProfilePurpose.tx_default_profile), t, rel)


def _external(ctx: _Context, evse_id: int, t: datetime) -> Optional[_Limit]:
    return _prevailing(ctx, ctx.candidates(evse_id, EXTERNAL), t, ctx.window_start)


def _evse_limit(ctx: _Context, evse_id: int, t: datetime) -> Optional[_Limit]:
    """The EVSE's own limit: min(its external constraints, TxProfile-else-TxDefaultProfile)."""
    return _min(_external(ctx, evse_id, t), _tx_limit(ctx, evse_id, t))


def _limit(ctx: _Context, evse_id: int, t: datetime) -> Optional[_Limit]:
    station = _min(_prevailing(ctx, ctx.candidates(0, ChargingProfilePurpose.charging_station_max_profile), t,
                               ctx.window_start), _external(ctx, 0, t))
    if evse_id != 0:
        return _min(station, _evse_limit(ctx, evse_id, t))
    parts = [_evse_limit(ctx, e, t) for e in range(1, ctx.store.evse_count + 1)]
    if not parts or any(p is None for p in parts):
        return station  # no EVSEs, or one is unconstrained
    return _min(station, (sum(cast(_Limit, p)[0] for p in parts), ctx.phases))


def _boundaries(
    profile: ChargingProfile, rel_origins: Sequence[datetime], start: datetime, end: datetime
) -> Iterator[datetime]:
    """Every instant at which ``profile`` may start, stop or change period (superset is fine)."""
    for t in (profile.valid_from, profile.valid_to):
        if t is not None:
            yield t
    offsets = [timedelta(seconds=p.start_period) for p in profile.schedule.periods]
    if profile.kind is ChargingProfileKind.recurring:
        origin, duration = _origin(profile, start, start)
        recurrence = _RECURRENCE[profile.recurrency_kind or RecurrencyKind.daily]
        while origin < end:
            yield from (origin + o for o in offsets if duration is None or o < duration)
            if duration is not None:
                yield origin + duration
            origin += recurrence
        return
    origins = {_origin(profile, start, r)[0] for r in rel_origins}
    duration = _origin(profile, start, start)[1]
    for origin in origins:
        yield from (origin + o for o in offsets)
        if duration is not None:
            yield origin + duration


def composite_schedule(
    store: ProfileStore,
    evse_id: int,
    start: datetime,
    duration: int,
    unit: Optional[ChargingRateUnit] = None,
    *,
    transactions: Optional[Mapping[int, ActiveTransaction]] = None,
    number_phases: int = DEFAULT_PHASES,
    voltage: float = DEFAULT_VOLTAGE,
) -> CompositeSchedule:
    """Composite schedule over [start, start + duration) seconds.

    ``transactions`` maps evseId -> its active transaction; ``number_phases`` / ``voltage`` describe the
    station supply. Raises ``ValueError`` for an unknown EVSE (station -> Rejected), a non-positive
    duration or a naive ``start``.
    """
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if duration < 1:
        raise ValueError("duration must be >= 1 s")
    if not 0 <= evse_id <= store.evse_count:
        raise ValueError(f"unknown EVSE {evse_id}")
    out_unit = unit or ChargingRateUnit.w
    txs = transactions or {}
    ctx = _Context(store, txs, number_phases, voltage, start)
    end = start + timedelta(seconds=duration)
    rel_origins = [start, *(tx.start for tx in txs.values())]

    offsets = {0}
    for ip in store.profiles():
        for b in _boundaries(ip.profile, rel_origins, start, end):
            if start < b < end:
                offsets.add(math.ceil((b - start).total_seconds()))

    periods: List[CompositePeriod] = []
    for offset in sorted(o for o in offsets if o < duration):
        lim = _limit(ctx, evse_id, start + timedelta(seconds=offset))
        if lim is None:
            period = CompositePeriod(offset, None)
        else:
            value = round(convert(lim[0], ChargingRateUnit.w, out_unit, lim[1], voltage), 6)
            phases = lim[1] if out_unit is ChargingRateUnit.a else None
            period = CompositePeriod(offset, value, phases)
        if periods and (periods[-1].limit, periods[-1].number_phases) == (period.limit, period.number_phases):
            continue
        periods.append(period)
    return CompositeSchedule(evse_id, start, duration, out_unit, tuple(periods))


def limit_at(
    store: ProfileStore,
    evse_id: int,
    now: datetime,
    *,
    transactions: Optional[Mapping[int, ActiveTransaction]] = None,
    number_phases: int = DEFAULT_PHASES,
    voltage: float = DEFAULT_VOLTAGE,
) -> Optional[float]:
    """Limit in W in force at ``now`` (None = unconstrained); the first period of a 1 s composite schedule."""
    schedule = composite_schedule(
        store,
        evse_id,
        now,
        1,
        ChargingRateUnit.w,
        transactions=transactions,
        number_phases=number_phases,
        voltage=voltage,
    )
    return schedule.periods[0].limit
