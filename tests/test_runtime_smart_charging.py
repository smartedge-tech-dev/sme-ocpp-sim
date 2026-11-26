"""Smart charging and the power model in ChargerRuntime, against the ``ocpp``-library CSMS peer (plan §5 M4b)."""

import asyncio

from peers.ocpp16_peer import CsmsPeer

from sme_ocpp_sim.config import ChargerConfig, ReconnectConfig
from sme_ocpp_sim.runtime.charger import ChargerRuntime

CP = "CP1"
FAST = ReconnectConfig(min_s=0.05, max_s=0.2, jitter=0)
MEASURANDS = "Power.Active.Import,Energy.Active.Import.Register,Current.Import,Voltage,SoC"


def _cfg(**kw):
    keys = {"MeterValueSampleInterval": "1", "MeterValuesSampledData": MEASURANDS}
    keys.update(kw.pop("config_keys", {}))
    kw.setdefault("auth_mode", "accept_all")
    return ChargerConfig(id=CP, config_keys=keys, **kw)


async def _until(cond, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.02)


def _scenario(body, cfg_kw=None, rt_kw=None, peer_kw=None):
    async def main():
        peer = CsmsPeer(**{"hb_interval": 30, **(peer_kw or {})})
        await peer.start()
        rt = ChargerRuntime(_cfg(**(cfg_kw or {})), peer.url, reconnect=FAST, call_timeout=5,
                            **{"tick_s": 0.05, **(rt_kw or {})})
        stop = asyncio.Event()
        task = asyncio.create_task(rt.run(stop))
        try:
            async with asyncio.timeout(20):
                await body(peer, rt)
        finally:
            stop.set()
            async with asyncio.timeout(3):
                await task
            await peer.stop()
    asyncio.run(main())


def _statuses(s, cid):
    return [st for c, st, _ in s.statuses if c == cid]


def _values(mv):
    return {v["measurand"]: float(v["value"]) for v in mv["meter_value"][0]["sampled_value"]}


def _profile(limit, unit="W", purpose="TxProfile", pid=1, tx=None, first_period=0):
    p = {"chargingProfileId": pid, "stackLevel": 0, "chargingProfilePurpose": purpose,
         "chargingProfileKind": "Relative",
         "chargingSchedule": {"chargingRateUnit": unit,
                              "chargingSchedulePeriod": [{"startPeriod": first_period, "limit": limit}]}}
    if tx is not None:
        p["transactionId"] = tx
    return p


async def _online(peer, rt):
    await _until(lambda: rt.online and CP in peer.sessions and len(peer.sessions[CP].statuses) >= 1 + len(rt.conns))
    return peer.sessions[CP]


async def _charging(peer, rt):
    s = await _online(peer, rt)
    await rt.plug(1)
    assert await rt.swipe(1, "T") == "Accepted"
    await _until(lambda: _statuses(s, 1)[-1:] == ["Charging"])
    return s, s.starts[-1]["transaction_id"]


def test_composite_schedule_matches_meter_values():
    """Completion criterion (plan §5 M4): GetCompositeSchedule == subsequent Power.Active.Import."""
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        await _until(lambda: s.meter_values)
        v = _values(s.meter_values[-1])
        assert v["Power.Active.Import"] == 6000  # no profile: hardware max (EV takes 30 A x 200 V)
        comp = await peer.get_composite_schedule(CP, 1, 60, "W")
        assert comp.status == "Accepted"
        assert comp.charging_schedule["charging_schedule_period"] == [{"start_period": 0, "limit": 6000.0}]

        assert (await peer.set_charging_profile(CP, 1, _profile(10, "A", tx=tx))).status == "Accepted"
        comp = await peer.get_composite_schedule(CP, 1, 60, "W")
        sched = comp.charging_schedule
        assert comp.status == "Accepted" and sched["duration"] == 60 and sched["charging_rate_unit"] == "W"
        assert sched["start_schedule"] == comp.schedule_start
        limit = sched["charging_schedule_period"][0]["limit"]
        assert limit == 2000.0  # 10 A x 200 V x 1 phase (the EV uses one phase)
        n = len(s.meter_values)
        await _until(lambda: len(s.meter_values) >= n + 2)
        v = _values(s.meter_values[-1])
        assert v["Power.Active.Import"] == limit
        assert v["Current.Import"] == 10 and v["Voltage"] == 200 and 30 <= v["SoC"] < 31
        comp_a = await peer.get_composite_schedule(CP, 1, 60, "A")
        assert comp_a.charging_schedule["charging_schedule_period"][0] == {
            "start_period": 0, "limit": 10.0, "number_phases": 1}
    _scenario(body)


def test_zero_limit_suspends_evse_until_cleared():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        assert (await peer.set_charging_profile(CP, 1, _profile(0, tx=tx))).status == "Accepted"
        await _until(lambda: _statuses(s, 1)[-1:] == ["SuspendedEVSE"])
        assert rt.snapshot()["connectors"][0]["power_w"] == 0
        assert (await peer.clear_charging_profile(CP, id=1)).status == "Accepted"
        await _until(lambda: _statuses(s, 1)[-1:] == ["Charging"])
        assert rt.snapshot()["connectors"][0]["power_w"] == 6000
        assert (await peer.clear_charging_profile(CP, id=1)).status == "Unknown"
        assert _statuses(s, 1) == ["Available", "Preparing", "Charging", "SuspendedEVSE", "Charging"]
    _scenario(body)


def test_target_soc_suspends_ev():
    async def body(peer, rt):
        s, _ = await _charging(peer, rt)
        await _until(lambda: _statuses(s, 1)[-1:] == ["SuspendedEV"])
        conn = rt.snapshot()["connectors"][0]
        assert conn["soc_pct"] >= 90 and conn["power_w"] == 0
    _scenario(body, cfg_kw={"session": {"initial_soc": 89.5, "target_soc": 90}}, rt_kw={"time_scale": 2000})


def test_tx_profile_dropped_at_transaction_end():
    async def body(peer, rt):
        s, tx = await _charging(peer, rt)
        assert (await peer.set_charging_profile(CP, 1, _profile(3000, tx=tx))).status == "Accepted"
        assert (await peer.set_charging_profile(CP, 0, _profile(5000, "W", "TxDefaultProfile", pid=2))).status \
            == "Accepted"
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 3000)
        assert len(rt.profiles.profiles()) == 2
        assert await rt.stop_tx(1)
        assert [ip.profile.profile_id for ip in rt.profiles.profiles()] == [2]
        assert rt.snapshot()["connectors"][0]["soc_pct"] is None  # EV state reset
        # no transaction: a TxProfile is refused (3.13.1)
        assert (await peer.set_charging_profile(CP, 1, _profile(3000, pid=3))).status == "Rejected"
    _scenario(body)


def test_remote_start_profile_bound_to_started_transaction():
    async def body(peer, rt):
        s = await _online(peer, rt)
        await rt.plug(1)
        bad = _profile(8, "A", pid=8, first_period=30)
        assert (await peer.remote_start(CP, "R1", 1, charging_profile=bad)).status == "Rejected"
        r = await peer.remote_start(CP, "R1", 1, charging_profile=_profile(8, "A", pid=9))
        assert r.status == "Accepted"
        await _until(lambda: s.starts and _statuses(s, 1)[-1:] == ["Charging"])
        tx = s.starts[0]["transaction_id"]
        [ip] = rt.profiles.profiles(1)
        assert ip.profile.profile_id == 9 and ip.profile.transaction_id == tx
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 1600)  # 8 A x 200 V

        # refused authorization: the profile is discarded with the start
        assert (await peer.remote_start(CP, "BAD", 2, charging_profile=_profile(8, "A", pid=10))).status \
            == "Accepted"
        await _until(lambda: "BAD" in s.authorizes and rt.conns[2].pending_tag is None)
        assert rt.conns[2].pending_profile is None and rt.profiles.profiles(2) == ()
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "csms_only",
                            "config_keys": {"AuthorizeRemoteTxRequests": "true"}},
              peer_kw={"tag_statuses": {"BAD": "Invalid"}})


def test_ramp_up_reaches_the_limit():
    async def body(peer, rt):
        s, _ = await _charging(peer, rt)
        assert rt.snapshot()["connectors"][0]["power_w"] < 6000  # ramping
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 6000)
    _scenario(body, cfg_kw={"ramp_up_w_per_s": 20000})
