"""OCPP 2.0.1 ProfileStore and composite evaluator (``protocol/v201/profiles.py`` / ``composite.py``, M5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sme_ocpp_sim.config import SimConfig
from sme_ocpp_sim.engine import OcppEngine
from sme_ocpp_sim.protocol.v201 import models as m
from sme_ocpp_sim.protocol.v201.adapter import charging_profile, charging_profile_type
from sme_ocpp_sim.protocol.v201.composite import ActiveTransaction, composite_schedule, limit_at
from sme_ocpp_sim.protocol.v201.profiles import (ChargingProfile, ChargingProfileKind, ChargingProfilePurpose,
                                                 ChargingRateUnit, ChargingSchedule, ClearStatus, Limits,
                                                 ProfileStatus, ProfileStore, RecurrencyKind, SchedulePeriod)

P = ChargingProfilePurpose
OK, NO = ProfileStatus.ACCEPTED, ProfileStatus.REJECTED
T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
TX = "6f1c2d3e-0000-4000-8000-000000000001"


def _p(limit, purpose=P.tx_default_profile, pid=1, stack=0, unit=ChargingRateUnit.w, tx=None, periods=None, **kw):
    # A ChargingStationMaxProfile cannot be Relative (K01.FR.38): Absolute from a day before T0.
    absolute = purpose is P.charging_station_max_profile
    sched = ChargingSchedule(1, unit, periods or (SchedulePeriod(0, limit),),
                             start_schedule=T0 - timedelta(days=1) if absolute else None)
    kind = ChargingProfileKind.absolute if absolute else ChargingProfileKind.relative
    return ChargingProfile(pid, stack, purpose, kind, sched, transaction_id=tx, **kw)


def _lim(store, evse, txs=None):
    return limit_at(store, evse, T0, transactions=txs or {}, number_phases=1, voltage=200)


# --------------------------------------------------------------------------------------------- ProfileStore


def test_purpose_rules():
    s = ProfileStore(2)
    assert s.install(1, _p(1, P.charging_station_max_profile)) is NO   # station max only on evseId 0
    assert s.install(0, _p(1, P.charging_station_max_profile)) is OK
    assert s.install(0, _p(1, P.charging_station_external_constraints, pid=2)) is NO  # not from the CSMS
    assert s.install(0, _p(1, P.charging_station_external_constraints, pid=2), external=True) is OK
    assert s.install(0, _p(1, P.tx_default_profile, pid=3), external=True) is NO   # operator: external only
    assert s.install(1, _p(1, P.tx_profile, pid=4)) is NO                     # no transaction
    assert s.install(1, _p(1, P.tx_profile, pid=4), active_transaction=TX) is NO  # no transactionId
    assert s.install(1, _p(1, P.tx_profile, pid=4, tx="x"), active_transaction=TX) is NO
    assert s.install(0, _p(1, P.tx_profile, pid=4, tx=TX), active_transaction=TX) is NO  # not on evseId 0
    assert s.install(1, _p(1, P.tx_profile, pid=4, tx=TX), active_transaction=TX) is OK
    assert s.install(3, _p(1, pid=5)) is NO                                   # unknown EVSE
    assert s.validate(1, _p(1, P.tx_profile, pid=6), for_remote_start=True) is OK
    assert s.validate(1, _p(1, P.tx_profile, pid=6, tx=TX), for_remote_start=True) is NO
    assert s.validate(1, _p(1, pid=6), for_remote_start=True) is NO


def test_same_id_replaces_and_stack_level_conflict_is_rejected():
    s = ProfileStore(2)
    assert s.install(0, _p(1, pid=1)) is OK
    assert s.install(1, _p(2, pid=1)) is OK  # same id: replaced, also on another EVSE
    assert [(ip.evse_id, ip.profile.schedule.periods[0].limit) for ip in s.profiles()] == [(1, 2)]
    assert s.install(1, _p(3, pid=2)) is NO  # same EVSE, purpose and stackLevel, overlapping validity
    assert s.install(1, _p(3, pid=2, stack=1)) is OK
    # non-overlapping validity periods do not conflict
    s2 = ProfileStore(1)
    assert s2.install(0, _p(1, pid=1, valid_to=T0)) is OK
    assert s2.install(0, _p(1, pid=2, valid_from=T0)) is OK
    assert s2.install(0, _p(1, pid=3, valid_from=T0 - timedelta(hours=1))) is NO


def test_external_ids_are_kept_apart_from_csms_ids():
    s = ProfileStore(1)
    assert s.install(0, _p(1, P.charging_station_external_constraints, pid=7), external=True) is OK
    assert s.install(0, _p(2, pid=7)) is OK  # the CSMS id 7 does not replace the operator's 7
    assert len(s.profiles()) == 2


@pytest.mark.parametrize("periods,ok", [
    ((SchedulePeriod(0, 1), SchedulePeriod(10, 2)), True),
    ((SchedulePeriod(5, 1),), False),
    ((SchedulePeriod(0, 1), SchedulePeriod(0, 2)), False),
    ((SchedulePeriod(0, -1),), False),
    ((SchedulePeriod(0, 1, number_phases=4),), False),
    ((SchedulePeriod(0, 1, number_phases=1, phase_to_use=2),), True),
    ((SchedulePeriod(0, 1, number_phases=3, phase_to_use=2),), False),
    ((SchedulePeriod(0, 1, number_phases=1, phase_to_use=4),), False),
])
def test_schedule_checks(periods, ok):
    assert (ProfileStore(1).install(0, _p(0, periods=periods)) is OK) == ok


def test_device_model_limits_and_other_checks():
    lim = Limits(max_stack_level=2, max_periods=2, units=frozenset({ChargingRateUnit.a}))
    s = ProfileStore(1)
    assert s.validate(0, _p(1, unit=ChargingRateUnit.a, stack=2), limits=lim) is OK
    assert s.validate(0, _p(1, unit=ChargingRateUnit.a, stack=3), limits=lim) is NO
    assert s.validate(0, _p(1), limits=lim) is NO  # W not in RateUnit
    three = tuple(SchedulePeriod(i * 10, 1) for i in range(3))
    assert s.validate(0, _p(1, unit=ChargingRateUnit.a, periods=three), limits=lim) is NO
    assert s.validate(0, _p(1, valid_from=T0, valid_to=T0)) is NO
    assert s.validate(0, replace(_p(1), kind=ChargingProfileKind.recurring)) is NO  # no recurrencyKind
    assert s.validate(0, _p(1, stack=-1)) is NO
    bad_second = (ChargingSchedule(2, ChargingRateUnit.w, (SchedulePeriod(3, 1),)),)
    assert s.validate(0, _p(1, other_schedules=bad_second)) is NO  # every schedule is checked


def test_clear_criteria():
    s = ProfileStore(2)
    s.install(0, _p(1, pid=1))
    s.install(1, _p(1, pid=2, stack=1))
    s.install(0, _p(1, P.charging_station_max_profile, pid=3))
    s.install(0, _p(1, P.charging_station_external_constraints, pid=-1), external=True)
    assert s.clear(profile_id=2, evse_id=0) is ClearStatus.ACCEPTED  # id given: the criteria are ignored
    assert s.clear(evse_id=0, purpose=P.tx_default_profile, stack_level=1) is ClearStatus.UNKNOWN
    assert s.clear(purpose=P.charging_station_external_constraints) is ClearStatus.UNKNOWN
    assert s.clear(profile_id=-1) is ClearStatus.UNKNOWN
    assert s.clear() is ClearStatus.ACCEPTED
    assert [ip.profile.profile_id for ip in s.profiles()] == [-1]  # only the operator's external constraints
    assert s.clear_external(0) and not s.clear_external(0)


def test_end_transaction_drops_tx_profiles():
    s = ProfileStore(1)
    s.install(1, _p(1, P.tx_profile, tx=TX), active_transaction=TX)
    s.install(1, _p(1, pid=2))
    assert s.end_transaction(1) == 1 and [ip.profile.profile_id for ip in s.profiles()] == [2]


# --------------------------------------------------------------------------------------------- composite


def test_external_constraints_take_part_in_the_minimum():
    s = ProfileStore(2)
    s.install(0, _p(5000, P.charging_station_max_profile))
    s.install(0, _p(4000, pid=2))  # TxDefault for every EVSE
    assert _lim(s, 1) == 4000
    s.install(1, _p(3000, P.charging_station_external_constraints, pid=-2), external=True)
    assert _lim(s, 1) == 3000 and _lim(s, 2) == 4000
    assert _lim(s, 0) == 5000  # min(5000, 3000 + 4000)
    s.install(0, _p(2500, P.charging_station_external_constraints, pid=-1), external=True)
    assert _lim(s, 1) == 2500 and _lim(s, 2) == 2500 and _lim(s, 0) == 2500


def test_tx_profile_overrules_tx_default_for_its_string_transaction():
    s = ProfileStore(1)
    s.install(1, _p(4000, pid=2))
    s.install(1, _p(1000, P.tx_profile, tx=TX), active_transaction=TX)
    assert _lim(s, 1, {1: ActiveTransaction(TX, T0)}) == 1000
    assert _lim(s, 1, {1: ActiveTransaction("other", T0)}) == 4000
    assert _lim(s, 1) == 4000


def test_evse_zero_sum_capped_by_station_max():
    s = ProfileStore(2)
    s.install(0, _p(1000, pid=1))  # TxDefault for both EVSEs: sum 2000
    assert _lim(s, 0) == 2000
    s.install(0, _p(1500, P.charging_station_max_profile, pid=2))
    assert _lim(s, 0) == 1500 and _lim(s, 1) == 1000


def test_recurring_daily_profile_restarts_every_day():
    s = ProfileStore(1)
    start = T0.replace(hour=0)
    periods = (SchedulePeriod(0, 1000), SchedulePeriod(12 * 3600, 2000))
    base = _p(0, periods=periods, recurrency_kind=RecurrencyKind.daily)
    daily = replace(base, kind=ChargingProfileKind.recurring, schedule=replace(base.schedule, start_schedule=start))
    assert s.install(0, daily) is OK
    cs = composite_schedule(s, 1, T0 - timedelta(hours=1), 2 * 86400, number_phases=1, voltage=200)
    assert [(p.start_period, p.limit) for p in cs.periods] == [
        (0, 1000), (3600, 2000), (13 * 3600, 1000), (25 * 3600, 2000), (37 * 3600, 1000)]  # window from 11:00


def test_evse_zero_sum_is_unconstrained_when_one_evse_is():
    s = ProfileStore(2)
    s.install(1, _p(1000, pid=1))
    assert _lim(s, 0) is None and _lim(s, 1) == 1000 and _lim(s, 2) is None


def test_more_than_one_schedule_is_rejected_and_composite_in_amps():
    s = ProfileStore(1)
    second = (ChargingSchedule(2, ChargingRateUnit.w, (SchedulePeriod(0, 100),)),)
    periods = (SchedulePeriod(0, 16), SchedulePeriod(30, 8))
    assert s.install(0, _p(16, unit=ChargingRateUnit.a, periods=periods, other_schedules=second)) is NO  # K01.FR.34
    assert s.install(0, _p(16, unit=ChargingRateUnit.a, periods=periods)) is OK
    cs = composite_schedule(s, 1, T0, 60, ChargingRateUnit.a, number_phases=1, voltage=200)
    assert [(p.start_period, p.limit, p.number_phases) for p in cs.periods] == [(0, 16, 1), (30, 8, 1)]
    assert cs.evse_id == 1 and cs.unit is ChargingRateUnit.a
    with pytest.raises(ValueError):
        composite_schedule(s, 2, T0, 60)


def test_wire_round_trip_keeps_every_schedule():
    wire = m.ChargingProfileType.model_validate({
        "id": 4, "stackLevel": 1, "chargingProfilePurpose": "TxDefaultProfile", "chargingProfileKind": "Absolute",
        "chargingSchedule": [
            {"id": 1, "startSchedule": "2026-11-27T12:00:00Z", "chargingRateUnit": "A",
             "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 10, "numberPhases": 1, "phaseToUse": 2}]},
            {"id": 2, "chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 500}]}]})
    profile = charging_profile(wire)
    assert profile.schedule.id == 1 and profile.schedule.start_schedule == T0
    assert profile.schedule.periods == (SchedulePeriod(0, 10, 1, 2),)
    assert [x.id for x in profile.other_schedules] == [2]
    assert charging_profile_type(profile).model_dump(by_alias=True, exclude_none=True, mode="json") == \
        wire.model_dump(by_alias=True, exclude_none=True, mode="json")


# --------------------------------------------------------------------------------------------- Web API


def test_web_external_limit_201_and_not_supported_for_16(tmp_path):
    from fastapi.testclient import TestClient

    from sme_ocpp_sim.web import create_app

    cfg = SimConfig.model_validate({"csms_defaults": {"url": "ws://127.0.0.1:9/ocpp"}, "chargers": [
        {"id": "A", "connectors": 2, "ocpp_version": "2.0.1"}, {"id": "B", "ocpp_version": "1.6"}]})
    with TestClient(create_app(OcppEngine(cfg), tmp_path / "sim.yaml")) as client:
        r = client.post("/api/chargers/A/external-limit", json={"connector": 1, "limit_w": 3000})
        assert r.status_code == 200, r.text
        [p] = client.get("/api/chargers/A/profiles").json()["profiles"]
        assert p["connector_id"] == 1 and p["purpose"] == "ChargingStationExternalConstraints"
        assert p["schedule"]["unit"] == "W" and len(p["schedule"]["periods"]) == 1  # the Web UI's fields
        comp = client.get("/api/chargers/A/composite?connector=1&unit=W").json()
        assert comp["periods"][0]["limit"] == 3000.0 and comp["duration"] == 3600 and comp["start"]
        # EVSE 2 is unconstrained, so the station is too: 2 x the hardware maximum
        assert client.get("/api/chargers/A/composite?connector=0").json()["periods"][0]["limit"] == 12000.0
        assert client.post("/api/chargers/A/external-limit", json={"connector": 1}).status_code == 200
        assert client.get("/api/chargers/A/profiles").json() == {"profiles": []}
        assert client.post("/api/chargers/A/external-limit", json={"connector": 3, "limit_w": 1}).status_code == 400
        assert client.post("/api/chargers/A/external-limit", json={"limit_w": -1}).status_code == 400
        r = client.post("/api/chargers/B/external-limit", json={"connector": 1, "limit_w": 3000})
        assert r.status_code == 400 and "not supported by OCPP 1.6" in r.text


# --------------------------------------------------------------------------------------------- batch B rules (#42)


def test_tx_default_on_evse_zero_and_an_evse_with_the_same_stack_level_is_a_duplicate():
    """K01.FR.52 / FR.53; the same id still replaces (K01.FR.05)."""
    s = ProfileStore(2)
    assert s.install(1, _p(1, pid=1)) is OK
    assert s.reject_reason(0, _p(1, pid=2)) == "DuplicateProfile"  # K01.FR.52
    assert s.install(0, _p(1, pid=2, stack=1)) is OK
    assert s.reject_reason(2, _p(1, pid=3, stack=1)) == "DuplicateProfile"  # K01.FR.53
    assert s.install(2, _p(1, pid=2, stack=1)) is OK  # same id: moves from evseId 0 to EVSE 2
    assert sorted((ip.evse_id, ip.profile.profile_id) for ip in s.profiles()) == [(1, 1), (2, 2)]


def test_evse_and_station_tx_defaults_are_stacked_by_stack_level():
    """K01.FR.14 / FR.15: with different stackLevels both apply to the EVSE, the higher one prevails."""
    s = ProfileStore(1)
    s.install(1, _p(3000, pid=1, stack=0))
    s.install(0, _p(2000, pid=2, stack=1))
    assert _lim(s, 1) == 2000
    s.clear(profile_id=2)
    assert _lim(s, 1) == 3000


def test_tx_profile_conflict_ignores_the_validity_period():
    """K01.FR.39 (TxProfile: stackLevel + transactionId) vs K01.FR.06 (others: overlapping validity)."""
    s = ProfileStore(1)
    assert s.install(1, _p(1, P.tx_profile, pid=1, tx=TX, valid_to=T0), active_transaction=TX) is OK
    later = _p(1, P.tx_profile, pid=2, tx=TX, valid_from=T0)
    assert s.reject_reason(1, later, active_transaction=TX) == "DuplicateProfile"


def test_station_max_cannot_be_relative():
    s = ProfileStore(1)
    relative = replace(_p(1, P.charging_station_max_profile), kind=ChargingProfileKind.relative,
                       schedule=ChargingSchedule(1, ChargingRateUnit.w, (SchedulePeriod(0, 1),)))
    assert s.validate(0, relative) is NO  # K01.FR.38
    assert s.validate(0, _p(1, P.charging_station_max_profile)) is OK


def test_start_schedule_presence_follows_the_kind():
    """K01.FR.40 (Absolute / Recurring need startSchedule), K01.FR.41 (Relative must not have one)."""
    s = ProfileStore(1)
    base = _p(1)
    with_start = replace(base.schedule, start_schedule=T0)
    assert s.validate(0, replace(base, kind=ChargingProfileKind.absolute)) is NO
    assert s.validate(0, replace(base, kind=ChargingProfileKind.absolute, schedule=with_start)) is OK
    assert s.validate(0, replace(base, kind=ChargingProfileKind.recurring, recurrency_kind=RecurrencyKind.daily)) is NO
    assert s.validate(0, replace(base, kind=ChargingProfileKind.recurring, recurrency_kind=RecurrencyKind.daily,
                                 schedule=with_start)) is OK
    assert s.validate(0, replace(base, schedule=with_start)) is NO  # Relative with startSchedule


def test_reason_codes():
    s = ProfileStore(1)
    ext = _p(1, P.charging_station_external_constraints)
    assert s.reject_reason(0, ext) == "UnsupportedPurpose"  # errata K01.FR.62
    assert s.reject_reason(0, _p(1), limits=Limits(units=frozenset({ChargingRateUnit.a}))) == "UnsupportedRateUnit"
    assert s.reject_reason(1, _p(1, P.tx_profile, tx=TX)) == "TxNotFound"  # K01.FR.09
    assert s.reject_reason(0, _p(1, P.tx_profile, tx=TX), active_transaction=TX) == "InvalidProfile"  # K01.FR.16
    assert s.reject_reason(0, _p(1, stack=-1)) == "InvalidProfile"
    assert s.reject_reason(0, _p(1)) is None