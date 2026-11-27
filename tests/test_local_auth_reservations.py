"""v0.2 M6: Local Authorization List, Authorization Cache and reservations, OCPP 1.6J and 2.0.1.

The domain scenarios run against the ``ocpp``-library CSMS peer of each version (the peer schema-validates every
message); the version differences (message shapes, 2.0.1 ReservationStatusUpdate, 1.6 connector 0) are spelled
out where they matter. The version-neutral stores of ``runtime.local_auth`` are tested directly at the end.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from ocpp.v16 import call as call16
from ocpp.v201 import call as call201
from test_runtime_charger import CP, PEERS, _scenario, _until

from sme_ocpp_sim.protocol.base import AuthResult as A
from sme_ocpp_sim.protocol.base import IdTokenInfo
from sme_ocpp_sim.protocol.v201 import device_model as dm
from sme_ocpp_sim.runtime.local_auth import AuthCache, ListUpdate, LocalAuthList, Reservation

VERSIONS = pytest.mark.parametrize("version", list(PEERS))

#: logical setting -> (1.6 configuration key, 2.0.1 Device Model variable)
SETTINGS = {
    "list": ("LocalAuthListEnabled", dm.LOCAL_LIST_ENABLED),
    "cache": ("AuthorizationCacheEnabled", dm.AUTH_CACHE_ENABLED),
    "pre": ("LocalPreAuthorize", dm.LOCAL_PRE_AUTHORIZE),
}


# --------------------------------------------------------------------------------------------- helpers


def _v16(version):
    return version == "1.6"


def _set(rt, version, name, value: bool):
    key, var = SETTINGS[name]
    if _v16(version):
        assert rt.station.store.set(key, "true" if value else "false").value == "Accepted"
    else:
        rt.station.dm.put(var, "true" if value else "false")


async def _online(peer, rt, version):
    n = rt.connector_count + (1 if _v16(version) else 0)  # 1.6 also reports connector 0
    await _until(lambda: rt.online and CP in peer.sessions and len(peer.sessions[CP].statuses) >= n)
    return peer.sessions[CP]


def _statuses(s, version, cid):
    if _v16(version):
        return [st for c, st, _ in s.statuses if c == cid]
    return [st for e, _, st in s.statuses if e == cid]


def _authorizes(s, version):
    return list(s.authorizes) if _v16(version) else [x["id_token"] for x in s.authorizes]


def _starts(s, version):
    """(connector / EVSE, id tag, reservationId) per started transaction."""
    if _v16(version):
        return [(x["connector_id"], x["id_tag"], x.get("reservation_id")) for x in s.starts]
    return [(x["evse"]["id"], x["id_token"]["id_token"], x.get("reservation_id"))
            for x in s.transaction_events if x["event_type"] == "Started"]


def _ended(s, version):
    if _v16(version):
        return len(s.stops)
    return sum(1 for x in s.transaction_events if x["event_type"] == "Ended")


def _iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _send_list(peer, version, list_version, full, entries=None):
    """``entries``: [(tag, status or None, parent or None)]; ``None`` status = no idTagInfo (remove)."""
    if _v16(version):
        items = None
        if entries is not None:
            items = []
            for tag, status, parent in entries:
                item = {"id_tag": tag}
                if status is not None:
                    item["id_tag_info"] = {"status": status, **({"parent_id_tag": parent} if parent else {})}
                items.append(item)
        r = await peer.request(CP, call16.SendLocalList(list_version=list_version,
                                                       update_type="Full" if full else "Differential",
                                                       local_authorization_list=items))
        return None if r is None else r.status
    items = None
    if entries:
        items = []
        for tag, status, parent in entries:
            item = {"id_token": {"id_token": tag, "type": "ISO14443"}}
            if status is not None:
                info = {"status": status}
                if parent:
                    info["group_id_token"] = {"id_token": parent, "type": "Central"}
                item["id_token_info"] = info
            items.append(item)
    r = await peer.request(CP, call201.SendLocalList(version_number=list_version,
                                                    update_type="Full" if full else "Differential",
                                                    local_authorization_list=items))
    return None if r is None else r.status


async def _list_version(peer, version):
    if _v16(version):
        return (await peer.request(CP, call16.GetLocalListVersion())).list_version
    return (await peer.request(CP, call201.GetLocalListVersion())).version_number


async def _clear_cache(peer, version):
    return (await peer.request(CP, (call16 if _v16(version) else call201).ClearCache())).status


async def _reserve(peer, version, rid, connector, tag, seconds=60.0, parent=None):
    """``connector`` 0 = 1.6 connectorId 0 / 2.0.1 without ``evseId``."""
    if _v16(version):
        r = await peer.request(CP, call16.ReserveNow(connector_id=connector, expiry_date=_iso(seconds), id_tag=tag,
                                                     reservation_id=rid, parent_id_tag=parent))
    else:
        group = {"id_token": parent, "type": "Central"} if parent else None
        r = await peer.request(CP, call201.ReserveNow(id=rid, expiry_date_time=_iso(seconds),
                                                      id_token={"id_token": tag, "type": "ISO14443"},
                                                      evse_id=connector or None, group_id_token=group))
    return r.status


async def _cancel(peer, version, rid):
    mod = call16 if _v16(version) else call201
    return (await peer.request(CP, mod.CancelReservation(reservation_id=rid))).status


async def _inoperative(peer, version, connector):
    if _v16(version):
        r = await peer.change_availability(CP, connector, "Inoperative")
    else:
        r = await peer.request(CP, call201.ChangeAvailability(operational_status="Inoperative",
                                                              evse={"id": connector}))
    assert r.status == "Accepted"


def _run(body, version, connectors=2, cfg_kw=None, peer_kw=None):
    kw = {"connectors": connectors, "auth_mode": "csms_only", **(cfg_kw or {})}
    _scenario(body, peer_kw, kw, version=version)


# --------------------------------------------------------------------------------------------- local list


@VERSIONS
def test_local_list_full_differential_and_version_numbers(version):
    async def body(peer, rt):
        await _online(peer, rt, version)
        st = rt.station
        assert await _list_version(peer, version) == 0  # no list received yet
        assert await _send_list(peer, version, 3, True, [("A", "Accepted", None), ("B", "Blocked", "G")]) \
            == "Accepted"
        assert await _list_version(peer, version) == 3
        assert st.local_list.get("B") == IdTokenInfo(A.BLOCKED, None, "G")
        # Differential: the version must go up; an entry without info is removed.
        assert await _send_list(peer, version, 3, False, [("C", "Accepted", None)]) == "VersionMismatch"
        assert await _send_list(peer, version, 2, False, [("C", "Accepted", None)]) == "VersionMismatch"
        assert await _send_list(peer, version, 5, False, [("B", None, None), ("C", "Accepted", None)]) \
            == "Accepted"
        assert set(st.local_list.entries()) == {"A", "C"} and await _list_version(peer, version) == 5
        # A differential update without entries changes only the version (D01.FR.05).
        assert await _send_list(peer, version, 6, False) == "Accepted"
        assert set(st.local_list.entries()) == {"A", "C"} and await _list_version(peer, version) == 6
        # Full replaces the list with any version > 0; refused updates change nothing.
        assert await _send_list(peer, version, 2, True, [("D", "Accepted", None)]) == "Accepted"
        assert set(st.local_list.entries()) == {"D"} and await _list_version(peer, version) == 2
        assert await _send_list(peer, version, 9, True, [("E", "Accepted", None), ("E", "Blocked", None)]) \
            == "Failed"  # duplicates
        assert await _send_list(peer, version, 9, True, [("E", None, None)]) == "Failed"  # Full needs info
        assert set(st.local_list.entries()) == {"D"} and await _list_version(peer, version) == 2
        assert await _send_list(peer, version, 0, True) is None  # CALLERROR PropertyConstraintViolation
        # Full without entries empties the list but keeps the version (D01.FR.04).
        assert await _send_list(peer, version, 4, True) == "Accepted"
        assert st.local_list.entries() == {} and await _list_version(peer, version) == 4
        assert rt.snapshot()["local_list_version"] == 4
        # Disabled: refused; 2.0.1 reports version 0 then (D02.FR.03), 1.6 the stored version.
        _set(rt, version, "list", False)
        assert await _send_list(peer, version, 7, True) == ("NotSupported" if _v16(version) else "Failed")
        assert await _list_version(peer, version) == (4 if _v16(version) else 0)
    _run(body, version)


@VERSIONS
def test_local_list_has_priority_over_the_cache_and_is_not_cached(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        _set(rt, version, "cache", True)
        _set(rt, version, "pre", True)
        await rt.plug(1)
        assert await rt.swipe(1, "T") == A.BLOCKED  # Authorize answers Blocked: cached as Blocked
        assert _authorizes(s, version) == ["T"]
        assert rt.station.cache.get("T", datetime.now(timezone.utc)).status is A.BLOCKED
        (peer.tag_statuses if _v16(version) else peer.token_statuses)["T"] = "Accepted"  # unblocked at the CSMS
        assert await _send_list(peer, version, 1, True, [("T", "Accepted", None), ("L", "Accepted", None),
                                                         ("X", "Invalid", None)]) == "Accepted"
        assert await rt.swipe(1, "T") == A.ACCEPTED  # the list entry wins: no Authorize (C14.FR.02)
        assert _authorizes(s, version) == ["T"]
        await _until(lambda: len(_starts(s, version)) == 1)
        assert await rt.swipe(1, "T") == A.STOPPED
        # A listed token that is not Accepted is authorized online (C14.FR.03); the answer is not cached.
        assert await rt.swipe(2, "X") == A.ACCEPTED
        assert _authorizes(s, version) == ["T", "X"] and "X" not in rt.station.cache.tokens()
        # LocalPreAuthorize false: a listed Accepted token is authorized online, and not cached either.
        _set(rt, version, "pre", False)
        await rt.unplug(1)
        await rt.plug(1)
        assert await rt.swipe(1, "L") == A.ACCEPTED
        assert _authorizes(s, version)[-1] == "L" and "L" not in rt.station.cache.tokens()
    _run(body, version, peer_kw={"tag_statuses": {"T": "Blocked"}} if version == "1.6"
         else {"token_statuses": {"T": "Blocked"}})


@VERSIONS
def test_cache_hit_and_clear_cache(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _clear_cache(peer, version) == "Rejected"  # the cache is disabled by default
        _set(rt, version, "cache", True)
        _set(rt, version, "pre", True)
        await rt.plug(1)
        assert await rt.swipe(1, "C") == A.ACCEPTED
        assert _authorizes(s, version) == ["C"]
        assert await rt.swipe(1, "C") == A.STOPPED
        assert await rt.swipe(1, "C") == A.ACCEPTED  # cache hit (C12): no second Authorize
        assert _authorizes(s, version) == ["C"]
        assert await rt.swipe(1, "C") == A.STOPPED
        await _until(lambda: _ended(s, version) == 2)  # its answer updates the cache: let it arrive first
        await asyncio.sleep(0.1)
        assert await _clear_cache(peer, version) == "Accepted"
        assert list(rt.station.cache.tokens()) == []
        assert await rt.swipe(1, "C") == A.ACCEPTED
        assert _authorizes(s, version) == ["C", "C"]  # invalidated: authorized again
    _run(body, version)


@VERSIONS
def test_cached_token_not_accepted_is_authorized_again(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        _set(rt, version, "cache", True)
        _set(rt, version, "pre", True)
        await rt.plug(1)
        assert await rt.swipe(1, "B") == A.BLOCKED
        statuses = peer.tag_statuses if _v16(version) else peer.token_statuses
        statuses["B"] = "Accepted"  # unblocked at the CSMS
        assert await rt.swipe(1, "B") == A.ACCEPTED  # C10.FR.03 / §3.5.1: Authorize again
        assert _authorizes(s, version) == ["B", "B"]
        assert rt.station.cache.get("B", datetime.now(timezone.utc)).status is A.ACCEPTED
    _run(body, version, peer_kw={"tag_statuses": {"B": "Blocked"}} if version == "1.6"
         else {"token_statuses": {"B": "Blocked"}})


def test_201_disable_post_authorize_uses_the_cached_status():
    async def body(peer, rt):
        s = await _online(peer, rt, "2.0.1")
        _set(rt, "2.0.1", "cache", True)
        rt.station.dm.put(dm.AUTH_CACHE_DISABLE_POST_AUTHORIZE, "true")
        await rt.plug(1)
        assert await rt.swipe(1, "B") == A.BLOCKED
        assert await rt.swipe(1, "B") == A.BLOCKED
        assert _authorizes(s, "2.0.1") == ["B"]
    _run(body, "2.0.1", peer_kw={"token_statuses": {"B": "Blocked"}})


# --------------------------------------------------------------------------------------------- reservations


@VERSIONS
def test_reservation_refuses_another_tag_and_is_used_by_the_reserved_one(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 11, 1, "R") == "Accepted"
        await _until(lambda: _statuses(s, version, 1)[-1:] == ["Reserved"])
        snap = rt.snapshot()
        assert snap["connectors"][0]["status"] == "Reserved" and snap["connectors"][0]["reservation"]["id"] == 11
        # A different id token on the reserved connector: refused without asking the CSMS.
        await rt.plug(1)
        assert await rt.swipe(1, "X") == A.RESERVED
        assert _authorizes(s, version) == [] and not rt.has_transaction(1)
        # The other connector is not reserved.
        await rt.plug(2)
        assert await rt.swipe(2, "X") == A.ACCEPTED
        assert await _reserve(peer, version, 12, 2, "R") == "Occupied"
        # The reserved token uses the reservation; its id goes with the start.
        assert await rt.swipe(1, "R") == A.ACCEPTED
        await _until(lambda: len(_starts(s, version)) == 2)
        assert _starts(s, version)[1] == (1, "R", 11)
        assert _starts(s, version)[0][2] is None
        assert rt.station.reservations == {} and rt.snapshot()["connectors"][0]["reservation"] is None
        assert await _cancel(peer, version, 11) == "Rejected"  # used: gone
        if not _v16(version):
            assert s.reservation_updates == []  # used, not expired / removed
    _run(body, version)


@VERSIONS
def test_reservation_expires(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 5, 1, "R", seconds=0.6) == "Accepted"
        await _until(lambda: _statuses(s, version, 1)[-1:] == ["Reserved"])
        await _until(lambda: _statuses(s, version, 1)[-1:] == ["Available"])
        assert _statuses(s, version, 1) == ["Available", "Reserved", "Available"]
        assert rt.station.reservations == {}
        if not _v16(version):
            assert s.reservation_updates == [(5, "Expired")]  # H04.FR.01
        await rt.plug(1)
        assert await rt.swipe(1, "X") == A.ACCEPTED  # charging allowed again
        assert await _reserve(peer, version, 6, 2, "R", seconds=-1) == "Rejected"  # not in the future
    _run(body, version)


@VERSIONS
def test_cancel_reservation(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 7, 2, "R") == "Accepted"
        await _until(lambda: _statuses(s, version, 2)[-1:] == ["Reserved"])
        assert await _cancel(peer, version, 99) == "Rejected"
        assert await _cancel(peer, version, 7) == "Accepted"
        await _until(lambda: _statuses(s, version, 2)[-1:] == ["Available"])
        assert await _cancel(peer, version, 7) == "Rejected"
        await rt.plug(2)
        assert await rt.swipe(2, "X") == A.ACCEPTED
        if not _v16(version):
            assert s.reservation_updates == []  # H02: no ReservationStatusUpdate
    _run(body, version)


@VERSIONS
def test_reserve_now_answers_by_connector_state_and_replaces_by_id(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        await rt.fault(1, "GroundFailure" if _v16(version) else "GroundFault")
        assert await _reserve(peer, version, 1, 1, "R") == "Faulted"
        await _inoperative(peer, version, 2)
        assert await _reserve(peer, version, 1, 2, "R") == "Unavailable"
        assert await _reserve(peer, version, 1, 3, "R") == "Accepted"
        assert await _reserve(peer, version, 2, 3, "Q") == "Occupied"  # reserved for another reservation
        await rt.plug(4)
        assert await _reserve(peer, version, 2, 4, "Q") == "Occupied"  # cable plugged
        # Same id: replaced (connector 3 -> connector 5).
        assert await _reserve(peer, version, 1, 5, "R2") == "Accepted"
        assert {r.connector for r in rt.station.reservations.values()} == {5}
        assert rt.station.reservations[1].id_token == "R2"
        await _until(lambda: _statuses(s, version, 3)[-1:] == ["Available"] and
                     _statuses(s, version, 5)[-1:] == ["Reserved"])
        # A refused replacement keeps the old reservation.
        assert await _reserve(peer, version, 1, 4, "R3") == "Occupied"
        assert rt.station.reservations[1].connector == 5
    _run(body, version, connectors=5)


@VERSIONS
def test_reservation_ends_when_its_connector_faults_or_becomes_unavailable(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 1, 1, "R") == "Accepted"
        assert await _reserve(peer, version, 2, 2, "R") == "Accepted"
        await _until(lambda: _statuses(s, version, 2)[-1:] == ["Reserved"])
        await rt.fault(1, "GroundFailure" if _v16(version) else "GroundFault")
        await _inoperative(peer, version, 2)
        await _until(lambda: _statuses(s, version, 2)[-1:] == ["Unavailable"])
        assert rt.station.reservations == {}
        if not _v16(version):
            await _until(lambda: len(s.reservation_updates) == 2)
            assert s.reservation_updates == [(1, "Removed"), (2, "Removed")]  # H01.FR.16 / FR.17
    _run(body, version)


@VERSIONS
def test_reservation_without_connector_keeps_one_connector_free(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 3, 0, "R") == "Accepted"
        await asyncio.sleep(0.1)
        assert rt.snapshot()["connectors"][0]["status"] == "Available"  # 2 free > 1 reservation
        await rt.plug(1)
        assert await rt.swipe(1, "X") == A.ACCEPTED  # connector 2 stays free for R
        await _until(lambda: _statuses(s, version, 2)[-1:] == ["Reserved"])
        assert await _reserve(peer, version, 4, 0, "Q") == "Occupied"  # no free connector left for a second one
        assert await _reserve(peer, version, 4, 2, "Q") == "Occupied"
        await rt.plug(2)
        assert await rt.swipe(2, "Y") == A.RESERVED
        assert await rt.swipe(2, "R") == A.ACCEPTED
        await _until(lambda: len(_starts(s, version)) == 2)
        assert _starts(s, version)[1] == (2, "R", 3)
        assert rt.station.reservations == {}
    _run(body, version)


@VERSIONS
def test_reservation_without_connector_can_be_switched_off(version):
    async def body(peer, rt):
        await _online(peer, rt, version)
        assert await _reserve(peer, version, 3, 0, "R") == "Rejected"
        assert await _reserve(peer, version, 3, 1, "R") == "Accepted"
    if _v16(version):
        _run(body, version, cfg_kw={"config_keys": {"ReserveConnectorZeroSupported": "false"}})
    else:
        _run(body, version, cfg_kw={"variables": [{"component": "ReservationCtrlr", "variable": "NonEvseSpecific",
                                                   "value": "false"}]})


@VERSIONS
def test_reservation_matches_the_parent_of_the_presented_tag(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 8, 1, "R", parent="G") == "Accepted"
        await rt.plug(1)
        assert await rt.swipe(1, "Z") == A.RESERVED  # its parent (from the Authorize answer) is not G
        assert await rt.swipe(1, "M") == A.ACCEPTED  # parent G
        await _until(lambda: len(_starts(s, version)) == 1)
        assert _starts(s, version)[0] == (1, "M", 8)
    groups = {"M": "G", "Z": "OTHER"}
    _run(body, version, peer_kw={"tag_parents": groups} if _v16(version) else {"token_groups": groups})


@VERSIONS
def test_remote_start_on_a_reserved_connector(version):
    async def body(peer, rt):
        s = await _online(peer, rt, version)
        assert await _reserve(peer, version, 9, 1, "R") == "Accepted"
        await rt.plug(1)
        if _v16(version):
            assert (await peer.remote_start(CP, "X", 1)).status == "Rejected"
            assert (await peer.remote_start(CP, "R", 1)).status == "Accepted"
        else:
            def start(tag, rid):
                return call201.RequestStartTransaction(id_token={"id_token": tag, "type": "Central"},
                                                       remote_start_id=rid, evse_id=1)
            r = await peer.request(CP, start("X", 1))
            assert r.status == "Rejected" and r.status_info["reason_code"] == "Reserved"
            assert (await peer.request(CP, start("R", 2))).status == "Accepted"
        await _until(lambda: len(_starts(s, version)) == 1)
        assert _starts(s, version)[0] == (1, "R", 9)
    _run(body, version)


def test_201_reservations_disabled():
    async def body(peer, rt):
        await _online(peer, rt, "2.0.1")
        assert await _reserve(peer, "2.0.1", 1, 1, "R") == "Accepted"
        rt.station.dm.put(dm.RESERVATION_ENABLED, "false")
        assert await _reserve(peer, "2.0.1", 2, 2, "R") == "Rejected"  # H01.FR.01
        await rt.plug(1)
        assert await rt.swipe(1, "X") == A.ACCEPTED  # kept, but not used
        assert 1 in rt.station.reservations
    _run(body, "2.0.1")


# --------------------------------------------------------------------------------------------- stores


T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_local_list_update_rules():
    lst = LocalAuthList(max_entries=2)
    ok = IdTokenInfo(A.ACCEPTED)
    assert lst.update(1, True, [("A", ok), ("B", ok)]) is ListUpdate.ACCEPTED and lst.version == 1
    assert lst.update(2, True, [("A", ok), ("B", ok), ("C", ok)]) is ListUpdate.FAILED  # too long
    assert lst.update(2, False, [("C", ok)]) is ListUpdate.FAILED  # would be too long
    assert lst.update(1, False, [("B", None)]) is ListUpdate.VERSION_MISMATCH
    assert lst.update(2, False, [("B", None), ("C", ok)]) is ListUpdate.ACCEPTED
    assert set(lst.entries()) == {"A", "C"} and lst.version == 2
    assert lst.update(3, False, [("Z", None)]) is ListUpdate.ACCEPTED  # removing an unknown token is no error


def test_cache_eviction_prefers_invalid_then_oldest():
    cache = AuthCache(max_entries=3)
    cache.update("A", IdTokenInfo(A.ACCEPTED), T0)
    cache.update("B", IdTokenInfo(A.BLOCKED), T0 + timedelta(seconds=1))
    cache.update("C", IdTokenInfo(A.ACCEPTED), T0 + timedelta(seconds=2))
    cache.update("D", IdTokenInfo(A.ACCEPTED), T0 + timedelta(seconds=3))  # full: B (not valid) goes
    assert list(cache.tokens()) == ["A", "C", "D"]
    cache.update("A", IdTokenInfo(A.ACCEPTED), T0 + timedelta(seconds=4))  # refreshed: now the newest
    cache.update("E", IdTokenInfo(A.ACCEPTED), T0 + timedelta(seconds=5))  # all valid: the oldest (C) goes
    assert list(cache.tokens()) == ["D", "A", "E"]


def test_cache_expiry_and_lifetime():
    cache = AuthCache()
    cache.update("X", IdTokenInfo(A.ACCEPTED, expiry=T0 + timedelta(minutes=1)), T0)
    cache.update("Y", IdTokenInfo(A.ACCEPTED), T0)
    assert cache.get("X", T0).status is A.ACCEPTED
    assert cache.get("X", T0 + timedelta(minutes=2)).status is A.EXPIRED  # changed to Expired (§3.5.1)
    assert cache.get("X", T0).status is A.EXPIRED  # it stays Expired
    assert cache.get("Y", T0 + timedelta(seconds=30), lifetime_s=60).status is A.ACCEPTED
    assert cache.get("Y", T0 + timedelta(seconds=61), lifetime_s=60).status is A.EXPIRED  # C10.FR.10
    assert cache.get("nope", T0) is None


def test_reservation_matching():
    r = Reservation(1, 0, "R", T0, parent="G")
    assert r.matches("R") and r.matches("X", "G") and r.matches("G") and r.matches("X", "R")
    assert not r.matches("X") and not r.matches("X", "H")
    assert not Reservation(2, 1, "R", T0).matches("X", None)
