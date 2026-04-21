"""
tests/test_pipeline_ingestion_to_research.py

Tests voor de koppeling: IngestionAnt → ResearchAnt

Scenarios:
  1.  Ingested candidate met geldige symbool → backtest wordt uitgevoerd
  2.  Backtest slaagt (sharpe > 0.5, win_rate > 0.45) → kandidaat geschreven naar research log
  3.  Backtest faalt (te lage sharpe) → geen kandidaat geschreven
  4.  Geen ingestion directory → geen crash
  5.  Candidate-id al gezien (_seen_ingestion_ids) → niet opnieuw verwerkt
  6.  Payload zonder symbool → overgeslagen zonder crash
  7.  Meerdere ingested candidates → elk apart verwerkt
  8.  logs_root=None → geen crash
  9.  Ongeldige JSON-regel in log → overgeslagen zonder crash
  10. Andere action dan candidate_ingested → genegeerd
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.ants.research_ant import ResearchAnt
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.strategy_candidate import BacktestResults

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"


def make_mission(symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id="m-pipeline-research-001",
        ant_type="research_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "backtest", "propose_candidate"],
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
        ttl=7200,
        heartbeat_interval=120,
        success_conditions=SuccessConditions(description="pipeline test"),
        abort_conditions=AbortConditions(),
    )


def make_candles(n: int = 200) -> list[MarketData]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        MarketData(
            symbol=_SYMBOL,
            timeframe="1h",
            timestamp=base + timedelta(hours=i),
            open=40_000.0,
            high=40_500.0,
            low=39_500.0,
            close=40_000.0 + i * 10,
            volume=1_000.0,
            biome_id=_BIOME,
        )
        for i in range(n)
    ]


def make_adapter(candles: list[MarketData] | None = None) -> MagicMock:
    adapter = MagicMock()
    adapter.biome_id = _BIOME
    adapter.is_available.return_value = True
    adapter.get_candles.return_value = candles or make_candles()
    return adapter


def make_registry(candles: list[MarketData] | None = None) -> BiomeRegistry:
    registry = BiomeRegistry()
    registry.register(make_adapter(candles=candles))
    return registry


def make_ant(tmp_path: Path, registry: BiomeRegistry | None = None) -> ResearchAnt:
    return ResearchAnt(
        ant_id="ant-research-pipeline-0001",
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=registry or make_registry(),
        logs_root=tmp_path,
    )


def stub_backtester(ant: ResearchAnt, *, sharpe: float = 0.8, win_rate: float = 0.55) -> MagicMock:
    mock_bt = MagicMock()
    mock_bt.run.return_value = BacktestResults(
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        total_trades=25,
        max_drawdown_pct=0.05,
    )
    ant._backtester = mock_bt
    return mock_bt


def write_ingestion_event(
    ingestion_dir: Path,
    *,
    candidate_id: str | None = None,
    symbol: str = _SYMBOL,
    action: str = "candidate_ingested",
) -> str:
    candidate_id = candidate_id or f"cand-{uuid.uuid4().hex[:8]}"
    ingestion_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "source": "ingestion-ant-001",
        "payload": {
            "action": action,
            "candidate_id": candidate_id,
            "market_scope": {"symbol": symbol, "timeframe": "1h"},
            "entry_conditions": {"direction": "long"},
            "parameters": {},
            "logic_summary": f"Test ingested candidate {candidate_id[:8]}",
        },
    }
    log_file = ingestion_dir / "ingestion-ant-001.jsonl"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return candidate_id


def read_research_log(tmp_path: Path) -> list[dict]:
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
# Tests
# ---------------------------------------------------------------------------

def test_ingested_candidate_triggers_backtest(tmp_path: Path) -> None:
    """Ingested candidate met geldige symbool → backtest wordt uitgevoerd."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)
    write_ingestion_event(tmp_path / "ingestion")

    ant._process_ingestion_candidates()

    assert ant._backtester.run.called


def test_passing_backtest_writes_research_log(tmp_path: Path) -> None:
    """Backtest slaagt → kandidaat geschreven naar research log."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)
    write_ingestion_event(tmp_path / "ingestion")

    ant._process_ingestion_candidates()

    records = read_research_log(tmp_path)
    assert len(records) == 1
    assert records[0]["payload"]["action"] == "candidate_accepted"


def test_failing_backtest_writes_no_log(tmp_path: Path) -> None:
    """Backtest slaagt niet (te lage sharpe) → geen kandidaat geschreven."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.1, win_rate=0.2)
    write_ingestion_event(tmp_path / "ingestion")

    ant._process_ingestion_candidates()

    assert read_research_log(tmp_path) == []


def test_no_ingestion_dir_no_crash(tmp_path: Path) -> None:
    """Geen ingestion directory → geen crash."""
    ant = make_ant(tmp_path)
    ant._process_ingestion_candidates()  # should not raise


def test_duplicate_candidate_id_not_reprocessed(tmp_path: Path) -> None:
    """Dezelfde candidate_id wordt niet twee keer verwerkt."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)
    cid = write_ingestion_event(tmp_path / "ingestion")

    ant._process_ingestion_candidates()
    ant._process_ingestion_candidates()

    records = read_research_log(tmp_path)
    assert len(records) == 1
    assert cid in ant._seen_ingestion_ids


def test_payload_without_symbol_uses_mission_symbols(tmp_path: Path) -> None:
    """Payload zonder symbool → valt terug op missie-symbolen, backtest wordt uitgevoerd."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)
    # Schrijf event zonder market_scope.symbol
    write_ingestion_event(tmp_path / "ingestion", symbol="")

    ant._process_ingestion_candidates()

    # Missie heeft BTC-EUR → backtest moet uitgevoerd zijn
    assert ant._backtester.run.called


def test_multiple_candidates_each_processed(tmp_path: Path) -> None:
    """Meerdere ingested candidates → elk apart backtested."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)
    for _ in range(3):
        write_ingestion_event(tmp_path / "ingestion")

    ant._process_ingestion_candidates()

    assert ant._backtester.run.call_count == 3


def test_logs_root_none_no_crash() -> None:
    """logs_root=None → geen crash."""
    ant = ResearchAnt(
        ant_id="ant-research-none",
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_registry(),
        logs_root=None,
    )
    ant._process_ingestion_candidates()  # should not raise


def test_invalid_json_line_skipped(tmp_path: Path) -> None:
    """Ongeldige JSON-regel → overgeslagen, rest verwerkt."""
    ant = make_ant(tmp_path)
    stub_backtester(ant, sharpe=0.9, win_rate=0.6)

    ingestion_dir = tmp_path / "ingestion"
    ingestion_dir.mkdir(parents=True, exist_ok=True)
    log_file = ingestion_dir / "ingestion-ant-001.jsonl"
    # schrijf ongeldige regel gevolgd door geldige
    cid = f"cand-{uuid.uuid4().hex[:8]}"
    with log_file.open("w", encoding="utf-8") as fh:
        fh.write("niet geldig json\n")
        fh.write(json.dumps({
            "event_type": "action_executed",
            "source": "ingestion-ant-001",
            "payload": {
                "action": "candidate_ingested",
                "candidate_id": cid,
                "market_scope": {"symbol": _SYMBOL, "timeframe": "1h"},
                "entry_conditions": {"direction": "long"},
                "parameters": {},
            },
        }) + "\n")

    ant._process_ingestion_candidates()

    records = read_research_log(tmp_path)
    assert len(records) == 1


def test_other_action_ignored(tmp_path: Path) -> None:
    """Action != candidate_ingested wordt genegeerd."""
    ant = make_ant(tmp_path)
    stub_backtester(ant)
    write_ingestion_event(tmp_path / "ingestion", action="something_else")

    ant._process_ingestion_candidates()

    assert ant._backtester.run.call_count == 0
