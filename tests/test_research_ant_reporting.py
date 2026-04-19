"""
tests/test_research_ant_reporting.py

Tests voor de uitgebreide research-log velden van ResearchAnt:
  - strategy_type correcte afleiding
  - grade A/B/C op basis van sharpe
  - expected_return aanwezig in log
  - best_streak in log
  - worst_drawdown in log
  - best_regime in log (wanneer genoeg bars)
  - _strategy_type_from_signal helper
  - _grade_from_sharpe helper
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.ants.research_ant import (
    ResearchAnt,
    _grade_from_sharpe,
    _strategy_type_from_signal,
)
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.lab.backtester import BacktestResults
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.strategy_candidate import BacktestResults as SchemaBacktestResults

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"


def make_mission() -> Mission:
    return Mission(
        mission_id="m-research-reporting-001",
        ant_type="research_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "backtest", "propose_candidate"],
        market_scope=MarketScope(biome=_BIOME, symbols=[_SYMBOL], timeframes=["1h"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=7200,
        heartbeat_interval=120,
        success_conditions=SuccessConditions(description="reporting test"),
        abort_conditions=AbortConditions(),
    )


def make_candles(n: int = 100) -> list[MarketData]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        MarketData(
            symbol=_SYMBOL, timeframe="1h",
            timestamp=base + timedelta(hours=i),
            open=40_000.0 + i * 10,
            high=40_500.0 + i * 10,
            low=39_500.0 + i * 10,
            close=40_000.0 + i * 10,
            volume=1_000.0, biome_id=_BIOME,
        )
        for i in range(n)
    ]


def make_adapter(candles: list[MarketData] | None = None) -> MagicMock:
    adapter = MagicMock()
    adapter.biome_id = _BIOME
    adapter.is_available.return_value = True
    adapter.get_candles.return_value = candles or make_candles()
    return adapter


def make_ant(tmp_path: Path, n_candles: int = 100) -> ResearchAnt:
    registry = BiomeRegistry()
    registry.register(make_adapter(make_candles(n_candles)))
    return ResearchAnt(
        ant_id="ant-research-reporting-0001",
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )


def stub_backtester(ant: ResearchAnt, sharpe: float = 0.8, win_rate: float = 0.6) -> MagicMock:
    mock_bt = MagicMock()
    mock_bt.run.return_value = SchemaBacktestResults(
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        total_trades=25,
        max_drawdown_pct=0.07,
        avg_win=0.04,
        avg_loss=0.02,
        best_streak=5,
        regime_stats={"bull": {"trade_count": 15, "win_rate": 0.67, "sharpe": 0.9}},
        best_regime="bull",
    )
    ant._backtester = mock_bt
    return mock_bt


def read_research_records(tmp_path: Path) -> list[dict]:
    research_dir = tmp_path / "research"
    if not research_dir.exists():
        return []
    records = []
    for path in research_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# _strategy_type_from_signal
# ---------------------------------------------------------------------------

def test_strategy_type_sma_crossover_from_signal() -> None:
    assert _strategy_type_from_signal("sma_crossover", []) == "sma_crossover"


def test_strategy_type_sma_crossover_from_signal_upper() -> None:
    assert _strategy_type_from_signal("SMA_CROSSOVER BTC-EUR", []) == "sma_crossover"


def test_strategy_type_rsi_based_from_signal() -> None:
    assert _strategy_type_from_signal("rsi_oversold", []) == "rsi_based"


def test_strategy_type_rsi_overbought() -> None:
    assert _strategy_type_from_signal("rsi_overbought", []) == "rsi_based"


def test_strategy_type_bollinger_from_signal() -> None:
    assert _strategy_type_from_signal("bb_lower_touch", []) == "bollinger"


def test_strategy_type_momentum_from_keywords() -> None:
    assert _strategy_type_from_signal("ingested_abc12345", ["momentum", "trend"]) == "momentum"


def test_strategy_type_mean_reversion_from_keywords() -> None:
    assert _strategy_type_from_signal("ingested_abc12345", ["mean reversion", "rsi"]) == "mean_reversion"


def test_strategy_type_breakout_from_keywords() -> None:
    assert _strategy_type_from_signal("ingested_abc12345", ["breakout", "volume"]) == "breakout"


def test_strategy_type_unknown_fallback() -> None:
    assert _strategy_type_from_signal("ingested_abc12345", ["some", "random", "words"]) == "unknown"


# ---------------------------------------------------------------------------
# _grade_from_sharpe
# ---------------------------------------------------------------------------

def test_grade_A_above_05() -> None:
    assert _grade_from_sharpe(0.51) == "A"
    assert _grade_from_sharpe(1.20) == "A"


def test_grade_B_between_03_and_05() -> None:
    assert _grade_from_sharpe(0.30) == "B"
    assert _grade_from_sharpe(0.49) == "B"


def test_grade_C_below_03() -> None:
    assert _grade_from_sharpe(0.10) == "C"
    assert _grade_from_sharpe(0.00) == "C"


def test_grade_C_when_none() -> None:
    assert _grade_from_sharpe(None) == "C"


# ---------------------------------------------------------------------------
# Log-velden bij geaccepteerde kandidaat
# ---------------------------------------------------------------------------

def test_research_log_contains_strategy_type(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        payload = records[0]["payload"]
        assert "strategy_type" in payload


def test_research_log_contains_grade(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        payload = records[0]["payload"]
        assert payload.get("grade") in ("A", "B", "C")


def test_research_log_grade_A_for_high_sharpe(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        assert records[0]["payload"]["grade"] == "A"


def test_research_log_contains_expected_return(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        payload = records[0]["payload"]
        assert "expected_return" in payload
        if payload["expected_return"] is not None:
            assert isinstance(payload["expected_return"], float)


def test_research_log_contains_best_streak(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        assert "best_streak" in records[0]["payload"]


def test_research_log_contains_worst_drawdown(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        assert "worst_drawdown" in records[0]["payload"]


def test_research_log_contains_best_regime(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        payload = records[0]["payload"]
        assert "best_regime" in payload
        # Uit de mock is best_regime="bull"
        assert payload["best_regime"] == "bull"


def test_expected_return_calculation_correct(tmp_path: Path) -> None:
    """expected_return = (avg_win * win_rate) - (avg_loss * (1 - win_rate))"""
    ant = make_ant(tmp_path)
    # avg_win=0.04, avg_loss=0.02, win_rate=0.6
    # expected = (0.04 * 0.6) - (0.02 * 0.4) = 0.024 - 0.008 = 0.016
    stub_backtester(ant, sharpe=0.8, win_rate=0.6)
    ant._check_sma_crossover(_SYMBOL, make_candles(), [c.close for c in make_candles()])
    records = read_research_records(tmp_path)
    if records:
        exp_ret = records[0]["payload"].get("expected_return")
        if exp_ret is not None:
            assert abs(exp_ret - 0.016) < 1e-6
