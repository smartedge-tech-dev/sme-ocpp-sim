"""OCPP 2.1 transaction limits, tariff / cost and availability (v0.3.2, 2.1-M3), plus the 2.1 profile rules of
RequestStartTransaction and the limit_L2 / limit_L3 PhaseConflict checks.

Unit level: :mod:`sme_ocpp_sim.protocol.v21.tariff`, the tariff handlers of the station. Closed loop: a 2.1 charger
against the Mini CSMS; every TransactionEvent / NotifyEvent / StatusNotification frame and the answers are checked
against the bundled 2.1 JSON schemas.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from test_mini_csms import _cfg, _conn, _until, _with_charger, _with_csms
from test_station201 import CP
from test_v21 import _validator
from test_v21_smart_charging import METERED, NOW, _charging, _wire

from sme_ocpp_sim.config import ChargerConfig
from sme_ocpp_sim.protocol.v21 import codec as c21
from sme_ocpp_sim.protocol.v21 import models as m
from sme_ocpp_sim.protocol.v21.profiles import charging_profile21, reject_reason_21
from sme_ocpp_sim.protocol.v21.station import TARIFF_ENABLED, Station21
from sme_ocpp_sim.protocol.v21.tariff import CostMeter, has_conditions, limits_of, reached, too_many_elements
from sme_ocpp_sim.rpc import CallError
from sme_ocpp_sim.runtime.charger import ChargerRuntime

TARIFF = {"tariffId": "T1", "currency": "JPY", "energy": {"prices": [{"priceKwh": 30.0}]}}
ALL = {"modes": ("ChargingOnly", "CentralSetpoint", "Idle"), "purposes": (), "bidirectional": True}


def _tariff(**kw):
    return m.TariffType.model_validate({**TARIFF, **kw})


# ---------------------------------------------------------------------- unit: limits and cost


def test_limits_of_keeps_only_supported_limits_and_reached_picks_the_first():
    lim = m.TransactionLimitType(max_energy=1000, max_time=60, max_so_c=80)
    assert limits_of(lim, ("maxEnergy", "maxCost")) == {"max_energy": 1000}
    assert limits_of(None, ("maxEnergy",)) == {}
    limits = {"max_energy": 1000, "max_time": 60, "max_cost": 5.0, "max_so_c": 80}
    assert reached(limits, energy_wh=999, elapsed_s=59, soc_pct=79, cost=4.9) is None
    assert reached(limits, energy_wh=1000, elapsed_s=60, soc_pct=None, cost=None) == "max_energy"
    assert reached(limits, energy_wh=0, elapsed_s=0, soc_pct=80, cost=None) == "max_so_c"
    assert reached({"max_energy": -1}, energy_wh=0, elapsed_s=0, soc_pct=None, cost=None) == "max_energy"  # FR.10


def test_cost_meter_energy_fixed_min_max_and_tariff_switch():
    c = CostMeter(_tariff(fixedFee={"prices": [{"priceFixed": 100.0}]}, maxCost={"exclTax": 500.0}))
    assert c.total(2000) == (160.0, "NormalCost")  # 2 kWh * 30 + 100
    assert c.total(20000) == (500.0, "MaxCost")
    c.switch(_tariff(tariffId="T2", energy={"prices": [{"priceKwh": 10.0}]}), 2000)  # I11.FR.08: not retroactive
    assert c.total(3000) == (70.0, "NormalCost")  # 60 (before) + 1 kWh * 10, the new tariff has no fixed fee
    low = CostMeter(_tariff(minCost={"exclTax": 50.0}))
    assert low.total(100) == (50.0, "MinCost")
    v = _validator()
    d = c.details(3000)
    payload = {"eventType": "Ended", "timestamp": "2026-11-28T12:00:00Z", "triggerReason": "EVDeparted",
               "seqNo": 1, "transactionInfo": {"transactionId": "X"},
               "costDetails": d.model_dump(mode="json", by_alias=True, exclude_none=True)}
    v.validate("TransactionEvent", payload, "request")
    assert payload["costDetails"]["totalUsage"]["energy"] == 3000
    assert payload["costDetails"]["totalCost"]["total"] == {"exclTax": 70.0}


def test_tariff_element_and_condition_checks():
    assert not too_many_elements(_tariff(), 1)
    assert too_many_elements(_tariff(energy={"prices": [{"priceKwh": 1}, {"priceKwh": 2}]}), 1)
    assert not has_conditions(_tariff())
    assert has_conditions(_tariff(energy={"prices": [{"priceKwh": 1, "conditions": {"startTimeOfDay": "08:00"}}]}))


# ---------------------------------------------------------------------- K01.FR.143 - FR.146 PhaseConflict


@pytest.mark.parametrize("period, purpose, reason", [
    ({"startPeriod": 0, "limit": 10, "limit_L2": 10}, "TxDefaultProfile", "PhaseConflict"),  # K01.FR.143 / 144
    ({"startPeriod": 0, "limit_L3": 10, "setpoint": 5, "operationMode": "CentralSetpoint"}, "TxProfile",
     "PhaseConflict"),  # K01.FR.145 / 146
    ({"startPeriod": 0, "limit": 10, "limit_L2": 10, "limit_L3": 10}, "TxProfile", None),
])
def test_limit_l2_l3_phase_conflict(period, purpose, reason):
    profile, _ = charging_profile21(_wire([period], purpose=purpose), NOW)
    assert reject_reason_21(profile, **ALL) == reason


# ---------------------------------------------------------------------- station handlers (no connection)


def _station(**cfg):
    rt = ChargerRuntime(ChargerConfig(id=CP, connectors=2, **cfg), "ws://127.0.0.1:9/ocpp", "2.1")
    return rt, rt.station


def _call(st: Station21, action, payload, v=None):
    if v is not None:
        v.validate(action, payload, "request")
    res = c21.dump(asyncio.run(st.handle(c21.parse(action, payload)[0])))
    if v is not None:
        v.validate(action, res, "response")
    return res


def test_default_tariff_answers_and_valid_from():
    v = _validator()
    rt, st = _station()
    assert _call(st, "SetDefaultTariff", {"evseId": 3, "tariff": TARIFF}, v) == {
        "status": "Rejected", "statusInfo": {"reasonCode": "UnknownEVSE"}}
    many = {**TARIFF, "energy": {"prices": [{"priceKwh": 1}] * 11}}
    assert _call(st, "SetDefaultTariff", {"evseId": 1, "tariff": many}, v) == {"status": "TooManyElements"}
    cond = {**TARIFF, "energy": {"prices": [{"priceKwh": 1, "conditions": {"minEnergy": 1}}]}}
    assert _call(st, "SetDefaultTariff", {"evseId": 1, "tariff": cond}, v) == {"status": "ConditionNotSupported"}
    assert _call(st, "SetDefaultTariff", {"evseId": 1, "tariff": TARIFF}, v) == {"status": "Accepted"}
    assert _call(st, "SetDefaultTariff", {"evseId": 2, "tariff": TARIFF}, v) == {"status": "DuplicateTariffId"}
    later = {**TARIFF, "tariffId": "T2", "validFrom": "2099-01-01T00:00:00Z"}
    assert _call(st, "SetDefaultTariff", {"evseId": 1, "tariff": later}, v) == {"status": "Accepted"}
    assert st.default_tariff(1).tariff_id == "T1"  # I07.FR.19: T2 is not valid yet
    assert st.default_tariff(1, datetime(2099, 6, 1, tzinfo=timezone.utc)).tariff_id == "T2"  # I07.FR.20
    ids = [a["tariffId"] for a in _call(st, "GetTariffs", {"evseId": 1}, v)["tariffAssignments"]]
    assert ids == ["T1", "T2"]


def test_tariff_messages_are_not_supported_when_tariffs_are_disabled():
    rt, st = _station()
    res = _call(st, "SetVariables", {"setVariableData": [{"component": {"name": TARIFF_ENABLED[0]}, "variable": {
        "name": TARIFF_ENABLED[1], "instance": "Tariff"}, "attributeValue": "false"}]})
    assert res["setVariableResult"][0]["attributeStatus"] == "Accepted"
    for action, payload in [("SetDefaultTariff", {"evseId": 0, "tariff": TARIFF}), ("GetTariffs", {"evseId": 0}),
                            ("ClearTariffs", {}), ("ChangeTransactionTariff", {"transactionId": "x",
                                                                               "tariff": TARIFF})]:
        with pytest.raises(CallError) as ei:
            _call(st, action, payload)
        assert ei.value.code == "NotSupported"  # I07.FR.01 / I11.FR.01


def test_device_model_reports_the_m3_variables():
    v = _validator()
    rt, st = _station()
    req = {"getVariableData": [
        {"component": {"name": "TxCtrlr"}, "variable": {"name": "SupportedLimits"}},
        {"component": {"name": "TariffCostCtrlr"}, "variable": {"name": "Enabled", "instance": "Cost"}},
        {"component": {"name": "TariffCostCtrlr"}, "variable": {"name": "Currency"}}]}
    res = _call(st, "GetVariables", req, v)["getVariableResult"]
    assert [r["attributeValue"] for r in res] == ["maxCost,maxEnergy,maxTime,maxSoC", "true", "JPY"]


@pytest.mark.parametrize("period, reason", [
    ({"startPeriod": 0, "limit": 1000, "operationMode": "Idle"}, "InvalidSchedule"),  # Q10.FR.02
    ({"startPeriod": 0, "limit_L2": 1000, "limit": None}, "PhaseConflict"),
])
def test_request_start_checks_the_2_1_profile_rules(period, reason):
    v = _validator()
    rt, st = _station()
    period = {k: x for k, x in period.items() if x is not None}
    profile = _wire([period], purpose="TxProfile").model_dump(mode="json", by_alias=True, exclude_none=True)
    req = {"idToken": {"idToken": "T", "type": "Central"}, "remoteStartId": 1, "evseId": 1,
           "chargingProfile": profile}
    res = _call(st, "RequestStartTransaction", req, v)
    assert res["status"] == "Rejected" and res["statusInfo"]["reasonCode"] == reason


# ---------------------------------------------------------------------- closed loop against the Mini CSMS


def _payloads(csms, v, action):
    """Validate every CALL / CALLRESULT of ``action`` in the Mini CSMS frame log; returns the CALL payloads."""
    calls, uids = [], set()
    for f in csms.frame_log.since(0, charger="CP1"):
        frame = json.loads(f.raw)
        if frame[0] == 2 and frame[2] == action:
            v.validate(action, frame[3], "request")
            calls.append(frame[3])
            uids.add(frame[1])
        elif frame[0] == 3 and frame[1] in uids:
            v.validate(action, frame[2], "response")
    return calls


def test_energy_limit_from_the_csms_suspends_and_cost_details_at_the_end():
    v = _validator()
    limit = {"max_energy_wh": 3.0}

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: csms.connected("CP1") and csms.sessions["CP1"].boot_status == "Accepted")
                res = await csms.call("CP1", "SetDefaultTariff", {"evseId": 0, "tariff": TARIFF})
                assert res["result"] == {"status": "Accepted"}
                tx = await _charging(rt, csms)
                await _until(lambda: _conn(rt)["limit_reached"] == "maxEnergy", timeout=20)
                assert _conn(rt)["status"] == "SuspendedEVSE" and _conn(rt)["tx_limit"] == {"max_energy": 3.0}
                assert _conn(rt)["tariff_id"] == "T1" and _conn(rt)["cost"]["currency"] == "JPY"
                # the events are delivered by the outbox: wait until the suspension event went out
                await _until(lambda: any(p["triggerReason"] == "EnergyLimitReached"
                                         and p["transactionInfo"]["chargingState"] == "SuspendedEVSE"
                                         for p in _payloads(csms, v, "TransactionEvent")))
                events = _payloads(csms, v, "TransactionEvent")
                started = next(p for p in events if p["eventType"] == "Started")
                assert started["transactionInfo"]["tariffId"] == "T1"
                assert any(p["triggerReason"] == "LimitSet"
                           and p["transactionInfo"]["transactionLimit"] == {"maxEnergy": 3.0} for p in events)
                # E16.FR.14: a higher local limit is capped by the CSMS one, so the transfer stays suspended
                assert rt.station.set_transaction_limit(1, max_energy=10_000) == {"max_energy": 3.0}
                assert _conn(rt)["limit_reached"] == "maxEnergy"
                assert await rt.stop_tx(1)
                await _until(lambda: any(p["eventType"] == "Ended" for p in _payloads(csms, v, "TransactionEvent")))
                ended = next(p for p in _payloads(csms, v, "TransactionEvent") if p["eventType"] == "Ended")
                total = ended["costDetails"]["totalCost"]
                assert total["currency"] == "JPY" and total["typeOfCost"] == "NormalCost"
                assert total["total"]["exclTax"] == pytest.approx(
                    ended["costDetails"]["totalUsage"]["energy"] / 1000 * 30.0, abs=0.01)
                kept = csms.sessions["CP1"].transactions[tx]
                assert kept["tariff_id"] == "T1" and kept["total_cost"] == total["total"]["exclTax"]
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(transaction_limit=limit), with_csms)
    asyncio.run(main())


def test_change_transaction_tariff_and_local_time_limit():
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                tx = await _charging(rt, csms)
                other = {**TARIFF, "tariffId": "T9", "currency": "EUR"}
                res = await csms.call("CP1", "ChangeTransactionTariff", {"transactionId": tx, "tariff": TARIFF})
                assert res["result"] == {"status": "Accepted"}
                await _until(lambda: any(p["triggerReason"] == "TariffChanged"
                                         and p["transactionInfo"].get("tariffId") == "T1"
                                         for p in _payloads(csms, v, "TransactionEvent")))
                res = await csms.call("CP1", "ChangeTransactionTariff", {"transactionId": tx, "tariff": other})
                assert res["result"] == {"status": "NoCurrencyChange"}  # I11.FR.05
                res = await csms.call("CP1", "GetTariffs", {"evseId": 0})
                assert res["result"]["tariffAssignments"] == [{"tariffId": "T1", "tariffKind": "DriverTariff"}]
                rt.station.set_transaction_limit(1, max_time=0)  # E16.FR.01 / FR.10: reached at once
                await _until(lambda: any(p["triggerReason"] == "TimeLimitReached"
                                         for p in _payloads(csms, v, "TransactionEvent")))
                assert _conn(rt)["status"] == "SuspendedEVSE" and _conn(rt)["power_w"] == 0
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


@pytest.mark.parametrize("mode, status, event", [("both", True, True), ("status", True, False),
                                                 ("event", False, True)])
def test_availability_report_switch(mode, status, event):
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: csms.connected("CP1") and csms.sessions["CP1"].boot_status == "Accepted")
                await rt.plug(1)
                await _until(lambda: csms.sessions["CP1"].statuses.get("1/1", {}).get("status") == "Occupied")
                events = _payloads(csms, v, "NotifyEvent")
                assert bool(_payloads(csms, v, "StatusNotification")) is status
                assert bool(events) is event
                if event:
                    data = events[-1]["eventData"][0]
                    assert data["component"] == {"name": "Connector", "evse": {"id": 1, "connectorId": 1}}
                    assert data["variable"] == {"name": "AvailabilityState"} and data["trigger"] == "Delta"
            await _with_charger(url, "2.1", body, availability_report=mode, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())


def test_remote_start_with_a_2_1_setpoint_profile_discharges():
    v = _validator()

    async def main():
        async def with_csms(csms, url):
            async def body(rt):
                await _until(lambda: csms.connected("CP1") and csms.sessions["CP1"].boot_status == "Accepted")
                await rt.plug(1)
                profile = _wire([{"startPeriod": 0, "operationMode": "CentralSetpoint", "setpoint": -2000,
                                  "dischargeLimit": -4000, "limit": 7000}], pid=11)
                req = {"idToken": {"idToken": "T", "type": "Central"}, "remoteStartId": 5, "evseId": 1,
                       "chargingProfile": profile.model_dump(mode="json", by_alias=True, exclude_none=True)}
                v.validate("RequestStartTransaction", req, "request")
                res = await csms.call("CP1", "RequestStartTransaction", req)
                v.validate("RequestStartTransaction", res["result"], "response")
                assert res["result"]["status"] == "Accepted"
                await _until(lambda: _conn(rt)["power_w"] == -2000.0)
                await _until(lambda: _conn(rt)["operation_mode"] == "CentralSetpoint")  # reported by the next tick
            await _with_charger(url, "2.1", body, **METERED)
        await _with_csms(_cfg(), with_csms)
    asyncio.run(main())
