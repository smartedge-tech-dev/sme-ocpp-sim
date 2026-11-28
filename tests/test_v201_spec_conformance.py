"""OCPP 2.0.1 spec conformance (issue #42, batch A): RPC error rules, Device Model variables, B use cases."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from ocpp.v201 import call
from rpc_fakes import FakeWs
from test_station201 import CP, _frames, _online, _scenario, _until
from test_station201_device_model import _statuses, _sv

from sme_ocpp_sim.config import ChargerConfig
from sme_ocpp_sim.protocol.base import AuthResult as A
from sme_ocpp_sim.protocol.base import AuthResult
from sme_ocpp_sim.protocol.v201 import device_model as dm
from sme_ocpp_sim.protocol.v201 import models as m
from sme_ocpp_sim.protocol.v201.enums import AttributeEnumType as AT
from sme_ocpp_sim.protocol.v201.enums import ReportBaseEnumType
from sme_ocpp_sim.rpc import OCPP16_ERRORS, OCPP201_ERRORS, CallError, RpcConnection
from sme_ocpp_sim.rpc.transport import basic_auth_header
from sme_ocpp_sim.runtime.charger import ChargerRuntime


# --------------------------------------------------------------------------------------------- RPC (Part 4)

async def _answers(codes, frames, handlers=None) -> list:
    ws = FakeWs()
    rpc = RpcConnection(ws, handlers or {}, error_codes=codes)
    rx = asyncio.create_task(rpc.run())
    for f in frames:
        ws.feed(f)
    ws.feed([2, "marker", "FooBar", {}])
    await _until(lambda: any(json.loads(r)[1] == "marker" for r in ws.sent))
    await rpc.close()
    await rx
    return sorted((f for f in (json.loads(r) for r in ws.sent) if f[1] != "marker"), key=lambda f: f[1])


def test_occurrence_spelling_follows_part4_table8():
    assert OCPP201_ERRORS.occurrence_constraint_violation == "OccurrenceConstraintViolation"
    assert OCPP16_ERRORS.occurrence_constraint_violation == "OccurenceConstraintViolation"  # 1.6 unchanged


def test_bad_call_frame_is_rpc_framework_error_and_bad_payload_format_violation():
    frames = [[2, "a", "Heartbeat"], [2, "b", 1, {}], [2, "c", "Heartbeat", "x"], [2, "d", "Heartbeat", {}, 1]]
    sent = asyncio.run(_answers(OCPP201_ERRORS, frames))
    assert [(f[1], f[2]) for f in sent] == [("a", "RpcFrameworkError"), ("b", "RpcFrameworkError"),
                                            ("c", "FormatViolation"), ("d", "RpcFrameworkError")]
    sent = asyncio.run(_answers(OCPP16_ERRORS, frames))
    assert {f[2] for f in sent} == {"FormationViolation"}  # 1.6 wire behaviour unchanged


def test_message_id_longer_than_36_is_rpc_framework_error():
    ok, long = "u" * 36, "u" * 37
    sent = asyncio.run(_answers(OCPP201_ERRORS, [[2, ok, "FooBar", {}], [2, long, "FooBar", {}]]))
    assert [(f[1], f[2]) for f in sent] == [(ok, "NotImplemented"), (long, "RpcFrameworkError")]
    sent = asyncio.run(_answers(OCPP16_ERRORS, [[2, long, "FooBar", {}]]))
    assert [(f[1], f[2]) for f in sent] == [(long, "NotImplemented")]  # 1.6: no check


def test_error_description_is_cut_to_255():
    async def handler(payload):
        raise CallError("GenericError", "x" * 300)
    sent = asyncio.run(_answers(OCPP201_ERRORS, [[2, "a", "Do", {}]], {"Do": handler}))
    assert sent[0][2] == "GenericError" and sent[0][3] == "x" * 255
    sent = asyncio.run(_answers(OCPP16_ERRORS, [[2, "a", "Do", {}]], {"Do": handler}))
    assert len(sent[0][3]) == 300  # 1.6J defines no limit


# --------------------------------------------------------------------------------------------- Device Model

def _cfg(**kw):
    base = dict(auth_mode="csms_only", meter_sample_interval_s=None, connectors=2, password=None, id="CP1",
                vendor="V", max_power_w=7400.0, phases=3, variables=[])
    return SimpleNamespace(**{**base, **kw})


def _set(model, component, variable, value, **kw):
    evse = kw.pop("evse", None)
    return model.set(m.SetVariableDataType(
        attribute_value=value, component=m.ComponentType(name=component, evse=evse),
        variable=m.VariableType(name=variable, **kw)))


def _status(r):
    return r.attribute_status.value


def test_heartbeat_interval_min_limit_is_1():
    model = dm.build(_cfg())
    assert model.lookup("OCPPCommCtrlr", "HeartbeatInterval").min_limit == 1
    assert _status(_set(model, "OCPPCommCtrlr", "HeartbeatInterval", "0")) == "Rejected"
    assert _status(_set(model, "OCPPCommCtrlr", "HeartbeatInterval", "1")) == "Accepted"


@pytest.mark.parametrize("mode,expected", [
    # Enabled, LocalPreAuthorize, LocalAuthorizeOffline, OfflineTxForUnknownIdEnabled, DisableRemoteAuthorization
    ("accept_all", (False, False, False, True, False)),
    ("local_only", (True, True, True, False, True)),
    ("local_then_csms", (True, True, True, False, False)),
    ("csms_only", (True, False, False, False, False)),
])
def test_auth_controls_start_from_auth_mode_and_accept_set_variables(mode, expected):
    model = dm.build(_cfg(auth_mode=mode))
    names = (dm.AUTH_ENABLED, dm.LOCAL_PRE_AUTHORIZE, dm.LOCAL_AUTHORIZE_OFFLINE, dm.OFFLINE_TX_FOR_UNKNOWN_ID,
             dm.DISABLE_REMOTE_AUTHORIZATION)
    assert tuple(model.bool_value(n) for n in names) == expected
    for n in names:
        assert model.lookup(*n).mutability.value == "ReadWrite"
        assert _status(_set(model, n[0], n[1], "true")) == "Accepted"


def test_smart_charging_enabled_read_write_and_notify_with_schedules_default_false():
    model = dm.build(_cfg())
    assert model.lookup(*dm.SC_ENABLED).mutability.value == "ReadWrite"
    assert _status(_set(model, "SmartChargingCtrlr", "Enabled", "false")) == "Accepted"
    assert not model.bool_value(dm.NOTIFY_CHARGING_LIMIT_WITH_SCHEDULES)


def test_limits_of_identity_measurands_and_password():
    model = dm.build(_cfg())
    assert model.lookup("SecurityCtrlr", "Identity").max_limit == 48
    full = len(",".join(dm.MEASURANDS))
    for name in ("TxStartedMeasurands", "TxUpdatedMeasurands", "TxEndedMeasurands"):
        assert model.lookup("SampledDataCtrlr", name).max_limit == full
    pw = model.lookup(*dm.BASIC_AUTH_PASSWORD)
    assert (pw.min_limit, pw.max_limit, pw.reboot_required) == (16, 64, False)
    assert _status(_set(model, "SecurityCtrlr", "BasicAuthPassword", "p" * 64)) == "Accepted"
    assert _status(_set(model, "SecurityCtrlr", "BasicAuthPassword", "p" * 65)) == "Rejected"
    assert model.int_value(dm.SECURITY_PROFILE) == 0  # simulator-only value without a password


# (component, variable, variable instance, evse, connector) -> (dataType, mutability, unit)
REQUIRED = {
    ("ChargingStation", "Available", None, None, None): ("boolean", "ReadOnly", None),
    ("ChargingStation", "AvailabilityState", None, None, None): ("OptionList", "ReadOnly", None),
    ("EVSE", "Available", None, 1, None): ("boolean", "ReadOnly", None),
    ("EVSE", "AvailabilityState", None, 2, None): ("OptionList", "ReadOnly", None),
    ("EVSE", "Power", None, 1, None): ("decimal", "ReadOnly", "W"),
    ("EVSE", "SupplyPhases", None, 2, None): ("integer", "ReadOnly", None),
    ("Connector", "Available", None, 1, 1): ("boolean", "ReadOnly", None),
    ("Connector", "AvailabilityState", None, 2, 1): ("OptionList", "ReadOnly", None),
    ("OCPPCommCtrlr", "FileTransferProtocols", None, None, None): ("MemberList", "ReadOnly", None),
    ("OCPPCommCtrlr", "NetworkConfigurationPriority", None, None, None): ("SequenceList", "ReadOnly", None),
    ("OCPPCommCtrlr", "NetworkProfileConnectionAttempts", None, None, None): ("integer", "ReadOnly", None),
    ("OCPPCommCtrlr", "OfflineThreshold", None, None, None): ("integer", "ReadOnly", "s"),
    ("OCPPCommCtrlr", "ResetRetries", None, None, None): ("integer", "ReadOnly", None),
    ("OCPPCommCtrlr", "UnlockOnEVSideDisconnect", None, None, None): ("boolean", "ReadOnly", None),
    ("TxCtrlr", "StopTxOnEVSideDisconnect", None, None, None): ("boolean", "ReadOnly", None),
    ("SecurityCtrlr", "CertificateEntries", None, None, None): ("integer", "ReadOnly", None),
    ("SmartChargingCtrlr", "Entries", "ChargingProfiles", None, None): ("integer", "ReadOnly", None),
    ("SmartChargingCtrlr", "LimitChangeSignificance", None, None, None): ("decimal", "ReadOnly", None),
}


def test_required_variables_exist_with_their_characteristics():
    model = dm.build(_cfg())
    for (c, v, vi, evse, conn), (data_type, mutability, unit) in REQUIRED.items():
        var = model.lookup(c, v, variable_instance=vi, evse_id=evse, connector_id=conn)
        assert var is not None, (c, v)
        assert (var.data_type.value, var.mutability.value, var.unit) == (data_type, mutability, unit), var.label()
        assert var.derived  # YAML may not override them
    assert model.lookup("EVSE", "Power", evse_id=1).max_limit == 7400.0
    assert model.lookup("EVSE", "SupplyPhases", evse_id=1).value() == "3"
    assert model.lookup("OCPPCommCtrlr", "FileTransferProtocols").values_list == dm.FILE_TRANSFER_PROTOCOLS
    with pytest.raises(ValueError, match="derived"):
        dm.build(_cfg(variables=[SimpleNamespace(
            component="OCPPCommCtrlr", variable="OfflineThreshold", component_instance=None, variable_instance=None,
            evse=None, connector=None, value="5", target=None, min_set=None, max_set=None,
            **{f: None for f in dm._DEFINITION_FIELDS})]))


def test_live_availability_power_and_profile_entries():
    states = {1: "Unavailable", 2: "Occupied"}
    count = [0]
    model = dm.build(_cfg(), availability=states.__getitem__, power_w=lambda i: 1234.5 * i,
                     profile_count=lambda: count[0])
    station = model.lookup("ChargingStation", "AvailabilityState")
    assert station.value() == "Available"
    assert model.lookup("Connector", "AvailabilityState", evse_id=2, connector_id=1).value() == "Occupied"
    states[2] = "Unavailable"
    assert station.value() == "Unavailable"
    assert model.lookup("EVSE", "Power", evse_id=2).value() == "2469"
    assert dm._decimal(1e6) == "1000000" and dm._decimal(11043.78) == "11043.78"
    count[0] = 3
    assert model.lookup("SmartChargingCtrlr", "Entries", variable_instance="ChargingProfiles").value() == "3"


def test_summary_inventory_follows_b07_fr_09():
    model = dm.build(_cfg())
    got = [(r.component.name, r.variable.name, r.component.evse.id if r.component.evse else None)
           for r in model.report(ReportBaseEnumType.summary_inventory)]
    assert got == [("ChargingStation", "AvailabilityState", None), ("EVSE", "AvailabilityState", 1),
                   ("EVSE", "AvailabilityState", 2)]


# --------------------------------------------------------------------------------------------- authorization

def _offline_station(**kw):
    rt = ChargerRuntime(ChargerConfig(id=CP, **kw), "ws://127.0.0.1:9", "2.0.1")
    return rt.station


@pytest.mark.parametrize("changes,tag,expected", [
    ({}, "T1", A.OFFLINE),                                           # csms_only offline
    ({dm.LOCAL_AUTHORIZE_OFFLINE: "true"}, "T1", A.ACCEPTED),        # local list offline
    ({dm.LOCAL_AUTHORIZE_OFFLINE: "true"}, "X", A.OFFLINE),
    ({dm.OFFLINE_TX_FOR_UNKNOWN_ID: "true"}, "X", A.ACCEPTED),       # unknown id offline
    ({dm.DISABLE_REMOTE_AUTHORIZATION: "true"}, "T1", A.ACCEPTED),
    ({dm.DISABLE_REMOTE_AUTHORIZATION: "true"}, "X", A.INVALID),
    ({dm.AUTH_ENABLED: "false"}, "X", A.ACCEPTED),
])
def test_auth_controls_drive_offline_authorization(changes, tag, expected):
    # The _authorize truth table (dm.put sets the values directly); the SetVariables path is tested online below.
    st = _offline_station(auth_mode="csms_only", id_tags=["T1"])
    for name, value in changes.items():
        st.dm.put(name, value)
    assert asyncio.run(st._authorize(tag)) == expected


def test_auth_controls_change_online_authorization():
    async def body(peer, rt):
        s = await _online(peer, rt)

        def authorizes():
            return sum(1 for f in _frames(s) if f[0] == 2 and f[2] == "Authorize")
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("AuthCtrlr", "LocalPreAuthorize", "true")]))
        assert _statuses(r.set_variable_result) == ["Accepted"]
        await rt.plug(1)
        assert await rt.swipe(1, "T1") == AuthResult.ACCEPTED  # local list, no Authorize
        assert authorizes() == 0
        assert await rt.stop_tx(1)
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("AuthCtrlr", "DisableRemoteAuthorization", "true")]))
        assert _statuses(r.set_variable_result) == ["Accepted"]
        assert await rt.swipe(1, "X") == AuthResult.INVALID
        assert authorizes() == 0
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("AuthCtrlr", "DisableRemoteAuthorization", "false")]))
        assert _statuses(r.set_variable_result) == ["Accepted"]
        assert await rt.swipe(1, "X") == AuthResult.ACCEPTED  # the CSMS accepts it
        assert authorizes() == 1
    _scenario(body, cfg_kw={"auth_mode": "csms_only", "id_tags": ["T1"]})


# --------------------------------------------------------------------------------------------- B use cases

def test_basic_auth_password_is_accepted_and_the_station_reconnects_with_it():
    old, new = "o" * 16, "n" * 40

    async def body(peer, rt):
        s = await _online(peer, rt)
        assert s.authorization == basic_auth_header(CP, old)
        peer.passwords[CP] = new
        r = await peer.request(CP, call.SetVariables(set_variable_data=[
            _sv("SecurityCtrlr", "BasicAuthPassword", new)]))
        assert _statuses(r.set_variable_result) == ["Accepted"]
        s2 = await _online(peer, rt, session=1)
        assert s2.authorization == basic_auth_header(CP, new)
        assert rt.last_error is None or rt.last_error.get("kind") != "disconnected"
        assert rt.station.dm.int_value(dm.SECURITY_PROFILE) == 1
    _scenario(body, peer_kw={"passwords": {CP: old}}, cfg_kw={"password": old})


def test_empty_base_report_is_accepted_with_one_empty_notify_report():
    async def body(peer, rt):
        s = await _online(peer, rt)
        rt.station.dm.report = lambda base: []
        r = await peer.request(CP, call.GetBaseReport(request_id=5, report_base="SummaryInventory"))
        assert r.status == "Accepted"
        await _until(lambda: len(s.reports) == 1)
        assert s.reports[0]["request_id"] == 5 and s.reports[0]["seq_no"] == 0
        assert not s.reports[0].get("report_data") and not s.reports[0].get("tbc")
    _scenario(body)


def test_values_are_reported_with_the_attribute():
    model = dm.build(_cfg())
    entry = dm.report_data(model.lookup("EVSE", "Power", evse_id=1))
    assert entry.variable_characteristics.max_limit == 7400.0 and entry.variable_attribute[0].type is AT.actual


def test_tx_ended_intervals_and_aligned_measurands_are_read_write():
    """2.7.8 / AlignedDataCtrlr: TxEndedInterval (both controllers) and AlignedDataCtrlr.TxEndedMeasurands RW."""
    model = dm.build(_cfg())
    for comp in ("SampledDataCtrlr", "AlignedDataCtrlr"):
        var = model.lookup(comp, "TxEndedInterval")
        assert (var.data_type.value, var.mutability.value, var.unit, var.value()) == ("integer", "ReadWrite", "s",
                                                                                      "0")
        assert _status(_set(model, comp, "TxEndedInterval", "30")) == "Accepted"
        assert _status(_set(model, comp, "TxEndedInterval", "-1")) == "Rejected"
    assert model.int_value(dm.TX_ENDED_INTERVAL) == model.int_value(dm.ALIGNED_TX_ENDED_INTERVAL) == 30
    assert not model.list_value(dm.ALIGNED_TX_ENDED_MEASURANDS)
    assert _status(_set(model, "AlignedDataCtrlr", "TxEndedMeasurands", "Voltage")) == "Accepted"
    assert _status(_set(model, "AlignedDataCtrlr", "TxEndedMeasurands", "Temperature")) == "Rejected"
