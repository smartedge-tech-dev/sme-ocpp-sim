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
    await _until(lambda: rt.online and CP in peer.sessions and len(peer.sessions[CP].statuses) >= 1 + len(rt.station.conns))
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
        assert len(rt.station.profiles.profiles()) == 2
        assert await rt.stop_tx(1)
        assert [ip.profile.profile_id for ip in rt.station.profiles.profiles()] == [2]
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
        [ip] = rt.station.profiles.profiles(1)
        assert ip.profile.profile_id == 9 and ip.profile.transaction_id == tx
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 1600)  # 8 A x 200 V

        # refused authorization: the profile is discarded with the start
        assert (await peer.remote_start(CP, "BAD", 2, charging_profile=_profile(8, "A", pid=10))).status \
            == "Accepted"
        await _until(lambda: "BAD" in s.authorizes and rt.station.conns[2].pending_tag is None)
        assert rt.station.conns[2].pending_profile is None and rt.station.profiles.profiles(2) == ()
    _scenario(body, cfg_kw={"connectors": 2, "auth_mode": "csms_only",
                            "config_keys": {"AuthorizeRemoteTxRequests": "true"}},
              peer_kw={"tag_statuses": {"BAD": "Invalid"}})


def test_ramp_up_reaches_the_limit():
    async def body(peer, rt):
        s, _ = await _charging(peer, rt)
        assert rt.snapshot()["connectors"][0]["power_w"] < 6000  # ramping
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 6000)
    _scenario(body, cfg_kw={"ramp_up_w_per_s": 20000})


def test_remote_start_profile_survives_plug_during_reservation():
    """Cable plugged while the RemoteStart follow-up still waits for Authorize: plug() starts the transaction,
    which must carry the RemoteStart TxProfile (§5.16.2)."""
    async def body(peer, rt):
        s = await _online(peer, rt)
        release = asyncio.Event()
        authorize = rt.station._authorize

        async def slow_authorize(tag):
            await release.wait()
            return await authorize(tag)

        rt.station._authorize = slow_authorize
        r = await peer.remote_start(CP, "R1", 1, charging_profile=_profile(8, "A", pid=9))
        assert r.status == "Accepted"
        assert rt.station.conns[1].pending_tag == "R1" and rt.station.conns[1].pending_profile is not None
        await rt.plug(1)  # the follow-up is still parked in Authorize
        release.set()
        await _until(lambda: s.starts and _statuses(s, 1)[-1:] == ["Charging"])
        tx = s.starts[0]["transaction_id"]
        [ip] = rt.station.profiles.profiles(1)
        assert ip.profile.profile_id == 9 and ip.profile.transaction_id == tx
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 1600)  # 8 A x 200 V
        assert len(s.starts) == 1 and rt.station.conns[1].pending_profile is None
    _scenario(body, cfg_kw={"auth_mode": "csms_only", "config_keys": {"AuthorizeRemoteTxRequests": "true"}})


def test_tick_loop_survives_a_failing_tick(caplog):
    async def body(peer, rt):
        await _charging(peer, rt)
        calls = []
        tick_all = rt.station.tick

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            tick_all()

        rt.station.tick = flaky
        await _until(lambda: len(calls) >= 3)
        assert any("power model tick failed" in r.getMessage() and r.exc_info for r in caplog.records)
    _scenario(body)


def _one_vehicle_pool():
    """A pool where only ``pool-ev`` (2 kW, initial SoC 50-60 %) can be drawn."""
    from sme_ocpp_sim.charging.ev import EvModel
    from sme_ocpp_sim.charging.vehicles import PoolEntry, SocSpec, VehiclePool, build_pool
    ev = EvModel("pool-ev", capacity_kwh=50, max_ac_w=2000, max_current_a=10)
    zero = [PoolEntry(e.ev, 0, e.initial_soc, e.target_soc)
            for e in build_pool((), initial_soc_pct=30, target_soc_pct=90).entries]
    return VehiclePool((*zero, PoolEntry(ev, 1, SocSpec("uniform", 50, 60), SocSpec.fixed(95))))


def test_pool_session_draws_its_vehicle():
    """``session.ev_model: pool``: the transaction runs on the drawn EV (power limit, initial SoC)."""
    async def body(peer, rt):
        await _charging(peer, rt)
        await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == 2000)
        assert 50 <= rt.snapshot()["connectors"][0]["soc_pct"] <= 60.5
        assert rt.ev.id == "pool-ev"  # the pool's default model while idle
    _scenario(body, cfg_kw={"session": {"ev_model": "pool"}}, rt_kw={"ev": _one_vehicle_pool()})


def test_pool_vehicle_sequence_is_seeded():
    from sme_ocpp_sim.charging.vehicles import build_pool, stream_rng
    rt = ChargerRuntime(_cfg(session={"ev_model": "pool"}), "ws://csms", vehicle_seed=7)
    pool = build_pool((), initial_soc_pct=30, target_soc_pct=90)
    assert rt.pool == pool  # no ev=: the presets
    rng = stream_rng(7, CP, 1)
    expected = [pool.draw(rng) for _ in range(5)]
    expected_first = expected[0]
    assert [rt.vehicle(1) for _ in range(5)] == expected
    again = ChargerRuntime(_cfg(session={"ev_model": "pool"}), "ws://csms", vehicle_seed=7)
    assert again.vehicle(1) == expected_first  # a rebuilt runtime (config reload) replays the stream from the start
    fixed = ChargerRuntime(_cfg(), "ws://csms").vehicle(1)
    assert (fixed.ev.id, fixed.initial_soc_pct, fixed.target_soc_pct) == ("compact-40kwh", 30, 90)


def test_pool_draws_per_transaction_and_phases_follow_the_vehicle():
    """Two transactions in a row on a 3-phase supply: each draws its own EV (1-phase 2 kW / 3-phase 6 kW); power
    and the phases an A limit applies to follow the drawn vehicle."""
    from sme_ocpp_sim.charging.ev import EvModel
    from sme_ocpp_sim.charging.vehicles import PoolEntry, SocSpec, VehiclePool, stream_rng
    one = EvModel("one-phase", capacity_kwh=50, max_ac_w=2000, max_current_a=10, phases=1)
    three = EvModel("three-phase", capacity_kwh=50, max_ac_w=6000, max_current_a=10, phases=3)
    pool = VehiclePool((PoolEntry(one, 1, SocSpec.fixed(20), SocSpec.fixed(95)),
                        PoolEntry(three, 1, SocSpec.fixed(20), SocSpec.fixed(95))))
    seed = 0
    while True:  # the first seed whose two first draws are different vehicles
        rng = stream_rng(seed, CP, 1)
        drawn = [pool.draw(rng).ev for _ in range(2)]
        if drawn[0] != drawn[1]:
            break
        seed += 1
    watts = {"one-phase": 2000, "three-phase": 6000}

    async def body(peer, rt):
        s = await _online(peer, rt)
        for n, ev in enumerate(drawn):
            await rt.plug(1)
            assert await rt.swipe(1, "T") == "Accepted"
            await _until(lambda: len(s.starts) == n + 1 and _statuses(s, 1)[-1:] == ["Charging"])
            await _until(lambda: rt.snapshot()["connectors"][0]["power_w"] == watts[ev.id])
            assert rt.station._phases(1) == ev.phases
            await rt.unplug(1)
            await _until(lambda: _statuses(s, 1)[-1:] == ["Available"])
            assert rt.station._phases(1) == 3  # no EV: every supply phase
    _scenario(body, cfg_kw={"phases": 3, "max_power_w": 22000, "session": {"ev_model": "pool"}},
              rt_kw={"ev": pool, "vehicle_seed": seed})
