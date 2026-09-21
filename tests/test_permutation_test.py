"""Validatie van het instrument zelf, op synthetische data.

Een toets die structuur mist die er aantoonbaar in zit, of structuur vindt
in pure ruis, is geen instrument. Deze tests bewaken beide kanten.
"""

from __future__ import annotations

import numpy as np

from ant_colony.lab.permutation_test import (
    MIN_TRADES_FOR_VERDICT,
    corrected_alpha,
    donchian_trades,
    permutation_test,
    run_with_subperiods,
    shuffle_series,
    synth_random_walk,
    synth_with_trend,
)

N_PERM = 200


def test_no_false_positives_on_pure_noise():
    hits = 0
    for seed in range(5):
        c, h, l = synth_random_walk(1500, seed)
        r = permutation_test(c, h, l, n_perm=N_PERM, seed=seed + 100)
        if r["p_values"]["shuffle"]["mean"] < 0.05:
            hits += 1
    assert hits <= 1, f"{hits}/5 vals-positief op ruis"


def test_detects_structure_with_enough_trades():
    c, h, l = synth_with_trend(4000, seed=7)
    r = permutation_test(c, h, l, n_perm=N_PERM, seed=999)
    assert r["n_trades"] >= MIN_TRADES_FOR_VERDICT
    assert r["p_values"]["shuffle"]["mean"] < 0.05
    assert r["p_values"]["random_entry"]["mean"] < 0.05


def test_underpowered_result_is_flagged():
    c, h, l = synth_with_trend(1200, seed=7)
    r = permutation_test(c, h, l, n_perm=20, seed=1)
    assert r["n_trades"] < MIN_TRADES_FOR_VERDICT
    assert r["power_ok"] is False


def test_same_seed_same_result():
    c, h, l = synth_random_walk(800, seed=3)
    a = permutation_test(c, h, l, n_perm=50, seed=11)
    b = permutation_test(c, h, l, n_perm=50, seed=11)
    assert a["p_values"] == b["p_values"]


def test_shuffle_keeps_return_distribution():
    c, h, l = synth_random_walk(500, seed=5)
    c2, h2, l2 = shuffle_series(c, h, l, np.random.default_rng(0))
    assert np.allclose(np.sort(np.diff(np.log(c))), np.sort(np.diff(np.log(c2))))
    assert np.all(h2 >= c2) and np.all(l2 <= c2)


def test_donchian_enters_after_signal_bar():
    close = np.array([10.0] * 25 + [20.0] + [21.0] * 5)
    high = close.copy()
    low = close.copy()
    trades = donchian_trades(close, high, low)
    assert trades, "uitbraak niet herkend"
    assert trades[0].entry_idx == 26, "instap moet op de bar NA het signaal"


def test_subperiods_get_own_null_and_correction():
    c, h, l = synth_random_walk(900, seed=2)
    out = run_with_subperiods(c, h, l, n_subperiods=3, n_perm=20, seed=4)
    assert [s["label"] for s in out["segments"]] == ["VOLLEDIG", "DEEL1", "DEEL2", "DEEL3"]
    assert out["corrected_alpha"] == corrected_alpha(4)
    assert out["corrected_alpha"] < 0.05 / 20
