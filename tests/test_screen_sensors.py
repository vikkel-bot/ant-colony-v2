"""Tests voor de outcome-blinde sensorscreening.

Kern: de screening mag nooit een rendement na het selectiemoment aanraken, en
de gemeten turnover/persistence moeten kloppen bij bekende invoer.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import screen_sensors as S  # noqa: E402
from ant_colony.lab import xs_rank_test as X  # noqa: E402

D = S.U.DAY_MS
START = int(datetime(2021, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
DAYS = 1900


def _candles(n_coins: int = 30, seed: int = 1) -> dict[str, list]:
    import random
    rng = random.Random(seed)
    out = {}
    for i in range(n_coins):
        price, rows = 10.0, []
        vol = 0.01 + 0.04 * (i / n_coins)          # vaste, verschillende volatiliteit
        for d in range(DAYS):
            price *= math.exp(rng.gauss(0, vol))
            rows.append([START + d * D, price, price, price, price, 300_000 / price])
        out[f"C{i:02d}-EUR"] = rows
    out["BTC-EUR"] = out.pop("C00-EUR")
    return out


@pytest.fixture(scope="module")
def cost_model(tmp_path_factory):
    import build_cost_model as B
    rows = []
    for s in range(4):
        for m in range(12):
            base = 0.002
            r = {"taken_at": f"s{s}", "market": f"M{m}-EUR", "turnover_30d": 100_000 * (m + 1),
                 "spread": base}
            for i, size in enumerate(B.SIZES_EUR):
                r[f"buy_{size}"] = base * (1 + i)
                r[f"sell_{size}"] = base * (1 + i)
            rows.append(r)
    p = tmp_path_factory.mktemp("cm") / "COST_MODEL_TEST.json"
    p.write_text(json.dumps(B.build(rows, "COST_MODEL_TEST")), encoding="utf-8")
    return p


def test_buffer_reduces_turnover_for_a_noisy_sensor():
    ranked_weeks = [[f"C{i:02d}" for i in order] for order in
                    ([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
                     [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])]
    k = 4
    held_plain, held_buf = set(), set()
    t_plain = t_buf = 0
    for ranked in ranked_weeks:
        plain = S.select_plain(ranked, k)
        buf = S.select_buffered(ranked, k, held_buf)
        if held_plain:
            t_plain += len(set(plain) - held_plain)
            t_buf += len(set(buf) - held_buf)
        held_plain, held_buf = set(plain), set(buf)
    assert t_buf <= t_plain


def test_buffer_keeps_basket_size_at_k():
    ranked = [f"C{i:02d}" for i in range(20)]
    for k in (4, 8, 13):
        assert len(S.select_buffered(ranked, k, set())) == k
        assert len(S.select_buffered(ranked, k, {"C15", "C16"})) == k


def test_spearman_recognises_identical_and_reversed_order():
    a = {f"C{i}": float(i) for i in range(10)}
    b = {f"C{i}": float(-i) for i in range(10)}
    assert S.spearman(a, a) == pytest.approx(1.0)
    assert S.spearman(a, b) == pytest.approx(-1.0)


def test_static_sensor_has_high_persistence_and_low_turnover(cost_model):
    res = S.run(_candles(), cost_model_path=cost_model)
    vol = next(r for r in res["kandidaten"] if r["sensor"] == "realized_vol_60d@laag")
    assert vol["rank_persistence_spearman"] > 0.8      # vaste volatiliteit per munt
    assert vol["turnover_per_rebalance"] < 0.2
    assert vol["mean_tenure_weeks"] > 5


def test_screening_never_reads_data_at_or_after_selection(cost_model):
    base = _candles()
    poisoned = {m: [list(r) for r in c] for m, c in base.items()}
    cutoff = X.week_starts()[-1]                      # laatste selectiemoment
    for c in poisoned.values():
        for r in c:
            if r[0] >= cutoff:
                r[4] = r[4] * 1000                    # absurde toekomst
    a = S.run(base, cost_model_path=cost_model)
    b = S.run(poisoned, cost_model_path=cost_model)
    assert a["kandidaten"] == b["kandidaten"]


def test_ineligible_sensor_is_reported_not_proxied(cost_model):
    res = S.run(_candles(), cost_model_path=cost_model)
    size = next(r for r in res["kandidaten"] if r["sensor"] == "size_marketcap")
    assert size["status"] == "DATA INELIGIBLE"
    assert "proxy" in size["reden"] or "geimproviseerd" in size["reden"]


def test_assumptions_are_recorded(cost_model):
    a = S.run(_candles(), cost_model_path=cost_model)["screening_assumpties"]
    assert a["buffer_marge"] == 0.25
    assert a["order_notional_eur"] == 5_000
    assert a["kostenhorde_factor"] == 3.0
    assert a["richtingen"] == ["hoog", "laag"]
