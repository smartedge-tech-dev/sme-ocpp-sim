import math

import pytest

from sme_ocpp_sim.charging.ev import PRESETS, EvModel
from sme_ocpp_sim.charging.power import (
    AcSupply, PowerState, amps_to_watts, meter_reading, step, watts_to_amps,
)

EV = PRESETS["compact-40kwh"]
SUPPLY_1P = AcSupply(200.0, 1)


def run(state, n, **kw):
    out = [state]
    for _ in range(n):
        out.append(step(out[-1], **kw))
    return out


def base(**over):
    kw = dict(dt_s=1.0, ev=EV, target_soc_pct=100.0, evse_limit_w=None, evse_max_w=7000.0,
              supply=SUPPLY_1P)
    kw.update(over)
    return kw


def test_unlimited_follows_ev_max():
    s = step(PowerState(soc_pct=30), **base())
    assert s.power_w == 6000 and s.suspension is None
    assert s.energy_wh == pytest.approx(3000 / 3600)  # trapezoid 0 -> 6000 over 1 s


def test_cc_cv_curve_over_a_session():
    ev = EvModel("small", capacity_kwh=1.0, max_ac_w=6000, max_current_a=30, efficiency=1.0)
    states = run(PowerState(soc_pct=0, power_w=6000), 2000, **base(ev=ev, dt_s=0.5))
    cc = [s.power_w for s in states if s.soc_pct < 80]
    cv = [s.power_w for s in states if s.soc_pct > 81]
    assert all(p == 6000 for p in cc)
    assert cv and all(a >= b for a, b in zip(cv, cv[1:]))
    assert cv[-1] < 6000 * 0.2


def test_target_soc_suspended_ev():
    states = run(PowerState(soc_pct=89.99), 20, **base(target_soc_pct=90.0))
    assert states[1].suspension is None
    final = states[-1]
    assert final.suspension == "SuspendedEV" and final.power_w == 0
    assert final.soc_pct >= 90


def test_target_already_reached_and_limit_zero_prefers_ev():
    s = step(PowerState(soc_pct=95), **base(target_soc_pct=90.0, evse_limit_w=0.0))
    assert s.suspension == "SuspendedEV"


@pytest.mark.parametrize("limit", [0.0, -5.0, 1000.0])  # 1000 W < 6 A * 200 V
def test_limit_zero_or_below_min_suspended_evse(limit):
    s = step(PowerState(soc_pct=30, power_w=6000), **base(evse_limit_w=limit))
    assert s.suspension == "SuspendedEVSE"
    assert s.power_w == 0 and s.target_w == 0


def test_limit_at_min_is_used():
    s = step(PowerState(soc_pct=30), **base(evse_limit_w=1200.0))
    assert s.suspension is None and s.power_w == 1200


def test_ramp_up_rate_then_limit_honoured_exactly():
    states = run(PowerState(soc_pct=30), 10, **base(evse_limit_w=3700.0, ramp_up_w_per_s=1000.0))
    assert [s.power_w for s in states[1:5]] == [1000, 2000, 3000, 3700]
    assert all(s.power_w == 3700 and s.target_w == 3700 for s in states[4:])


def test_ramp_down_rate_and_default_step_down():
    start = PowerState(soc_pct=30, power_w=6000)
    slow = run(start, 3, **base(evse_limit_w=3000.0, ramp_down_w_per_s=2000.0))
    assert [s.power_w for s in slow[1:]] == [4000, 3000, 3000]
    fast = step(start, **base(evse_limit_w=3000.0))
    assert fast.power_w == 3000


def test_evse_max_caps_unlimited_schedule():
    s = step(PowerState(soc_pct=30), **base(evse_max_w=4000.0, evse_limit_w=None))
    assert s.power_w == 4000
    s = step(PowerState(soc_pct=30), **base(evse_max_w=4000.0, evse_limit_w=5000.0))
    assert s.power_w == 4000


def test_time_scale_scales_energy_soc_and_ramp():
    kw = base(evse_limit_w=None)
    start = PowerState(soc_pct=30, power_w=6000)
    a = step(start, **kw, time_scale=60.0)
    b = step(start, **{**kw, "dt_s": 60.0})
    assert a.energy_wh == pytest.approx(100.0)  # 6 kW * 60 s
    assert a.energy_wh == pytest.approx(b.energy_wh) and a.soc_pct == pytest.approx(b.soc_pct)
    assert a.soc_pct == pytest.approx(30 + 100 * 0.9 / 40000 * 100)
    r = step(PowerState(soc_pct=30), **base(ramp_up_w_per_s=100.0), time_scale=10.0)
    assert r.power_w == 1000


def test_energy_register_monotonic_across_suspensions():
    s = PowerState(soc_pct=50)
    regs = []
    for limit in [None] * 5 + [0.0] * 5 + [2000.0] * 5 + [None] * 5:
        s = step(s, **base(evse_limit_w=limit, ramp_up_w_per_s=500.0, ramp_down_w_per_s=500.0))
        regs.append(s.energy_wh)
    assert all(b >= a for a, b in zip(regs, regs[1:]))
    assert regs[-1] > regs[0]


def test_200v_single_phase_meter():
    s = step(PowerState(soc_pct=30), **base(evse_limit_w=4000.0))
    m = meter_reading(s, EV, SUPPLY_1P)
    assert m.phases == 1 and m.voltage_v == 200
    assert m.current_import_a == pytest.approx(20.0)
    assert m.power_active_import_w == pytest.approx(m.voltage_v * m.current_import_a)
    assert m.energy_active_import_register_wh == s.energy_wh and m.soc_pct == s.soc_pct


def test_230v_three_phase_meter_and_amps_limit():
    ev = PRESETS["midsize-60kwh"]
    supply = AcSupply(230.0, 3)
    limit_w = amps_to_watts(10.0, supply, 3)
    assert limit_w == pytest.approx(6900)
    s = step(PowerState(soc_pct=30), **base(ev=ev, supply=supply, evse_max_w=22000.0,
                                             evse_limit_w=limit_w))
    m = meter_reading(s, ev, supply)
    assert m.phases == 3 and m.current_import_a == pytest.approx(10.0)
    assert m.power_active_import_w == pytest.approx(230 * 10.0 * 3)
    # unlimited: capped by the EV's 16 A per phase (11 040 W) rather than its 11 kW max
    s = step(PowerState(soc_pct=30), **base(ev=ev, supply=supply, evse_max_w=22000.0))
    assert s.power_w == pytest.approx(11000)


def test_200v_three_phase_line_to_line():
    supply = AcSupply(200.0, 3, line_to_line=True)
    assert supply.phase_voltage_v() == pytest.approx(200 / math.sqrt(3))
    p = amps_to_watts(16.0, supply, 3)
    assert p == pytest.approx(math.sqrt(3) * 200 * 16)
    assert watts_to_amps(p, supply, 3) == pytest.approx(16.0)


def test_single_phase_ev_on_three_phase_supply_uses_one_phase():
    m = meter_reading(PowerState(soc_pct=30, power_w=2300.0), EV, AcSupply(230.0, 3))
    assert m.phases == 1 and m.current_import_a == pytest.approx(10.0)


def test_deterministic_and_input_unchanged():
    s0 = PowerState(soc_pct=30)
    kw = base(evse_limit_w=3000.0, ramp_up_w_per_s=700.0)
    assert step(s0, **kw) == step(s0, **kw)
    assert s0 == PowerState(soc_pct=30)


def test_invalid_args():
    with pytest.raises(ValueError):
        step(PowerState(soc_pct=30), **base(dt_s=-1.0))
    with pytest.raises(ValueError):
        step(PowerState(soc_pct=30), **base(), time_scale=0)
    with pytest.raises(ValueError):
        step(PowerState(soc_pct=30), **base(ramp_up_w_per_s=0.0))


def test_ev_taper_below_its_minimum_suspends_ev():
    # default curve: 10 % of 6000 W at 100 % SoC = 600 W < 6 A x 200 V; 1200 W is reached at 97.78 % SoC
    s = step(PowerState(soc_pct=99.0, power_w=1500), **base())
    assert s.suspension == "SuspendedEV" and s.target_w == 0 and s.power_w == 0
    # the EV-side cause wins over a too-low EVSE offer as well
    assert step(PowerState(soc_pct=99.0), **base(evse_limit_w=500.0)).suspension == "SuspendedEV"
    s = step(PowerState(soc_pct=97.0), **base())
    assert s.suspension is None and s.power_w > 1200
    # an EV without a minimum keeps tapering
    ev = EvModel("nomin", capacity_kwh=40, max_ac_w=6000, max_current_a=30, min_current_a=0.0)
    s = step(PowerState(soc_pct=99.0), **base(ev=ev))
    assert s.suspension is None and 0 < s.power_w < 1200
