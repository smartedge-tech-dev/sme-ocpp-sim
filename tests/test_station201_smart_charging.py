"""Station201 smart charging (OCPP 2.0.1, v0.2 M5) against the ``ocpp``-library 2.0.1 CSMS peer.

The closed-loop tests are the 2.0.1 twins of ``test_runtime_smart_charging.py`` (1.6): limit -> composite
schedule -> the power in the TransactionEvent readings -> SoC, with the same shared power model. The peer
schema-validates every message the station sends.
"""

from __future__ import annotations

from ocpp.v201 import call
from test_station201 import CP, _events, _online, _scenario, _until

from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.protocol.v201.station import external_profile_id

MEASURANDS = "Power.Active.Import,Energy.Active.Import.Register,Current.Import,Voltage,SoC"
METERED = {"meter_sample_interval_s": 1, "auth_mode": "accept_all",
           "variables": [{"component": "SampledDataCtrlr", "variable": "TxUpdatedMeasurands", "value": MEASURANDS}]}


#: startSchedule of the Absolute profiles (a ChargingStationMaxProfile cannot be Relative, K01.FR.38).
ABSOLUTE_START = "2026-01-01T00:00:00Z"


def _profile(limit, unit="W", purpose="TxProfile", pid=1, tx=None, stack=0, first_period=0, **kw):
    absolute = purpose == "ChargingStationMaxProfile"
    sched = {"id": 1, "charging_rate_unit": unit,
             "charging_schedule_period": [{"start_period": first_period, "limit": limit}]}
    if absolute:
        sched["start_schedule"] = ABSOLUTE_START  # K01.FR.40
    p = {"id": pid, "stack_level": stack, "charging_profile_purpose": purpose,
         "charging_profile_kind": "Absolute" if absolute else "Relative", "charging_schedule": [sched], **kw}
    if tx is not None:
        p["transaction_id"] = tx
    return p


def _set(evse_id, profile):
    return call.SetChargingProfile(evse_id=evse_id, charging_profile=profile)


def _composite(evse_id, duration=60, unit=None):
    return call.GetCompositeSchedule(duration=duration, evse_id=evse_id, charging_rate_unit=unit)


def _values(ev):
    return {v["measurand"]: float(v["value"]) for v in ev["meter_value"][0]["sampled_value"]}


def _periodic(s):
    return [e for e in s.transaction_events if e["trigger_reason"] == "MeterValuePeriodic"]


def _conn(rt, i=1):
    return rt.snapshot()["connectors"][i - 1]


async def _charging(peer, rt, evse=1):
    s = await _online(peer, rt)
    await rt.plug(evse)
    assert await rt.swipe(evse, "T") == AuthResult.ACCEPTED
    await _until(lambda: rt.has_transaction(evse) and _conn(rt, evse)["status"] == "Charging")
    return s, rt.snapshot()["connectors"][evse - 1]["transaction"]["tx_id"]


# --------------------------------------------------------------------------------------------- closed loop


def test_composite_schedule_matches_transaction_event_power():
    """The #15 completion criterion: GetCompositeSchedule == the following Power.Active.Import (as in 1.6)."""
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        await _until(lambda: _periodic(s))
        assert _values(_periodic(s)[-1])["Power.Active.Import"] == 6000  # no profile: hardware max
        r = await peer.request(CP, _composite(1, unit="W"))
        assert r.status == "Accepted"
        assert r.schedule["charging_schedule_period"] == [{"start_period": 0, "limit": 6000.0}]

        assert (await peer.request(CP, _set(1, _profile(10, "A", tx=tx)))).status == "Accepted"
        r = await peer.request(CP, _composite(1, unit="W"))
        sched = r.schedule
        assert sched["evse_id"] == 1 and sched["duration"] == 60 and sched["charging_rate_unit"] == "W"
        limit = sched["charging_schedule_period"][0]["limit"]
        assert limit == 2000.0  # 10 A x 200 V x 1 phase (the EV uses one phase)
        n = len(_periodic(s))
        await _until(lambda: len(_periodic(s)) >= n + 2)
        v = _values(_periodic(s)[-1])
        assert v["Power.Active.Import"] == limit
        assert v["Current.Import"] == 10 and v["Voltage"] == 200 and 30 <= v["SoC"] < 31
        r = await peer.request(CP, _composite(1, unit="A"))
        assert r.schedule["charging_schedule_period"][0] == {"start_period": 0, "limit": 10.0, "number_phases": 1}
        r = await peer.request(CP, _composite(2))
        assert r.status == "Rejected" and r.status_info["reason_code"] == "UnknownEvse"
        r = await peer.request(CP, _composite(1, duration=0))
        assert r.status == "Rejected" and r.status_info["reason_code"] == "InvalidDuration"
    _scenario(body, cfg_kw=METERED, rt_kw={"tick_s": 0.05})


def test_zero_limit_suspends_evse_until_cleared():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        assert (await peer.request(CP, _set(1, _profile(0, tx=tx)))).status == "Accepted"
        await _until(lambda: _conn(rt)["status"] == "SuspendedEVSE")
        assert _conn(rt)["power_w"] == 0
        await _until(lambda: any(e["transaction_info"].get("charging_state") == "SuspendedEVSE"
                                 for e in _events(s, tx)))
        assert (await peer.request(CP, call.ClearChargingProfile(charging_profile_id=1))).status == "Accepted"
        await _until(lambda: _conn(rt)["status"] == "Charging")
        assert _conn(rt)["power_w"] == 6000
        assert (await peer.request(CP, call.ClearChargingProfile(charging_profile_id=1))).status == "Unknown"
    _scenario(body, cfg_kw={"auth_mode": "accept_all"}, rt_kw={"tick_s": 0.05})


def test_target_soc_suspends_ev():
    async def body(peer, rt):
        await _charging(peer, rt)
        await _until(lambda: _conn(rt)["status"] == "SuspendedEV")
        assert _conn(rt)["soc_pct"] >= 90 and _conn(rt)["power_w"] == 0
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "session": {"initial_soc": 89.5, "target_soc": 90}},
              rt_kw={"tick_s": 0.05, "time_scale": 2000})


def test_ramp_up_reaches_the_limit():
    async def body(peer, rt):
        await _charging(peer, rt)
        assert _conn(rt)["power_w"] < 6000  # ramping
        await _until(lambda: _conn(rt)["power_w"] == 6000)
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "ramp_up_w_per_s": 20000}, rt_kw={"tick_s": 0.05})


def test_tx_profile_dropped_at_transaction_end_and_rules():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        assert (await peer.request(CP, _set(1, _profile(3000, tx=tx)))).status == "Accepted"
        assert (await peer.request(CP, _set(0, _profile(5000, purpose="TxDefaultProfile", pid=2)))).status \
            == "Accepted"
        await _until(lambda: _conn(rt)["power_w"] == 3000)
        rejected = [
            (_set(1, _profile(3000, pid=3)), "TxNotFound"),                   # no transactionId (K01.FR.09)
            (_set(1, _profile(3000, pid=3, tx="other")), "TxNotFound"),       # another transaction
            (_set(2, _profile(3000, pid=3, tx=tx)), "TxNotFound"),            # no transaction on EVSE 2
            (_set(1, _profile(3000, purpose="ChargingStationMaxProfile", pid=3)), "InvalidProfile"),  # on an EVSE
            (_set(0, _profile(3000, purpose="ChargingStationExternalConstraints", pid=3)),
             "UnsupportedPurpose"),                                           # CSMS cannot set it (K01.FR.62)
            (_set(0, _profile(3000, purpose="TxDefaultProfile", pid=3)), "DuplicateProfile"),  # K01.FR.06
            (_set(0, _profile(3000, purpose="TxDefaultProfile", pid=3, stack=101)), "InvalidProfile"),
            (_set(0, _profile(3000, purpose="TxDefaultProfile", pid=3, stack=1, first_period=5)), "InvalidProfile"),
            (_set(3, _profile(3000, purpose="TxDefaultProfile", pid=3, stack=1)), "InvalidProfile"),  # unknown
        ]
        for req, code in rejected:
            r = await peer.request(CP, req)
            assert r.status == "Rejected" and r.status_info["reason_code"] == code, req
        assert [ip.profile.profile_id for ip in rt.installed_profiles()] == [1, 2]
        assert await rt.stop_tx(1)
        assert [ip.profile.profile_id for ip in rt.installed_profiles()] == [2]
        assert _conn(rt)["soc_pct"] is None  # EV state reset
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "connectors": 2}, rt_kw={"tick_s": 0.05})


def test_request_start_profile_bound_to_started_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        start = call.RequestStartTransaction(id_token={"id_token": "R1", "type": "Central"}, remote_start_id=1,
                                             evse_id=1, charging_profile=_profile(8, "A", pid=9))
        assert (await peer.request(CP, start)).status == "Accepted"
        await _until(lambda: rt.has_transaction(1) and _conn(rt)["power_w"] == 1600)  # 8 A x 200 V
        tx = _conn(rt)["transaction"]["tx_id"]
        [ip] = rt.installed_profiles(1)
        assert ip.profile.profile_id == 9 and ip.profile.transaction_id == tx and ip.wire.transaction_id == tx
        bad = call.RequestStartTransaction(id_token={"id_token": "R2", "type": "Central"}, remote_start_id=2,
                                           evse_id=2, charging_profile=_profile(8, "A", pid=10, first_period=30))
        assert (await peer.request(CP, bad)).status == "Rejected"
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "connectors": 2}, rt_kw={"tick_s": 0.05})


# --------------------------------------------------------------------------------------------- GetChargingProfiles


def test_get_charging_profiles_reports_in_parts_with_tbc():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        assert (await peer.request(CP, _set(1, _profile(3000, tx=tx)))).status == "Accepted"
        assert (await peer.request(CP, _set(0, _profile(9000, purpose="ChargingStationMaxProfile", pid=2)))).status \
            == "Accepted"
        await rt.set_external_limit(2, 4000)
        r = await peer.request(CP, call.GetChargingProfiles(request_id=5, charging_profile={}))
        assert r.status == "Accepted"
        await _until(lambda: len(s.profile_reports) == 3)
        parts = [(p["evse_id"], p["charging_limit_source"], [x["id"] for x in p["charging_profile"]], p.get("tbc"))
                 for p in s.profile_reports]
        assert parts == [(0, "CSO", [2], True), (1, "CSO", [1], True), (2, "EMS", [external_profile_id(2)], None)]
        assert s.profile_reports[1]["charging_profile"][0]["transaction_id"] == tx
        assert all(p["request_id"] == 5 for p in s.profile_reports)

        r = await peer.request(CP, call.GetChargingProfiles(request_id=6, evse_id=0, charging_profile={
            "charging_profile_purpose": "ChargingStationMaxProfile"}))
        assert r.status == "Accepted"
        await _until(lambda: len(s.profile_reports) == 4)
        assert s.profile_reports[3]["evse_id"] == 0 and s.profile_reports[3].get("tbc") is None
        for crit in ({"charging_profile_id": [99]}, {"charging_limit_source": ["SO"]}, {"stack_level": 7}):
            r = await peer.request(CP, call.GetChargingProfiles(request_id=7, charging_profile=crit))
            assert r.status == "NoProfiles", crit
        r = await peer.request(CP, call.GetChargingProfiles(request_id=8, charging_profile={
            "charging_limit_source": ["EMS"]}))
        assert r.status == "Accepted"
        await _until(lambda: len(s.profile_reports) == 5)
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "connectors": 2}, rt_kw={"tick_s": 0.05})


# --------------------------------------------------------------------------------------------- external limit


def test_external_limit_notifies_and_the_power_follows():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        await rt.set_external_limit(1, 2500)
        await _until(lambda: s.charging_limits and _conn(rt)["power_w"] == 2500)
        n = s.charging_limits[0]
        assert n["charging_limit"] == {"charging_limit_source": "EMS"} and n["evse_id"] == 1
        assert n["charging_schedule"][0]["charging_schedule_period"] == [{"start_period": 0, "limit": 2500.0}]
        r = await peer.request(CP, _composite(1, unit="W"))
        assert r.schedule["charging_schedule_period"][0]["limit"] == 2500.0
        # min with the CSMS profile; the CSMS cannot clear the external constraints
        assert (await peer.request(CP, _set(1, _profile(2000, tx=tx)))).status == "Accepted"
        await _until(lambda: _conn(rt)["power_w"] == 2000)
        assert (await peer.request(CP, call.ClearChargingProfile(charging_profile_id=1))).status == "Accepted"
        assert (await peer.request(CP, call.ClearChargingProfile(charging_profile_criteria={
            "charging_profile_purpose": "ChargingStationExternalConstraints"}))).status == "Unknown"
        await _until(lambda: _conn(rt)["power_w"] == 2500)
        # a station-wide external limit applies to the EVSE as well
        await rt.set_external_limit(0, 1500)
        await _until(lambda: len(s.charging_limits) == 2 and _conn(rt)["power_w"] == 1500)
        assert "evse_id" not in s.charging_limits[1]
        assert rt.station.external_limit(0) == 1500 and rt.station.external_limit(1) == 2500
        await rt.set_external_limit(0, None)
        await rt.set_external_limit(1, None)
        await _until(lambda: len(s.cleared_limits) == 2 and _conn(rt)["power_w"] == 6000)
        assert s.cleared_limits == [{"charging_limit_source": "EMS"}, {"charging_limit_source": "EMS", "evse_id": 1}]
        await rt.set_external_limit(1, None)  # nothing to clear: nothing sent
        assert rt.installed_profiles() == ()
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "variables": [
        {"component": "SmartChargingCtrlr", "variable": "NotifyChargingLimitWithSchedules", "value": True}]},
              rt_kw={"tick_s": 0.05})


def test_external_limit_refused_when_external_control_is_disabled():
    async def body(peer, rt):
        await _online(peer, rt)
        try:
            await rt.set_external_limit(1, 1000)
        except ValueError as e:
            assert "ExternalControlSignalsEnabled" in str(e)
        else:
            raise AssertionError("not refused")
        assert rt.installed_profiles() == ()
    _scenario(body, cfg_kw={"variables": [{"component": "SmartChargingCtrlr",
                                           "variable": "ExternalControlSignalsEnabled", "value": "false"}]})


def test_external_limit_offline_is_applied_but_not_sent():
    async def body(peer, rt):
        await rt.set_external_limit(0, 3000)  # before the first connection
        s = await _online(peer, rt)
        assert rt.station.external_limit(0) == 3000 and s.charging_limits == []
        r = await peer.request(CP, _composite(0, unit="W"))
        assert r.schedule["charging_schedule_period"][0]["limit"] == 3000.0
    _scenario(body)
