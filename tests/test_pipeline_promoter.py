"""
tests/test_pipeline_promoter.py

Tests voor StrategyPromoter — Queen promotie gate.

Scenarios:
  1.  RESEARCH kandidaat met hoge metrics → APPROVED geschreven naar disk
  2.  RESEARCH kandidaat met lage sharpe → niet gepromoveerd
  3.  RESEARCH kandidaat met lage win_rate → niet gepromoveerd
  4.  RESEARCH kandidaat met te weinig trades → niet gepromoveerd
  5.  Geen research/strategy dirs → geen crash
  6.  Kandidaat al gezien (_seen_candidate_ids) → niet opnieuw verwerkt
  7.  Strategy-log (AuditEvent wrapper) → juist geparsed en gepromoveerd
  8.  Kandidaat met status != RESEARCH → overgeslagen
  9.  logs_root=None → geen crash
  10. Ongeldige JSON-regel → overgeslagen zonder crash
  11. Queen weigert RESEARCH→PAPER → geen APPROVED geschreven
  12. tick() omhult exceptions (fail-closed)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.colony.promoter import StrategyPromoter, _MIN_TRADES, _SHARPE_THRESHOLD, _WIN_RATE_THRESHOLD
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.lab.backtester import BacktestResults
from ant_colony.queen.queen import Queen, PromotionResult
from ant_colony.schemas.strategy_candidate import CandidateStatus, StrategyCandidate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"


def make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def make_queen(tmp_path: Path) -> Queen:
    return Queen(capital_total=10_000.0, scheduler=make_scheduler(tmp_path), logs_root=tmp_path)


def make_candidate(
    *,
    status: CandidateStatus = CandidateStatus.RESEARCH,
    sharpe: float = 0.8,
    win_rate: float = 0.6,
    total_trades: int = 25,
) -> StrategyCandidate:
    return StrategyCandidate(
        candidate_id=f"cand-{uuid.uuid4().hex[:8]}",
        name="Test kandidaat",
        source="test",
        biome=_BIOME,
        market_scope={"symbol": _SYMBOL, "timeframe": "1h"},
        logic_summary="Test logic",
        parameters={"direction": "long"},
        entry_conditions={"direction": "long"},
        exit_conditions={"take_profit_pct": 0.06, "stop_loss_pct": 0.03},
        backtest_results=BacktestResults(
            sharpe_ratio=sharpe,
            win_rate=win_rate,
            total_trades=total_trades,
            max_drawdown_pct=0.05,
        ),
        fitness_score=sharpe,
        status=status,
    )


def write_research_candidate(
    research_dir: Path,
    candidate: StrategyCandidate,
    filename: str = "research-ant-001.jsonl",
) -> None:
    research_dir.mkdir(parents=True, exist_ok=True)
    with (research_dir / filename).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(candidate.model_dump(mode="json"), default=str) + "\n")


def write_strategy_candidate(strategy_dir: Path, candidate: StrategyCandidate) -> None:
    """Sla op als AuditEvent (strategy ant formaat)."""
    strategy_dir.mkdir(parents=True, exist_ok=True)
    payload = {"action": "variant_emitted", **candidate.model_dump(mode="json")}
    record  = {
        "event_type": "action_executed",
        "source": "strategy-ant-001",
        "payload": payload,
    }
    with (strategy_dir / "strategy-ant-001.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_approved(tmp_path: Path) -> list[dict]:
    approved_dir = tmp_path / "approved"
    if not approved_dir.exists():
        return []
    records = []
    for path in approved_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_high_quality_candidate_becomes_approved(tmp_path: Path) -> None:
    """RESEARCH kandidaat met hoge metrics → APPROVED op disk."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    records = read_approved(tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "approved"
    assert records[0]["candidate_id"] == candidate.candidate_id


def test_low_sharpe_not_promoted(tmp_path: Path) -> None:
    """Kandidaat met sharpe < drempel → niet gepromoveerd."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=_SHARPE_THRESHOLD - 0.1, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    assert read_approved(tmp_path) == []


def test_low_win_rate_not_promoted(tmp_path: Path) -> None:
    """Kandidaat met win_rate < drempel → niet gepromoveerd."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=_WIN_RATE_THRESHOLD - 0.01, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    assert read_approved(tmp_path) == []


def test_too_few_trades_not_promoted(tmp_path: Path) -> None:
    """Kandidaat met total_trades < minimum → niet gepromoveerd."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=_MIN_TRADES - 1)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    assert read_approved(tmp_path) == []


def test_no_dirs_no_crash(tmp_path: Path) -> None:
    """Geen research/strategy dirs → geen crash."""
    queen    = make_queen(tmp_path)
    promoter = StrategyPromoter(queen=queen, logs_root=tmp_path)
    promoter.tick()  # should not raise


def test_duplicate_not_reprocessed(tmp_path: Path) -> None:
    """Zelfde candidate_id twee keer aangeboden → slechts één keer gepromoveerd."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()
    promoter.tick()

    assert len(read_approved(tmp_path)) == 1


def test_strategy_log_format_parsed_correctly(tmp_path: Path) -> None:
    """Kandidaat in strategy log (AuditEvent formaat) → juist geparsed en gepromoveerd."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)
    write_strategy_candidate(tmp_path / "strategy", candidate)

    promoter.tick()

    records = read_approved(tmp_path)
    assert len(records) == 1
    assert records[0]["candidate_id"] == candidate.candidate_id


def test_non_research_status_skipped(tmp_path: Path) -> None:
    """Kandidaat met status != RESEARCH → overgeslagen."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(status=CandidateStatus.PAPER, sharpe=0.9, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    assert read_approved(tmp_path) == []


def test_logs_root_none_no_crash() -> None:
    """logs_root=None → geen crash."""
    scheduler = MagicMock()
    queen     = MagicMock()
    promoter  = StrategyPromoter(queen=queen, logs_root=None)
    promoter.tick()  # should not raise


def test_invalid_json_skipped(tmp_path: Path) -> None:
    """Ongeldige JSON-regel → overgeslagen, rest verwerkt."""
    queen     = make_queen(tmp_path)
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)

    research_dir = tmp_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    log_file = research_dir / "research-ant-001.jsonl"
    with log_file.open("w", encoding="utf-8") as fh:
        fh.write("niet geldig json\n")
        fh.write(json.dumps(candidate.model_dump(mode="json"), default=str) + "\n")

    promoter.tick()

    assert len(read_approved(tmp_path)) == 1


def test_queen_refuses_paper_promotion_no_approved(tmp_path: Path) -> None:
    """Als Queen RESEARCH→PAPER weigert → geen APPROVED geschreven."""
    queen    = MagicMock()
    queen.promote_candidate.return_value = PromotionResult(
        accepted=False, rejection_reason="test weigering"
    )
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()

    assert read_approved(tmp_path) == []


def test_tick_is_fail_closed(tmp_path: Path) -> None:
    """tick() absorbeert alle exceptions (fail-closed)."""
    queen    = MagicMock()
    queen.promote_candidate.side_effect = RuntimeError("onverwachte fout")
    promoter  = StrategyPromoter(queen=queen, logs_root=tmp_path)
    candidate = make_candidate(sharpe=0.9, win_rate=0.65, total_trades=30)
    write_research_candidate(tmp_path / "research", candidate)

    promoter.tick()  # should not raise
