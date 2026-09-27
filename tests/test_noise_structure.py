"""Tests voor de ruisstructuurmeting.

Kern: de meting moet bekende afhankelijkheid terugvinden en bij onafhankelijke
ruis niets vinden. En ze mag geen enkele sensor of rangschikking gebruiken.
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import noise_structure as NS  # noqa: E402
from ant_colony.lab import xs_rank_test as X  # noqa: E402

D = NS.U.DAY_MS
START = int(datetime(2021, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
DAYS = 1900


def _candles(n_coins: int = 25, phi: float = 0.0, seed: int = 3) -> dict[str, list]:
    """Munten met AR(1)-afhankelijkheid phi in hun weekrendement.

    Let op: de driftblokken lopen synchroon met de meetweken (X.week_starts()).
    Doen ze dat niet, dan deelt elk meetweekpaar een stuk drift en meet je
    afhankelijkheid die de generator zelf heeft ingebracht.
    """
    rng = random.Random(seed)
    week0 = X.week_starts()[0]
    out = {}
    for i in range(n_coins):
        price, rows, drift = 10.0, [], 0.0
        for d in range(DAYS):
            ts = START + d * D
            if phi is not None and (ts - week0) % X.WEEK_MS == 0:
                drift = phi * drift + rng.gauss(0, 0.05)
            price *= math.exp(drift / 7 + rng.gauss(0, 0.01))
            rows.append([ts, price, price, price, price, 300_000 / price])
        out[f"C{i:02d}-EUR"] = rows
    return out


def _candles_iid(n_coins: int = 25, seed: int = 3) -> dict[str, list]:
    """Zuiver onafhankelijke dagreturns: geen enkele afhankelijkheid."""
    rng = random.Random(seed)
    out = {}
    for i in range(n_coins):
        price, rows = 10.0, []
        for d in range(DAYS):
            price *= math.exp(rng.gauss(0, 0.02))
            rows.append([START + d * D, price, price, price, price, 300_000 / price])
        out[f"C{i:02d}-EUR"] = rows
    return out


@pytest.fixture(scope="module")
def onafhankelijk():
    return NS.analyse(_candles_iid(), seed=1)


@pytest.fixture(scope="module")
def afhankelijk():
    return NS.analyse(_candles(phi=0.7), seed=1)


def test_independent_noise_shows_no_lag1_dependence(onafhankelijk):
    lag1 = onafhankelijk["lags"]["r_rel"]["1"]
    assert abs(lag1["pooled"]) < 0.10
    lo, hi = lag1["ci95_cluster_bootstrap"]
    assert lo < 0 < hi


def test_injected_dependence_is_recovered(afhankelijk):
    lag1 = afhankelijk["lags"]["r_rel"]["1"]
    assert lag1["pooled"] > 0.2
    lo, hi = lag1["ci95_cluster_bootstrap"]
    assert lo > 0


def test_per_coin_distribution_is_reported(afhankelijk):
    pc = afhankelijk["lags"]["r_rel"]["1"]["per_munt"]
    assert pc["n_munten"] >= 10
    assert pc["p10"] <= pc["mediaan"] <= pc["p90"]


def test_pair_counts_shrink_with_lag(onafhankelijk):
    n1 = onafhankelijk["lags"]["r_rel"]["1"]["n_paren"]
    n8 = onafhankelijk["lags"]["r_rel"]["8"]["n_paren"]
    assert n8 < n1 and n8 > 0


def test_absolute_and_squared_are_measured(onafhankelijk):
    for label in ("|r_rel|", "r_rel^2"):
        assert onafhankelijk["lags"][label]["1"]["pooled"] is not None


def test_sticky_baskets_alone_create_no_dependence(onafhankelijk):
    """Kernbevinding: bij onafhankelijke returns geeft vasthouden GEEN
    autocorrelatie in de spread. Selectiepersistentie kost op zichzelf dus geen
    informatie; alleen in combinatie met afhankelijke returns telt ze mee."""
    for tenure in ("tenure_1", "tenure_6", "tenure_12"):
        assert abs(onafhankelijk["sticky_random_spread_acf"][tenure]["1"]) < 0.10


def test_sticky_baskets_amplify_existing_return_dependence(afhankelijk):
    los = afhankelijk["sticky_random_spread_acf"]["tenure_1"]["1"]
    vast = afhankelijk["sticky_random_spread_acf"]["tenure_12"]["1"]
    assert vast > los


def test_residual_matrix_is_stored(onafhankelijk):
    res = onafhankelijk["residuen"]["per_munt"]
    assert len(res) >= 10
    assert all(isinstance(v, dict) and v for v in res.values())


def test_measurement_uses_no_ranking_and_no_sensor_module():
    """Alleen code telt, geen toelichting: geen rangschikking, geen sensorimport."""
    src = Path(NS.__file__).read_text(encoding="utf-8")
    code = "\n".join(r for r in src.splitlines()
                     if not r.strip().startswith("#") and '"""' not in r)
    assert "top_k" not in code
    assert "sensor_momentum" not in code and "screen_sensors" not in code
    assert "sorted(scores" not in code
    # xs_rank_test levert alleen week_starts/truncate_before/_fwd_return, geen rangschikking
    zonder_import = "\n".join(r for r in code.splitlines() if "xs_rank_test" not in r)
    assert "rank" not in zonder_import.lower().replace("random", "")


def test_holdout_data_cannot_influence_result():
    base = _candles(phi=0.5)
    poisoned = {m: [list(r) for r in c] for m, c in base.items()}
    for c in poisoned.values():
        for r in c:
            if r[0] >= X.HOLDOUT_START:
                r[4] = r[4] * 1000
    a = NS.analyse(base, seed=5)
    b = NS.analyse(poisoned, seed=5)
    assert a["lags"] == b["lags"]
