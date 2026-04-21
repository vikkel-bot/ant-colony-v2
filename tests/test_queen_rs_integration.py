"""
tests/test_queen_rs_integration.py

Tests voor RS-regime kapitaalverschuiving via rs_allocator.

Getest gedrag:
  - compute_allocation_factor() retourneert correcte fractie per regime
  - Onbekend regime → fail-open (1.0)
  - apply_rs_regime_allocation() past equities biome-kapitaal correct aan
  - apply_rs_regime_allocation() doet niets als geen signaal aanwezig is
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.queen.rs_allocator import (
    compute_allocation_factor,
    apply_rs_regime_allocation,
    _ALLOCATION_FACTORS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_regime_signal(logs_root: Path, regime: str) -> None:
    rs_dir = logs_root / "rs_regime"
    rs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-04-21T12:00:00+00:00",
        "payload": {
            "action": "regime_signal",
            "regime": regime,
            "qqq_vs_50d": 0.9 if "RISK_OFF" in regime or "CRISIS" in regime else 1.05,
            "avg_defensive_rs": 1.25 if regime == "CRISIS" else 0.9,
        },
    }
    (rs_dir / "ant-test.jsonl").write_text(json.dumps(record) + "\n")


def _make_queen():
    queen = MagicMock()
    return queen


# ---------------------------------------------------------------------------
# compute_allocation_factor() — pure functie
# ---------------------------------------------------------------------------

class TestComputeAllocationFactor:
    def test_risk_on_factor(self):
        assert compute_allocation_factor("RISK_ON") == 1.0

    def test_neutral_factor(self):
        assert compute_allocation_factor("NEUTRAL") == 0.8

    def test_risk_off_factor(self):
        assert compute_allocation_factor("RISK_OFF") == 0.5

    def test_crisis_factor(self):
        assert compute_allocation_factor("CRISIS") == 0.2

    def test_unknown_regime_fails_open(self):
        assert compute_allocation_factor("UNKNOWN") == 1.0

    def test_all_regimes_covered(self):
        for regime in ("RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"):
            factor = compute_allocation_factor(regime)
            assert 0 < factor <= 1.0

    def test_crisis_has_lowest_factor(self):
        assert compute_allocation_factor("CRISIS") < compute_allocation_factor("RISK_OFF")
        assert compute_allocation_factor("RISK_OFF") < compute_allocation_factor("NEUTRAL")
        assert compute_allocation_factor("NEUTRAL") < compute_allocation_factor("RISK_ON")


# ---------------------------------------------------------------------------
# apply_rs_regime_allocation() — Queen integratie
# ---------------------------------------------------------------------------

class TestApplyRsRegimeAllocation:
    def test_risk_on_uses_full_capital(self, tmp_path):
        queen = _make_queen()
        _write_regime_signal(tmp_path, "RISK_ON")
        base = 10_000.0
        result = apply_rs_regime_allocation(queen, tmp_path, base)
        assert result == "RISK_ON"
        queen.set_biome_capital.assert_called_once_with("equities", 10_000.0)

    def test_crisis_uses_20_pct_capital(self, tmp_path):
        queen = _make_queen()
        _write_regime_signal(tmp_path, "CRISIS")
        base = 10_000.0
        result = apply_rs_regime_allocation(queen, tmp_path, base)
        assert result == "CRISIS"
        queen.set_biome_capital.assert_called_once_with("equities", 2_000.0)

    def test_risk_off_uses_50_pct_capital(self, tmp_path):
        queen = _make_queen()
        _write_regime_signal(tmp_path, "RISK_OFF")
        base = 10_000.0
        result = apply_rs_regime_allocation(queen, tmp_path, base)
        assert result == "RISK_OFF"
        queen.set_biome_capital.assert_called_once_with("equities", 5_000.0)

    def test_neutral_uses_80_pct_capital(self, tmp_path):
        queen = _make_queen()
        _write_regime_signal(tmp_path, "NEUTRAL")
        base = 10_000.0
        result = apply_rs_regime_allocation(queen, tmp_path, base)
        assert result == "NEUTRAL"
        queen.set_biome_capital.assert_called_once_with("equities", 8_000.0)

    def test_no_signal_returns_none_and_no_change(self, tmp_path):
        queen = _make_queen()
        result = apply_rs_regime_allocation(queen, tmp_path, 10_000.0)
        assert result is None
        queen.set_biome_capital.assert_not_called()

    def test_no_signal_dir_returns_none_and_no_change(self, tmp_path):
        queen = _make_queen()
        result = apply_rs_regime_allocation(queen, tmp_path / "nonexistent", 10_000.0)
        assert result is None
        queen.set_biome_capital.assert_not_called()
