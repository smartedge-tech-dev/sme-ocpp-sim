"""OCPP 2.1 smart charging and V2X basics (v0.3.1, 2.1-M2).

Unit level: the bidirectional power model, the 2.1 composite evaluator and the 2.1 SetChargingProfile checks.
Closed loop: a 2.1 charger against the Mini CSMS (dynamic profiles, discharge, priority charging); the station's
answers and the Mini CSMS answers are checked against the bundled 2.1 JSON schemas.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from test_mini_csms import _cfg, _conn, _until, _with_charger, _with_csms
from test_v21 import _validator

from sme_ocpp_sim.charging import power
from sme_ocpp_sim.charging.ev import EvModel
from sme_ocpp_sim.csms.handlers import _pull_dynamic, remember_dynamic
from sme_ocpp_sim.csms.session import CpSession
from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.protocol.v201.composite import ActiveTransaction
from sme_ocpp_sim.protocol.v21 import models as m
from sme_ocpp_sim.protocol.v21.composite import composite_schedule, setting_at
from sme_ocpp_sim.protocol.v21.profiles import (MODE, ProfileStore21, charging_profile21, reject_reason_21)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
EV = EvModel(id="test", capacity_kwh=40.0, max_ac_w=7400.0, max_current_a=32.0, phases=1)
SUPPLY = power.AcSupply(voltage_v=230.0, phases=1)
ALL_MODES = tuple(x.value for x in MODE)


# ---------------------------------------------------------------------- physics


def _run(state, seconds, **kw):
    for _ in range(seconds):
        state = power.step(state, dt_s=1.0, ev=EV, target_soc_pct=100.0, evse_limit_w=None, evse_max_w=7400.0,
                           supply=SUPPLY, **kw)
    return state


def test_discharge_negative_power_export_register_and_soc():
    s = _run(power.PowerState(soc_pct=50.0), 3600, setpoint_w=-3000.0)
    assert s.power_w == -3000.0 and s.suspension is None
    # (the first step ramps from 0 W: half a second at 3 kW less)
    assert s.energy_wh == 0.0 and s.energy_export_wh == pytest.approx(3000.0, abs=0.5)
    # 3 kWh out of the battery at 90 % efficiency: 3 / 0.9 / 40 kWh = 8.33 %
    assert s.soc_pct == pytest.approx(50.0 - 3000.0 / 0.9 / 400.0, abs=0.01)
    r = power.meter_reading(s, EV, SUPPLY)
    assert r.power_active_import_w == 0.0 and r.current_import_a == 0.0
    assert r.power_active_export_w == 3000.0 and r.current_export_a == pytest.approx(3000.0 / 230.0)
    assert r.energy_active_export_register_wh == s.energy_export_wh


def test_discharge_stops_at_the_minimum_soc_and_charging_setpoints_cap():
    s = _run(power.PowerState(soc_pct=power.DISCHARGE_MIN_SOC_PCT), 1, setpoint_w=-3000.0)
    assert s.power_w == 0.0 and s.suspension == "SuspendedEV"
    s = _run(power.PowerState(soc_pct=50.0), 1, setpoint_w=2000.0)
    assert s.power_w == 2000.0  # a positive setpoint caps the charging target
    s = _run(power.PowerState(soc_pct=50.0), 1, setpoint_w=0.0)
    assert s.power_w == 0.0 and s.suspension == "SuspendedEVSE"  # Idle
    assert _run(power.PowerState(soc_pct=50.0), 10) == _run(power.PowerState(soc_pct=50.0), 10, setpoint_w=None)


def test_a_step_through_zero_splits_import_and_export():
    s = power.PowerState(soc_pct=50.0, power_w=2000.0)
    s = power.step(s, dt_s=3600.0, ev=EV, target_soc_pct=100.0, evse_limit_w=None, evse_max_w=7400.0,
                   supply=SUPPLY, ramp_down_w_per_s=4000.0 / 3600.0, setpoint_w=-2000.0)
    assert s.power_w == pytest.approx(-2000.0)
    assert s.energy_wh == pytest.approx(500.0) and s.energy_export_wh == pytest.approx(500.0)


# ---------------------------------------------------------------------- composite / checks


def _wire(periods, purpose="TxProfile", kind="Relative", pid=1, stack=0, tx=None, **kw):
    sched = {"id": 1, "chargingRateUnit": "W", "chargingSchedulePeriod": periods}
    sched.update(kw.pop("schedule", {}))
    if kind in ("Absolute",):
        sched.setdefault("startSchedule", "2026-01-01T00:00:00Z")
    p = {"id": pid, "stackLevel": stack, "chargingProfilePurpose": purpose, "chargingProfileKind": kind,
         "chargingSchedule": [sched], **kw}
    if tx:
        p["transactionId"] = tx
    return m.ChargingProfileType.model_validate(p)


def _store(*profiles):
    store = ProfileStore21(2)
    for evse, wire in profiles:
        profile, wire = charging_profile21(wire, NOW)
        assert store.reject_reason(evse, profile, active_transaction="TX") is None
        store.install(evse, profile, active_transaction="TX", wire=wire)
    return store


TXS = {1: ActiveTransaction("TX", NOW)}


def test_composite_reports_operation_mode_discharge_limit_and_setpoint():
    store = _store((1, _wire([
        {"startPeriod": 0, "limit": 7000},
        {"startPeriod": 600, "operationMode": "CentralSetpoint", "setpoint": -6000, "dischargeLimit": -4000,
         "limit": 7000},
        {"startPeriod": 1200, "operationMode": "Idle"}], tx="TX")))
    sched = composite_schedule(store, 1, NOW, 1800, transactions=TXS)
    assert [(p.start_period, p.limit, p.discharge_limit, p.setpoint, p.operation_mode) for p in sched.periods] == [
        (0, 7000.0, None, None, MODE.charging_only),
        (600, 7000.0, -4000.0, -4000.0, MODE.central_setpoint),  # setpoint capped at dischargeLimit (V2X.07)
        (1200, None, None, None, MODE.idle)]
    assert setting_at(store, 1, NOW + timedelta(seconds=700), transactions=TXS).setpoint == -4000.0
    assert setting_at(store, 1, NOW + timedelta(seconds=1300), transactions=TXS).setpoint == 0.0  # Idle


def test_external_setpoint_takes_precedence_and_local_generation_adds():
    store = _store(
        (1, _wire([{"startPeriod": 0, "operationMode": "ExternalSetpoint", "limit": 5000}], tx="TX")),
        (0, _wire([{"startPeriod": 0, "limit": 4000}], purpose="ChargingStationMaxProfile", kind="Absolute",
                  pid=2)),
        (0, _wire([{"startPeriod": 0, "limit": 1500}], purpose="LocalGeneration", kind="Absolute", pid=3)))
    s = setting_at(store, 1, NOW, transactions=TXS)
    assert s.limit == 5500.0 and s.setpoint is None and s.operation_mode is MODE.external_setpoint
    ext, wire = charging_profile21(_wire([{"startPeriod": 0, "operationMode": "ExternalSetpoint",
                                           "setpoint": -2500}], purpose="ChargingStationExternalConstraints",
                                         kind="Absolute", pid=9), NOW)
    store.install(1, ext, external=True, wire=wire)
    assert setting_at(store, 1, NOW, transactions=TXS).setpoint == -2500.0


def test_limit_at_soc_caps_above_the_threshold():
    store = _store((1, _wire([{"startPeriod": 0, "limit": 7000}], tx="TX",
                             schedule={"limitAtSoC": {"soc": 80, "limit": 2000}})))
    assert setting_at(store, 1, NOW, transactions=TXS, soc={1: 79.0}).limit == 7000.0
    assert setting_at(store, 1, NOW, transactions=TXS, soc={1: 80.0}).limit == 2000.0


def test_max_offline_duration_falls_back_and_invalid_after_offline_drops():
    v2x = _wire([{"startPeriod": 0, "operationMode": "CentralSetpoint", "setpoint": -3000}], tx="TX", stack=2, pid=2,
                maxOfflineDuration=60, invalidAfterOfflineDuration=True)
    store = _store((1, _wire([{"startPeriod": 0, "limit": 3000}], tx="TX", stack=1)), (1, v2x))
    assert setting_at(store, 1, NOW, transactions=TXS).setpoint == -3000.0
    store.go_offline(NOW)
    assert setting_at(store, 1, NOW + timedelta(seconds=60), transactions=TXS).setpoint == -3000.0
    s = setting_at(store, 1, NOW + timedelta(seconds=61), transactions=TXS)
    assert s.setpoint is None and s.limit == 3000.0 and s.operation_mode is MODE.charging_only
    assert store.go_online(NOW + timedelta(seconds=90)) == (2,)
    assert [ip.profile.profile_id for ip in store.profiles()] == [1]


@pytest.mark.parametrize("wire, kw, reason", [
    (dict(periods=[{"startPeriod": 0, "limit": 1}, {"startPeriod": 10, "limit": 2}], kind="Dynamic"), {},
     "InvalidSchedule"),
    (dict(periods=[{"startPeriod": 0, "limit": 1}], dynUpdateInterval=10), {}, "InvalidProfile"),
    (dict(periods=[{"startPeriod": 0, "setpoint": 1, "operationMode": "CentralSetpoint"}],
          purpose="ChargingStationMaxProfile", kind="Absolute"), {}, "InvalidOperationMode"),
    (dict(periods=[{"startPeriod": 0, "setpoint": -1, "operationMode": "CentralSetpoint"}]),
     {"bidirectional": False}, "InvalidOperationMode"),
    (dict(periods=[{"startPeriod": 0, "setpoint": -1, "operationMode": "CentralSetpoint"}]),
     {"modes": ("ChargingOnly",)}, "InvalidOperationMode"),
    (dict(periods=[{"startPeriod": 0, "limit": 1, "operationMode": "Idle"}]), {}, "InvalidSchedule"),
    (dict(periods=[{"startPeriod": 0, "operationMode": "CentralSetpoint", "setpoint": -900, "limit": 1000,
                    "dischargeLimit": -500}]), {}, "InvalidSchedule"),
    (dict(periods=[{"startPeriod": 0}]), {}, "InvalidSchedule"),  # ChargingOnly without limit
    (dict(periods=[{"startPeriod": 0, "limit": 1}], purpose="PriorityCharging"), {"purposes": ()},
     "UnsupportedPurpose"),
])
def test_2_1_set_charging_profile_checks(wire, kw, reason):
    profile, _ = charging_profile21(_wire(**wire), NOW)
    args = {"modes": ALL_MODES, "purposes": ("PriorityCharging", "LocalGeneration"), "bidirectional": True, **kw}
    assert reject_reason_21(profile, **args) == reason


def test_mini_csms_pull_answers_are_schema_valid():
    v = _validator()
    s = CpSession("X", "2.1", "ocpp2.1")
    assert _pull_dynamic(s, {"chargingProfileId": 5}) == {"status": "Rejected",
                                                           "statusInfo": {"reasonCode": "InvalidProfile"}}
    profile = {"evseId": 1, "chargingProfile": _wire([{"startPeriod": 0, "limit": 3000}], kind="Dynamic", pid=5)
               .model_dump(mode="json", by_alias=True, exclude_none=True)}
    remember_dynamic(s, "SetChargingProfile", profile, {"status": "Accepted"})
    remember_dynamic(s, "UpdateDynamicSchedule", {"chargingProfileId": 5, "scheduleUpdate": {"limit": 2000}},
                     {"status": "Accepted"})
    answer = _pull_dynamic(s, {"chargingProfileId": 5})
    assert answer == {"status": "Accepted", "scheduleUpdate": {"limit": 2000}}
    for a in (answer, _pull_dynamic(s, {"chargingProfileId": 6})):
        v.validate("PullDynamicScheduleUpdate", a, "response")


# ---------------------------------------------------------------------- closed loop against the Mini CSMS

METERED = {"auth_mode": "accept_all", "meter_sample_interval_s": 1, "max_power_w": 7400.0}


async def _charging(rt, csms):
    await _until(lambda: csms.connected("CP1") and csms.sessions["CP1"].boot_status == "Accepted"
                 and _conn(rt)["status"] == "Available")
    await rt.plug(1)
    assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
    await _until(lambda: rt.has_transaction(1) and _conn(rt)["status"] == "Charging")
    return _conn(rt)["transaction"]["tx_id"]


async def _call(csms, v, action, payload):
    v.validate(action, payload, "request")
    res = await csms.call("CP1", action, payload)
    assert res["ok"] and "warning" not in res, res
    v.validate(action, res["result"], "response")
    return res["result"]


def _set(evse, wire):
    return {"evseId": evse, "chargingProfile": wire.model_dump(mode="json", by_alias=True, exclude_none=True)}


def _frames(csms, action):
    return [str(f) for f in csms.frame_log.since(0, charger="CP1", action=action)]


def test_dynamic_profile_update_and_pull_round_trip():
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                tx = await _charging(rt, csms)
                profile = _wire([{"startPeriod": 0, "limit": 3000}], kind="Dynamic", pid=7, tx=tx,
                                dynUpdateInterval=1)
                assert (await _call(csms, v, "SetChargingProfile", _set(1, profile)))["status"] == "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == 3000.0)
                s = csms.sessions["CP1"]
                await _until(lambda: s.counts.get("PullDynamicScheduleUpdate", 0) >= 1)
                res = await _call(csms, v, "UpdateDynamicSchedule",
                                  {"chargingProfileId": 7, "scheduleUpdate": {"limit": 2000}})
                assert res == {"status": "Accepted"}
                await _until(lambda: _conn(rt)["power_w"] == 2000.0)
                n = s.counts["PullDynamicScheduleUpdate"]
                await _until(lambda: s.counts["PullDynamicScheduleUpdate"] > n)  # answered with limit 2000
                assert _conn(rt)["power_w"] == 2000.0
                assert any('"Accepted"' in f and "2000" in f for f in _frames(csms, "PullDynamicScheduleUpdate"))
                comp = await _call(csms, v, "GetCompositeSchedule", {"evseId": 1, "duration": 60})
                assert comp["schedule"]["chargingSchedulePeriod"] == [{"startPeriod": 0, "limit": 2000.0}]
                bad = await _call(csms, v, "UpdateDynamicSchedule",
                                  {"chargingProfileId": 99, "scheduleUpdate": {"limit": 1}})
                assert bad["status"] == "Rejected"
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


def test_v2x_discharge_round_trip():
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                tx = await _charging(rt, csms)
                profile = _wire([{"startPeriod": 0, "operationMode": "CentralSetpoint", "setpoint": -3000,
                                  "dischargeLimit": -5000, "limit": 7000}], tx=tx, pid=3)
                assert (await _call(csms, v, "SetChargingProfile", _set(1, profile)))["status"] == "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == -3000.0)
                assert _conn(rt)["operation_mode"] == "CentralSetpoint"
                await _until(lambda: any("OperationModeChanged" in f and "CentralSetpoint" in f
                                         for f in _frames(csms, "TransactionEvent")))
                await _until(lambda: any("Energy.Active.Export.Register" in f and "Power.Active.Export" in f
                                         for f in _frames(csms, "TransactionEvent")))
                comp = await _call(csms, v, "GetCompositeSchedule", {"evseId": 1, "duration": 60,
                                                                    "chargingRateUnit": "W"})
                assert comp["schedule"]["chargingSchedulePeriod"] == [
                    {"startPeriod": 0, "limit": 7000.0, "dischargeLimit": -5000.0, "setpoint": -3000.0,
                     "operationMode": "CentralSetpoint"}]
                await _until(lambda: _conn(rt)["energy_export_wh"] > 0)  # (the SoC drop: the physics tests)
                assert _conn(rt)["meter_wh"] == 0.0  # the import register does not move while discharging
                assert (await _call(csms, v, "ClearChargingProfile", {"chargingProfileId": 3}))["status"] == \
                    "Accepted"
                await _until(lambda: _conn(rt)["power_w"] > 0)
                await _until(lambda: any("OperationModeChanged" in f and "ChargingOnly" in f
                                         for f in _frames(csms, "TransactionEvent")))
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


def test_priority_charging_round_trip():
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                tx = await _charging(rt, csms)
                use = {"transactionId": tx, "activate": True}
                assert (await _call(csms, v, "UsePriorityCharging", use))["status"] == "NoProfile"
                assert (await _call(csms, v, "UsePriorityCharging", {**use, "transactionId": "nope"})
                        )["status"] == "Rejected"
                tx_default = _wire([{"startPeriod": 0, "limit": 2000}], purpose="TxDefaultProfile", pid=1)
                priority = _wire([{"startPeriod": 0, "limit": 6000}], purpose="PriorityCharging", pid=2)
                for wire in (tx_default, priority):
                    assert (await _call(csms, v, "SetChargingProfile", _set(0, wire)))["status"] == "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == 2000.0)
                assert (await _call(csms, v, "UsePriorityCharging", use))["status"] == "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == 6000.0 and _conn(rt)["priority_charging"])
                s = csms.sessions["CP1"]
                await _until(lambda: s.last.get("NotifyPriorityCharging", {}).get("activated") is True)
                assert (await _call(csms, v, "UsePriorityCharging", {**use, "activate": False}))["status"] == \
                    "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == 2000.0)
                await _until(lambda: s.last["NotifyPriorityCharging"].get("activated") is False)
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


# ---------------------------------------------------------------------- 2.0.1 regression


@pytest.mark.parametrize("change", [{"chargingProfilePurpose": "PriorityCharging"},
                                    {"chargingProfileKind": "Dynamic"}])
def test_a_2_0_1_station_still_rejects_2_1_profile_values(change):
    from sme_ocpp_sim.protocol.v201 import codec as c201
    from sme_ocpp_sim.rpc.errors import CallError

    profile = {"id": 1, "stackLevel": 0, "chargingProfilePurpose": "TxDefaultProfile",
               "chargingProfileKind": "Relative", "chargingSchedule": [
                   {"id": 1, "chargingRateUnit": "W", "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1}]}],
               **change}
    with pytest.raises(CallError):
        c201.parse("SetChargingProfile", {"evseId": 1, "chargingProfile": profile})
    with pytest.raises(CallError) as ei:
        c201.parse("UpdateDynamicSchedule", {"chargingProfileId": 1, "scheduleUpdate": {}})
    assert ei.value.code == "NotImplemented"
