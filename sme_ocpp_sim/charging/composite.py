"""Composite charging schedule and live power limit from one evaluator (design §3.3, plan §6.1).

:func:`composite_schedule` answers GetCompositeSchedule; :func:`limit_at` is the limit the power model applies
each tick and is literally the first period of a one-second composite schedule, so the schedule reported to the
CSMS and the power actually drawn cannot diverge (C12).

Rules implemented from OCPP 1.6 edition 2 (and its errata sheet v4.0):

- 3.13.2: per purpose, the prevailing profile is the valid one (validFrom/validTo) with the highest stackLevel;
  when its schedule is not running (not started, ``duration`` elapsed) the next lower stackLevel applies
  (errata 3.43); without any profile there is no limit.
- 3.13.1 / 3.13.3: TxProfile overrules TxDefaultProfile for the current transaction; the result is the minimum
  of that and ChargePointMaxProfile. A connector-specific TxDefaultProfile replaces the connector-0 one with
  the same stackLevel for that connector only.
- 7.9 / 7.13: Absolute starts at ``startSchedule``; Relative and Absolute without ``startSchedule`` start at the
  transaction start; Recurring restarts every day/week; a ``duration`` longer than the recurrence is cut to
  the recurrence (errata 3.43/3.44).
- 7.12 / 7.14: W = A x line voltage x phases, phases = ``numberPhases`` or 3 if absent; errata 3.34: a profile
  with more phases than the charger has applies its limit to the phases actually available.
- 5.7: the schedule covers [start, start + duration); connectorId 0 reports the whole charger's grid draw.

Interpretations (not defined by the spec)
-----------------------------------------
- Relative schedules (and Absolute without startSchedule) with no transaction on the connector use the start
  of the requested window as origin (5.7, 7.13 checked). ChargePointMaxProfiles always use the window start.
  Hence :func:`limit_at` without a transaction always sees the first period of a Relative schedule.
- Recurring without ``startSchedule`` is anchored at 00:00 UTC (Daily) or Monday 00:00 UTC (Weekly); the
  recurrence also extends before ``startSchedule`` (which only fixes the time of day / week; 7.8, 7.9 checked).
- validFrom is inclusive, validTo is exclusive (7.8 checked).
- connectorId 0 = min(ChargePointMax, sum over connectors 1..N of their TxProfile/TxDefault limit); if any
  connector is unconstrained the sum is unconstrained (5.7, 3.13.3 checked). Idle connectors (no transaction)
  are included: they contribute their TxDefault limit, or make the sum unconstrained if they have none, so the
  value is the most the charger *may* draw, not what it draws now. A single connector gets the full
  ChargePointMax value (no sharing between connectors is modelled here).
- ``minChargingRate`` does not alter the limit; refusing to charge below it is the EV model's job (7.13 checked).
- An unconstrained interval is reported as ``limit=None``; the adapter decides how to encode it. No unit given
  -> W. Limits are returned unrounded except for float noise (6 decimals); 1-decimal rounding is the adapter's.
- Period boundaries falling between whole seconds start at the next whole second (startPeriod is an integer).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Mapping, Optional, Sequence, Tuple, cast

from sme_ocpp_sim.charging.profiles import (
    ChargingProfile,
    ChargingProfileKind,
    ChargingProfilePurpose,
    ChargingRateUnit,
    ProfileStore,
    RecurrencyKind,
    SchedulePeriod,
)

DEFAULT_VOLTAGE = 230.0  # set line-to-neutral voltage (7.12 example)
DEFAULT_PHASES = 3  # numberPhases absent -> 3 (7.12, 7.14)

_ZERO = timedelta(0)
_RECURRENCE = {RecurrencyKind.DAILY: timedelta(days=1), RecurrencyKind.WEEKLY: timedelta(weeks=1)}
_ANCHOR = {
    RecurrencyKind.DAILY: datetime(1970, 1, 1, tzinfo=timezone.utc),
    RecurrencyKind.WEEKLY: datetime(1970, 1, 5, tzinfo=timezone.utc),  # a Monday
}

_Limit = Tuple[float, int]  # (watts, effective phases)


@dataclass(frozen=True)
class ActiveTransaction:
    transaction_id: int
    start: datetime


@dataclass(frozen=True)
class CompositePeriod:
    start_period: int  # seconds from CompositeSchedule.start
    limit: Optional[float]  # None = unconstrained
    number_phases: Optional[int] = None  # set for unit A only


@dataclass(frozen=True)
class CompositeSchedule:
    connector_id: int
    start: datetime
    duration: int
    unit: ChargingRateUnit
    periods: Tuple[CompositePeriod, ...]


def convert(value: float, src: ChargingRateUnit, dst: ChargingRateUnit, phases: int, voltage: float) -> float:
    """A (per phase) <-> W: P = U x I x phases."""
    if src is dst:
        return value
    if src is ChargingRateUnit.A:
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

    def relative_origin(self, connector_id: int) -> datetime:
        tx = self.transactions.get(connector_id)
        return tx.start if tx else self.window_start

    def candidates(self, connector_id: int, purpose: ChargingProfilePurpose) -> List[ChargingProfile]:
        key = (connector_id, purpose)
        if key not in self._cache:
            own = [ip.profile for ip in self.store.profiles(connector_id) if ip.profile.purpose is purpose]
            if purpose is ChargingProfilePurpose.TX:
                tx = self.transactions.get(connector_id)
                own = [p for p in own if tx and p.transaction_id in (None, tx.transaction_id)]
            elif purpose is ChargingProfilePurpose.TX_DEFAULT and connector_id != 0:
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
    if profile.kind is ChargingProfileKind.RECURRING:
        kind = profile.recurrency_kind or RecurrencyKind.DAILY
        period = _RECURRENCE[kind]
        anchor = sched.start_schedule or _ANCHOR[kind]
        origin = anchor + ((t - anchor) // period) * period
        return origin, min(duration, period) if duration is not None else period
    if profile.kind is ChargingProfileKind.ABSOLUTE and sched.start_schedule is not None:
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
            watts = convert(period.limit, profile.schedule.unit, ChargingRateUnit.W, phases, ctx.voltage)
            return watts, phases
    return None


def _min(a: Optional[_Limit], b: Optional[_Limit]) -> Optional[_Limit]:
    if a is None or b is None:
        return a if b is None else b
    return a if a[0] <= b[0] else b


def _tx_limit(ctx: _Context, connector_id: int, t: datetime) -> Optional[_Limit]:
    rel = ctx.relative_origin(connector_id)
    tx = _prevailing(ctx, ctx.candidates(connector_id, ChargingProfilePurpose.TX), t, rel)
    if tx is not None:
        return tx
    return _prevailing(ctx, ctx.candidates(connector_id, ChargingProfilePurpose.TX_DEFAULT), t, rel)


def _limit(ctx: _Context, connector_id: int, t: datetime) -> Optional[_Limit]:
    cp_max = _prevailing(ctx, ctx.candidates(0, ChargingProfilePurpose.CHARGE_POINT_MAX), t, ctx.window_start)
    if connector_id != 0:
        return _min(cp_max, _tx_limit(ctx, connector_id, t))
    parts = [_tx_limit(ctx, c, t) for c in range(1, ctx.store.connector_count + 1)]
    if not parts or any(p is None for p in parts):
        return cp_max  # no connectors, or one is unconstrained
    return _min(cp_max, (sum(cast(_Limit, p)[0] for p in parts), ctx.phases))


def _boundaries(
    profile: ChargingProfile, rel_origins: Sequence[datetime], start: datetime, end: datetime
) -> Iterator[datetime]:
    """Every instant at which ``profile`` may start, stop or change period (superset is fine)."""
    for t in (profile.valid_from, profile.valid_to):
        if t is not None:
            yield t
    offsets = [timedelta(seconds=p.start_period) for p in profile.schedule.periods]
    if profile.kind is ChargingProfileKind.RECURRING:
        origin, duration = _origin(profile, start, start)
        recurrence = _RECURRENCE[profile.recurrency_kind or RecurrencyKind.DAILY]
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
    connector_id: int,
    start: datetime,
    duration: int,
    unit: Optional[ChargingRateUnit] = None,
    *,
    transactions: Optional[Mapping[int, ActiveTransaction]] = None,
    number_phases: int = DEFAULT_PHASES,
    voltage: float = DEFAULT_VOLTAGE,
) -> CompositeSchedule:
    """Composite schedule over [start, start + duration) seconds.

    ``transactions`` maps connectorId -> its active transaction; ``number_phases`` / ``voltage`` describe the
    charger's supply. Raises ``ValueError`` for an unknown connector (adapter -> Rejected), a non-positive
    duration or a naive ``start``.
    """
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if duration < 1:
        raise ValueError("duration must be >= 1 s")
    if not 0 <= connector_id <= store.connector_count:
        raise ValueError(f"unknown connector {connector_id}")
    out_unit = unit or ChargingRateUnit.W
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
        lim = _limit(ctx, connector_id, start + timedelta(seconds=offset))
        if lim is None:
            period = CompositePeriod(offset, None)
        else:
            value = round(convert(lim[0], ChargingRateUnit.W, out_unit, lim[1], voltage), 6)
            phases = lim[1] if out_unit is ChargingRateUnit.A else None
            period = CompositePeriod(offset, value, phases)
        if periods and (periods[-1].limit, periods[-1].number_phases) == (period.limit, period.number_phases):
            continue
        periods.append(period)
    return CompositeSchedule(connector_id, start, duration, out_unit, tuple(periods))


def limit_at(
    store: ProfileStore,
    connector_id: int,
    now: datetime,
    *,
    transactions: Optional[Mapping[int, ActiveTransaction]] = None,
    number_phases: int = DEFAULT_PHASES,
    voltage: float = DEFAULT_VOLTAGE,
) -> Optional[float]:
    """Limit in W in force at ``now`` (None = unconstrained); the first period of a 1 s composite schedule."""
    schedule = composite_schedule(
        store,
        connector_id,
        now,
        1,
        ChargingRateUnit.W,
        transactions=transactions,
        number_phases=number_phases,
        voltage=voltage,
    )
    return schedule.periods[0].limit
