"""
tests/test_strategy_ant.py

Volledige coverage voor StrategyAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.strategy_ant import (
    StrategyAnt,
    _SourceCandidate,
    _SHARPE_THRESHOLD,
    _TRAIN_SPLIT,
    _WIN_RATE_THRESHOLD,
    _MUTATION_CONFIGS,
    _EXPANSION_CONFIGS,
    _DEFAULT_TP,
    _DEFAULT_SL,
    _DEFAULT_BARS,
)
from ant_colony.lab.backtester import BacktestResults, OHLCVBar
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.strategy_candidate import CandidateStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"


def make_mission(symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="strategy_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "propose_candidate"],
        market_scope=MarketScope(
            biome=_BIOME,
            symbols=symbols or [_SYMBOL],
            timeframes=["1h"],
        ),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=120,
        success_conditions=SuccessConditions(description="strategy test"),
    )


def make_ant(
    logs_root: Path | None = None,
    symbols: list[str] | None = None,
) -> StrategyAnt:
    return StrategyAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(symbols),
        scheduler=MagicMock(),
        biome_registry=MagicMock(),
        logs_root=logs_root,
    )


def make_source_candidate(
    candidate_id: str | None = None,
    symbol: str = _SYMBOL,
    direction: str = "long",
    tp_pct: float = 0.06,
    sl_pct: float = 0.02,
    max_bars_held: int = 10,
    source_type: str = "research",
) -> _SourceCandidate:
    return _SourceCandidate(
        candidate_id     = candidate_id or str(uuid.uuid4()),
        name             = "TEST SMA_CROSSOVER BTC-EUR",
        symbol           = symbol,
        direction        = direction,
        tp_pct           = tp_pct,
        sl_pct           = sl_pct,
        max_bars_held    = max_bars_held,
        entry_conditions = {"sma20_crosses_sma50": direction},
        exit_conditions  = {
            "take_profit_pct": tp_pct,
            "stop_loss_pct":   sl_pct,
            "max_bars_held":   max_bars_held,
        },
        logic_summary    = "SMA20 kruist SMA50",
        source_type      = source_type,
        fitness_score    = 0.8,
    )


def make_ohlcv_bars(n: int = 100, base: float = 100.0) -> list[OHLCVBar]:
    """Stijgende prijsreeks met lichte variatie — geeft positieve sharpe op LONG."""
    bars = []
    price = base
    for i in range(n):
        # Algemeen stijgend + alternerend patroon voor win-rate variatie
        delta = 0.5 + (1.5 if i % 5 == 0 else -0.2)
        price = max(1.0, price + delta)
        bars.append(OHLCVBar(
            timestamp=datetime(2026, 1, 1, i // 24, i % 24, tzinfo=timezone.utc),
            open=price - 0.1,
            high=price + 0.3,
            low=price - 0.3,
            close=price,
            volume=1000.0,
        ))
    return bars


def stub_backtester(
    ant: StrategyAnt,
    sharpe: float = 0.8,
    win_rate: float = 0.6,
    trades: int = 8,
) -> None:
    """Vervang ant._backtester met een mock die goede resultaten retourneert."""
    mock_bt = MagicMock()
    mock_bt.run.return_value = BacktestResults(
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        total_trades=trades,
        max_drawdown_pct=0.05,
    )
    ant._backtester = mock_bt


def make_adapter_with_candles(bars: list[OHLCVBar] | None = None) -> MagicMock:
    if bars is None:
        bars = make_ohlcv_bars(100)

    # Convert OHLCVBar → mock MarketData objects
    md_list = []
    for b in bars:
        md = MagicMock()
        md.close     = b.close
        md.open      = b.open
        md.high      = b.high
        md.low       = b.low
        md.volume    = b.volume
        md.timestamp = b.timestamp
        md_list.append(md)

    adapter = MagicMock()
    adapter.is_available.return_value = True
    adapter.get_candles.return_value  = md_list
    return adapter


def write_research_candidate(
    research_dir: Path,
    *,
    candidate_id: str | None = None,
    symbol: str = _SYMBOL,
    direction: str = "long",
    tp_pct: float = 0.06,
    sl_pct: float = 0.03,
    max_bars_held: int = 10,
    filename: str = "research_1.jsonl",
) -> str:
    cid = candidate_id or str(uuid.uuid4())
    research_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "candidate_id": cid,
        "name": f"SMA_CROSSOVER {symbol}",
        "source": "internal",
        "biome": _BIOME,
        "market_scope": {"symbol": symbol, "timeframe": "1h"},
        "logic_summary": f"SMA crossover op {symbol}",
        "parameters": {"sma_fast": 20, "sma_slow": 50},
        "entry_conditions": {"sma20_crosses_sma50": direction},
        "exit_conditions": {
            "take_profit_pct": tp_pct,
            "stop_loss_pct":   sl_pct,
            "max_bars_held":   max_bars_held,
        },
        "fitness_score": 0.75,
        "status": "research",
    }
    (research_dir / filename).write_text(json.dumps(record) + "\n", encoding="utf-8")
    return cid


def write_ingestion_candidate(
    ingestion_dir: Path,
    *,
    candidate_id: str | None = None,
    filename: str = "ingestion_1.jsonl",
) -> str:
    cid = candidate_id or str(uuid.uuid4())
    ingestion_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "source": "ingestion-ant",
        "sequence": 0,
        "payload": {
            "action":          "candidate_ingested",
            "candidate_id":    cid,
            "name":            "user/trading-bot",
            "source_url":      "https://github.com/user/trading-bot",
            "status":          "ingested",
            "entry_keywords":  ["rsi", "sma", "crossover"],
            "exit_keywords":   ["stop_loss", "take_profit"],
            "logic_summary":   "Momentum strategy with RSI filter",
        },
    }
    (ingestion_dir / filename).write_text(json.dumps(record) + "\n", encoding="utf-8")
    return cid


# ---------------------------------------------------------------------------
# 1. Broncandidaten lezen — research
# ---------------------------------------------------------------------------


class TestReadResearchCandidates:
    def test_research_candidate_parsed(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert len(sources) == 1
        assert sources[0].source_type == "research"
        assert sources[0].symbol == _SYMBOL

    def test_research_direction_extracted_from_entry_conditions(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research", direction="short")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources[0].direction == "short"

    def test_research_tp_sl_extracted(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research", tp_pct=0.08, sl_pct=0.03)
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert abs(sources[0].tp_pct - 0.08) < 1e-6
        assert abs(sources[0].sl_pct - 0.03) < 1e-6

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "research").mkdir()
        (tmp_path / "research" / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources == []

    def test_missing_candidate_id_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "research").mkdir()
        record = {"name": "X", "status": "research",
                  "market_scope": {"symbol": _SYMBOL},
                  "exit_conditions": {}, "entry_conditions": {}, "logic_summary": "x"}
        (tmp_path / "research" / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources == []

    def test_no_research_dir_returns_empty(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources == []

    def test_logs_root_none_returns_empty(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._read_source_candidates() == []

    def test_multiple_research_files(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research", filename="r1.jsonl")
        write_research_candidate(tmp_path / "research", symbol="ETH-EUR", filename="r2.jsonl")
        ant = make_ant(logs_root=tmp_path, symbols=[_SYMBOL, "ETH-EUR"])
        sources = ant._read_source_candidates()
        assert len(sources) == 2


# ---------------------------------------------------------------------------
# 2. Broncandidaten lezen — ingestion
# ---------------------------------------------------------------------------


class TestReadIngestionCandidates:
    def test_ingestion_candidate_parsed(self, tmp_path: Path) -> None:
        write_ingestion_candidate(tmp_path / "ingestion")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert len(sources) == 1
        assert sources[0].source_type == "ingested"

    def test_ingestion_direction_defaults_to_long(self, tmp_path: Path) -> None:
        write_ingestion_candidate(tmp_path / "ingestion")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources[0].direction == "long"

    def test_ingestion_defaults_applied(self, tmp_path: Path) -> None:
        write_ingestion_candidate(tmp_path / "ingestion")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        assert sources[0].tp_pct == _DEFAULT_TP
        assert sources[0].sl_pct == _DEFAULT_SL
        assert sources[0].max_bars_held == _DEFAULT_BARS

    def test_ingestion_no_entry_keywords_skipped(self, tmp_path: Path) -> None:
        ingestion_dir = tmp_path / "ingestion"
        ingestion_dir.mkdir()
        record = {"payload": {"action": "candidate_ingested", "candidate_id": "x",
                               "entry_keywords": [], "exit_keywords": ["stop_loss"]}}
        (ingestion_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        assert ant._read_source_candidates() == []

    def test_combined_research_and_ingestion(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research")
        write_ingestion_candidate(tmp_path / "ingestion")
        ant = make_ant(logs_root=tmp_path)
        sources = ant._read_source_candidates()
        types = {s.source_type for s in sources}
        assert types == {"research", "ingested"}


# ---------------------------------------------------------------------------
# 3. Mutatie-generatie
# ---------------------------------------------------------------------------


class TestMutationGeneration:
    def test_generates_up_to_3_mutations(self) -> None:
        ant = make_ant()
        c = make_source_candidate(tp_pct=0.05, sl_pct=0.015, max_bars_held=7)
        variants = ant._generate_mutations(c)
        assert 1 <= len(variants) <= 3

    def test_mutation_type_correct(self) -> None:
        ant = make_ant()
        c = make_source_candidate(tp_pct=0.05, sl_pct=0.015, max_bars_held=7)
        for v in ant._generate_mutations(c):
            assert v["mutation_type"] == "mutation"

    def test_parent_id_in_mutation(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        for v in ant._generate_mutations(c):
            assert c.candidate_id in v["parent_ids"]

    def test_identical_params_skipped(self) -> None:
        ant = make_ant()
        # Parent has exact same params as one of _MUTATION_CONFIGS
        cfg = _MUTATION_CONFIGS[1]  # standard
        c = make_source_candidate(
            tp_pct=cfg["tp_pct"],
            sl_pct=cfg["sl_pct"],
            max_bars_held=cfg["max_bars_held"],
        )
        variants = ant._generate_mutations(c)
        # The "standard" variant should be skipped
        for v in variants:
            assert not (
                abs(v["tp_pct"] - cfg["tp_pct"]) < 1e-6 and
                abs(v["sl_pct"] - cfg["sl_pct"]) < 1e-6 and
                v["max_bars_held"] == cfg["max_bars_held"]
            )

    def test_mutation_entry_conditions_preserved(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        variants = ant._generate_mutations(c)
        for v in variants:
            assert v["entry_conditions"] == c.entry_conditions


# ---------------------------------------------------------------------------
# 4. Combinatie-generatie
# ---------------------------------------------------------------------------


class TestCombinationGeneration:
    def test_combination_type(self) -> None:
        ant = make_ant()
        a = make_source_candidate(tp_pct=0.06, sl_pct=0.02)
        b = make_source_candidate(tp_pct=0.08, sl_pct=0.03)
        spec = ant._generate_combination(a, b)
        assert spec["mutation_type"] == "combination"

    def test_both_parent_ids_present(self) -> None:
        ant = make_ant()
        a = make_source_candidate()
        b = make_source_candidate()
        spec = ant._generate_combination(a, b)
        assert a.candidate_id in spec["parent_ids"]
        assert b.candidate_id in spec["parent_ids"]

    def test_averaged_tp_sl(self) -> None:
        ant = make_ant()
        a = make_source_candidate(tp_pct=0.04, sl_pct=0.01)
        b = make_source_candidate(tp_pct=0.08, sl_pct=0.03)
        spec = ant._generate_combination(a, b)
        assert abs(spec["tp_pct"] - 0.06) < 1e-4
        assert abs(spec["sl_pct"] - 0.02) < 1e-4

    def test_max_bars_held_is_max(self) -> None:
        ant = make_ant()
        a = make_source_candidate(max_bars_held=5)
        b = make_source_candidate(max_bars_held=15)
        spec = ant._generate_combination(a, b)
        assert spec["max_bars_held"] == 15

    def test_entry_conditions_merged(self) -> None:
        ant = make_ant()
        a = make_source_candidate()
        a.entry_conditions = {"signal_a": "long"}
        b = make_source_candidate()
        b.entry_conditions = {"signal_b": "long"}
        spec = ant._generate_combination(a, b)
        assert "signal_a" in spec["entry_conditions"]
        assert "signal_b" in spec["entry_conditions"]


# ---------------------------------------------------------------------------
# 5. Feature-expansie generatie
# ---------------------------------------------------------------------------


class TestFeatureExpansion:
    def test_generates_two_expansions(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        variants = ant._generate_expansions(c)
        assert len(variants) == len(_EXPANSION_CONFIGS)

    def test_expansion_type(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        for v in ant._generate_expansions(c):
            assert v["mutation_type"] == "feature_expansion"

    def test_parent_id_in_expansion(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        for v in ant._generate_expansions(c):
            assert c.candidate_id in v["parent_ids"]

    def test_feature_in_entry_conditions(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        variants = ant._generate_expansions(c)
        features = {v["entry_conditions"].get("extra_filter") for v in variants}
        assert "rsi_filter" in features
        assert "bb_filter" in features

    def test_sl_never_negative(self) -> None:
        ant = make_ant()
        c = make_source_candidate(sl_pct=0.005)  # very tight SL
        for v in ant._generate_expansions(c):
            assert v["sl_pct"] > 0


# ---------------------------------------------------------------------------
# 6. Walk-forward backtest + emissie
# ---------------------------------------------------------------------------


class TestWalkForwardBacktest:
    def test_good_result_emits_candidate(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.65)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result is True
        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_low_sharpe_not_emitted(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.3, win_rate=0.65)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result is False

    def test_low_win_rate_not_emitted(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.3)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result is False

    def test_uses_test_split(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        adapter = make_adapter_with_candles(make_ohlcv_bars(100))
        ant.biome_registry.get.return_value = adapter

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        # Check that Backtester.run was called with the test-set bars (30% of 100 = 30 bars)
        call_args = ant._backtester.run.call_args
        bars_used = call_args[0][0]
        expected_test_size = 100 - int(100 * _TRAIN_SPLIT)
        assert len(bars_used) == expected_test_size

    def test_too_few_candles_not_emitted(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        # Only 10 candles — below _MIN_CANDLES
        adapter = make_adapter_with_candles(make_ohlcv_bars(10))
        ant.biome_registry.get.return_value = adapter

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result is False

    def test_symbol_not_in_scope_not_emitted(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path, symbols=["ETH-EUR"])
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)

        c = make_source_candidate(symbol="BTC-EUR")
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit("BTC-EUR", spec)  # BTC not in scope

        assert result is False

    def test_adapter_none_not_emitted(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = None

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        result = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result is False


# ---------------------------------------------------------------------------
# 7. Deduplicatie
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_variant_not_emitted_twice(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]

        result1 = ant._try_backtest_and_emit(_SYMBOL, spec)
        result2 = ant._try_backtest_and_emit(_SYMBOL, spec)

        assert result1 is True
        assert result2 is False

    def test_variant_key_deterministic(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]

        key1 = ant._variant_key(spec)
        key2 = ant._variant_key(spec)
        assert key1 == key2

    def test_different_params_different_key(self) -> None:
        ant = make_ant()
        c = make_source_candidate(tp_pct=0.05, sl_pct=0.015, max_bars_held=7)
        variants = ant._generate_mutations(c)
        if len(variants) >= 2:
            keys = {ant._variant_key(v) for v in variants}
            assert len(keys) == len(variants)

    def test_seen_variants_grows(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        assert len(ant._seen_variants) == 1


# ---------------------------------------------------------------------------
# 8. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_log_file_created_on_emit(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_required_fields(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        record = json.loads(log_path.read_text().strip())
        payload = record["payload"]
        for field in ("candidate_id", "name", "mutation_type", "parent_ids",
                      "symbol", "sharpe_ratio", "win_rate", "total_trades", "status"):
            assert field in payload, f"Veld ontbreekt: {field}"

    def test_log_mutation_type_correct(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        record = json.loads(log_path.read_text().strip())
        assert record["payload"]["mutation_type"] == "mutation"

    def test_log_parent_ids_present(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        record = json.loads(log_path.read_text().strip())
        assert c.candidate_id in record["payload"]["parent_ids"]

    def test_log_status_is_research(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        record = json.loads(log_path.read_text().strip())
        assert record["payload"]["status"] == "research"

    def test_sequence_increments(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c1 = make_source_candidate()
        c2 = make_source_candidate()
        spec1 = ant._generate_mutations(c1)[0]
        spec2 = ant._generate_mutations(c2)[0] if len(ant._generate_mutations(c2)) > 0 else \
                ant._generate_expansions(c2)[0]

        ant._try_backtest_and_emit(_SYMBOL, spec1)
        ant._try_backtest_and_emit(_SYMBOL, spec2)

        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["sequence"] == 0
        assert records[1]["sequence"] == 1

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        ant._try_backtest_and_emit(_SYMBOL, spec)  # should not crash


# ---------------------------------------------------------------------------
# 9. Tick — integratie
# ---------------------------------------------------------------------------


class TestTick:
    def test_tick_with_sources_attempts_variants(self, tmp_path: Path) -> None:
        write_research_candidate(tmp_path / "research")
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        ant._tick()
        assert ant._last_action.startswith("tick_emitted:")

    def test_tick_no_sources_sets_no_sources_action(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._last_action == "tick_no_sources"

    def test_tick_combinations_from_two_same_symbol_candidates(self, tmp_path: Path) -> None:
        cid1 = write_research_candidate(tmp_path / "research", filename="r1.jsonl")
        cid2 = write_research_candidate(tmp_path / "research", filename="r2.jsonl")
        ant = make_ant(logs_root=tmp_path)
        stub_backtester(ant, sharpe=0.9, win_rate=0.6)
        ant.biome_registry.get.return_value = make_adapter_with_candles()

        ant._tick()
        # Should have tried combination variants as well as mutations/expansions
        log_path = tmp_path / "strategy" / f"{ant.ant_id}.jsonl"
        if log_path.exists():
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            variant_records = [r for r in records if r["payload"].get("action") == "variant_emitted"]
            mutation_types = {r["payload"]["mutation_type"] for r in variant_records}
            # At minimum mutations and/or combinations should appear
            assert len(mutation_types) > 0


# ---------------------------------------------------------------------------
# 10. Kandidaat bouwen
# ---------------------------------------------------------------------------


class TestBuildCandidate:
    def _make_results(self) -> BacktestResults:
        return BacktestResults(
            sharpe_ratio=0.75,
            win_rate=0.60,
            total_trades=12,
            max_drawdown_pct=0.08,
        )

    def test_candidate_status_is_research(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        candidate = ant._build_candidate(_SYMBOL, spec, self._make_results())
        assert candidate.status == CandidateStatus.RESEARCH

    def test_candidate_provenance_has_mutation_type(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        candidate = ant._build_candidate(_SYMBOL, spec, self._make_results())
        assert candidate.provenance[0].details["mutation_type"] == "mutation"

    def test_candidate_provenance_has_parent_ids(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        candidate = ant._build_candidate(_SYMBOL, spec, self._make_results())
        assert c.candidate_id in candidate.provenance[0].details["parent_ids"]

    def test_candidate_backtest_results_filled(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        results = self._make_results()
        candidate = ant._build_candidate(_SYMBOL, spec, results)
        assert candidate.backtest_results is not None
        assert candidate.backtest_results.sharpe_ratio == results.sharpe_ratio

    def test_candidate_has_exit_conditions(self) -> None:
        ant = make_ant()
        c = make_source_candidate()
        spec = ant._generate_mutations(c)[0]
        candidate = ant._build_candidate(_SYMBOL, spec, self._make_results())
        assert candidate.exit_conditions  # not empty

    def test_combination_candidate_has_two_parent_ids(self) -> None:
        ant = make_ant()
        a = make_source_candidate()
        b = make_source_candidate()
        spec = ant._generate_combination(a, b)
        candidate = ant._build_candidate(_SYMBOL, spec, self._make_results())
        parents = candidate.provenance[0].details["parent_ids"]
        assert len(parents) == 2


# ---------------------------------------------------------------------------
# 11. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_contains_ant_id(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        hb = ant.scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()


# ---------------------------------------------------------------------------
# 12. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock()

        with patch("ant_colony.ants.strategy_ant.time.sleep"):
            with patch("ant_colony.ants.strategy_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()

        with patch("ant_colony.ants.strategy_ant.time.sleep", side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.strategy_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_exception_in_tick_returns_aborted(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(side_effect=RuntimeError("tick boom"))

        with patch("ant_colony.ants.strategy_ant.time.sleep", side_effect=Exception("stop")):
            with patch("ant_colony.ants.strategy_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_final_heartbeat_on_exit(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock()

        with patch("ant_colony.ants.strategy_ant.time.sleep"):
            with patch("ant_colony.ants.strategy_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                ant.run()

        assert ant.scheduler.record_heartbeat.call_count >= 1
