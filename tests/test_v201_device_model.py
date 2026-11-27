"""OCPP 2.0.1 Device Model (v0.2 M4): lookup, mutability, validation, reports and the YAML ``variables``."""

from __future__ import annotations

import json

import pytest

from sme_ocpp_sim.config import ChargerConfig, SimConfig
from sme_ocpp_sim.protocol.v201 import device_model as dm
from sme_ocpp_sim.protocol.v201 import models as m
from sme_ocpp_sim.protocol.v201.codec import dump
from sme_ocpp_sim.protocol.v201.enums import (AttributeEnumType, DataEnumType, MutabilityEnumType,
                                              ReportBaseEnumType)

AT = AttributeEnumType


def _model(**cfg):
    return dm.build(ChargerConfig(id="CS1", **cfg))


def _get(model, component, variable, attr=None, evse=None, c_inst=None, v_inst=None):
    item = m.GetVariableDataType(
        attribute_type=attr, component=m.ComponentType(name=component, instance=c_inst,
                                                       evse=m.EVSEType(id=evse) if evse else None),
        variable=m.VariableType(name=variable, instance=v_inst))
    return model.get(item)


def _set(model, component, variable, value, attr=None, evse=None, v_inst=None):
    item = m.SetVariableDataType(
        attribute_type=attr, attribute_value=value,
        component=m.ComponentType(name=component, evse=m.EVSEType(id=evse) if evse else None),
        variable=m.VariableType(name=variable, instance=v_inst))
    return model.set(item)


def _reason(res):
    return res.attribute_status_info.reason_code if res.attribute_status_info else None


USER_VARS = [
    {"component": "VendorCtrlr", "variable": "Mode", "data_type": "OptionList", "values_list": ["Eco", "Fast"],
     "value": "Eco", "target": "Fast", "summary": True},
    {"component": "VendorCtrlr", "variable": "Limit", "data_type": "decimal", "value": 1.5, "min_set": "0",
     "max_set": "10", "min_limit": 0, "max_limit": 10, "unit": "kW"},
    {"component": "VendorCtrlr", "variable": "Secret", "data_type": "string", "value": "s", "mutability": "WriteOnly"},
    {"component": "VendorCtrlr", "variable": "Serial", "data_type": "string", "value": "X1", "mutability": "ReadOnly"},
    {"component": "VendorCtrlr", "variable": "Mode2", "data_type": "boolean", "value": True, "reboot_required": True},
    {"component": "Connector", "evse": 1, "connector": 1, "variable": "Type", "data_type": "string",
     "value": "cType2"},
]


# --------------------------------------------------------------------------------------------- standard set


def test_standard_set_names_and_defaults():
    model = _model(connectors=2)
    names = {(v.component, v.variable, v.variable_instance, v.evse_id) for v in model.variables()}
    for want in [("OCPPCommCtrlr", "HeartbeatInterval", None, None),
                 ("SampledDataCtrlr", "TxUpdatedInterval", None, None),
                 ("AuthCtrlr", "AuthorizeRemoteStart", None, None),
                 ("TxCtrlr", "TxStartPoint", None, None), ("TxCtrlr", "TxStopPoint", None, None),
                 ("AuthCtrlr", "OfflineTxForUnknownIdEnabled", None, None),
                 ("SecurityCtrlr", "SecurityProfile", None, None),
                 ("DeviceDataCtrlr", "ItemsPerMessage", "GetReport", None),
                 ("OCPPCommCtrlr", "MessageAttempts", "TransactionEvent", None),
                 ("EVSE", "AvailabilityState", None, 1), ("EVSE", "AvailabilityState", None, 2)]:
        assert want in names
    assert model.int_value(dm.HEARTBEAT_INTERVAL) == dm.DEFAULT_HEARTBEAT_S
    assert model.int_value(dm.TX_UPDATED_INTERVAL) == dm.DEFAULT_TX_UPDATED_INTERVAL_S
    assert model.list_value(dm.TX_UPDATED_MEASURANDS) == dm.DEFAULT_TX_UPDATED
    assert not model.bool_value(dm.AUTHORIZE_REMOTE_START) and model.bool_value(dm.STOP_TX_ON_INVALID_ID)


def test_standard_names_are_the_ocpp_library_names():
    """Component / variable / instance names of the standard set exist in ``ocpp.v201.enums`` (Appendix 3)."""
    enums = pytest.importorskip("ocpp.v201.enums")
    components = {c.value for c in enums.ControllerComponentName} | {"EVSE"}
    for v in _model().variables():
        assert v.component in components
        if v.component == "EVSE":
            assert v.variable in {x.value for x in enums.EVSEVariableName}
            continue
        names = {x.value for x in getattr(enums, f"{v.component}VariableName")}
        assert v.variable in names, v.label()
        if v.variable_instance:
            instances = {x.value for x in getattr(enums, f"{v.component}InstanceName")}
            assert v.variable_instance in instances, v.label()


@pytest.mark.parametrize("mode,enabled,pre,offline,unknown,no_remote", [
    ("accept_all", "false", "false", "false", "true", "false"),
    ("local_only", "true", "true", "true", "false", "true"),
    ("local_then_csms", "true", "true", "true", "false", "false"),
    ("csms_only", "true", "false", "false", "false", "false"),
])
def test_auth_controls_reflect_auth_mode(mode, enabled, pre, offline, unknown, no_remote):
    model = _model(auth_mode=mode)
    got = [model.value(("AuthCtrlr", n)) for n in ("Enabled", "LocalPreAuthorize", "LocalAuthorizeOffline",
                                                     "OfflineTxForUnknownIdEnabled", "DisableRemoteAuthorization")]
    assert got == [enabled, pre, offline, unknown, no_remote]


def test_old_yaml_fields_are_initial_values_and_variables_override_them():
    assert _model(meter_sample_interval_s=15).int_value(dm.TX_UPDATED_INTERVAL) == 15
    model = _model(meter_sample_interval_s=15, variables=[
        {"component": "SampledDataCtrlr", "variable": "TxUpdatedInterval", "value": 5}])
    assert model.int_value(dm.TX_UPDATED_INTERVAL) == 5


@pytest.mark.parametrize("password,url,profile", [(None, "ws://x", 0), ("p" * 16, "ws://x", 1),
                                                  ("p" * 16, "wss://x", 2)])
def test_security_profile_and_identity(password, url, profile):
    model = dm.build(ChargerConfig(id="CS9", password=password), url=url)
    assert model.int_value(("SecurityCtrlr", "SecurityProfile")) == profile
    assert model.value(("SecurityCtrlr", "Identity")) == "CS9"


# --------------------------------------------------------------------------------------------- GetVariables


def test_get_accepted_unknown_component_variable_and_attribute():
    model = _model(variables=USER_VARS)
    r = _get(model, "OCPPCommCtrlr", "HeartbeatInterval")
    assert r.attribute_status.value == "Accepted" and r.attribute_value == "300" and r.attribute_type is None
    assert _get(model, "NoSuchCtrlr", "HeartbeatInterval").attribute_status.value == "UnknownComponent"
    assert _get(model, "OCPPCommCtrlr", "NoSuch").attribute_status.value == "UnknownVariable"
    assert _get(model, "OCPPCommCtrlr", "HeartbeatInterval", AT.target).attribute_status.value == \
        "NotSupportedAttributeType"
    assert _get(model, "VendorCtrlr", "Mode", AT.target).attribute_value == "Fast"
    assert _get(model, "VendorCtrlr", "Limit", AT.max_set).attribute_value == "10"
    assert _get(model, "VendorCtrlr", "Limit").attribute_value == "1.5"
    # Instances and EVSEs are matched exactly.
    assert _get(model, "DeviceDataCtrlr", "ItemsPerMessage").attribute_status.value == "UnknownVariable"
    assert _get(model, "DeviceDataCtrlr", "ItemsPerMessage", v_inst="GetReport").attribute_value == "50"
    assert _get(model, "EVSE", "AvailabilityState").attribute_status.value == "UnknownComponent"
    assert _get(model, "EVSE", "AvailabilityState", evse=1).attribute_value == "Available"
    assert _get(model, "EVSE", "AvailabilityState", evse=2).attribute_status.value == "UnknownComponent"


def test_get_write_only_is_rejected():
    model = _model(variables=USER_VARS)
    r = _get(model, "VendorCtrlr", "Secret")
    assert r.attribute_status.value == "Rejected" and r.attribute_value is None and _reason(r) == "WriteOnly"
    r = _get(model, "SecurityCtrlr", "BasicAuthPassword")
    assert r.attribute_status.value == "Rejected"


def test_live_value_comes_from_the_getter():
    state = {"v": "Faulted"}
    model = dm.build(ChargerConfig(id="CS1"), availability=lambda i: state["v"])
    assert _get(model, "EVSE", "AvailabilityState", evse=1).attribute_value == "Faulted"
    state["v"] = "Occupied"
    assert _get(model, "EVSE", "AvailabilityState", evse=1).attribute_value == "Occupied"


# --------------------------------------------------------------------------------------------- SetVariables


def test_set_accepted_calls_listener_once_per_change():
    model = _model()
    seen = []
    model.on_change(dm.HEARTBEAT_INTERVAL, seen.append)
    assert _set(model, "OCPPCommCtrlr", "HeartbeatInterval", " 60").attribute_status.value == "Accepted"
    assert _set(model, "OCPPCommCtrlr", "HeartbeatInterval", "60").attribute_status.value == "Accepted"
    assert seen == ["60"] and model.int_value(dm.HEARTBEAT_INTERVAL) == 60
    model.put(dm.HEARTBEAT_INTERVAL, "90")  # the station's own update: no listener
    assert seen == ["60"] and _get(model, "OCPPCommCtrlr", "HeartbeatInterval").attribute_value == "90"


def test_set_read_only_unknown_and_attribute():
    model = _model(variables=USER_VARS)
    r = _set(model, "TxCtrlr", "TxStartPoint", "EVConnected")
    assert r.attribute_status.value == "Rejected" and _reason(r) == "ReadOnly"
    assert _set(model, "VendorCtrlr", "Serial", "Y").attribute_status.value == "Rejected"
    assert _set(model, "Nope", "X", "1").attribute_status.value == "UnknownComponent"
    assert _set(model, "VendorCtrlr", "Nope", "1").attribute_status.value == "UnknownVariable"
    assert _set(model, "VendorCtrlr", "Serial", "1", AT.min_set).attribute_status.value == \
        "NotSupportedAttributeType"
    assert _set(model, "VendorCtrlr", "Mode", "Eco", AT.target).attribute_status.value == "Accepted"
    assert _get(model, "VendorCtrlr", "Mode", AT.target).attribute_value == "Eco"


def test_set_write_only_and_reboot_required():
    model = _model(variables=USER_VARS)
    assert _set(model, "VendorCtrlr", "Secret", "new").attribute_status.value == "Accepted"
    r = _set(model, "SecurityCtrlr", "BasicAuthPassword", "x" * 20)
    assert r.attribute_status.value == "RebootRequired"
    r = _set(model, "VendorCtrlr", "Mode2", "FALSE")
    assert r.attribute_status.value == "RebootRequired" and model.lookup("VendorCtrlr", "Mode2").value() == "false"
    # An invalid value is still Rejected, not RebootRequired.
    r = _set(model, "SecurityCtrlr", "BasicAuthPassword", "short")
    assert r.attribute_status.value == "Rejected" and _reason(r) == "OutOfRange"


@pytest.mark.parametrize("component,variable,value,reason", [
    ("OCPPCommCtrlr", "HeartbeatInterval", "abc", "InvalidValue"),
    ("OCPPCommCtrlr", "HeartbeatInterval", "1.5", "InvalidValue"),
    ("OCPPCommCtrlr", "HeartbeatInterval", "-1", "OutOfRange"),
    ("OCPPCommCtrlr", "HeartbeatInterval", "+5", "InvalidValue"),
    ("OCPPCommCtrlr", "MessageAttempts", "0", "OutOfRange"),
    ("AuthCtrlr", "AuthorizeRemoteStart", "yes", "InvalidValue"),
    ("SampledDataCtrlr", "TxUpdatedMeasurands", "Energy.Active.Import.Register,Bogus", "NotInValuesList"),
    ("SampledDataCtrlr", "TxUpdatedMeasurands", "Voltage,Voltage", "InvalidValue"),
    ("SampledDataCtrlr", "TxUpdatedMeasurands", "Voltage,", "InvalidValue"),
    ("VendorCtrlr", "Mode", "Turbo", "NotInValuesList"),
    ("VendorCtrlr", "Limit", "10.5", "OutOfRange"),
    ("VendorCtrlr", "Limit", "nan", "InvalidValue"),
    ("SecurityCtrlr", "OrganizationName", "O" * 51, "OutOfRange"),
])
def test_set_value_validation(component, variable, value, reason):
    model = _model(variables=USER_VARS)
    inst = "TransactionEvent" if variable == "MessageAttempts" else None
    before = model.lookup(component, variable, variable_instance=inst).value()
    r = _set(model, component, variable, value, v_inst=inst)
    assert r.attribute_status.value == "Rejected" and _reason(r) == reason
    assert r.attribute_status_info.additional_info
    assert model.lookup(component, variable, variable_instance=inst).value() == before


@pytest.mark.parametrize("data_type,value,stored", [
    ("integer", "-0012", "-12"), ("decimal", "2.50", "2.50"), ("boolean", "True", "true"),
    ("dateTime", "2026-11-27T10:00:00Z", "2026-11-27T10:00:00Z"), ("string", " a ", " a "),
    ("SequenceList", "b, a", "b,a"), ("MemberList", "", ""),
])
def test_normalize_accepts(data_type, value, stored):
    var = dm.Variable("C", "V", DataEnumType(data_type), {AT.actual: "x"}, values_list=("a", "b"))
    assert dm.normalize(var, value) == stored


def test_normalize_rejects_bad_datetime_and_long_values():
    var = dm.Variable("C", "V", DataEnumType.date_time, {AT.actual: "x"})
    with pytest.raises(dm.InvalidValue):
        dm.normalize(var, "tomorrow")
    var = dm.Variable("C", "V", DataEnumType.string, {AT.actual: "x"})
    with pytest.raises(dm.InvalidValue):
        dm.normalize(var, "x" * (dm.MAX_VALUE_LENGTH + 1))


# --------------------------------------------------------------------------------------------- reports


def test_report_bases():
    model = _model(connectors=2, variables=USER_VARS)
    full = model.report(ReportBaseEnumType.full_inventory)
    assert len(full) == len(model.variables())
    conf = model.report(ReportBaseEnumType.configuration_inventory)
    assert conf and all(r.variable_attribute[0].mutability is not MutabilityEnumType.read_only for r in conf)
    assert len(conf) < len(full)
    summary = {(r.component.name, r.variable.name) for r in model.report(ReportBaseEnumType.summary_inventory)}
    assert summary == {("AuthCtrlr", "Enabled"), ("EVSE", "AvailabilityState"), ("VendorCtrlr", "Mode")}
    by_name = {(r.component.name, r.variable.name, r.variable.instance): r for r in full}
    mode = by_name[("VendorCtrlr", "Mode", None)]
    assert [(a.type.value, a.value) for a in mode.variable_attribute] == [("Actual", "Eco"), ("Target", "Fast")]
    assert mode.variable_characteristics.values_list == "Eco,Fast"
    assert by_name[("VendorCtrlr", "Limit", None)].variable_characteristics.unit == "kW"
    secret = by_name[("VendorCtrlr", "Secret", None)].variable_attribute[0]
    assert secret.value is None and secret.mutability is MutabilityEnumType.write_only
    evse = [r for r in full if r.component.name == "EVSE"]
    assert [r.component.evse.id for r in evse] == [1, 2]
    connector = by_name[("Connector", "Type", None)].component.evse
    assert (connector.id, connector.connector_id) == (1, 1)


def test_report_dump_is_json():
    model = _model(variables=USER_VARS)
    req = m.NotifyReportRequest(request_id=1, generated_at="2026-11-27T00:00:00Z", seq_no=0,
                                report_data=model.report(ReportBaseEnumType.full_inventory))
    json.dumps(dump(req))


def test_view_masks_write_only():
    view = {(v["component"], v["variable"]): v for v in _model(variables=USER_VARS).view()}
    assert view[("VendorCtrlr", "Secret")]["attributes"] == {"Actual": None}
    assert view[("VendorCtrlr", "Limit")]["attributes"] == {"Actual": "1.5", "MinSet": "0", "MaxSet": "10"}


# --------------------------------------------------------------------------------------------- YAML


def _sim(variables, version="2.0.1", **charger):
    return SimConfig.model_validate({"csms_defaults": {"url": "ws://h/ocpp", "ocpp_version": version},
                                     "chargers": [{"id": "CP1", "variables": variables, **charger}]})


def test_yaml_variables_accepted():
    cfg = _sim(USER_VARS)
    assert len(cfg.chargers[0].variables) == len(USER_VARS)


@pytest.mark.parametrize("variables,error", [
    ([{"component": "OCPPCommCtrlr", "variable": "HeartbeatInterval", "value": "x"}], "not an integer"),
    ([{"component": "OCPPCommCtrlr", "variable": "HeartbeatInterval", "value": 1, "unit": "min"}],
     "standard variable"),
    ([{"component": "OCPPCommCtrlr", "variable": "HeartbeatInterval", "target": 1}], "no Target"),
    ([{"component": "SecurityCtrlr", "variable": "Identity", "value": "X"}], "derived"),
    ([{"component": "EVSE", "evse": 1, "variable": "AvailabilityState", "value": "Faulted"}], "derived"),
    ([{"component": "VendorCtrlr", "variable": "X", "value": "1"}], "needs data_type"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "integer"}], "needs a value"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "OptionList", "value": "a"}], "values_list"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "OptionList", "value": "c",
       "values_list": ["a", "b"]}], "not one of"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "integer", "value": 1, "min_limit": 5,
       "max_limit": 2}], "min_limit"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "passwordString", "value": "a"}], "data_type"),
    ([{"component": "VendorCtrlr", "variable": "X", "data_type": "string", "value": "a", "connector": 1}],
     "connector needs evse"),
    ([{"component": "Connector", "evse": 3, "variable": "X", "data_type": "string", "value": "a"}], "no EVSE 3"),
    ([{"component": "V", "variable": "X", "data_type": "string", "value": "a"}] * 2, "twice"),
    ([{"component": "V" * 51, "variable": "X", "data_type": "string", "value": "a"}], "50"),
    ([{"component": "V", "variable": "X", "data_type": "string", "value": "a", "foo": 1}], "foo"),
])
def test_yaml_variables_rejected(variables, error):
    with pytest.raises(ValueError, match=error):
        _sim(variables)


def test_yaml_variables_only_for_201():
    with pytest.raises(ValueError, match="variables is for OCPP 2.0.1"):
        _sim(USER_VARS, version="1.6")
    _sim(USER_VARS, version="1.6", ocpp_version="2.0.1")  # the charger's own version counts
