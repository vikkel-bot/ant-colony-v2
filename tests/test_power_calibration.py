import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import power_calibration as pc  # noqa: E402
from ant_colony.lab.xs_rank_test import spreads  # noqa: E402


def synthetic(tmp_path: Path, weeks: int = 40, coins: int = 60, seed: int = 1) -> Path:
    rng = np.random.default_rng(seed)
    per_munt = {}
    for j in range(coins):
        name = f"C{j:03d}-EUR"
        per_munt[name] = {}
        for i in range(weeks):
            if rng.random() < 0.85:
                per_munt[name][str(1_000_000 + i * 604_800_000)] = float(rng.normal(0, 0.05))
    p = tmp_path / "res.json"
    p.write_text(json.dumps({"residuen": {"per_munt": per_munt}}), encoding="utf-8")
    return p


def test_load_residuals_shape_and_nan(tmp_path):
    weeks, coins, m = synthetic_load(tmp_path)
    assert m.shape == (len(weeks), len(coins)) == (40, 60)
    assert weeks == sorted(weeks) and coins == sorted(coins)
    assert np.isnan(m).any() and not np.isnan(m).all()


def synthetic_load(tmp_path, **kw):
    return pc.load_residuals(synthetic(tmp_path, **kw))


def test_base_panel_uses_instrument_k(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    panel = pc.base_panel(weeks, m)
    assert len(panel) == 40
    for w in panel:
        assert w["n"] == (~np.isnan(m[weeks.index(w["t"])])).sum()
        assert w["k"] == max(8, w["n"] // 5)
        assert not w["top"].any() and not w["no_trade"].any()


def test_inject_zero_delta_keeps_returns(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    base = pc.base_panel(weeks, m)
    inj = pc.inject(base, 0.0, np.random.default_rng(0))
    for b, w in zip(base, inj):
        assert np.array_equal(b["r"], w["r"])
        assert w["top"].sum() == w["k"]


def test_inject_adds_exactly_delta_to_spread(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    base = pc.base_panel(weeks, m)
    inj = pc.inject(base, 0.005, np.random.default_rng(3))
    same_top = [{**b, "top": w["top"]} for b, w in zip(base, inj)]
    assert np.allclose(spreads(inj), spreads(same_top) + 0.005)


def test_inject_preserves_week_mean(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    base = pc.base_panel(weeks, m)
    inj = pc.inject(base, 0.01, np.random.default_rng(5))
    for b, w in zip(base, inj):
        assert abs(b["r"].mean() - w["r"].mean()) < 1e-12


def test_detect_returns_three_p_values(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    inj = pc.inject(pc.base_panel(weeks, m), 0.0, np.random.default_rng(7))
    p = pc.detect(inj, 200, np.random.default_rng(8))
    assert p.shape == (3,) and (p > 0).all() and (p <= 1).all()


def test_power_one_for_large_effect_and_not_one_for_zero(tmp_path):
    weeks, _, m = synthetic_load(tmp_path)
    base = pc.base_panel(weeks, m)
    curve = pc.power_curve(base, (0.0, 0.05), reps=10, draws=200, seed=11)
    assert curve["0.0500"]["power"]["alpha_0.05"]["S1_gemiddelde"] == 1.0
    assert curve["0.0000"]["power"]["alpha_0.05"]["S1_gemiddelde"] < 1.0


def test_level_dispersion_detects_persistent_offsets(tmp_path):
    weeks, _, m = synthetic_load(tmp_path, weeks=60)
    flat = pc.level_dispersion(m, draws=100, seed=1, min_obs=10)
    shifted = m + np.linspace(-0.03, 0.03, m.shape[1])[None, :]
    bumped = pc.level_dispersion(shifted, draws=100, seed=1, min_obs=10)
    assert bumped["std_gemiddelde_per_munt"] > flat["std_gemiddelde_per_munt"]
    assert bumped["p"] < 0.05 < flat["p"]
