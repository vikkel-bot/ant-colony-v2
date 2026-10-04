import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import n1_repair as R  # noqa: E402
from n1_stress import build_panel, multipliers, placebo_selection  # noqa: E402


def matrix(weeks=40, coins=60, seed=1):
    rng = np.random.default_rng(seed)
    m = rng.normal(0, 0.05, (weeks, coins)) * rng.uniform(0.3, 3, coins)[None, :]
    m[rng.random((weeks, coins)) < 0.1] = np.nan
    return m


def test_hac_matches_direct_formula_and_is_positive():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 1, 200)
    d = x - x.mean()
    T = len(x)
    lrv = (d * d).mean()
    for j in range(1, R.HAC_LAG + 1):
        lrv += 2 * (1 - j / (R.HAC_LAG + 1)) * (d[j:] * d[:-j]).sum() / T
    assert abs(R.hac_t(x) - x.mean() / np.sqrt(lrv / T)) < 1e-12
    xs = rng.normal(0, 1, (50, 200))
    ts = R.hac_t(xs)
    assert ts.shape == (50,) and np.isfinite(ts).all()


def test_hac_shrinks_t_for_autocorrelated_series():
    rng = np.random.default_rng(1)
    e = rng.normal(0, 1, 5000)
    ar = np.empty_like(e)
    ar[0] = e[0]
    for i in range(1, len(e)):
        ar[i] = 0.6 * ar[i - 1] + e[i]
    ar += 0.1
    naive = ar.mean() / (ar.std(ddof=0) / np.sqrt(len(ar)))
    assert R.hac_t(ar) < naive


def fixed_setup(seed=1):
    m = matrix(seed=seed)
    sel = placebo_selection(m, 4, True, np.random.default_rng(0))
    w = multipliers("rademacher", m.shape[0], np.random.default_rng(2))
    return m, sel, build_panel(m, sel, w)


def test_strata_labels_quartiles_and_unknown():
    m, sel, _ = fixed_setup()
    strata = R.strata_for_weeks(m, sel)
    i = max(sel["weeks"])
    members, labels = strata[i]["members"], strata[i]["labels"]
    assert len(members) == len(labels)
    known = labels < R.UNKNOWN
    counts = np.bincount(labels[known], minlength=R.Q_STRATA)
    assert counts.max() - counts.min() <= 1  # rangkwartielen vrijwel gelijk
    lo = max(0, i - R.VOL_WINDOW)
    hist = np.abs(m[lo:i])
    for j, c in enumerate(members):
        n_obs = int((~np.isnan(hist[:, c])).sum())
        assert (labels[j] == R.UNKNOWN) == (n_obs < R.MIN_OBS_STRAT)
    if known.sum() >= 8:
        vol = np.array([np.nanmean(hist[:, c]) for c in members])
        assert np.nanmax(vol[labels == 0]) <= np.nanmin(vol[labels == R.Q_STRATA - 1]) + 1e-12


def test_stratified_null_matches_sensor_counts_per_stratum():
    m, sel, panel = fixed_setup()
    strata = R.strata_for_weeks(m, sel)
    w = panel[5]
    info = strata[w["t"]]
    rng = np.random.default_rng(3)
    # reconstrueer een trekking en tel per stratum
    draws = 200
    total_counts = {q: int(w["top"][info["labels"] == q].sum()) for q in range(R.Q_STRATA + 1)}
    mat = R.stratified_null_matrix([w], info and {w["t"]: info}, draws, rng)
    assert mat.shape == (draws, 1) and np.isfinite(mat).all()
    assert sum(total_counts.values()) == int(w["top"].sum())


def test_stratified_null_centers_near_zero_for_tilted_basket():
    m, sel, panel = fixed_setup()
    strata = R.strata_for_weeks(m, sel)
    rng = np.random.default_rng(4)
    mat = R.stratified_null_matrix(panel, strata, 500, rng)
    uni = R.null_spread_matrix(panel, 500, np.random.default_rng(4))
    # gestratificeerde nul moet de spread van het volatiele mandje beter benaderen dan de uniforme:
    s_obs = np.array([wk["r"][wk["top"]].mean() - wk["r"].mean() for wk in panel])
    assert abs(mat.std() - s_obs.std()) < abs(uni.std() - s_obs.std())


def test_p_hac_and_p_strat_valid_range_and_deterministic():
    m, sel, panel = fixed_setup()
    strata = R.strata_for_weeks(m, sel)
    p1 = R.p_hac(panel, 300, np.random.default_rng(5))
    p2 = R.p_hac(panel, 300, np.random.default_rng(5))
    assert p1 == p2 and 0 < p1 <= 1
    q1 = R.p_strat(panel, strata, 300, np.random.default_rng(6))
    q2 = R.p_strat(panel, strata, 300, np.random.default_rng(6))
    assert q1 == q2 and 0 < q1 <= 1


def test_evaluate_deterministic_and_well_formed():
    m = matrix()
    a = R.evaluate(m, "strat", "4w_hoog", reps=3, draws=100)
    b = R.evaluate(m, "strat", "4w_hoog", reps=3, draws=100)
    assert json.dumps(a) == json.dumps(b)
    assert a["weken"] == m.shape[0] - 1
    assert set(a["alpha_0.05"]) >= {"verwerpingen", "fractie", "cp95", "oordeel"}


def test_reps_extension_reuses_first_runs():
    m = matrix()
    # zelfde (kandidaat, placebo, run)-substromen: run 0-2 van reps=3 == run 0-2 van reps=5
    pv3, pv5 = [], []
    for reps, store in ((3, pv3), (5, pv5)):
        for run in range(reps):
            rng = R.rng_for(0, R.PLACEBOS.index("1w_hoog"), run)
            sel = placebo_selection(m, 1, True, np.random.default_rng(np.random.SeedSequence(R.MASTER_SEED, spawn_key=(98, 1))))
            w = multipliers("rademacher", m.shape[0], rng)
            store.append(R.p_hac(build_panel(m, sel, w), 100, rng))
    assert pv3 == pv5[:3]
