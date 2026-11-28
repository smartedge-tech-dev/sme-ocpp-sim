"""Vehicle profile pool (v0.3 M1): SoC distributions, weighted seeded draws, config and Excel round-trip."""

import random
from collections import Counter

import pytest
from pydantic import ValidationError

from sme_ocpp_sim.charging.ev import PRESETS, EvModel
from sme_ocpp_sim.charging.vehicles import (PRESET_PROFILES, PoolEntry, SocSpec, VehiclePool, build_pool,
                                            stream_rng)
from sme_ocpp_sim.config import EvModelConfig, SimConfig
from sme_ocpp_sim.xlsx_io import export_rows, export_xlsx, import_xlsx

EV_A = EvModel("a", capacity_kwh=40, max_ac_w=6000, max_current_a=30)
EV_B = EvModel("b", capacity_kwh=60, max_ac_w=11000, max_current_a=16, phases=3)
FIXED = SocSpec.fixed(30)


def _pool(wa=1.0, wb=3.0):
    return VehiclePool((PoolEntry(EV_A, wa, FIXED, SocSpec.fixed(90)), PoolEntry(EV_B, wb, FIXED, SocSpec.fixed(80))))


# --------------------------------------------------------------------------------------------- SoC distributions


def test_fixed_and_uniform():
    rng = random.Random(1)
    assert SocSpec.fixed(42).sample(rng) == 42
    xs = [SocSpec("uniform", 20, 40).sample(rng) for _ in range(2000)]
    assert min(xs) >= 20 and max(xs) <= 40
    assert 29 < sum(xs) / len(xs) < 31


def test_normal_is_truncated_to_min_max():
    spec = SocSpec("normal", 30, 50, mean=35, sd=20)  # wide sd: many raw values would fall outside
    rng = random.Random(2)
    xs = [spec.sample(rng) for _ in range(5000)]
    assert min(xs) >= 30 and max(xs) <= 50
    assert sum(x < 40 for x in xs) > sum(x >= 40 for x in xs)  # mass stays on the mean's side
    assert SocSpec("normal", 0, 100, mean=60, sd=0).sample(rng) == 60


@pytest.mark.parametrize("kw, msg", [
    ({"dist": "uniform", "min": 60, "max": 40}, "min <= max"),
    ({"dist": "normal", "mean": 40}, "needs sd"),
    ({"dist": "normal", "mean": 90, "max": 80, "sd": 5}, "within"),
    ({"dist": "fixed"}, "needs mean"),
    ({"dist": "uniform", "mean": 5}, "no mean"),
    ({"dist": "fixed", "mean": 5, "sd": 1}, "no sd"),
    ({"dist": "beta"}, "dist must be"),
])
def test_soc_spec_validation(kw, msg):
    with pytest.raises(ValueError, match=msg):
        SocSpec(**kw)


# --------------------------------------------------------------------------------------------- pool draws


def test_weighted_draw_matches_weights():
    """Chi-square goodness of fit (1 dof, p = 0.001 critical value 10.83) for weights 1 : 3."""
    rng, n = random.Random(7), 20000
    counts = Counter(_pool().draw(rng).ev.id for _ in range(n))
    exp = {"a": n / 4, "b": 3 * n / 4}
    chi2 = sum((counts[k] - e) ** 2 / e for k, e in exp.items())
    assert chi2 < 10.83


def test_zero_weight_is_never_drawn_and_total_must_be_positive():
    rng = random.Random(3)
    assert {_pool(wa=0).draw(rng).ev.id for _ in range(200)} == {"b"}
    with pytest.raises(ValueError, match="total weight"):
        _pool(wa=0, wb=0)
    with pytest.raises(ValueError, match="weight must be >= 0"):
        PoolEntry(EV_A, -1, FIXED, FIXED)


def test_same_seed_same_sequence_and_streams_are_independent():
    pool = build_pool((), initial_soc_pct=30, target_soc_pct=90)

    def seq(seed, cid, conn, n=20):
        rng = stream_rng(seed, cid, conn)
        return [(v.ev.id, v.initial_soc_pct, v.target_soc_pct) for v in (pool.draw(rng) for _ in range(n))]

    assert seq(42, "CP1", 1) == seq(42, "CP1", 1)
    assert seq(42, "CP1", 1) != seq(42, "CP1", 2) != seq(42, "CP2", 1)
    assert seq(42, "CP1", 1) != seq(43, "CP1", 1)


def test_golden_sequence():
    """Fixed-seed snapshot: a change here changes every seeded run (str seeding is stable across processes)."""
    pool = build_pool((), initial_soc_pct=30, target_soc_pct=90)
    rng = stream_rng(42, "CP001", 1)
    got = [(v.ev.id, v.initial_soc_pct) for v in (pool.draw(rng) for _ in range(4))]
    assert got == GOLDEN


GOLDEN = [('suv-90kwh', 61.93), ('suv-90kwh', 32.93), ('van-75kwh', 34.8), ('kei-20kwh', 22.98)]


def test_weighted_override_keeps_only_listed_ids():
    pool = build_pool((), initial_soc_pct=30, target_soc_pct=90).weighted({"kei-20kwh": 1, "suv-90kwh": 0})
    assert pool.ids == ("kei-20kwh", "suv-90kwh")
    assert {pool.draw(random.Random(i)).ev.id for i in range(50)} == {"kei-20kwh"}
    with pytest.raises(ValueError, match="unknown vehicle"):
        pool.weighted({"nope": 1})


def test_build_pool_presets_and_overrides():
    assert set(PRESET_PROFILES) == set(PRESETS) and len(PRESETS) == 6
    custom = EvModel("compact-40kwh", capacity_kwh=41, max_ac_w=6000, max_current_a=30)
    pool = build_pool([(custom, 5, None, None), (EV_A, 1, SocSpec.fixed(10), None)],
                      initial_soc_pct=25, target_soc_pct=85)
    assert pool.ids == (*PRESETS, "a")  # an override takes the preset's place
    e = {x.ev.id: x for x in pool.entries}
    assert e["compact-40kwh"].ev.capacity_kwh == 41 and e["compact-40kwh"].weight == 5
    assert e["compact-40kwh"].initial_soc == SocSpec.fixed(25) and e["a"].target_soc == SocSpec.fixed(85)
    assert e["kei-20kwh"].target_soc == SocSpec.fixed(85)


# --------------------------------------------------------------------------------------------- config


def _sim(evs, ev_model="pool"):
    return SimConfig.model_validate({"csms_defaults": {"url": "ws://csms"}, "ev_models": evs,
                                     "chargers": [{"id": "CP1", "session": {"ev_model": ev_model}}]})


VAN = {"id": "my-van", "capacity_kwh": 75, "max_ac_kw": 11, "phases": 3, "weight": 2,
       "initial_soc": {"dist": "normal", "mean": 35, "sd": 10, "min": 5, "max": 80}, "target_soc": 90}


def test_config_pool_resolves_to_vehicle_pool():
    cfg = _sim([VAN])
    pool = cfg.resolve_ev(cfg.chargers[0])
    assert isinstance(pool, VehiclePool) and pool.ids[-1] == "my-van"
    van = pool.entries[-1]
    assert van.weight == 2 and van.initial_soc == SocSpec("normal", 5, 80, mean=35, sd=10)
    assert van.target_soc == SocSpec.fixed(90)
    # the plain model of a normal session is unchanged
    assert _sim([VAN], ev_model="my-van").resolve_ev(_sim([VAN], "my-van").chargers[0]).id == "my-van"


def test_config_keeps_the_yaml_shape():
    dumped = _sim([VAN]).model_dump(mode="json", exclude_defaults=True)["ev_models"][0]
    assert dumped["target_soc"] == 90 and dumped["initial_soc"]["dist"] == "normal"


@pytest.mark.parametrize("extra, loc", [
    ({"initial_soc": {"dist": "normal", "mean": 35}}, ("initial_soc", "mapping")),
    ({"initial_soc": 120}, ("initial_soc", "number")),
    ({"target_soc": {"dist": "uniform", "min": 1, "bogus": 2}}, ("target_soc", "mapping", "bogus")),
    ({"weight": -1}, ("weight",)),
    ({"id": "pool"}, ("id",)),
])
def test_ev_model_validation(extra, loc):
    with pytest.raises(ValidationError) as ei:
        EvModelConfig.model_validate({"id": "x", "capacity_kwh": 10, "max_ac_kw": 3, **extra})
    assert [tuple(e["loc"]) for e in ei.value.errors()] == [loc]


def test_all_zero_weights_rejected_for_pool_chargers():
    zero = [{"id": k, "capacity_kwh": 10, "max_ac_kw": 3, "weight": 0} for k in PRESETS]
    with pytest.raises(ValidationError, match="total weight"):
        _sim(zero)
    _sim(zero, ev_model="kei-20kwh")  # only a pool charger needs a drawable vehicle


# --------------------------------------------------------------------------------------------- Excel


def test_xlsx_round_trip_of_pool_fields():
    fixed = {"id": "fx", "capacity_kwh": 10, "max_ac_kw": 3, "initial_soc": 20,
             "target_soc": {"dist": "uniform", "min": 70, "max": 90}}
    uni = {"id": "un", "capacity_kwh": 10, "max_ac_kw": 3, "weight": 0.5, "initial_soc": {"min": 10, "max": 30}}
    cfg = _sim([VAN, fixed, uni])
    rows = {r["id"]: r for r in export_rows(cfg)["ev_models"]}
    assert rows["my-van"]["initial_soc_dist"] == "normal" and rows["my-van"]["initial_soc_sd"] == 10
    assert rows["fx"]["initial_soc_dist"] == "fixed" and rows["fx"]["initial_soc_mean"] == 20
    assert rows["un"]["initial_soc_dist"] == "uniform" and rows["un"]["weight"] == 0.5
    data, warnings = export_xlsx(cfg)
    back, errors = import_xlsx(data, cfg)
    assert errors == [] and back.ev_models == cfg.ev_models


def test_xlsx_import_error_names_the_soc_column():
    cfg = _sim([{**VAN, "initial_soc": {"dist": "normal", "mean": 35, "sd": 10, "min": 5, "max": 80}}])
    data, _ = export_xlsx(cfg)
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(data))
    ws = wb["ev_models"]
    headers = [c.value for c in ws[1]]
    ws.cell(row=3, column=headers.index("initial_soc_sd") + 1).value = None
    out = BytesIO()
    wb.save(out)
    back, errors = import_xlsx(out.getvalue(), cfg)
    assert back is None
    assert any("ev_models row 3, column 'initial_soc_dist'" in e and "needs sd" in e for e in errors), errors
