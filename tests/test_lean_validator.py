from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ant_colony.research.lean_validator import LeanValidator


class _FakeTrades:
    def count(self) -> int:
        return 12

    def win_rate(self) -> float:
        return 0.58


class _FakePortfolio:
    trades = _FakeTrades()

    def sharpe_ratio(self) -> float:
        return 1.23

    def max_drawdown(self) -> float:
        return -0.045


class _FakeVectorBT:
    class Portfolio:
        last_kwargs = None

        @classmethod
        def from_signals(cls, close, **kwargs):
            cls.last_kwargs = kwargs
            return _FakePortfolio()


def _sample_ohlcv() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=80, freq="D")
    close = pd.Series(range(100, 180), index=idx, dtype=float)
    return pd.DataFrame(
        {
            "Open": close - 1,
            "High": close + 2,
            "Low": close - 2,
            "Close": close,
            "Volume": 1000,
        },
        index=idx,
    )


def test_validate_returns_unavailable_when_vectorbt_missing(tmp_path: Path, monkeypatch) -> None:
    validator = LeanValidator(tmp_path)
    monkeypatch.setattr(
        validator,
        "_load_vectorbt",
        lambda: (None, "vectorbt niet beschikbaar: test"),
    )

    result = validator.validate({"candidate_id": "cand-vectorbt-missing"})

    assert result["lean_status"] == "unavailable"
    assert result["lean_sharpe"] is None
    assert (tmp_path / "lean" / "cand-vectorbt-missing.json").exists()


def test_validate_runs_vectorbt_and_writes_passed_result(tmp_path: Path, monkeypatch) -> None:
    validator = LeanValidator(tmp_path)
    monkeypatch.setattr(validator, "_load_vectorbt", lambda: (_FakeVectorBT, ""))
    monkeypatch.setattr(validator, "_fetch_ohlcv", lambda _candidate: _sample_ohlcv())

    result = validator.validate(
        {
            "candidate_id": "cand-vectorbt-ok",
            "biome": "crypto",
            "market_scope": {"symbol": "BTC-EUR"},
            "strategy_type": "sma_crossover",
            "parameters": {"short_period": 10, "long_period": 30},
        }
    )

    assert result["lean_status"] == "passed"
    assert result["lean_reason"] == "VectorBT backtest voltooid"
    assert result["lean_sharpe"] == 1.23
    assert result["lean_max_drawdown"] == 0.045
    assert result["lean_win_rate"] == 0.58
    assert result["lean_trades"] == 12
    stored = json.loads((tmp_path / "lean" / "cand-vectorbt-ok.json").read_text(encoding="utf-8"))
    assert stored["lean_status"] == "passed"


def test_failed_when_data_is_empty(tmp_path: Path, monkeypatch) -> None:
    validator = LeanValidator(tmp_path)
    monkeypatch.setattr(validator, "_load_vectorbt", lambda: (_FakeVectorBT, ""))
    monkeypatch.setattr(validator, "_fetch_ohlcv", lambda _candidate: pd.DataFrame())

    result = validator.validate({"candidate_id": "cand-no-data"})

    assert result["lean_status"] == "failed"
    assert "OHLCV" in result["lean_reason"]


def test_sma_strategy_builds_long_entries(tmp_path: Path) -> None:
    validator = LeanValidator(tmp_path)
    signals = validator._build_signals(
        {"strategy_type": "sma_crossover", "parameters": {"short_period": 3, "long_period": 5}},
        _sample_ohlcv()["Close"],
    )

    assert signals["entries"] is not None
    assert signals["exits"] is not None
    assert signals["short_entries"] is None
    assert bool(signals["entries"].any())


def test_rsi_short_strategy_uses_short_entries(tmp_path: Path) -> None:
    validator = LeanValidator(tmp_path)
    close = pd.Series([100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 119, 118, 117, 116])
    signals = validator._build_signals(
        {"strategy_type": "rsi_overbought", "direction": "short"},
        close,
    )

    assert signals["entries"] is None
    assert signals["short_entries"] is not None
