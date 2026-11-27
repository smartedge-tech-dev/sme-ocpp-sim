"""Station201 remote control (OCPP 2.0.1, v0.2 M3) against the ``ocpp``-library 2.0.1 CSMS peer.

Every message has its Accepted and its Rejected (or other negative) branch here. The peer schema-validates
every answer the charger sends.
"""

from __future__ import annotations

import asyncio

import pytest
from ocpp.v201 import call
from test_station201 import CP, _events, _frames, _online, _scenario, _statuses, _until

from sme_ocpp_sim.protocol.base import AuthResult

TX_PROFILE = {"id": 1, "stack_level": 0, "charging_profile_purpose": "TxProfile", "charging_profile_kind": "Relative",
              "charging_schedule": [{"id": 1, "charging_rate_unit": "A",
                                     "charging_schedule_period": [{"start_period": 0, "limit": 16}]}]}


def _start(token="R1", remote_id=1, **kw):
    return call.RequestStartTransaction(id_token={"id_token": token, "type": "Central"}, remote_start_id=remote_id,
                                        **kw)


def _reason(r):
    return (r.status_info or {}).get("reason_code")


def _answer_precedes(s, action):
    """The CALLRESULT to the last CSMS CALL went out before the next ``action`` CALL of the charger."""
    rx = _frames(s)
    last_call = [f for f in _frames(s, "tx") if f[0] == 2][-1]
    answer = next(i for i, f in enumerate(rx) if f[0] == 3 and f[1] == last_call[1])
    follow = [i for i, f in enumerate(rx) if f[0] == 2 and f[2] == action]
    return follow and answer < follow[-1]


# --------------------------------------------------------------------------------------------- RequestStart


def test_request_start_on_a_plugged_evse_starts_after_the_answer():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(2)
        r = await peer.request(CP, _start(evse_id=2, remote_id=42))
        assert r.status == "Accepted" and r.transaction_id is None
        await _until(lambda: len(s.transaction_events) == 1)
        started = s.transaction_events[0]
        assert started["event_type"] == "Started" and started["trigger_reason"] == "RemoteStart"
        assert started["id_token"] == {"id_token": "R1", "type": "Central"}
        assert started["evse"] == {"id": 2, "connector_id": 1}
        assert started["transaction_info"]["remote_start_id"] == 42
        assert _answer_precedes(s, "TransactionEvent")
        assert rt.has_transaction(2) and s.authorizes == []  # the CSMS authorized it
        # The same token presented at the charger ends it.
        assert await rt.swipe(2, "R1") == AuthResult.STOPPED
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "local_only"})


def test_request_start_without_evse_waits_for_the_cable():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(2)
        await rt.fault(1, "GroundFault")
        r = await peer.request(CP, _start(remote_id=7))
        assert r.status == "Accepted"
        await _until(lambda: len(s.transaction_events) == 1)  # the free EVSE with a cable: 2
        assert s.transaction_events[0]["evse"]["id"] == 2
        await rt.clear_fault(1)
        r = await peer.request(CP, _start("R2", remote_id=8, charging_profile=TX_PROFILE))
        assert r.status == "Accepted"
        await _until(lambda: rt.snapshot()["connectors"][0]["pending_tag"] == "R2")
        assert len(s.transaction_events) == 1 and not rt.has_transaction(1)
        await rt.plug(1)
        await _until(lambda: len(s.transaction_events) == 2)
        started = s.transaction_events[1]
        assert started["trigger_reason"] == "RemoteStart" and started["evse"]["id"] == 1  # F02.FR.21
        assert started["transaction_info"]["remote_start_id"] == 8
        assert started["id_token"]["type"] == "Central"
    _scenario(body, cfg_kw={"connectors": 2})


def test_request_start_rejected():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        bad_purpose = dict(TX_PROFILE, charging_profile_purpose="TxDefaultProfile")
        with_tx_id = dict(TX_PROFILE, transaction_id="T1")
        cases = [
            (_start(evse_id=9), "UnknownEvse"),
            (_start(evse_id=1, charging_profile=bad_purpose), "InvalidProfile"),
            (_start(evse_id=1, charging_profile=with_tx_id), "InvalidProfile"),
        ]
        for req, code in cases:
            r = await peer.request(CP, req)
            assert (r.status, _reason(r)) == ("Rejected", code)
        assert (await peer.request(CP, _start(evse_id=1))).status == "Accepted"
        await _until(lambda: rt.has_transaction(1))
        r = await peer.request(CP, _start(evse_id=1, remote_id=2))
        assert (r.status, _reason(r)) == ("Rejected", "Occupied")
        r = await peer.request(CP, _start(remote_id=3))  # no free EVSE left
        assert (r.status, _reason(r)) == ("Rejected", "Occupied")
        await rt.stop_tx(1)
        await rt.fault(1, "PowerLoss")
        r = await peer.request(CP, _start(evse_id=1, remote_id=4))
        assert (r.status, _reason(r)) == ("Rejected", "Faulted")
        await rt.clear_fault(1)
        await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))
        r = await peer.request(CP, _start(evse_id=1, remote_id=5))
        assert (r.status, _reason(r)) == ("Rejected", "Unavailable")
        assert [e["event_type"] for e in s.transaction_events] == ["Started", "Ended"]
    _scenario(body)


# --------------------------------------------------------------------------------------------- RequestStop


def test_request_stop_accepted_and_rejected():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        await _until(lambda: len(s.transaction_events) == 1)
        tx_id = s.transaction_events[0]["transaction_info"]["transaction_id"]
        r = await peer.request(CP, call.RequestStopTransaction(transaction_id="unknown"))
        assert (r.status, _reason(r)) == ("Rejected", "UnknownTransaction")
        r = await peer.request(CP, call.RequestStopTransaction(transaction_id=tx_id))
        assert r.status == "Accepted"
        await _until(lambda: len(_events(s, tx_id)) == 2)
        ended = _events(s, tx_id)[-1]
        assert ended["event_type"] == "Ended" and ended["trigger_reason"] == "RemoteStop"
        assert ended["transaction_info"]["stopped_reason"] == "Remote" and "id_token" not in ended
        assert _answer_precedes(s, "TransactionEvent")
        r = await peer.request(CP, call.RequestStopTransaction(transaction_id=tx_id))  # already ended
        assert r.status == "Rejected"
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


# --------------------------------------------------------------------------------------------- UnlockConnector


def test_unlock_connector_answers():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await peer.request(CP, call.UnlockConnector(evse_id=1, connector_id=1))).status == "Unlocked"
        for evse, conn in ((9, 1), (1, 2)):
            r = await peer.request(CP, call.UnlockConnector(evse_id=evse, connector_id=conn))
            assert r.status == "UnknownConnector"
        rt.station.unlock_fails = True
        assert (await peer.request(CP, call.UnlockConnector(evse_id=1, connector_id=1))).status == "UnlockFailed"
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        r = await peer.request(CP, call.UnlockConnector(evse_id=1, connector_id=1))
        assert r.status == "OngoingAuthorizedTransaction"
        assert rt.has_transaction(1)  # not stopped
        await _until(lambda: len(s.transaction_events) == 1)
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


# --------------------------------------------------------------------------------------------- Reset


def test_reset_immediate_reboots_with_remote_reset_reason():
    async def body(peer, rt):
        s = await _online(peer, rt)
        r = await peer.request(CP, call.Reset(type="Immediate", evse_id=1))
        assert (r.status, _reason(r)) == ("Rejected", "NotSupported")
        r = await peer.request(CP, call.Reset(type="Immediate"))
        assert r.status == "Accepted"
        s2 = await _online(peer, rt, 1)
        assert [b["reason"] for b in s.boot_requests] == ["PowerUp"]
        assert [b["reason"] for b in s2.boot_requests] == ["RemoteReset"]
        # Accepted: the next boot is a plain one (set once the state report after the boot went out).
        await _until(lambda: rt.station.adapter.boot_reason.value == "PowerUp")
    _scenario(body)


def test_reset_immediate_ends_the_transactions_first():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        assert (await peer.request(CP, call.Reset(type="Immediate"))).status == "Accepted"
        s2 = await _online(peer, rt, 1)
        assert [e["event_type"] for e in s.transaction_events] == ["Started", "Ended"]  # sent before the reboot
        ended = s.transaction_events[-1]
        assert ended["trigger_reason"] == "ResetCommand"
        assert ended["transaction_info"]["stopped_reason"] == "ImmediateReset"
        assert s2.transaction_events == [] and not rt.has_transaction(1)
        assert _statuses(s2, 1) == ["Occupied"]  # the cable is still plugged
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_reset_on_idle_is_scheduled_until_the_transaction_ends():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        assert (await peer.request(CP, call.Reset(type="OnIdle"))).status == "Scheduled"
        await _until(lambda: len(s.transaction_events) == 1)
        assert len(peer.history) == 1 and rt.has_transaction(1)  # no reboot yet
        await rt.unplug(1)
        s2 = await _online(peer, rt, 1)
        assert s.transaction_events[-1]["transaction_info"]["stopped_reason"] == "EVDisconnected"
        assert s2.boot_requests[0]["reason"] == "RemoteReset"
        # Idle: OnIdle resets at once.
        assert (await peer.request(CP, call.Reset(type="OnIdle"))).status == "Accepted"
        await _online(peer, rt, 2)
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


# --------------------------------------------------------------------------------------------- ChangeAvailability


def test_change_availability_station_and_evse_level():
    async def body(peer, rt):
        s = await _online(peer, rt)
        r = await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))
        assert r.status == "Accepted"
        await _until(lambda: _statuses(s, 1)[-1:] == ["Unavailable"] and _statuses(s, 2)[-1:] == ["Unavailable"])
        assert _answer_precedes(s, "StatusNotification")
        assert await rt.swipe(1, "TAG1") == AuthResult.UNAVAILABLE
        r = await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))
        assert r.status == "Accepted"  # unchanged
        # G03 / G04 Note 1: an EVSE Operative does not undo the station's Inoperative.
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 1}, operational_status="Operative"))
        assert r.status == "Accepted"
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 2}, operational_status="Inoperative"))
        assert r.status == "Accepted"
        await asyncio.sleep(0.2)
        assert _statuses(s, 1)[-1] == "Unavailable" and _statuses(s, 2)[-1] == "Unavailable"
        assert rt.snapshot()["station"]["operative"] is False
        for evse in ({"id": 9}, {"id": 1, "connector_id": 2}, {"id": 0, "connector_id": 1}):
            r = await peer.request(CP, call.ChangeAvailability(evse=evse, operational_status="Operative"))
            assert (r.status, _reason(r)) == ("Rejected", "UnknownEvse")
        assert rt.snapshot()["connectors"][1]["operative"] is False
        # G04.FR.08: the station Operative brings every EVSE back to its own state.
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 0}, operational_status="Operative"))
        assert r.status == "Accepted"
        await _until(lambda: _statuses(s, 1)[-1] == "Available")
        await asyncio.sleep(0.2)
        assert _statuses(s, 2)[-1] == "Unavailable" and rt.snapshot()["station"]["operative"] is True
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "accept_all"})


def test_change_availability_is_scheduled_while_a_transaction_runs_and_survives_a_reset():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 1, "connector_id": 1},
                                                           operational_status="Inoperative"))
        assert r.status == "Scheduled"
        snap = rt.snapshot()["connectors"][0]
        assert snap["operative"] is True and snap["pending_operative"] is False and rt.has_transaction(1)
        # Operative cancels the scheduled change; Inoperative schedules it again.
        r = await peer.request(CP, call.ChangeAvailability(evse={"id": 1}, operational_status="Operative"))
        assert r.status == "Accepted" and rt.snapshot()["connectors"][0]["pending_operative"] is None
        r = await peer.request(CP, call.ChangeAvailability(operational_status="Inoperative"))
        assert r.status == "Scheduled"
        await rt.stop_tx(1)
        await _until(lambda: _statuses(s, 1)[-1:] == ["Unavailable"])
        assert rt.snapshot()["connectors"][0]["operative"] is False
        assert (await peer.request(CP, call.Reset(type="Immediate"))).status == "Accepted"
        s2 = await _online(peer, rt, 1)
        assert _statuses(s2, 1) == ["Unavailable"]
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


# --------------------------------------------------------------------------------------------- TriggerMessage


def test_trigger_message_accepted():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await peer.request(CP, call.TriggerMessage(requested_message="Heartbeat"))).status == "Accepted"
        await _until(lambda: s.heartbeats == 1)
        r = await peer.request(CP, call.TriggerMessage(requested_message="StatusNotification",
                                                       evse={"id": 2, "connector_id": 1}))
        assert r.status == "Accepted"
        await _until(lambda: len(s.statuses) == 3)
        assert s.statuses[-1] == (2, 1, "Available")
        assert (await peer.request(CP, call.TriggerMessage(requested_message="MeterValues"))).status == "Accepted"
        await _until(lambda: len(s.meter_values) == 2)
        assert [mv["evse_id"] for mv in s.meter_values] == [1, 2]
        assert s.meter_values[0]["meter_value"][0]["sampled_value"][0]["context"] == "Trigger"
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        r = await peer.request(CP, call.TriggerMessage(requested_message="TransactionEvent"))
        assert r.status == "Accepted"
        await _until(lambda: len(s.transaction_events) == 2)
        ev = s.transaction_events[1]
        assert ev["event_type"] == "Updated" and ev["trigger_reason"] == "Trigger" and ev["seq_no"] == 1
        assert "evse" not in ev and ev["meter_value"][0]["sampled_value"][0]["context"] == "Trigger"
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "accept_all"})


def test_trigger_message_rejected_and_not_implemented():
    async def body(peer, rt):
        await _online(peer, rt)
        cases = [
            (call.TriggerMessage(requested_message="BootNotification"), "Rejected", "AlreadyRegistered"),
            (call.TriggerMessage(requested_message="TransactionEvent"), "Rejected", "NoTransaction"),
            (call.TriggerMessage(requested_message="TransactionEvent", evse={"id": 1}), "Rejected", "NoTransaction"),
            (call.TriggerMessage(requested_message="MeterValues", evse={"id": 3}), "Rejected", "UnknownEvse"),
            (call.TriggerMessage(requested_message="StatusNotification", evse={"id": 1, "connector_id": 2}),
             "Rejected", "UnknownEvse"),
            (call.TriggerMessage(requested_message="SignV2GCertificate"), "NotImplemented", None),
            (call.TriggerMessage(requested_message="SignCombinedCertificate"), "NotImplemented", None),
        ]
        for req, status, code in cases:
            r = await peer.request(CP, req)
            assert (r.status, _reason(r)) == (status, code), req
    _scenario(body)


def test_trigger_boot_notification_while_pending():
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        s = peer.history[0]
        # B02.FR.02: a triggered message may be sent while Pending.
        r = await peer.request(CP, call.TriggerMessage(requested_message="Heartbeat"))
        assert r.status == "Accepted"
        await _until(lambda: s.heartbeats == 1)
        r = await peer.request(CP, call.TriggerMessage(requested_message="BootNotification"))
        assert r.status == "Accepted"
        await _until(lambda: rt.online)  # well before the 60 s retry interval
        assert [b["reason"] for b in s.boot_requests] == ["PowerUp", "Triggered"]
        assert s.boot_statuses == ["Pending", "Accepted"]
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 60})


@pytest.mark.parametrize("message", ["PublishFirmwareStatusNotification",
                                     "SignV2GCertificate", "SignCombinedCertificate"])
def test_trigger_message_other_values_not_implemented(message):
    async def body(peer, rt):
        await _online(peer, rt)
        r = await peer.request(CP, call.TriggerMessage(requested_message=message))
        assert r.status == "NotImplemented"
    _scenario(body)


def test_a_waiting_token_keeps_its_evse():
    async def body(peer, rt):
        s = await _online(peer, rt)
        assert (await peer.request(CP, _start(evse_id=1, remote_id=1))).status == "Accepted"
        await _until(lambda: rt.snapshot()["connectors"][0]["pending_tag"] == "R1")
        r = await peer.request(CP, _start("R2", evse_id=1, remote_id=2))
        assert (r.status, _reason(r)) == ("Rejected", "Occupied")
        assert await rt.swipe(2, "LOCAL") == AuthResult.ACCEPTED  # waits on EVSE 2
        r = await peer.request(CP, _start("R3", remote_id=3))  # no EVSE without a waiting token
        assert (r.status, _reason(r)) == ("Rejected", "Occupied")
        await rt.plug(1)
        await _until(lambda: len(s.transaction_events) == 1)
        assert s.transaction_events[0]["transaction_info"]["remote_start_id"] == 1
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "accept_all"})


def test_immediate_reset_overrides_a_scheduled_on_idle_reset():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED
        assert (await peer.request(CP, call.Reset(type="OnIdle"))).status == "Scheduled"
        assert (await peer.request(CP, call.Reset(type="Immediate"))).status == "Accepted"
        s2 = await _online(peer, rt, 1)
        assert s.transaction_events[-1]["transaction_info"]["stopped_reason"] == "ImmediateReset"
        assert s2.boot_requests[0]["reason"] == "RemoteReset"
        assert await rt.swipe(1, "TAG1") == AuthResult.ACCEPTED  # nothing scheduled any more
        await _until(lambda: len(s2.transaction_events) == 1)
        assert len(peer.history) == 2
    _scenario(body, cfg_kw={"auth_mode": "accept_all"})


def test_reset_while_pending_reboots_at_once():
    async def body(peer, rt):
        await _until(lambda: rt.registration == "Pending")
        assert (await peer.request(CP, call.Reset(type="Immediate"))).status == "Accepted"
        s2 = await _online(peer, rt, 1)
        assert s2.boot_requests[0]["reason"] == "RemoteReset"
    _scenario(body, {"boot_statuses": ["Pending"], "retry_interval": 60})
