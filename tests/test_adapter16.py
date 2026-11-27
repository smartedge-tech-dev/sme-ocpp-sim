"""Adapter16 against a fake runtime / fake CALL function (no network)."""

import asyncio
from datetime import datetime, timezone

import pytest

from sme_ocpp_sim.protocol.v16 import profiles as cp
from sme_ocpp_sim.protocol.v16.composite import CompositePeriod, CompositeSchedule
from sme_ocpp_sim.protocol import base as d
from sme_ocpp_sim.protocol.v16.adapter import Adapter16
from sme_ocpp_sim.rpc import CallError

PCV = "PropertyConstraintViolation"
TS = datetime(2026, 11, 26, 1, 2, 3, 456000, tzinfo=timezone.utc)


class FakeRuntime:
    connector_count = 2

    def __init__(self, result=None):
        self.cmds = []
        self.result = result

    async def handle(self, cmd):
        self.cmds.append(cmd)
        return self.result(cmd) if callable(self.result) else self.result


def _call(action, payload, result=None, strict=False):
    rt = FakeRuntime(result)
    out = asyncio.run(Adapter16(strict=strict).handlers(rt)[action](payload))
    return out, rt.cmds


def _error(action, payload, result=None, strict=False):
    with pytest.raises(CallError) as ei:
        _call(action, payload, result, strict)
    return ei.value.code


A, R = d.CommandStatus.ACCEPTED, d.CommandStatus.REJECTED


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected")])
def test_remote_start(st, wire):
    out, cmds = _call("RemoteStartTransaction", {"idTag": "T1", "connectorId": 2}, st)
    assert out == {"status": wire}
    assert cmds == [d.RequestStart(2, "T1", None)]


def test_remote_start_without_connector():
    _, cmds = _call("RemoteStartTransaction", {"idTag": "T1"}, A)
    assert cmds[0].evse_id is None


@pytest.mark.parametrize("cid", [0, 3, -1])
def test_remote_start_connector_out_of_range(cid):
    assert _error("RemoteStartTransaction", {"idTag": "T1", "connectorId": cid}, A) == PCV


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected")])
def test_remote_stop(st, wire):
    out, cmds = _call("RemoteStopTransaction", {"transactionId": 42}, st)
    assert out == {"status": wire} and cmds == [d.RequestStop("42")]


def test_get_configuration():
    res = d.GetConfigResult([d.ConfigEntry("HeartbeatInterval", False, "300")], ["Nope"])
    out, cmds = _call("GetConfiguration", {"key": ["HeartbeatInterval", "Nope"]}, res)
    assert cmds == [d.GetConfig(("HeartbeatInterval", "Nope"))]
    assert out == {"configurationKey": [{"key": "HeartbeatInterval", "readonly": False, "value": "300"}],
                   "unknownKey": ["Nope"]}


def test_get_configuration_all():
    out, cmds = _call("GetConfiguration", {"key": []}, d.GetConfigResult([], []))
    assert cmds == [d.GetConfig(())] and out == {"configurationKey": []}
    _, cmds = _call("GetConfiguration", {}, d.GetConfigResult([], []))
    assert cmds == [d.GetConfig(())]


@pytest.mark.parametrize("st", list(d.ConfigStatus))
def test_change_configuration(st):
    out, cmds = _call("ChangeConfiguration", {"key": "HeartbeatInterval", "value": "10"}, st)
    assert out == {"status": st.value} and cmds == [d.SetConfig("HeartbeatInterval", "10")]


@pytest.mark.parametrize("kind", ["Soft", "Hard"])
@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected")])
def test_reset(kind, st, wire):
    out, cmds = _call("Reset", {"type": kind}, st)
    assert out == {"status": wire} and cmds == [d.Reset(d.ResetKind(kind))]


@pytest.mark.parametrize("msg", ["BootNotification", "Heartbeat", "MeterValues", "StatusNotification"])
@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected"), (d.CommandStatus.NOT_IMPLEMENTED,
                                                                         "NotImplemented")])
def test_trigger(msg, st, wire):
    out, cmds = _call("TriggerMessage", {"requestedMessage": msg, "connectorId": 1}, st)
    assert out == {"status": wire} and cmds == [d.Trigger(d.TriggerWhat(msg), 1)]


@pytest.mark.parametrize("msg", ["DiagnosticsStatusNotification", "FirmwareStatusNotification"])
def test_trigger_maintenance_status(msg):
    """M7: the firmware / diagnostics triggers go to the station like the others (they were NotImplemented)."""
    out, cmds = _call("TriggerMessage", {"requestedMessage": msg}, A)
    assert out == {"status": "Accepted"} and cmds == [d.Trigger(d.TriggerWhat(msg), None)]


def test_update_firmware_and_get_diagnostics():
    out, cmds = _call("UpdateFirmware", {"location": "ftp://x/fw.bin", "retrieveDate": "2026-11-27T10:00:00Z",
                                         "retries": 2, "retryInterval": 5})
    assert out == {} and cmds[0].location == "ftp://x/fw.bin" and cmds[0].retries == 2
    assert cmds[0].retry_interval_s == 5 and cmds[0].retrieve_at.tzinfo is not None
    out, cmds = _call("GetDiagnostics", {"location": "ftp://x/"}, "CP1-diagnostics.log")
    assert out == {"fileName": "CP1-diagnostics.log"} and isinstance(cmds[0], d.GetDiagnostics)
    out, _ = _call("GetDiagnostics", {"location": "ftp://x/"}, None)
    assert out == {}


def test_trigger_connector_range():
    _, cmds = _call("TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 0}, A)
    assert cmds[0].evse_id == 0
    assert _error("TriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 3}, A) == PCV


@pytest.mark.parametrize("st", list(d.UnlockStatus))
def test_unlock(st):
    out, cmds = _call("UnlockConnector", {"connectorId": 1}, st)
    assert out == {"status": st.value} and cmds == [d.Unlock(1)]


@pytest.mark.parametrize("cid", [0, 3])
def test_unlock_out_of_range(cid):
    assert _error("UnlockConnector", {"connectorId": cid}, d.UnlockStatus.UNLOCKED) == PCV


@pytest.mark.parametrize("st", list(d.AvailabilityStatus))
@pytest.mark.parametrize("kind, operative", [("Operative", True), ("Inoperative", False)])
def test_change_availability(st, kind, operative):
    out, cmds = _call("ChangeAvailability", {"connectorId": 0, "type": kind}, st)
    assert out == {"status": st.value} and cmds == [d.ChangeAvailability(0, operative)]


def test_change_availability_out_of_range():
    assert _error("ChangeAvailability", {"connectorId": 3, "type": "Operative"},
                  d.AvailabilityStatus.ACCEPTED) == PCV


def test_data_transfer():
    out, cmds = _call("DataTransfer", {"vendorId": "X", "messageId": "m", "data": "q"},
                      d.DataTransferResult(d.DataTransferStatus.UNKNOWN_VENDOR_ID))
    assert out == {"status": "UnknownVendorId"} and cmds == [d.DataTransfer("X", "m", "q")]
    out, _ = _call("DataTransfer", {"vendorId": "X"}, d.DataTransferResult(d.DataTransferStatus.ACCEPTED, "r"))
    assert out == {"status": "Accepted", "data": "r"}


def _wire_profile(purpose="TxDefaultProfile", unit="W", **kw):
    return {"chargingProfileId": 7, "stackLevel": 1, "chargingProfilePurpose": purpose,
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {"chargingRateUnit": unit, "startSchedule": "2026-11-26T00:00:00",
                                 "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 16, "numberPhases": 1},
                                                            {"startPeriod": 60, "limit": 8.5}]},
            **kw}


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected"), (d.CommandStatus.NOT_SUPPORTED, "NotSupported")])
def test_set_charging_profile(st, wire):
    out, cmds = _call("SetChargingProfile", {"connectorId": 0, "csChargingProfiles": _wire_profile(unit="A")}, st)
    assert out == {"status": wire}
    cmd = cmds[0]
    assert isinstance(cmd, d.SetProfile) and cmd.evse_id == 0
    p = cmd.profile
    assert (p.profile_id, p.stack_level, p.purpose, p.kind) == (
        7, 1, cp.ChargingProfilePurpose.TX_DEFAULT, cp.ChargingProfileKind.ABSOLUTE)
    assert p.schedule.unit is cp.ChargingRateUnit.A
    assert p.schedule.periods == (cp.SchedulePeriod(0, 16, 1), cp.SchedulePeriod(60, 8.5, None))
    assert p.schedule.start_schedule == datetime(2026, 11, 26, tzinfo=timezone.utc)  # naive -> UTC


def test_set_charging_profile_errors():
    good = _wire_profile()
    assert _error("SetChargingProfile", {"connectorId": 3, "csChargingProfiles": good}, A) == PCV
    # errata 3.24: an invalid chargingRateUnit is PropertyConstraintViolation
    assert _error("SetChargingProfile", {"connectorId": 1, "csChargingProfiles": _wire_profile(unit="kW")}, A) == PCV
    assert _error("GetCompositeSchedule", {"connectorId": 1, "duration": 60, "chargingRateUnit": "kW"}, A) == PCV


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (d.CommandStatus.UNKNOWN, "Unknown")])
def test_clear_charging_profile(st, wire):
    out, cmds = _call("ClearChargingProfile", {"connectorId": 1, "chargingProfilePurpose": "TxProfile",
                                               "stackLevel": 2}, st)
    assert out == {"status": wire}
    assert cmds == [d.ClearProfiles(None, 1, cp.ChargingProfilePurpose.TX, 2)]
    _, cmds = _call("ClearChargingProfile", {"id": 5}, st)
    assert cmds == [d.ClearProfiles(5)]
    assert _error("ClearChargingProfile", {"connectorId": 9}, A) == PCV


def test_get_composite_schedule():
    sched = CompositeSchedule(1, TS, 300, cp.ChargingRateUnit.A, (
        CompositePeriod(0, 10.04, 1), CompositePeriod(60, 9.96, 1), CompositePeriod(120, 6.0, 1)))
    res = d.GetCompositeResult(A, 1, TS, sched)
    out, cmds = _call("GetCompositeSchedule", {"connectorId": 1, "duration": 300, "chargingRateUnit": "A"}, res)
    assert cmds == [d.GetComposite(1, 300, cp.ChargingRateUnit.A)]
    assert out == {"status": "Accepted", "connectorId": 1, "scheduleStart": "2026-11-26T01:02:03.456Z",
                   "chargingSchedule": {
                       "chargingRateUnit": "A", "duration": 300, "startSchedule": "2026-11-26T01:02:03.456Z",
                       "chargingSchedulePeriod": [  # 1-decimal rounding merges 10.04 / 9.96
                           {"startPeriod": 0, "limit": 10.0, "numberPhases": 1},
                           {"startPeriod": 120, "limit": 6.0, "numberPhases": 1}]}}
    _, cmds = _call("GetCompositeSchedule", {"connectorId": 0, "duration": 5}, res)
    assert cmds[0].unit is None


@pytest.mark.parametrize("payload", [{"connectorId": 3, "duration": 60}, {"connectorId": 1, "duration": 0}])
def test_get_composite_schedule_rejected_without_runtime(payload):
    out, cmds = _call("GetCompositeSchedule", payload, None)
    assert out == {"status": "Rejected"} and cmds == []


def test_get_composite_schedule_runtime_rejects():
    out, _ = _call("GetCompositeSchedule", {"connectorId": 1, "duration": 60}, d.GetCompositeResult(R))
    assert out == {"status": "Rejected"}


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected")])
def test_remote_start_with_tx_profile(st, wire):
    out, cmds = _call("RemoteStartTransaction",
                      {"idTag": "T1", "connectorId": 1, "chargingProfile": _wire_profile("TxProfile")}, st)
    assert out == {"status": wire}
    assert cmds[0].profile.purpose is cp.ChargingProfilePurpose.TX and cmds[0].profile.transaction_id is None


@pytest.mark.parametrize("profile", [_wire_profile("TxDefaultProfile"), _wire_profile("TxProfile", transactionId=4)])
def test_remote_start_with_invalid_profile_rejected(profile):
    out, cmds = _call("RemoteStartTransaction", {"idTag": "T1", "chargingProfile": profile}, A)
    assert out == {"status": "Rejected"} and cmds == []


def test_unlisted_actions_have_no_handler():
    h = Adapter16().handlers(FakeRuntime())
    assert "SignCertificate" not in h and "SignedUpdateFirmware" not in h
    assert {"UpdateFirmware", "GetDiagnostics"} <= set(h)  # M7
    assert {"SendLocalList", "GetLocalListVersion", "ClearCache", "ReserveNow", "CancelReservation"} <= set(h)


def test_lenient_and_strict_unknown_fields():
    out, _ = _call("Reset", {"type": "Soft", "extra": 1}, A)
    assert out == {"status": "Accepted"}
    assert _error("Reset", {"type": "Soft", "extra": 1}, A, strict=True) == "FormationViolation"


def test_schema_errors_mapped():
    assert _error("Reset", {}, A) == "OccurenceConstraintViolation"
    assert _error("Reset", {"type": "Warm"}, A) == PCV


# ------------------------------------------------------------------ outbound


class FakeCall:
    def __init__(self, responses):
        self.responses, self.sent = responses, []

    async def __call__(self, action, payload):
        self.sent.append((action, payload))
        return self.responses[action]


def _out(coro_fn, responses):
    a = Adapter16()
    fc = FakeCall(responses)
    a.bind(fc)
    return asyncio.run(coro_fn(a)), fc.sent


def test_send_booting():
    res, sent = _out(lambda a: a.send_booting(d.Booting("V", "M", "S1", "1.0")), {"BootNotification": {
        "status": "Pending", "currentTime": "2026-11-26T00:00:00Z", "interval": 5}})
    assert sent == [("BootNotification", {"chargePointVendor": "V", "chargePointModel": "M",
                                          "chargePointSerialNumber": "S1", "firmwareVersion": "1.0"})]
    assert res.status is d.BootStatus.PENDING and res.interval == 5 and res.current_time.year == 2026


def test_send_status_and_heartbeat():
    res, sent = _out(lambda a: a.send_connector_status(d.ConnectorStatus(1, 1, d.DomainStatus.CHARGING,
                                                                         timestamp=TS)),
                     {"StatusNotification": {}})
    assert sent[0][1] == {"connectorId": 1, "errorCode": "NoError", "status": "Charging",
                          "timestamp": "2026-11-26T01:02:03.456Z"}
    t, _ = _out(lambda a: a.send_heartbeat(d.Heartbeat()), {"Heartbeat": {"currentTime": "2026-11-26T00:00:00Z"}})
    assert t.tzinfo is not None


def test_send_transaction_messages():
    resp = {"StartTransaction": {"transactionId": 7, "idTagInfo": {"status": "Blocked"}},
            "MeterValues": {}, "StopTransaction": {}, "Authorize": {"idTagInfo": {"status": "Accepted"}}}
    sample = [d.Sample("Energy.Active.Import.Register", 1234, "Wh", location="Outlet"),
              d.Sample("Power.Active.Import", 7400.5, "W", location="Outlet")]

    async def flow(a):
        info = await a.send_authorize(d.AuthorizeRequest("T1"))
        started = await a.send_tx_started(d.TxStarted(1, "T1", 1000, TS))
        await a.send_tx_updated(d.TxUpdated(1, started.tx_id, TS, sample))
        await a.send_tx_updated(d.TxUpdated(1, started.tx_id, TS, ()))  # nothing to send
        ended = await a.send_tx_ended(d.TxEnded(1, started.tx_id, 1500, TS, d.StopReason.LOCAL, "T1", sample[:1]))
        return info, started, ended

    (info, started, ended), sent = _out(flow, resp)
    assert info.accepted and started == d.TxStartedResult("7", d.IdTokenInfo("Blocked")) and ended is None
    assert [a for a, _ in sent] == ["Authorize", "StartTransaction", "MeterValues", "StopTransaction"]
    mv = sent[2][1]
    assert mv["transactionId"] == 7 and mv["connectorId"] == 1
    assert mv["meterValue"][0]["sampledValue"][1] == {
        "value": "7400.5", "context": "Sample.Periodic", "measurand": "Power.Active.Import", "unit": "W",
        "location": "Outlet"}
    stop = sent[3][1]
    assert stop["transactionId"] == 7 and stop["reason"] == "Local" and stop["meterStop"] == 1500
    assert stop["transactionData"][0]["sampledValue"][0]["context"] == "Transaction.End"


def test_send_meter_sample_and_data_transfer():
    resp = {"MeterValues": {}, "DataTransfer": {"status": "Rejected", "data": "x"}}

    async def flow(a):
        await a.send_meter_sample(d.MeterSample(2, TS, [d.Sample("Voltage", 230, "V")], "Trigger"))
        return await a.send_data_transfer(d.DataTransfer("V", "m", "d"))

    res, sent = _out(flow, resp)
    assert "transactionId" not in sent[0][1] and sent[0][1]["meterValue"][0]["sampledValue"][0]["context"] == "Trigger"
    assert sent[1] == ("DataTransfer", {"vendorId": "V", "messageId": "m", "data": "d"})
    assert res == d.DataTransferResult(d.DataTransferStatus.REJECTED, "x")


def test_bad_response_and_unbound():
    with pytest.raises(CallError):
        _out(lambda a: a.send_heartbeat(d.Heartbeat()), {"Heartbeat": {}})
    with pytest.raises(ConnectionError):
        asyncio.run(Adapter16().send_heartbeat(d.Heartbeat()))


# --------------------------------------------------------------------------------------------- M6: local list / reservations


def test_send_local_list_full_and_differential():
    entries = [{"idTag": "T1", "idTagInfo": {"status": "Accepted", "expiryDate": "2026-11-27T00:00:00Z",
                                             "parentIdTag": "P"}},
               {"idTag": "T2"}]
    out, cmds = _call("SendLocalList", {"listVersion": 5, "updateType": "Differential",
                                        "localAuthorizationList": entries}, d.LocalListStatus.VERSION_MISMATCH)
    assert out == {"status": "VersionMismatch"}
    cmd = cmds[0]
    assert (cmd.version, cmd.full) == (5, False)
    assert cmd.entries == (("T1", d.IdTokenInfo(d.AuthResult.ACCEPTED, datetime(2026, 11, 27, tzinfo=timezone.utc),
                                                "P")), ("T2", None))
    out, cmds = _call("SendLocalList", {"listVersion": 1, "updateType": "Full"}, d.LocalListStatus.NOT_SUPPORTED)
    assert out == {"status": "NotSupported"} and cmds == [d.SendLocalList(1, True, ())]


@pytest.mark.parametrize("version", [0, -1])
def test_send_local_list_version_must_be_positive(version):
    """Errata 3.52: listVersion SHALL NOT be -1 or 0."""
    assert _error("SendLocalList", {"listVersion": version, "updateType": "Full"},
                  d.LocalListStatus.ACCEPTED) == PCV


def test_get_local_list_version_and_clear_cache():
    out, cmds = _call("GetLocalListVersion", {}, 7)
    assert out == {"listVersion": 7} and cmds == [d.GetLocalListVersion()]
    assert _call("ClearCache", {}, A)[0] == {"status": "Accepted"}
    assert _call("ClearCache", {}, R)[0] == {"status": "Rejected"}


@pytest.mark.parametrize("st", list(d.ReservationStatus))
def test_reserve_now(st):
    out, cmds = _call("ReserveNow", {"connectorId": 2, "expiryDate": "2026-11-26T01:02:03.456Z", "idTag": "T1",
                                     "parentIdTag": "P", "reservationId": 9}, st)
    assert out == {"status": st.value}
    assert cmds == [d.ReserveNow(9, 2, "T1", TS, "P")]


@pytest.mark.parametrize("cid", [3, -1])
def test_reserve_now_connector_out_of_range(cid):
    assert _error("ReserveNow", {"connectorId": cid, "expiryDate": "2026-11-26T01:02:03Z", "idTag": "T1",
                                 "reservationId": 1}, d.ReservationStatus.ACCEPTED) == PCV


@pytest.mark.parametrize("st, wire", [(A, "Accepted"), (R, "Rejected")])
def test_cancel_reservation(st, wire):
    out, cmds = _call("CancelReservation", {"reservationId": 4}, st)
    assert out == {"status": wire} and cmds == [d.CancelReservation(4)]


def test_extended_trigger_connector_must_be_positive():
    """WP ed.4 §5.5: ExtendedTriggerMessage connectorId > 0 (the schema allows any integer)."""
    for cid in (0, 3):
        assert _error("ExtendedTriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": cid}, A) == PCV
    _, cmds = _call("ExtendedTriggerMessage", {"requestedMessage": "StatusNotification", "connectorId": 2}, A)
    assert cmds == [d.Trigger(d.TriggerWhat.STATUS, 2, extended=True)]
