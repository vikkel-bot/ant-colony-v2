"""Validatie T001-instrument: vindt het ingebouwde momentum, niets in ruis,
negeert de verkeerde richting, en de holdout is onbereikbaar."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from ant_colony.lab import xs_rank_test as X
from ant_colony.lab.sensor_momentum import mom21

D = X.DAY_MS
START = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
DAYS = 1500  # loopt door tot na de holdout-grens
DRAWS = 300


def _market(seed: int, n_coins: int = 40, persistence: float = 0.0, drift_sd: float = 0.0):
    rng = np.random.default_rng(seed)
    data = {}
    for i in range(n_coins):
        mu = np.zeros(DAYS)
        shocks = rng.normal(0, drift_sd * np.sqrt(1 - persistence**2) if persistence else 0.0, DAYS)
        for d in range(1, DAYS):
            mu[d] = persistence * mu[d - 1] + shocks[d]
        close = 10 * np.exp(np.cumsum(mu + rng.normal(0, 0.03, DAYS)))
        data[f"C{i:02d}-EUR"] = [[START + d * D, c, c, c, c, 200_000 / c] for d, c in enumerate(close)]
    return data


def test_no_structure_rarely_significant():
    hits = 0
    for seed in range(4):
        res = X.run(_market(seed), mom21, draws=DRAWS, seed=seed)
        if res["primary"]["p_N1"][0] < 0.05:
            hits += 1
    assert hits <= 1


def test_detects_built_in_momentum():
    res = X.run(_market(7, persistence=0.99, drift_sd=0.004), mom21, draws=DRAWS, seed=1)
    assert res["primary"]["p_N1"][0] < 0.01
    assert res["primary"]["S"][0] > 0


def test_wrong_direction_is_not_significant():
    def neg(c, t):
        v = mom21(c, t)
        return None if v is None else -v
    res = X.run(_market(7, persistence=0.99, drift_sd=0.004), neg, draws=DRAWS, seed=1)
    assert res["primary"]["p_N1"][0] > 0.5
    assert res["verdict"] == "FAIL"


def test_holdout_cannot_influence_result():
    base = _market(3, persistence=0.99, drift_sd=0.004)
    poisoned = {m: [list(r) for r in c] for m, c in base.items()}
    for c in poisoned.values():
        for r in c:
            if r[0] >= X.HOLDOUT_START:
                r[4] = r[4] * 1000  # absurde toekomst
    a = X.run(base, mom21, draws=50, seed=2)
    b = X.run(poisoned, mom21, draws=50, seed=2)
    assert a == b


def test_weeks_end_before_holdout():
    weeks = X.week_starts()
    assert weeks[0] == X.DEV_START
    assert weeks[-1] + X.WEEK_MS <= X.HOLDOUT_START


def test_sensor_ignores_future():
    c = [[START + d * D, 1.0, 1.0, 1.0, 1.0 + d, 1.0] for d in range(100)]
    t = START + 60 * D
    future_changed = [r if r[0] < t else [r[0], 9, 9, 9, 999.0, 9] for r in c]
    assert mom21(c, t) == mom21(future_changed, t)


def test_verdict_rules():
    ok = {"s1_sig_N1": True, "c1": True, "c2": True, "c3": True}
    assert X.verdict(ok, ok, 5.0) == "PASS"
    assert X.verdict(ok, ok, 1.0) == "INFORMATIEF_NIET_VERHANDELBAAR"
    assert X.verdict({**ok, "s1_sig_N1": False, "c1": False}, ok, 5.0) == "FAIL"
    assert X.verdict({**ok, "c2": False}, {**ok, "c2": False}, 5.0) == "INCONCLUSIVE"
    assert X.verdict(ok, {**ok, "c3": False}, 5.0) == "INCONCLUSIVE"
