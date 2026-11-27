"""Station201 functional spec rules (OCPP 2.0.1 Part 2 B / E / F / G / K, issue #42) against the 2.0.1 peer."""

from __future__ import annotations

import asyncio
import json

from ocpp.v201 import call
from test_station201 import CP, _online, _scenario, _statuses, _until
from test_station201_smart_charging import _charging, _composite, _profile, _set

from sme_ocpp_sim.protocol.base import AuthResult


def _reason(r):
    return (r.status_info or {}).get("reason_code")


def _var(component, variable, value):
    return {"component": component, "variable": variable, "value": value}


def _call_errors(s):
    """(action-less) CALLERROR frames the charger sent: (uid, code)."""
    return [(f[1], f[2]) for f in (json.loads(raw) for d, raw in s.frames if d == "rx") if f[0] == 4]


def _start(token="R1", remote_id=1, token_type="Central", **kw):
    return call.RequestStartTransaction(id_token={"id_token": token, "type": token_type}, remote_start_id=remote_id,
                                        **kw)


# --------------------------------------------------------------------------------------------- B02 / B03


def test_pending_rejects_remote_start_and_stop_first():
    """B02.FR.05: Rejected while Pending, before every other check (EVSE 9 does not exist)."""
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        r = await peer.request(CP, _start(evse_id=9))
        assert (r.status, _reason(r)) == ("Rejected", "NotRegistered")
        r = await peer.request(CP, call.RequestStopTransaction(transaction_id="x"))
        assert (r.status, _reason(r)) == ("Rejected", "NotRegistered")
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 60})


def test_rejected_answers_every_csms_call_with_security_error():
    """B03 (Table 32): also an action that is otherwise NotImplemented, and DataTransfer."""
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Rejected")
        s = peer.history[0]
        assert await peer.request(CP, call.GetVariables(get_variable_data=[
            {"component": {"name": "OCPPCommCtrlr"}, "variable": {"name": "HeartbeatInterval"}}])) is None
        assert await peer.data_transfer(CP, "acme") is None
        assert await peer.request(CP, call.ClearCache()) is None
        await _until(lambda: len(_call_errors(s)) == 3)
        assert [code for _, code in _call_errors(s)] == ["SecurityError"] * 3
    _scenario(body, {"boot_statuses": ["Rejected"], "retry_interval": 60})


def test_pending_serves_triggered_messages():
    """B02.FR.02: StatusNotification / MeterValues / LogStatusNotification go out while Pending; a triggered
    TransactionEvent waits in the outbox for Accepted (seqNo order)."""
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        s = peer.history[0]
        for what in ("StatusNotification", "MeterValues", "LogStatusNotification"):
            assert (await peer.request(CP, call.TriggerMessage(requested_message=what))).status == "Accepted"
        await _until(lambda: s.statuses and s.meter_values and s.log_statuses)
        assert s.statuses[0] == (1, 1, "Available") and s.log_statuses == ["Idle"]
        assert s.boot_statuses == ["Pending"] and s.transaction_events == []
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 60})


# --------------------------------------------------------------------------------------------- E


def test_evse_only_in_the_first_transaction_event():
    """E01.FR.16: Started carries evse, the periodic Updated / Ended do not."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
        await _until(lambda: any(e["trigger_reason"] == "MeterValuePeriodic" for e in s.transaction_events))
        await rt.stop_tx(1)
        await _until(lambda: s.transaction_events[-1]["event_type"] == "Ended")
        assert s.transaction_events[0]["evse"] == {"id": 1, "connector_id": 1}
        assert all("evse" not in e for e in s.transaction_events[1:])
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "meter_sample_interval_s": 1})


# --------------------------------------------------------------------------------------------- F


def test_central_and_no_authorization_tokens_are_not_authorized():
    """F01.FR.02 / F02.FR.10: with AuthorizeRemoteStart a Central / NoAuthorization token is not authorized."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.plug(2)
        assert (await peer.request(CP, _start("BAD", evse_id=1))).status == "Accepted"
        assert (await peer.request(CP, _start("BAD2", 2, "NoAuthorization", evse_id=2))).status == "Accepted"
        await _until(lambda: rt.has_transaction(1) and rt.has_transaction(2))
        assert s.authorizes == []
        assert {e["trigger_reason"] for e in s.transaction_events[:2]} == {"RemoteStart"}
    _scenario(body, {"token_statuses": {"BAD": "Blocked", "BAD2": "Blocked"}},
              {"connectors": 2, "auth_mode": "csms_only",
               "variables": [_var("AuthCtrlr", "AuthorizeRemoteStart", True)]})


def test_waiting_token_dropped_after_ev_connection_timeout():
    """E03 / F02.FR.07: no cable within TxCtrlr.EVConnectionTimeOut drops the token; no transaction, no Ended."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert await rt.swipe(1, "LOCAL") == AuthResult.ACCEPTED
        assert (await peer.request(CP, _start(evse_id=2))).status == "Accepted"
        await _until(lambda: [c["pending_tag"] for c in rt.snapshot()["connectors"]] == ["LOCAL", "R1"])
        await _until(lambda: [c["pending_tag"] for c in rt.snapshot()["connectors"]] == [None, None], timeout=5)
        await rt.plug(1)
        await asyncio.sleep(0.2)
        assert not rt.has_transaction(1) and s.transaction_events == []
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "accept_all",
                            "variables": [_var("TxCtrlr", "EVConnectionTimeOut", 1)]})


def test_triggered_readings_use_their_measurand_variables():
    """F06.FR.06: MeterValues -> AlignedDataCtrlr.Measurands; F06.FR.07: TransactionEvent -> TxUpdatedMeasurands."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
        assert (await peer.request(CP, call.TriggerMessage(requested_message="MeterValues"))).status == "Accepted"
        assert (await peer.request(CP, call.TriggerMessage(requested_message="TransactionEvent"))).status \
            == "Accepted"
        await _until(lambda: s.meter_values and len(s.transaction_events) == 2)
        assert [v["measurand"] for v in s.meter_values[0]["meter_value"][0]["sampled_value"]] == ["Voltage"]
        ev = s.transaction_events[1]
        assert [v["measurand"] for v in ev["meter_value"][0]["sampled_value"]] == ["Current.Import"]
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "meter_sample_interval_s": 0, "variables": [
        _var("AlignedDataCtrlr", "Measurands", "Voltage"),
        _var("SampledDataCtrlr", "TxUpdatedMeasurands", "Current.Import")]})


def test_trigger_log_and_firmware_status_notification_send_idle():
    """F06.FR.14 / FR.16."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        for what in ("LogStatusNotification", "FirmwareStatusNotification"):
            assert (await peer.request(CP, call.TriggerMessage(requested_message=what))).status == "Accepted"
        await _until(lambda: s.log_statuses and s.firmware_statuses)
        assert (s.log_statuses, s.firmware_statuses) == (["Idle"], ["Idle"])
    _scenario(body)


# --------------------------------------------------------------------------------------------- G


def test_station_inoperative_with_a_transaction_is_scheduled_and_kept_apart_from_the_evses():
    """G04.FR.06; AMBIGUOUS: the idle EVSE becomes Unavailable at once; G03 / G04 Note 1."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "T") == AuthResult.ACCEPTED
        r = await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))
        assert r.status == "Scheduled"
        await _until(lambda: _statuses(s, 2)[-1:] == ["Unavailable"])
        assert rt.has_transaction(1) and rt.snapshot()["connectors"][0]["status"] == "Charging"
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 1}, operational_status="Operative"))
        assert r.status == "Accepted"
        await rt.stop_tx(1)
        await _until(lambda: _statuses(s, 1)[-1:] == ["Unavailable"])  # the station is still Inoperative
        assert (await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))).status \
            == "Accepted"  # G04.FR.04: already in that state
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "accept_all"})


# --------------------------------------------------------------------------------------------- K


def test_set_charging_profile_rule_reason_codes():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        two = _profile(3000, purpose="TxDefaultProfile", pid=5, stack=5)
        two["charging_schedule"].append(dict(two["charging_schedule"][0], id=2))
        abs_no_start = _profile(3000, purpose="TxDefaultProfile", pid=6, stack=6)
        abs_no_start["charging_profile_kind"] = "Absolute"
        relative_max = _profile(3000, purpose="ChargingStationMaxProfile", pid=7)
        relative_max["charging_profile_kind"] = "Relative"
        relative_max["charging_schedule"][0].pop("start_schedule")
        assert (await peer.request(CP, _set(1, _profile(3000, purpose="TxDefaultProfile", pid=1)))).status \
            == "Accepted"
        cases = [
            (_set(1, two), "InvalidProfile"),                                                    # K01.FR.34
            (_set(0, abs_no_start), "InvalidProfile"),                                           # K01.FR.40
            (_set(0, relative_max), "InvalidProfile"),                                           # K01.FR.38
            (_set(0, _profile(3000, purpose="TxDefaultProfile", pid=2)), "DuplicateProfile"),    # K01.FR.52
            (_set(1, _profile(3000, tx=tx, pid=3)), None),
            (_set(1, _profile(2000, tx=tx, pid=4)), "DuplicateProfile"),                         # K01.FR.39
        ]
        for req, code in cases:
            r = await peer.request(CP, req)
            assert (r.status, _reason(r)) == (("Accepted", None) if code is None else ("Rejected", code)), req
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "connectors": 2})


def test_composite_schedule_rate_unit_must_be_configured():
    """K08.FR.07."""
    async def body(peer, rt):
        await _online(peer, rt)
        r = await peer.request(CP, _composite(1, unit="A"))
        assert (r.status, _reason(r)) == ("Rejected", "UnsupportedRateUnit")
        assert (await peer.request(CP, _composite(1, unit="W"))).status == "Accepted"
        assert (await peer.request(CP, _composite(1))).status == "Accepted"
    _scenario(body, cfg_kw={"variables": [_var("SmartChargingCtrlr", "RateUnit", "W")]})


def test_external_limit_change_sends_charging_rate_changed():
    """K11.FR.04 / K13.FR.03 (LimitChangeSignificance 0); a limit above the hardware maximum changes nothing."""
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)

        def rate_events():
            return [e for e in s.transaction_events if e["trigger_reason"] == "ChargingRateChanged"]

        await rt.set_external_limit(1, 3000)
        await _until(lambda: len(rate_events()) == 1)
        await rt.set_external_limit(0, 99999)  # above max_power_w: the rate stays 3000 W
        await rt.set_external_limit(1, None)
        await _until(lambda: len(rate_events()) == 2)
        assert all(e["transaction_info"]["transaction_id"] == tx and e["event_type"] == "Updated"
                   for e in rate_events())
        await rt.set_external_limit(2, 1000)  # EVSE 2 has no transaction
        await asyncio.sleep(0.2)
        assert len(rate_events()) == 2
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "connectors": 2})
