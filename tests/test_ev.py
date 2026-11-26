import pytest

from sme_ocpp_sim.charging.ev import (
    DEFAULT_CURVE, PRESETS, EvModel, curve_fraction, ev_accept_w, ev_max_power_w, ev_min_power_w,
    validate_curve,
)


def test_curve_cc_then_linear_taper():
    assert curve_fraction(DEFAULT_CURVE, 0) == 1.0
    assert curve_fraction(DEFAULT_CURVE, 50) == 1.0
    assert curve_fraction(DEFAULT_CURVE, 80) == 1.0
    assert curve_fraction(DEFAULT_CURVE, 90) == pytest.approx(0.55)
    assert curve_fraction(DEFAULT_CURVE, 100) == pytest.approx(0.1)
    # clamped outside the table
    assert curve_fraction(((20.0, 0.5), (60.0, 1.0)), 10) == 0.5
    assert curve_fraction(((20.0, 0.5), (60.0, 1.0)), 90) == 1.0


def test_curve_monotonic_non_increasing_above_cc():
    fracs = [curve_fraction(DEFAULT_CURVE, s) for s in range(80, 101)]
    assert all(a >= b for a, b in zip(fracs, fracs[1:]))


@pytest.mark.parametrize("curve", [(), ((50.0, 1.0), (50.0, 0.5)), ((0.0, 1.5),), ((-1.0, 1.0),)])
def test_curve_validation(curve):
    with pytest.raises(ValueError):
        validate_curve(curve)


def test_model_validation():
    with pytest.raises(ValueError):
        EvModel("x", capacity_kwh=0, max_ac_w=1000, max_current_a=10)
    with pytest.raises(ValueError):
        EvModel("x", capacity_kwh=10, max_ac_w=1000, max_current_a=10, phases=4)
    with pytest.raises(ValueError):
        EvModel("x", capacity_kwh=10, max_ac_w=1000, max_current_a=10, min_current_a=12)


def test_max_power_taper_and_current_cap():
    ev = PRESETS["compact-40kwh"]
    assert ev_max_power_w(ev, 30, 200, 1) == 6000
    assert ev_max_power_w(ev, 90, 200, 1) == pytest.approx(3300)
    # 30 A at 100 V caps below max_ac_w
    assert ev_max_power_w(ev, 30, 100, 1) == 3000


def test_accept_zero_at_target():
    ev = PRESETS["compact-40kwh"]
    assert ev_accept_w(ev, 89.9, 90, 200, 1) > 0
    assert ev_accept_w(ev, 90, 90, 200, 1) == 0
    assert ev_accept_w(ev, 95, 90, 200, 1) == 0


def test_min_power_and_presets():
    ev = PRESETS["midsize-60kwh"]
    assert ev.phases == 3
    assert ev_min_power_w(ev, 230, 3) == pytest.approx(6 * 230 * 3)
    assert set(PRESETS) >= {"compact-40kwh"}
    assert PRESETS["compact-40kwh"].capacity_kwh == 40
