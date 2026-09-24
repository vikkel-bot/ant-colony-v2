"""Tests voor het kostenmodel en de artefactbouw.

Nadruk op fail-closed gedrag: buiten het gemeten bereik, onbekende estimator,
ontbrekende velden en het overschrijven van een bestaande versie moeten allemaal
een fout geven in plaats van stil een getal.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_cost_model as B  # noqa: E402
from ant_colony.lab import cost_model as C  # noqa: E402

SIZES = B.SIZES_EUR


def _rows(n_snapshots: int = 8, n_markets: int = 12) -> list[dict]:
    rows = []
    for s in range(n_snapshots):
        for m in range(n_markets):
            turnover = 50_000 * (m + 1)
            base = 0.004 / (m + 1) ** 0.5          # duurder bij lage omzet
            jitter = 1.0 + 0.1 * s                  # latere snapshots duurder
            r = {"taken_at": f"snap{s}", "market": f"M{m:02d}-EUR", "turnover_30d": turnover,
                 "spread": base}
            for i, size in enumerate(SIZES):
                c = base * (1 + i) * jitter
                r[f"buy_{size}"] = c
                r[f"sell_{size}"] = c * 0.99
            rows.append(r)
    return rows


@pytest.fixture()
def model(tmp_path) -> dict:
    art = tmp_path / "COST_MODEL_TEST.json"
    art.write_text(json.dumps(B.build(_rows(), "COST_MODEL_TEST")), encoding="utf-8")
    return C.load_model(art)


# ---------------------------------------------------------------- artefact

def test_percentile_order_is_monotonic(model):
    for b in model["turnover_buckets"]:
        for size in map(str, SIZES):
            c = b["costs"]
            assert c["median"][size] <= c["p75"][size] <= c["p90"][size] <= c["max"][size]


def test_cost_rises_with_order_size(model):
    for b in model["turnover_buckets"]:
        vals = [b["costs"]["p75"][str(s)] for s in SIZES]
        assert vals == sorted(vals)


def test_low_turnover_bucket_is_most_expensive(model):
    first = model["turnover_buckets"][0]["costs"]["p75"]["5000"]
    last = model["turnover_buckets"][-1]["costs"]["p75"]["5000"]
    assert first > last


def test_gate_estimator_is_p75_and_fee_is_taker(model):
    assert model["estimator_for_gate"] == "p75"
    assert model["fee_per_side"] == 0.0025
    assert model["reference_notional_eur"] == 5_000


def test_limitations_are_recorded(model):
    text = " ".join(model["limitations"]).lower()
    assert "adverse selection" in text and "voorlopige policy choice" in text
    assert "geen sizing-regel" in text


# ---------------------------------------------------------------- rekenregels

def test_round_trip_is_twice_cost_per_side(model):
    rt = C.round_trip(model, 100_000, 5_000)
    side = C.cost_per_side(model, 100_000, 5_000)
    assert abs(rt - 2 * side) < 1e-12
    assert side > model["fee_per_side"]  # tarief plus impact


def test_interpolation_between_measured_sizes(model):
    lo = C.impact_per_side(model, 100_000, 1_000)
    mid = C.impact_per_side(model, 100_000, 3_000)
    hi = C.impact_per_side(model, 100_000, 5_000)
    assert lo < mid < hi


def test_outside_measured_range_raises(model):
    with pytest.raises(ValueError):
        C.impact_per_side(model, 100_000, 500)
    with pytest.raises(ValueError):
        C.impact_per_side(model, 100_000, 50_000)


def test_unknown_estimator_raises(model):
    with pytest.raises(ValueError):
        C.impact_per_side(model, 100_000, 5_000, estimator="optimistisch")


def test_turnover_below_lowest_bucket_gets_most_expensive(model):
    cheap = C.impact_per_side(model, 10_000_000, 5_000)
    thin = C.impact_per_side(model, 1.0, 5_000)
    assert thin > cheap


def test_weekly_cost_scales_with_turnover_fraction(model):
    half = C.weekly_cost(model, 100_000, 5_000, 0.5)
    full = C.weekly_cost(model, 100_000, 5_000, 1.0)
    assert abs(full - 2 * half) < 1e-12
    with pytest.raises(ValueError):
        C.weekly_cost(model, 100_000, 5_000, 1.5)


def test_missing_field_fails_closed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": "x", "fee_per_side": 0.0025}), encoding="utf-8")
    with pytest.raises(ValueError):
        C.load_model(bad)


def test_bad_gate_estimator_in_artifact_fails_closed(tmp_path):
    art = json.loads(json.dumps(B.build(_rows(), "T")))
    art["estimator_for_gate"] = "wensdenken"
    p = tmp_path / "a.json"
    p.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ValueError):
        C.load_model(p)


# ---------------------------------------------------------------- bouwscript

def test_builder_refuses_to_overwrite_existing_version(tmp_path):
    snaps = tmp_path / "s.jsonl"
    snaps.write_text("\n".join(json.dumps(r) for r in _rows()), encoding="utf-8")
    out = tmp_path / "v.json"
    out.write_text("{}", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_cost_model.py"
    r = subprocess.run([sys.executable, str(script), "--snapshots", str(snaps),
                        "--version", "V", "--out", str(out)], capture_output=True, text=True)
    assert r.returncode != 0 and "nooit overschreven" in (r.stdout + r.stderr)


def test_market_with_thin_book_in_one_snapshot_is_dropped():
    rows = _rows(n_snapshots=4, n_markets=6)
    for r in rows:
        if r["market"] == "M00-EUR" and r["taken_at"] == "snap2":
            r["buy_25000"] = None
    markets = B.per_market(rows)
    assert "M00-EUR" not in markets and "M01-EUR" in markets


def test_percentile_interpolates():
    assert B.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert B.percentile([1.0, 2.0, 3.0, 4.0], 0.75) == 3.25
    assert B.percentile([5.0], 0.9) == 5.0
