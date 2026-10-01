import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import n1_stress as S  # noqa: E402
from ant_colony.lab.xs_rank_test import spreads  # noqa: E402


def matrix(weeks=30, coins=60, seed=1):
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.05, (weeks, coins)) * rng.uniform(0.3, 3, coins)[None, :]
    m[rng.random((weeks, coins)) < 0.1] = np.nan
    return m


def test_multipliers_rademacher_and_block3():
    rng = np.random.default_rng(0)
    w = S.multipliers("rademacher", 500, rng)
    assert set(np.unique(w)) == {-1.0, 1.0}
    b = S.multipliers("block3", 300, np.random.default_rng(1))
    assert set(np.unique(b)) == {-1.0, 1.0}
    changes = np.flatnonzero(np.diff(b) != 0) + 1
    assert all(np.diff(changes) % 3 == 0)


def test_mammen_moments():
    w = S.multipliers("mammen", 400_000, np.random.default_rng(2))
    assert abs(w.mean()) < 0.01 and abs((w ** 2).mean() - 1) < 0.01 and abs((w ** 3).mean() - 1) < 0.03


def test_selection_uses_only_past_weeks():
    m = matrix()
    sel = S.placebo_selection(m, 4, True, np.random.default_rng(0))
    m2 = m.copy()
    m2[10] = m2[10] * 100
    sel2 = S.placebo_selection(m2, 4, True, np.random.default_rng(0))
    assert np.array_equal(sel["weeks"][10], sel2["weeks"][10])
    assert not np.array_equal(sel["weeks"][11], sel2["weeks"][11])


def test_high_and_low_pick_extremes_of_previous_week():
    m = matrix()
    for high in (True, False):
        sel = S.placebo_selection(m, 1, high, np.random.default_rng(0))
        i = 5
        members = np.flatnonzero(~np.isnan(m[i]))
        cand = [c for c in members if not np.isnan(m[i - 1, c])]
        prev = {c: abs(m[i - 1, c]) for c in cand}
        k = S.top_k(len(members))
        picked = set(sel["weeks"][i].tolist())
        rest = [prev[c] for c in cand if c not in picked]
        if high:
            assert min(prev[c] for c in picked) >= max(rest)
        else:
            assert max(prev[c] for c in picked) <= min(rest)
        assert len(picked) == min(k, len(members))


def test_first_week_dropped_and_fill_counted():
    m = matrix()
    sel = S.placebo_selection(m, 1, True, np.random.default_rng(0))
    assert 0 not in sel["weeks"] and len(sel["weeks"]) == m.shape[0] - 1
    sparse = m.copy()
    sparse[4, 5:] = np.nan
    sel2 = S.placebo_selection(sparse, 1, True, np.random.default_rng(0))
    assert sel2["fill_weeks"] >= 1 and len(sel2["weeks"][5]) == min(S.top_k(int((~np.isnan(m[5])).sum())), 60)


def test_multiplier_scales_spread_exactly():
    m = matrix()
    sel = S.placebo_selection(m, 4, True, np.random.default_rng(0))
    w = S.multipliers("mammen", m.shape[0], np.random.default_rng(3))
    base = spreads(S.build_panel(m, sel, np.ones(m.shape[0])))
    weeks = sorted(sel["weeks"])
    assert np.allclose(spreads(S.build_panel(m, sel, w)), w[weeks] * base)


def test_clopper_pearson_reference_values():
    lo, hi = S.clopper_pearson(0, 300)
    assert lo == 0.0 and abs(hi - 0.012221) < 1e-5
    lo, hi = S.clopper_pearson(50, 1000)
    assert abs(lo - 0.037335) < 1e-5 and abs(hi - 0.065390) < 1e-5


def test_classify_and_more_runs_rule():
    assert S.classify(90, 1000, 0.05) == "ANTI_CONSERVATIEF"
    assert S.classify(20, 1000, 0.05) == "CONSERVATIEF"
    assert S.classify(52, 1000, 0.05) == "GEEN_AANWIJZING"
    assert S.needs_more_runs(11, 1000) and not S.needs_more_runs(5, 1000)


def test_evaluate_is_deterministic_and_well_formed():
    m = matrix()
    a = S.evaluate(m, "rademacher", "1w_hoog", reps=4, draws=50)
    b = S.evaluate(m, "rademacher", "1w_hoog", reps=4, draws=50)
    assert json.dumps(a) == json.dumps(b)
    assert a["weken"] == m.shape[0] - 1 and 0 <= a["Q"] <= 1
    assert set(a["alpha_0.05"]) >= {"verwerpingen", "fractie", "cp95", "oordeel"}


def test_uniform_control_draws_new_path_per_run():
    m = matrix()
    p0 = S.uniform_selection(m, S.rng_for(0, 0, 0))["weeks"][3]
    p1 = S.uniform_selection(m, S.rng_for(0, 0, 1))["weeks"][3]
    assert not np.array_equal(np.sort(p0), np.sort(p1))
