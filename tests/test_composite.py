"""Composite schedule cases C1-C14 (plan §6.1). Pure functions, fixed ``NOW``, no asyncio."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, Tuple

import pytest

from sme_ocpp_sim.protocol.v16.composite import (
    ActiveTransaction,
    CompositePeriod,
    composite_schedule,
    convert,
    limit_at,
)
from sme_ocpp_sim.protocol.v16.profiles import (
    ChargingProfile,
    ChargingProfileKind,
    ChargingProfilePurpose,
    ChargingRateUnit,
    ChargingSchedule,
    ClearStatus,
    ProfileStatus,
    ProfileStore,
    RecurrencyKind,
    SchedulePeriod,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 26, 0, 0, tzinfo=UTC)  # a Thursday, 00:00 UTC
A, W = ChargingRateUnit.A, ChargingRateUnit.W
CPMAX, TXDEF, TX = (
    ChargingProfilePurpose.CHARGE_POINT_MAX,
    ChargingProfilePurpose.TX_DEFAULT,
    ChargingProfilePurpose.TX,
)
ABS, REC, REL = ChargingProfileKind.ABSOLUTE, ChargingProfileKind.RECURRING, ChargingProfileKind.RELATIVE
W_PER_A = 230.0 * 3  # 16 A three-phase = 11040 W


def prof(
    pid: int,
    purpose: ChargingProfilePurpose = TXDEF,
    stack: int = 0,
    periods: Sequence[Tuple[int, float]] = ((0, 16),),
    *,
    kind: ChargingProfileKind = ABS,
    start: Optional[datetime] = NOW,
    duration: Optional[int] = None,
    unit: ChargingRateUnit = A,
    phases: Optional[int] = None,
    recurrency: Optional[RecurrencyKind] = None,
    valid_from: Optional[datetime] = None,
    valid_to: Optional[datetime] = None,
    tx_id: Optional[int] = None,
    min_rate: Optional[float] = None,
) -> ChargingProfile:
    sched = ChargingSchedule(
        unit=unit,
        periods=tuple(SchedulePeriod(s, lim, phases) for s, lim in periods),
        duration=duration,
        start_schedule=start if kind is not REL else None,
        min_charging_rate=min_rate,
    )
    return ChargingProfile(pid, stack, purpose, kind, sched, tx_id, recurrency, valid_from, valid_to)


def at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def store_with(*items: Tuple[int, ChargingProfile], connectors: int = 1, tx: Optional[int] = 1) -> ProfileStore:
    store = ProfileStore(connectors)
    for connector_id, profile in items:
        assert store.install(connector_id, profile, active_transaction=tx) is ProfileStatus.ACCEPTED
    return store


def amps(store: ProfileStore, t: datetime, connector_id: int = 1, **kw) -> Optional[float]:
    watts = limit_at(store, connector_id, t, **kw)
    return None if watts is None else round(watts / W_PER_A, 6)


def periods(schedule) -> list:
    return [(p.start_period, p.limit) for p in schedule.periods]


# ---------------------------------------------------------------------------------------------- C1


def test_c1_highest_stack_level_wins():
    store = store_with((1, prof(1, stack=0, periods=[(0, 32)])), (1, prof(2, stack=1, periods=[(0, 16)])))
    assert amps(store, NOW) == 16


def test_c1_boundary_higher_level_not_yet_valid_and_connector_shadowing():
    store = store_with(
        (1, prof(1, stack=0, periods=[(0, 32)])),
        (1, prof(2, stack=1, periods=[(0, 16)], valid_from=at(60))),
    )
    assert amps(store, at(59)) == 32
    assert amps(store, at(60)) == 16
    # connector-specific TxDefault replaces the connector-0 one with the same stackLevel (3.13.1)
    store2 = store_with((0, prof(3, stack=0, periods=[(0, 8)])), (1, prof(4, stack=0, periods=[(0, 20)])))
    assert amps(store2, NOW) == 20


# ---------------------------------------------------------------------------------------------- C2


def test_c2_fallback_after_duration():
    store = store_with((1, prof(1, stack=0, periods=[(0, 32)])), (1, prof(2, stack=1, duration=3600)))
    assert amps(store, at(3599)) == 16
    assert amps(store, at(3600)) == 32
    assert periods(composite_schedule(store, 1, NOW, 7200, A)) == [(0, 16), (3600, 32)]


def test_c2_fallback_after_valid_to_and_never_without_duration():
    store = store_with((1, prof(1, stack=0, periods=[(0, 32)])), (1, prof(2, stack=1, valid_to=at(600))))
    assert periods(composite_schedule(store, 1, NOW, 1200, A)) == [(0, 16), (600, 32)]
    store2 = store_with((1, prof(1, stack=0, periods=[(0, 32)])), (1, prof(2, stack=1, periods=[(0, 16), (60, 10)])))
    assert periods(composite_schedule(store2, 1, NOW, 86400, A)) == [(0, 16), (60, 10)]


# ---------------------------------------------------------------------------------------------- C3


def test_c3_absolute_counts_from_start_schedule():
    store = store_with((1, prof(1, start=at(-600), periods=[(0, 32), (900, 16)])))
    assert amps(store, at(299)) == 32
    assert amps(store, at(300)) == 16


def test_c3_boundary_future_start_schedule_not_active_before():
    store = store_with((1, prof(1, start=at(600))))
    assert limit_at(store, 1, at(599)) is None
    assert amps(store, at(600)) == 16
    assert periods(composite_schedule(store, 1, NOW, 1200, A)) == [(0, None), (600, 16)]


# ---------------------------------------------------------------------------------------------- C4


def daily_store(duration: Optional[int] = None) -> ProfileStore:
    # every day from 22:00: 10 A for 8 h, then 32 A
    p = prof(1, kind=REC, recurrency=RecurrencyKind.DAILY, start=at(-2 * 3600), duration=duration,
             periods=[(0, 10), (8 * 3600, 32)])
    return store_with((1, p))


def test_c4_daily_crosses_midnight_and_aligns_to_start_schedule():
    store = daily_store()
    assert amps(store, NOW) == 10  # 22:00 yesterday + 2 h
    assert amps(store, at(6 * 3600 - 1)) == 10
    assert amps(store, at(6 * 3600)) == 32
    assert amps(store, at(22 * 3600 - 1)) == 32
    assert amps(store, at(22 * 3600)) == 10  # restarts at 22:00
    sched = composite_schedule(store, 1, NOW, 86400, A)
    assert periods(sched) == [(0, 10), (6 * 3600, 32), (22 * 3600, 10)]


def test_c4_daily_duration_shorter_than_recurrence_falls_back():
    store = daily_store(duration=4 * 3600)
    assert amps(store, at(2 * 3600 - 1)) == 10
    assert limit_at(store, 1, at(2 * 3600)) is None


def test_c4_weekly_wraps_at_week_boundary():
    monday = datetime(2026, 11, 23, tzinfo=UTC)
    p = prof(1, kind=REC, recurrency=RecurrencyKind.WEEKLY, start=monday, periods=[(0, 10), (5 * 86400, 32)])
    store = store_with((1, p))
    assert amps(store, NOW) == 10  # Thursday
    assert amps(store, datetime(2026, 11, 28, tzinfo=UTC)) == 32  # Saturday
    assert amps(store, datetime(2026, 11, 29, 23, 59, 59, tzinfo=UTC)) == 32
    assert amps(store, datetime(2026, 11, 30, tzinfo=UTC)) == 10  # next Monday


def test_c4_recurring_without_start_schedule_anchors_at_utc_midnight():
    p = prof(1, kind=REC, recurrency=RecurrencyKind.DAILY, start=None, periods=[(0, 10), (12 * 3600, 20)])
    store = store_with((1, p))
    assert amps(store, at(12 * 3600 - 1)) == 10
    assert amps(store, at(12 * 3600)) == 20
    assert amps(store, at(24 * 3600)) == 10


# ---------------------------------------------------------------------------------------------- C5


def test_c5_relative_counts_from_transaction_start():
    store = store_with((1, prof(1, kind=REL, periods=[(0, 32), (3600, 16)])))
    txs = {1: ActiveTransaction(7, at(-1800))}
    assert amps(store, at(1799), transactions=txs) == 32
    assert amps(store, at(1800), transactions=txs) == 16
    assert periods(composite_schedule(store, 1, NOW, 7200, A, transactions=txs)) == [(0, 32), (1800, 16)]


def test_c5_relative_without_transaction_uses_window_start():
    store = store_with((1, prof(1, kind=REL, periods=[(0, 32), (3600, 16)])))
    assert periods(composite_schedule(store, 1, NOW, 7200, A)) == [(0, 32), (3600, 16)]
    assert amps(store, at(5000)) == 32  # limit_at window starts at `now` -> first period


# ---------------------------------------------------------------------------------------------- C6


def test_c6_valid_from_inclusive_valid_to_exclusive():
    store = store_with((1, prof(1, valid_from=at(100), valid_to=at(200))))
    assert limit_at(store, 1, at(99)) is None
    assert amps(store, at(100)) == 16
    assert amps(store, at(199)) == 16
    assert limit_at(store, 1, at(200)) is None
    assert periods(composite_schedule(store, 1, NOW, 300, A)) == [(0, None), (100, 16), (200, None)]


# ---------------------------------------------------------------------------------------------- C7


def test_c7_charge_point_max_caps_tx_default():
    store = store_with((0, prof(1, CPMAX, periods=[(0, 20)])), (1, prof(2, periods=[(0, 32)])))
    assert amps(store, NOW) == 20
    store.install(1, prof(2, periods=[(0, 10)]))
    assert amps(store, NOW) == 10


def test_c7_tx_profile_overrules_tx_default_then_capped():
    txs = {1: ActiveTransaction(5, at(-60))}
    store = store_with(
        (0, prof(1, CPMAX, periods=[(0, 20)])),
        (1, prof(2, periods=[(0, 10)])),
        (1, prof(3, TX, periods=[(0, 16)], tx_id=5)),
        tx=5,
    )
    assert amps(store, NOW, transactions=txs) == 16  # TxProfile beats the lower TxDefault
    store.install(1, prof(3, TX, periods=[(0, 32)], tx_id=5), active_transaction=5)
    assert amps(store, NOW, transactions=txs) == 20  # but never above ChargePointMax
    # boundary: TxProfile of another transaction is ignored -> TxDefault
    assert amps(store, NOW, transactions={1: ActiveTransaction(6, at(-60))}) == 10
    # boundary: TxProfile schedule ended -> TxDefault
    store.install(1, prof(3, TX, periods=[(0, 16)], duration=60, tx_id=5), active_transaction=5)
    assert amps(store, at(60), transactions=txs) == 10


# ---------------------------------------------------------------------------------------------- C8


def test_c8_connector_zero_sums_connectors_and_caps():
    store = store_with((0, prof(1, periods=[(0, 16)])), connectors=2)
    assert amps(store, NOW, connector_id=0) == 32
    store.install(0, prof(2, CPMAX, periods=[(0, 20)]))
    assert amps(store, NOW, connector_id=0) == 20
    assert amps(store, NOW, connector_id=1) == 16


def test_c8_boundary_unconstrained_connector_and_no_profiles():
    assert limit_at(ProfileStore(2), 0, NOW) is None
    store = store_with((1, prof(1, periods=[(0, 16)])), connectors=2)
    assert limit_at(store, 0, NOW) is None  # connector 2 unconstrained
    store.install(0, prof(2, CPMAX, periods=[(0, 40)]))
    assert amps(store, NOW, connector_id=0) == 40


# ---------------------------------------------------------------------------------------------- C9


def test_c9_min_charging_rate_does_not_alter_limit():
    store = store_with((1, prof(1, periods=[(0, 4)], min_rate=6.0)))
    assert amps(store, NOW) == 4


def test_c9_boundary_zero_limit_is_zero_not_unconstrained():
    store = store_with((1, prof(1, periods=[(0, 0)], min_rate=6.0)))
    assert limit_at(store, 1, NOW) == 0


# ---------------------------------------------------------------------------------------------- C10


def test_c10_amps_to_watts_phases_and_voltage():
    assert limit_at(store_with((1, prof(1))), 1, NOW) == 11040  # 3 phases assumed
    assert limit_at(store_with((1, prof(1, phases=1))), 1, NOW) == 3680
    assert limit_at(store_with((1, prof(1))), 1, NOW, voltage=200) == 9600
    # errata 3.34: more phases than the charger has -> limit applies to the phases available
    assert limit_at(store_with((1, prof(1))), 1, NOW, number_phases=1) == 3680


def test_c10_requested_unit_and_mixed_units():
    store = store_with((1, prof(1, unit=W, periods=[(0, 11000)])))
    assert composite_schedule(store, 1, NOW, 60).unit is W  # absent unit -> W
    assert periods(composite_schedule(store, 1, NOW, 60)) == [(0, 11000)]
    sched = composite_schedule(store, 1, NOW, 60, A)
    assert sched.periods == (CompositePeriod(0, round(11000 / 690, 6), 3),)
    mixed = store_with((0, prof(1, CPMAX, unit=W, periods=[(0, 7000)])), (1, prof(2, periods=[(0, 16)])))
    assert limit_at(mixed, 1, NOW) == 7000
    assert convert(convert(16, A, W, 1, 230), W, A, 1, 230) == 16


# ---------------------------------------------------------------------------------------------- C11


def test_c11_merges_equal_neighbours_and_truncates_to_duration():
    store = store_with(
        (1, prof(1, periods=[(0, 16), (100, 16), (200, 8)])),
        (1, prof(2, stack=1, start=at(200), periods=[(0, 8)])),  # same value, extra boundary at 200
    )
    assert periods(composite_schedule(store, 1, NOW, 300, A)) == [(0, 16), (200, 8)]
    assert periods(composite_schedule(store, 1, NOW, 150, A)) == [(0, 16)]
    assert periods(composite_schedule(store, 1, NOW, 200, A)) == [(0, 16)]


def test_c11_boundary_sub_second_boundary_and_bad_arguments():
    store = store_with((1, prof(1, start=at(-0.5), periods=[(0, 16), (10, 8)])))
    sched = composite_schedule(store, 1, NOW, 60, A)
    assert periods(sched) == [(0, 16), (10, 8)]  # 9.5 s rounded up to the next whole second
    starts = [p.start_period for p in sched.periods]
    assert starts == sorted(set(starts))
    with pytest.raises(ValueError):
        composite_schedule(store, 2, NOW, 60)
    with pytest.raises(ValueError):
        composite_schedule(store, 1, NOW, 0)


# ---------------------------------------------------------------------------------------------- C12


def _random_store(rng: random.Random) -> ProfileStore:
    store = ProfileStore(2)
    for pid in range(rng.randint(1, 6)):
        purpose = rng.choice([CPMAX, TXDEF, TX])
        connector = 0 if purpose is CPMAX else rng.choice([0, 1, 2] if purpose is TXDEF else [1, 2])
        kind = rng.choice([ABS, REC, REL] if purpose is not CPMAX else [ABS, REC])
        starts = sorted({0, *rng.sample(range(60, 7200, 60), rng.randint(0, 3))})
        profile = prof(
            pid,
            purpose,
            rng.randint(0, 3),
            [(s, rng.choice([0, 6, 10, 16, 32])) for s in starts],
            kind=kind,
            start=at(rng.randint(-7200, 3600)),
            duration=rng.choice([None, 1800, 5400]),
            unit=rng.choice([A, W]),
            phases=rng.choice([None, 1, 3]),
            recurrency=rng.choice(list(RecurrencyKind)) if kind is REC else None,
            valid_from=rng.choice([None, at(rng.randint(-3600, 3600))]),
            valid_to=rng.choice([None, at(rng.randint(3601, 9000))]),
        )
        store.install(connector, profile, active_transaction=connector)  # tx id == connector id
    return store


@pytest.mark.parametrize("seed", range(40))
def test_c12_limit_at_equals_composite_value(seed):
    rng = random.Random(seed)
    store = _random_store(rng)
    txs = {c: ActiveTransaction(c, at(-rng.randint(0, 3600))) for c in (1, 2)}
    for connector in (0, 1, 2):
        sched = composite_schedule(store, connector, NOW, 7200, W, transactions=txs)
        for _ in range(20):
            offset = rng.randrange(7200)
            expected = [p.limit for p in sched.periods if p.start_period <= offset][-1]
            assert limit_at(store, connector, at(offset), transactions=txs) == expected


# ---------------------------------------------------------------------------------------------- C13


def test_c13_validation_rejects():
    store = ProfileStore(2)
    assert store.install(1, prof(1, TX)) is ProfileStatus.REJECTED  # no transaction
    assert store.install(1, prof(1, CPMAX)) is ProfileStatus.REJECTED  # ChargePointMax off connector 0
    assert store.install(0, prof(1, TX), active_transaction=1) is ProfileStatus.REJECTED
    assert store.install(1, prof(1, TX, tx_id=9), active_transaction=1) is ProfileStatus.REJECTED
    assert store.install(3, prof(1)) is ProfileStatus.REJECTED  # unknown connector
    assert store.install(1, prof(1, periods=[(10, 16)])) is ProfileStatus.REJECTED  # first period != 0
    assert store.install(1, prof(1, periods=[(0, 16), (0, 8)])) is ProfileStatus.REJECTED
    assert store.install(1, prof(1, kind=REC)) is ProfileStatus.REJECTED  # no recurrencyKind
    assert store.profiles() == ()
    assert store.install(1, prof(1, TX), active_transaction=1) is ProfileStatus.ACCEPTED
    assert store.install(0, prof(2, CPMAX)) is ProfileStatus.ACCEPTED


def test_c13_replacement_by_id_and_by_stack_level_purpose():
    store = ProfileStore(2)
    store.install(1, prof(1, stack=0, periods=[(0, 32)]))
    store.install(1, prof(1, stack=2, periods=[(0, 20)]))  # same id replaces
    assert [ip.profile.stack_level for ip in store.profiles()] == [2]
    store.install(1, prof(2, stack=2, periods=[(0, 10)]))  # same stack+purpose replaces
    assert [ip.profile.profile_id for ip in store.profiles()] == [2]
    # boundary: other connector or other purpose stacks next to it
    store.install(2, prof(3, stack=2))
    store.install(1, prof(4, TX, stack=2), active_transaction=1)
    assert sorted(ip.profile.profile_id for ip in store.profiles()) == [2, 3, 4]
    assert store.end_transaction(1) == 1
    assert sorted(ip.profile.profile_id for ip in store.profiles()) == [2, 3]


# ---------------------------------------------------------------------------------------------- C14


def _clear_store() -> ProfileStore:
    return store_with(
        (0, prof(1, CPMAX, stack=0)),
        (0, prof(2, TXDEF, stack=1)),
        (1, prof(3, TXDEF, stack=1)),
        (1, prof(4, TX, stack=2)),
        connectors=2,
    )


@pytest.mark.parametrize(
    "kw, remaining",
    [
        ({}, []),
        ({"profile_id": 3}, [1, 2, 4]),
        ({"profile_id": 3, "connector_id": 0}, [1, 2, 4]),  # id given -> other fields ignored
        ({"connector_id": 1}, [1, 2]),
        ({"purpose": TXDEF}, [1, 4]),
        ({"stack_level": 1, "connector_id": 0}, [1, 3, 4]),
        ({"purpose": TXDEF, "stack_level": 1, "connector_id": 1}, [1, 2, 4]),
    ],
)
def test_c14_clear_filters(kw, remaining):
    store = _clear_store()
    assert store.clear(**kw) is ClearStatus.ACCEPTED
    assert sorted(ip.profile.profile_id for ip in store.profiles()) == remaining


@pytest.mark.parametrize(
    "kw", [{"profile_id": 99}, {"connector_id": 2}, {"purpose": CPMAX, "connector_id": 1}, {"stack_level": 7}]
)
def test_c14_no_match_is_unknown(kw):
    store = _clear_store()
    assert store.clear(**kw) is ClearStatus.UNKNOWN
    assert len(store.profiles()) == 4


def test_c8_connector_zero_includes_idle_connector_tx_default():
    # connector 1 charges under a 10 A TxProfile; idle connector 2 contributes its 16 A TxDefault
    store = store_with((0, prof(1, periods=[(0, 16)])), (1, prof(2, TX, periods=[(0, 10)])), connectors=2)
    txs = {1: ActiveTransaction(1, NOW)}
    assert amps(store, NOW, connector_id=1, transactions=txs) == 10
    assert amps(store, NOW, connector_id=2, transactions=txs) == 16
    assert amps(store, NOW, connector_id=0, transactions=txs) == 26
    # idle connector without any TxDefault -> the sum is unconstrained
    store.clear(profile_id=1)
    assert limit_at(store, 0, NOW, transactions=txs) is None


@pytest.mark.parametrize(
    "profile",
    [
        prof(1, periods=[(0, -1)]),  # negative limit
        prof(1, periods=[(0, 16), (-5, 8)]),  # negative start_period
        prof(1, phases=0),
        prof(1, phases=4),
        prof(1, duration=0),
        prof(1, duration=-60),
        prof(1, min_rate=-1.0),
    ],
)
def test_c13_validation_rejects_value_ranges(profile):
    store = ProfileStore(1)
    assert store.install(1, profile) is ProfileStatus.REJECTED
    assert store.profiles() == ()


def test_c13_validation_accepts_range_boundaries():
    store = ProfileStore(1)
    assert store.install(1, prof(1, periods=[(0, 0)], phases=1, duration=1, min_rate=0.0)) is ProfileStatus.ACCEPTED
    assert store.install(1, prof(2, stack=1, phases=3)) is ProfileStatus.ACCEPTED


def test_c13_validate_does_not_install_and_checks_remote_start_profiles():
    store = ProfileStore(2)
    assert store.validate(1, prof(1)) is ProfileStatus.ACCEPTED
    assert store.validate(1, prof(1, TX)) is ProfileStatus.REJECTED  # no transaction
    assert store.validate(1, prof(1, TX), active_transaction=5) is ProfileStatus.ACCEPTED
    assert store.profiles() == ()
    assert store.validate(1, prof(1, TX), for_remote_start=True) is ProfileStatus.ACCEPTED
    assert store.validate(0, prof(1, TX), for_remote_start=True) is ProfileStatus.REJECTED
    assert store.validate(1, prof(1, TXDEF), for_remote_start=True) is ProfileStatus.REJECTED
    assert store.validate(1, prof(1, TX, tx_id=3), for_remote_start=True) is ProfileStatus.REJECTED
    assert store.validate(1, prof(1, TX, periods=[(10, 16)]), for_remote_start=True) is ProfileStatus.REJECTED
    assert store.profiles() == ()
