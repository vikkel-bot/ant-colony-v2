"""Tests voor scripts/n1_repair_power.py (addendum 1945c1c)."""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import n1_repair_power as P  # noqa: E402
from ant_colony.lab.xs_rank_test import spreads  # noqa: E402
from n1_repair import hac_t  # noqa: E402
from n1_stress import build_panel, multipliers  # noqa: E402


def _synthetic(weeks=40, coins=30, seed=7):
    rng = np.random.default_rng(seed)
    m = rng.standard_normal((weeks, coins)) * 0.05
    m[rng.random((weeks, coins)) < 0.1] = np.nan
    return m


def test_block3_equivalent_to_n1_stress():
    a = P.block_multipliers(3, 157, np.random.default_rng(1))
    b = multipliers("block3", 157, np.random.default_rng(1))
    assert np.array_equal(a, b)


def test_iid_equals_rademacher():
    a = P.dgp_multipliers("iid", 157, np.random.default_rng(2))
    b = multipliers("rademacher", 157, np.random.default_rng(2))
    assert np.array_equal(a, b)


@pytest.mark.parametrize("L", [4, 8])
def test_block_constant_within_blocks(L):
    w = P.dgp_multipliers(f"block{L}", 157, np.random.default_rng(3))
    offset = int(np.random.default_rng(3).integers(0, L))
    block = (np.arange(157) + offset) // L
    assert set(np.unique(w)) <= {-1.0, 1.0}
    for b in np.unique(block):
        assert len(set(w[block == b])) == 1


def test_unknown_dgp_raises():
    with pytest.raises(ValueError):
        P.dgp_multipliers("block5x", 10, np.random.default_rng(0))


def test_inject_shifts_s1_exactly_and_keeps_universe_mean():
    m = _synthetic()
    sel = P.fixed_path(m, "4w_hoog")
    panel = build_panel(m, sel, np.ones(m.shape[0]))
    inj = P.inject_path(panel, 0.006)
    assert np.allclose(spreads(inj) - spreads(panel), 0.006, atol=1e-12)
    for a, b in zip(panel, inj):
        assert math.isclose(a["r"].mean(), b["r"].mean(), abs_tol=1e-12)


def test_inject_undefined_raises():
    panel = [{"t": 0, "n": 8, "k": 8, "r": np.zeros(8), "top": np.ones(8, bool)}]
    with pytest.raises(ValueError):
        P.inject_path(panel, 0.004)


def test_var_ratio_consistent_with_hac_t():
    x = np.random.default_rng(4).standard_normal(156).cumsum() * 0.01 + np.random.default_rng(5).standard_normal(156)
    T = len(x)
    d = x - x.mean()
    lrv = P.var_ratio(x) * float((d * d).mean())
    assert math.isclose(hac_t(x), x.mean() / math.sqrt(lrv / T), rel_tol=1e-10)


def test_holm_known_example():
    assert P.holm([0.01, 0.04, 0.03, 0.005]) == [True, False, False, True]
    assert P.holm([0.2, 0.3]) == [False, False]


def test_critical_count_is_minimal():
    n, a, level = 4000, 0.0083, 0.05 / 12
    x = P.critical_count(n, a, level)
    assert P.binom_sf(x, n, a) <= level < P.binom_sf(x - 1, n, a)


def test_gate_oc_monotone():
    oc = P.gate_oc(4000, 12)
    for a in P.GATE_ALPHAS:
        r = oc[f"alpha_{a}"]
        assert r["ware_size_1.25x"] < r["ware_size_1.5x"] < r["ware_size_2.0x"]


def test_extension_rule():
    assert P.needs_extension(240, 300)       # 0,80 in interval
    assert not P.needs_extension(150, 300)   # 0,50
    assert not P.needs_extension(299, 300)   # boven 0,90-interval


def test_mde_interpolation():
    d = (0.004, 0.006, 0.008, 0.010)
    assert math.isclose(P.mde(d, [0.2, 0.5, 0.85, 0.95], 0.80), 0.006 + (0.3 / 0.35) * 0.002)
    assert math.isclose(P.mde(d, [0.2, 0.5, 0.85, 0.95], 0.90), 0.009)
    assert P.mde(d, [0.1, 0.2, 0.3, 0.4], 0.80) is None
    assert P.mde(d, [0.85, 0.9, 0.95, 0.99], 0.80) == 0.004


def test_empirical_cutoff():
    p0 = np.linspace(0.0001, 1.0, 4000)
    c = P.empirical_cutoff(p0)
    assert (p0 < c).mean() <= P.ALPHA_DETECT


def test_check_gate_fail_closed():
    with pytest.raises(SystemExit):
        P.check_gate({}, False)
    fail = {"gate": {"compleet": True, "oordeel": "FAIL",
                     "verworpen": {"block8|1w_hoog|0.0083": True, "block4|1w_hoog|0.0083": False}}}
    with pytest.raises(SystemExit):
        P.check_gate(fail, False)
    assert P.check_gate(fail, True) == ["block4"]
    ok = {"gate": {"compleet": True, "oordeel": "PASS", "verworpen": {}}}
    assert P.check_gate(ok, False) == ["block4", "block8"]


def test_seeds_deterministic_and_distinct():
    a = P.rng_for(2, 1, 0, 0, 5).random(3)
    assert np.array_equal(a, P.rng_for(2, 1, 0, 0, 5).random(3))
    assert not np.array_equal(a, P.rng_for(2, 2, 0, 0, 5).random(3))


def test_part_b_smoke_and_resume(tmp_path):
    m = _synthetic()
    out = tmp_path / "b.json"
    payload = {}
    save = lambda: out.write_text(json.dumps(payload), encoding="utf-8")  # noqa: E731
    P.run_part_b(m, list(P.PATHS), 20, 3, payload, save)
    g = payload["gate"]
    assert g["compleet"] is True and g["familie"] == 12 and g["oordeel"] in ("PASS", "FAIL")
    first = payload["resultaten"]["block4|1w_hoog"]["p"]
    P.run_part_b(m, list(P.PATHS), 20, 3, payload, save)
    assert payload["resultaten"]["block4|1w_hoog"]["p"] == first


def test_part_c_smoke():
    m = _synthetic()
    gate = {}
    P.run_part_b(m, list(P.PATHS), 20, 4, gate, lambda: None)
    payload = {}
    P.run_part_c(m, ["4w_hoog"], 20, gate, ["block4"], payload, lambda: None, reps=3, reps_ext=3)
    assert "block4|4w_hoog|MDE" in payload["resultaten"]
    assert "MDE80" in payload["eligibility"]
