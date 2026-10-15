"""Tests SIT-1A (docs/PREREG_SIT1_20260926.md). Uitsluitend synthetische data:
geen test leest de echte uitkomstreeksen."""
from __future__ import annotations

import io
import json
import math
import zipfile

import numpy as np
import pytest

from ant_colony.lab import sit1 as S

START = S.M(1926, 7)


def synth(n_months: int = 300, d_true: float = 0.0, seed: int = 1):
    """Log-RV AR(1) met optioneel echt ΔCS-effect; CS random walk vanaf 1919."""
    rng = np.random.default_rng(seed)
    cs = {}
    level = 100.0
    for m in range(S.M(1919, 1), START + n_months + 2):
        level += rng.normal(0, 10)
        cs[m] = level
    y, rv = {}, {}
    prev = math.log(0.002)
    for k in range(n_months):
        m = START + k
        d = S.dcs(cs, m - 1) or 0.0
        prev = -6.2 + 0.6 * (prev + 6.2) + d_true * d + rng.normal(0, 0.4)
        y[m] = prev
        rv[m] = math.exp(prev)
    return rv, cs


def test_maandcodering():
    assert S.label(S.M(1937, 7)) == "1937-07"
    assert S.WINDOWS["development"][1] - S.WINDOWS["development"][0] + 1 == 642
    assert S.WINDOWS["lhe"][1] - S.WINDOWS["lhe"][0] + 1 == 429


def test_dcs_gebruikt_t_min_1_en_t_min_4():
    cs = {S.M(2000, m): float(m) for m in range(1, 13)}
    assert S.dcs(cs, S.M(2000, 6)) == 5.0 - 2.0
    assert S.dcs(cs, S.M(2000, 6), h=1) == 5.0 - 4.0
    assert S.dcs(cs, S.M(2000, 2)) is None


def test_eerste_target_en_inloop():
    rv, cs = synth(200)
    rows = S.build_rows(rv, cs)
    assert rows[0].target == S.FIRST_TARGET
    assert sum(r.target <= S.M(1937, 6) for r in rows) == S.INLOOP_N
    r = rows[0]
    ys = [math.log(rv[r.t - k]) for k in range(12)]
    assert np.allclose(r.x0, [1.0, ys[0], np.mean(ys[:3]), np.mean(ys)])
    assert r.max_y_month == r.t and r.max_cs_month == r.t - 1


def test_geen_toekomstlek_vergiftigde_toekomst():
    """Forecast voor target τ verandert niet als alle data ná τ-1 wordt vervangen."""
    rv, cs = synth(260)
    start, end = S.M(1937, 7), S.M(1938, 6)
    base = S.recursive_forecasts(S.build_rows(rv, cs), start, end)
    tau = S.M(1938, 1)
    i = base.target.index(tau)
    rv2 = {m: (v * 50 if m > tau - 1 else v) for m, v in rv.items()}
    cs2 = {m: (v + 999 if m > tau - 2 else v) for m, v in cs.items()}
    pois = S.recursive_forecasts(S.build_rows(rv2, cs2), start, end)
    assert pois.yhat0[i] == pytest.approx(base.yhat0[i], abs=1e-12)
    assert pois.yhat1[i] == pytest.approx(base.yhat1[i], abs=1e-12)
    assert pois.s2_1[i] == pytest.approx(base.s2_1[i], abs=1e-12)


def test_ontbrekende_target_faalt_gesloten():
    rv, cs = synth(150)
    with pytest.raises(ValueError):
        S.recursive_forecasts(S.build_rows(rv, cs), S.M(1937, 7), S.M(1990, 12))


def test_cw_vindt_echt_effect_en_niet_bij_nul():
    rv, cs = synth(700, d_true=0.01, seed=3)
    fc = S.recursive_forecasts(S.build_rows(rv, cs), S.M(1937, 7), S.M(1980, 12))
    assert S.clark_west(fc)["p"] < 0.001
    rv0, cs0 = synth(700, d_true=0.0, seed=3)
    fc0 = S.recursive_forecasts(S.build_rows(rv0, cs0), S.M(1937, 7), S.M(1980, 12))
    assert S.clark_west(fc0)["p"] > 0.001        # één trekking onder H0; size zelf: sit1_cw_size_sim.py


def test_nw_zonder_lags_is_gewone_variantie_van_gemiddelde():
    x = np.array([1.0, 3.0, 2.0, 5.0, 4.0])
    assert S.nw_var_of_mean(x, lags=0) == pytest.approx(np.var(x) / len(x))


def test_qlike():
    rv = np.array([0.01, 0.02])
    assert np.allclose(S.qlike(rv, rv), 0.0)
    assert (S.qlike(rv * 2, rv) > 0).all() and (S.qlike(rv / 2, rv) > 0).all()
    # onderschatting duurder dan overschatting met dezelfde factor
    assert S.qlike(rv / 2, rv)[0] > S.qlike(rv * 2, rv)[0]


def test_bootstrap_deterministisch_en_ci_rond_punt():
    rng = np.random.default_rng(0)
    delta, q0 = rng.normal(0.01, 0.1, 300), rng.uniform(0.5, 1.5, 300)
    a = S.qlike_gain(delta, q0, n_boot=500)
    b = S.qlike_gain(delta, q0, n_boot=500)
    assert a == b
    assert a["ci_lower"] <= a["delta"] <= a["ci_upper"]
    assert a["mde_delta"] == pytest.approx(2.8 * a["se_boot"])


def test_stationary_indices_blokstructuur():
    rng = np.random.Generator(np.random.PCG64(1))
    idx = S.stationary_indices(1000, 50, 12, rng)
    assert idx.min() >= 0 and idx.max() < 1000
    cont = np.mean((idx[:, 1:] - idx[:, :-1]) % 1000 == 1)
    assert 0.88 < cont < 0.95          # ~11/12 voortzettingen


@pytest.mark.parametrize("p,d,delta,ci_up,expected", [
    (0.001, 0.1, 0.03, 0.05, "PASS"),
    (0.001, 0.1, 0.01, 0.05, "INCONCLUSIVE"),     # significant maar Δ < 2 %
    (0.20, 0.1, 0.03, 0.05, "INCONCLUSIVE"),      # positief punt, niet significant
    (0.20, 0.1, 0.00, 0.019, "FAIL"),             # relevant effect uitgesloten
    (0.001, -0.1, 0.03, 0.05, "FAIL"),            # verkeerde richting
])
def test_beslisregel(p, d, delta, ci_up, expected):
    assert S.decide(p, S.THR["development"], d, delta, ci_up) == expected


def test_drempels():
    assert S.THR["development"] == pytest.approx(0.05 / 6)
    assert S.THR["lhe"] == 0.05


def _path(gap_months: int) -> dict[int, float]:
    """Episode A: maand +3 (val 30 %), herstel op +4; dan gap_months maanden zonder DD; dan episode B."""
    rets = [1.0] * 3 + [0.7, 1 / 0.7] + [1.0] * (gap_months - 1) + [0.7] + [1.0] * 3
    return {START + k: r for k, r in enumerate(rets)}


@pytest.mark.parametrize("gap,n_eps", [(5, 1), (6, 2)])
def test_episodes_samenvoegen_alleen_bij_gat_kleiner_dan_6(gap, n_eps):
    eps = S.episodes(_path(gap))
    assert len(eps) == n_eps
    assert eps[0]["start"] == START + 3


def test_ff_parser_en_minimum_dagen():
    lines = ["This file was created using the 202608 CRSP database.", "", ",Mkt-RF,SMB,HML,RF"]
    lines += [f"192607{d:02d},1.00,0,0,0.01" for d in range(1, 21)]
    lines += [f"192608{d:02d},-2.00,0,0,0.01" for d in range(1, 5)]
    lines += ["", "Copyright 2026 Eugene F. Fama and Kenneth R. French"]
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("F.CSV", "\n".join(lines))
    b.seek(0)
    daily = S.read_ff_daily(b)
    rv = S.monthly_rv(daily)
    assert rv == {S.M(1926, 7): pytest.approx(20 * 0.01 ** 2)}         # augustus: < 15 dagen


def _rec(date, start, end, value):
    return {"date": date, "realtime_start": start, "realtime_end": end, "value": value}


def test_vintagecontrole_markeert_afwijking_boven_5bp():
    dates = [f"2000-{m:02d}-01" for m in range(1, 9)]
    baa = [_rec(d, "2000-01-01", "9999-12-31", "8.00") for d in dates]
    aaa = [_rec(d, "2000-01-01", "9999-12-31", "7.00") for d in dates]
    cs_now = S.credit_spread_bp({S.M(2000, m): 8.0 for m in range(1, 9)},
                                {S.M(2000, m): 7.0 for m in range(1, 9)})
    assert S.vintage_check(baa, aaa, cs_now)["months_dcs_dev_over_5bp"] == []
    baa[2] = _rec(dates[2], "2000-01-01", "2000-06-30", "8.10")     # oude vintage 10 bp anders
    baa.append(_rec(dates[2], "2000-07-01", "9999-12-31", "8.00"))
    res = S.vintage_check(baa, aaa, cs_now)
    assert res["max_dev_cs_bp"] == pytest.approx(10.0)
    assert res["months_dcs_dev_over_5bp"]


def test_publicatiecontrole():
    recs = [_rec("2005-01-01", "2005-02-03", "9999-12-31", "7"),
            _rec("2005-02-01", "2005-04-10", "9999-12-31", "7")]
    res = S.publication_check(recs)
    assert res["months_checked"] == 2
    assert res["published_after_end_of_next_month"] == [{"maand": "2005-02", "eerste_publicatie": "2005-04-10"}]


def test_reverse_causality_en_episode_attributie_draaien():
    rv, cs = synth(320, seed=5)
    rows = S.build_rows(rv, cs)
    fc = S.recursive_forecasts(rows, S.M(1937, 7), S.M(1950, 12))
    rc = S.reverse_causality(rows, cs, S.M(1950, 12))
    assert set(rc["coef"]) == {"const", "y_t", "ybar3", "ybar12", "dcs_t"}
    growth = {m: 1.0 + 0.05 * math.sin(m / 7) for m in range(START, START + 320)}
    att = S.episode_attribution(fc, S.episodes(growth))
    assert att["volledig"]["n"] == len(fc.target)
