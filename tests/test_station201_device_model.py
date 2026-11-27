"""Station201 Device Model (OCPP 2.0.1, v0.2 M4) against the ``ocpp``-library 2.0.1 CSMS peer.

GetVariables / SetVariables / GetBaseReport -> NotifyReport on the wire, and the standard variables the station
acts on. The peer schema-validates every answer and NotifyReport the charger sends.
"""

from __future__ import annotations

import asyncio

from ocpp.v201 import call
from test_station201 import CP, _frames, _online, _scenario, _until

from sme_ocpp_sim.protocol.base import AuthResult


def _gv(component, variable, attr=None, evse=None, v_inst=None):
    item = {"component": {"name": component}, "variable": {"name": variable}}
    if evse is not None:
        item["component"]["evse"] = {"id": evse}
    if v_inst is not None:
        item["variable"]["instance"] = v_inst
    if attr is not None:
        item["attribute_type"] = attr
    return item


def _sv(component, variable, value, v_inst=None):
    return dict(_gv(component, variable, v_inst=v_inst), attribute_value=value)


def _statuses(results):
    return [r["attribute_status"] for r in results]


def _last_error(s):
    errors = [f for f in _frames(s) if f[0] == 4]
    return errors[-1][2] if errors else None


USER = {"component": "VendorCtrlr", "variable": "Secret", "data_type": "string", "value": "s",
        "mutability": "WriteOnly"}


def test_get_variables_statuses_on_the_wire():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(2)
        r = await peer.request(CP, call.GetVariables(get_variable_data=[
            _gv("OCPPCommCtrlr", "HeartbeatInterval"),
            _gv("NoSuchCtrlr", "HeartbeatInterval"),
            _gv("OCPPCommCtrlr", "NoSuch"),
            _gv("OCPPCommCtrlr", "HeartbeatInterval", attr="MaxSet"),
            _gv("VendorCtrlr", "Secret"),
            _gv("EVSE", "AvailabilityState", evse=1),
            _gv("EVSE", "AvailabilityState", evse=2),
            _gv("DeviceDataCtrlr", "ItemsPerMessage", v_inst="GetVariables"),
        ]))
        res = r.get_variable_result
        assert _statuses(res) == ["Accepted", "UnknownComponent", "UnknownVariable", "NotSupportedAttributeType",
                                  "Rejected", "Accepted", "Accepted", "Accepted"]
        assert res[0]["attribute_value"] == "30"  # the BootNotification interval of the peer
        assert res[3]["attribute_type"] == "MaxSet" and res[1]["component"] == {"name": "NoSuchCtrlr"}
        assert "attribute_value" not in res[4]
        assert [res[5]["attribute_value"], res[6]["attribute_value"]] == ["Available", "Occupied"]
        assert res[7]["attribute_value"] == "8"
        assert s.boots == 1
    _scenario(body, cfg_kw={"connectors": 2, "variables": [
        USER, {"component": "DeviceDataCtrlr", "variable": "ItemsPerMessage", "variable_instance": "GetVariables",
               "value": 8}]})


def test_set_variables_statuses_and_heartbeat_interval():
    async def body(peer, rt):
        s = await _online(peer, rt)
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("OCPPCommCtrlr", "HeartbeatInterval", "1"),
            _sv("TxCtrlr", "TxStartPoint", "EVConnected"),
            _sv("OCPPCommCtrlr", "HeartbeatInterval", "soon"),
            _sv("SecurityCtrlr", "BasicAuthPassword", "0123456789abcdef"),
            _sv("Nope", "X", "1"),
            _sv("OCPPCommCtrlr", "Nope", "1"),
        ]))
        res = r.set_variable_result
        assert _statuses(res) == ["Accepted", "Rejected", "Rejected", "RebootRequired", "UnknownComponent",
                                  "UnknownVariable"]
        assert res[1]["attribute_status_info"]["reason_code"] == "ReadOnly"
        assert res[2]["attribute_status_info"]["reason_code"] == "InvalidValue"
        assert rt.station.heartbeat_interval_s == 1
        before = s.heartbeats
        await _until(lambda: s.heartbeats >= before + 2)  # the new interval applies at once
        g = await peer.request(CP, call.GetVariables(get_variable_data=[_gv("OCPPCommCtrlr", "HeartbeatInterval")]))
        assert g.get_variable_result[0]["attribute_value"] == "1"
    _scenario(body)


def test_items_per_message_exceeded_is_a_callerror():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await peer.request(CP, call.GetVariables(get_variable_data=[_gv("OCPPCommCtrlr", "HeartbeatInterval")] * 3))
        assert _last_error(s) == "OccurenceConstraintViolation"  # the 2.0.1 spelling
        await peer.request(CP, call.SetVariables(set_variable_data=[_sv("OCPPCommCtrlr", "HeartbeatInterval", "9")]
                                                 * 2))
        assert _last_error(s) == "OccurenceConstraintViolation"  # the 2.0.1 spelling
        assert rt.station.heartbeat_interval_s == 30  # nothing applied
    _scenario(body, cfg_kw={"variables": [
        {"component": "DeviceDataCtrlr", "variable": "ItemsPerMessage", "variable_instance": "GetVariables",
         "value": 2},
        {"component": "DeviceDataCtrlr", "variable": "ItemsPerMessage", "variable_instance": "SetVariables",
         "value": 1}]})


def _report_items(reports):
    return [item for r in reports for item in r.get("report_data", [])]


def test_get_base_report_is_split_into_notify_report_parts():
    async def body(peer, rt):
        s = await _online(peer, rt)
        total = len(rt.station.dm.variables())
        r = await peer.request(CP, call.GetBaseReport(request_id=7, report_base="FullInventory"))
        assert r.status == "Accepted"
        parts = -(-total // 4)
        await _until(lambda: len(s.reports) == parts)
        assert [x["seq_no"] for x in s.reports] == list(range(parts))
        assert [x.get("tbc", False) for x in s.reports] == [True] * (parts - 1) + [False]
        assert {x["request_id"] for x in s.reports} == {7} and len({x["generated_at"] for x in s.reports}) == 1
        assert all(len(x["report_data"]) <= 4 for x in s.reports)
        items = _report_items(s.reports)
        assert len(items) == total
        hb = next(i for i in items if i["variable"]["name"] == "HeartbeatInterval")
        assert hb["variable_attribute"] == [{"type": "Actual", "value": "30", "mutability": "ReadWrite",
                                             "persistent": False, "constant": False}]
        assert hb["variable_characteristics"]["data_type"] == "integer"
        # The answer went out before the first NotifyReport.
        rx = _frames(s)
        answer = next(i for i, f in enumerate(rx) if f[0] == 3 and isinstance(f[2], dict) and f[2].get("status"))
        first = next(i for i, f in enumerate(rx) if f[0] == 2 and f[2] == "NotifyReport")
        assert answer < first

        s.reports.clear()
        r = await peer.request(CP, call.GetBaseReport(request_id=8, report_base="ConfigurationInventory"))
        assert r.status == "Accepted"
        await _until(lambda: s.reports and not s.reports[-1].get("tbc", False))
        items = _report_items(s.reports)
        assert items and all(i["variable_attribute"][0]["mutability"] != "ReadOnly" for i in items)
        assert len(items) < total

        s.reports.clear()
        r = await peer.request(CP, call.GetBaseReport(request_id=9, report_base="SummaryInventory"))
        await _until(lambda: s.reports and not s.reports[-1].get("tbc", False))
        names = sorted((i["component"]["name"], i["variable"]["name"]) for i in _report_items(s.reports))
        assert names == [("AuthCtrlr", "Enabled"), ("EVSE", "AvailabilityState"), ("EVSE", "AvailabilityState")]
    _scenario(body, cfg_kw={"connectors": 2, "variables": [
        {"component": "DeviceDataCtrlr", "variable": "ItemsPerMessage", "variable_instance": "GetReport",
         "value": 4}]})


def test_get_base_report_while_pending_and_empty_result():
    async def body(peer, rt):
        await _until(lambda: rt.connected and peer.history and peer.history[0].boots >= 1)
        s = peer.history[0]
        r = await peer.request(CP, call.GetBaseReport(request_id=1, report_base="SummaryInventory"))
        assert r.status == "Accepted"
        await _until(lambda: len(s.reports) == 1)
        assert rt.registration == "Pending"
    _scenario(body, {"boot_statuses": ["Pending"] * 50, "retry_interval": 30})


def test_tx_updated_interval_and_measurands_change_a_running_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "T1") == AuthResult.ACCEPTED
        await _until(lambda: len(s.transaction_events) == 1)
        assert [v["measurand"] for v in s.transaction_events[0]["meter_value"][0]["sampled_value"]] == \
            ["Energy.Active.Import.Register"]
        await asyncio.sleep(0.3)
        assert len(s.transaction_events) == 1  # TxUpdatedInterval 0 (meter_sample_interval_s): no Updated
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("SampledDataCtrlr", "TxUpdatedMeasurands", "Voltage"),
            _sv("SampledDataCtrlr", "TxUpdatedInterval", "1")]))
        assert _statuses(r.set_variable_result) == ["Accepted", "Accepted"]
        await _until(lambda: len(s.transaction_events) >= 2)
        updated = s.transaction_events[1]
        assert updated["trigger_reason"] == "MeterValuePeriodic"
        assert [v["measurand"] for v in updated["meter_value"][0]["sampled_value"]] == ["Voltage"]
        assert rt.station.tx_updated_interval_s == 1
    _scenario(body, cfg_kw={"auth_mode": "accept_all", "meter_sample_interval_s": 0})


def test_authorize_remote_start():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        await rt.plug(2)
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("AuthCtrlr", "AuthorizeRemoteStart", "true")]))
        assert _statuses(r.set_variable_result) == ["Accepted"]
        r = await peer.request(CP, call.RequestStartTransaction(
            id_token={"id_token": "BAD", "type": "Central"}, remote_start_id=1, evse_id=1))
        assert r.status == "Accepted"
        await _until(lambda: len(s.authorizes) == 1)
        await _until(lambda: rt.snapshot()["connectors"][0]["pending_tag"] is None)
        assert not rt.has_transaction(1) and s.transaction_events == []
        r = await peer.request(CP, call.RequestStartTransaction(
            id_token={"id_token": "OK", "type": "Central"}, remote_start_id=2, evse_id=2))
        await _until(lambda: len(s.transaction_events) == 1)
        assert len(s.authorizes) == 2 and s.transaction_events[0]["trigger_reason"] == "RemoteStart"
        assert rt.has_transaction(2)
    _scenario(body, {"token_statuses": {"BAD": "Blocked"}}, {"connectors": 2, "auth_mode": "csms_only"})


def test_stop_tx_on_invalid_id_false_keeps_the_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "BAD") == AuthResult.ACCEPTED
        await _until(lambda: len(s.transaction_events) == 1)
        await asyncio.sleep(0.3)
        assert rt.has_transaction(1) and len(s.transaction_events) == 1
    _scenario(body, {"token_statuses": {"BAD": "Invalid"}},
              {"auth_mode": "accept_all", "meter_sample_interval_s": 0,
               "variables": [{"component": "TxCtrlr", "variable": "StopTxOnInvalidId", "value": False}]})


def test_message_attempts_variable_bounds_retries():
    async def body(peer, rt):
        assert rt.station.dm.int_value(("OCPPCommCtrlr", "MessageAttempts"), "TransactionEvent") == 1
        await _online(peer, rt)
    _scenario(body, cfg_kw={"variables": [{"component": "OCPPCommCtrlr", "variable": "MessageAttempts",
                                           "variable_instance": "TransactionEvent", "value": 1}]})


def test_authorize_remote_start_a_plug_waits_for_the_authorization():
    async def body(peer, rt):
        s = await _online(peer, rt)
        gate = peer.gates["Authorize"] = asyncio.Event()
        await peer.request(CP, call.SetVariables(set_variable_data=[_sv("AuthCtrlr", "AuthorizeRemoteStart", "true")]))
        r = await peer.request(CP, call.RequestStartTransaction(
            id_token={"id_token": "BAD", "type": "Central"}, remote_start_id=1, evse_id=1))
        assert r.status == "Accepted"
        await _until(lambda: rt.snapshot()["connectors"][0]["pending_tag"] == "BAD")
        await rt.plug(1)  # while the token is being authorized
        await asyncio.sleep(0.2)
        assert not rt.has_transaction(1) and s.transaction_events == []
        gate.set()
        await _until(lambda: rt.snapshot()["connectors"][0]["pending_tag"] is None)
        assert not rt.has_transaction(1) and s.transaction_events == []
    _scenario(body, {"token_statuses": {"BAD": "Blocked"}}, {"auth_mode": "csms_only"})
