"""
tests/test_queen_advisor.py

Tests voor QueenAdvisor en Queen.apply_advisor_decision.

Scenarios:
  1.  Geen data → lege QueenDecision
  2.  Research kandidaten correct gelezen
  3.  Paper stats correct berekend (win_rate, total_trades, total_pnl)
  4.  win_rate > 0.60 over 20+ trades → kapitaal_verhogen + adviezen_gevolgd
  5.  win_rate < 0.30 over 10+ trades → kapitaal_verlagen + deprioriteer + adviezen_gevolgd
  6.  Niet genoeg trades → neutral (geen actie)
  7.  Claude advies aanwezig + paper bevestigt → prioriteit_kandidaten
  8.  Claude advies aanwezig maar paper tegenspreekt → adviezen_genegeerd
  9.  Claude advies verlopen → genegeerd (niet in actieve adviezen)
  10. Claude advies zonder paper-data → genegeerd, wacht op bewijs
  11. Claude confidence < 6 → genegeerd
  12. apply_advisor_decision lege beslissing → geen crash, geen log
  13. apply_advisor_decision met allocatie_aanpassingen → apply_allocation_plan wordt aangeroepen
  14. apply_advisor_decision logt naar ANT_LOGS/queen/decisions.jsonl
  15. logs_root=None → geen crash
  16. is_empty() op lege en gevulde beslissing
  17. Meerdere symbolen met gemengde prestaties
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.queen.queen_advisor import (
    QueenDecision, QueenAdvisor, _WIN_HIGH, _WIN_LOW,
    select_diverse_top_n,
)
from ant_colony.schemas.mission import (
    AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_queen(tmp_path: Path) -> MagicMock:
    queen = MagicMock()
    queen.active_missions = {}
    queen.allocation_snapshot.return_value = MagicMock(biomes=[])
    return queen


def make_advisor(tmp_path: Path, ttl_minutes: int = 60) -> QueenAdvisor:
    queen = make_queen(tmp_path)
    return QueenAdvisor(queen=queen, logs_root=tmp_path, advice_ttl_minutes=ttl_minutes)


def write_research_record(tmp_path: Path, candidate_id: str, symbol: str = "BTC-EUR",
                           sharpe: float = 0.8, grade: str = "A") -> None:
    research_dir = tmp_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "source": "ant-research-001",
        "payload": {
            "action":        "candidate_accepted",
            "candidate_id":  candidate_id,
            "symbol":        symbol,
            "sharpe":        sharpe,
            "win_rate":      0.6,
            "strategy_type": "sma_crossover",
            "grade":         grade,
        },
    }
    path = research_dir / "ant-research-001.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def write_trade(
    tmp_path: Path,
    symbol: str,
    pnl: float,
    mission_id: str = "paper-mission-001",
    closed_at: str | None = None,
) -> None:
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    trade: dict = {
        "position_id":  str(uuid.uuid4()),
        "symbol":       symbol,
        "side":         "long",
        "status":       "closed",
        "realized_pnl": pnl,
        "exit_reason":  "take_profit",
    }
    if closed_at is not None:
        trade["closed_at"] = closed_at
    path = paper_dir / f"{mission_id}_trades.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade) + "\n")


def write_claude_advice(tmp_path: Path, candidate_id: str, symbol: str = "BTC-EUR",
                         action: str = "promote", confidence: int = 8,
                         age_minutes: int = 0) -> str:
    advice_dir = tmp_path / "claude" / "advice"
    advice_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=age_minutes)
    advice_id = str(uuid.uuid4())
    advice = {
        "advice_id":    advice_id,
        "candidate_id": candidate_id,
        "symbol":       symbol,
        "timestamp":    ts.isoformat(),
        "confidence":   confidence,
        "rationale":    "Test rationale",
        "action":       action,
    }
    path = advice_dir / f"{advice_id}.json"
    path.write_text(json.dumps(advice), encoding="utf-8")
    return advice_id


def read_decisions(tmp_path: Path) -> list[dict]:
    path = tmp_path / "queen" / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# QueenDecision tests
# ---------------------------------------------------------------------------

def test_decision_is_empty_default() -> None:
    d = QueenDecision()
    assert d.is_empty()


def test_decision_not_empty_with_data() -> None:
    d = QueenDecision(prioriteit_kandidaten=["cid-1"])
    assert not d.is_empty()


def test_decision_to_dict_contains_all_keys() -> None:
    d = QueenDecision()
    keys = d.to_dict().keys()
    for k in ("allocatie_aanpassingen", "prioriteit_kandidaten", "deprioriteer_kandidaten",
              "kapitaal_verhogen", "kapitaal_verlagen", "adviezen_gevolgd", "adviezen_genegeerd"):
        assert k in keys


# ---------------------------------------------------------------------------
# _read_research_candidates
# ---------------------------------------------------------------------------

def test_read_research_no_dir(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    assert advisor._read_research_candidates() == []


def test_read_research_returns_candidates(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid)
    result = advisor._read_research_candidates()
    assert len(result) == 1
    assert result[0]["candidate_id"] == cid


def test_read_research_ignores_non_accepted(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    record = {"payload": {"action": "candidate_rejected", "candidate_id": "x"}}
    (research_dir / "test.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert advisor._read_research_candidates() == []


# ---------------------------------------------------------------------------
# _read_paper_stats
# ---------------------------------------------------------------------------

def test_read_paper_stats_no_dir(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    assert advisor._read_paper_stats() == {}


def test_read_paper_stats_win_rate_correct(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    for _ in range(15):   # 15 wins
        write_trade(tmp_path, "BTC-EUR", pnl=10.0)
    for _ in range(5):    # 5 losses
        write_trade(tmp_path, "BTC-EUR", pnl=-5.0)
    stats = advisor._read_paper_stats()
    assert "BTC-EUR" in stats
    assert stats["BTC-EUR"]["total_trades"] == 20
    assert stats["BTC-EUR"]["win_count"] == 15
    assert abs(stats["BTC-EUR"]["win_rate"] - 0.75) < 1e-6


def test_read_paper_stats_multiple_symbols(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    write_trade(tmp_path, "BTC-EUR", pnl=10.0)
    write_trade(tmp_path, "ETH-EUR", pnl=-5.0)
    stats = advisor._read_paper_stats()
    assert "BTC-EUR" in stats
    assert "ETH-EUR" in stats


def test_read_paper_stats_recent_trade_included(tmp_path: Path) -> None:
    """Trade met closed_at binnen het venster → meegenomen."""
    advisor = make_advisor(tmp_path)
    recent_ts = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
    write_trade(tmp_path, "BTC-EUR", pnl=10.0, closed_at=recent_ts)
    stats = advisor._read_paper_stats()
    assert stats["BTC-EUR"]["total_trades"] == 1


def test_read_paper_stats_old_trade_excluded(tmp_path: Path) -> None:
    """Trade met closed_at ouder dan stats_window_days → genegeerd."""
    advisor = make_advisor(tmp_path)
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=8)).isoformat()
    write_trade(tmp_path, "ETH-EUR", pnl=-5.0, closed_at=old_ts)
    stats = advisor._read_paper_stats()
    assert "ETH-EUR" not in stats


def test_read_paper_stats_no_closed_at_included(tmp_path: Path) -> None:
    """Trade zonder closed_at → fail-open: altijd meegenomen."""
    advisor = make_advisor(tmp_path)
    write_trade(tmp_path, "SOL-EUR", pnl=3.0)   # geen closed_at
    stats = advisor._read_paper_stats()
    assert "SOL-EUR" in stats
    assert stats["SOL-EUR"]["total_trades"] == 1


def test_read_paper_stats_mixed_old_and_recent(tmp_path: Path) -> None:
    """Combinatie van oude en recente trades — alleen recente tellen mee voor win_rate."""
    advisor = make_advisor(tmp_path)
    old_ts    = (datetime.now(tz=timezone.utc) - timedelta(days=10)).isoformat()
    recent_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=12)).isoformat()
    # 5 oude verliezen (worden genegeerd)
    for _ in range(5):
        write_trade(tmp_path, "BTC-EUR", pnl=-10.0, closed_at=old_ts)
    # 4 recente winsten
    for _ in range(4):
        write_trade(tmp_path, "BTC-EUR", pnl=10.0, closed_at=recent_ts)
    stats = advisor._read_paper_stats()
    assert stats["BTC-EUR"]["total_trades"] == 4
    assert abs(stats["BTC-EUR"]["win_rate"] - 1.0) < 1e-6


def test_read_paper_stats_old_losses_no_longer_trigger_deprioriteer(tmp_path: Path) -> None:
    """Win_rate 0% op 35 oude trades triggert geen deprioriteer meer na tijdfilter."""
    queen = make_queen(tmp_path)
    mission_mock = MagicMock()
    mission_mock.ant_type = "paper_ant"
    mission_mock.market_scope.symbols = ["ETH-EUR"]
    queen.active_missions = {"paper-mission-eth": mission_mock}
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="ETH-EUR")

    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    for _ in range(35):   # 35 verlies-trades uit het verleden (bevroren prijzen)
        write_trade(tmp_path, "ETH-EUR", pnl=-5.0, closed_at=old_ts)

    decision = advisor.advise()
    assert cid not in decision.deprioriteer_kandidaten
    assert "paper-mission-eth" not in decision.kapitaal_verlagen


def test_read_paper_stats_startup_log_written_once(tmp_path: Path, caplog) -> None:
    """Startup-log met historisch/recent tally wordt precies één keer geschreven."""
    import logging
    advisor = make_advisor(tmp_path)
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=14)).isoformat()
    write_trade(tmp_path, "BTC-EUR", pnl=-1.0, closed_at=old_ts)

    with caplog.at_level(logging.INFO, logger="ant_colony.queen.queen_advisor"):
        advisor._read_paper_stats()
        advisor._read_paper_stats()   # tweede aanroep

    startup_logs = [r for r in caplog.records if "Queen stats:" in r.message]
    assert len(startup_logs) == 1   # precies één keer


def test_read_paper_stats_window_respects_custom_days(tmp_path: Path) -> None:
    """stats_window_days=3 negeert trades van 4 dagen oud."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path, stats_window_days=3)
    borderline_ts = (datetime.now(tz=timezone.utc) - timedelta(days=4)).isoformat()
    write_trade(tmp_path, "BTC-EUR", pnl=10.0, closed_at=borderline_ts)
    stats = advisor._read_paper_stats()
    assert "BTC-EUR" not in stats


# ---------------------------------------------------------------------------
# _read_claude_advices
# ---------------------------------------------------------------------------

def test_read_claude_advices_no_dir(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    assert advisor._read_claude_advices() == []


def test_read_claude_advices_valid(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    write_claude_advice(tmp_path, "cid-1", age_minutes=0)
    result = advisor._read_claude_advices()
    assert len(result) == 1


def test_read_claude_advices_expired(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path, ttl_minutes=60)
    write_claude_advice(tmp_path, "cid-1", age_minutes=61)   # 61 min oud → verlopen
    result = advisor._read_claude_advices()
    assert len(result) == 0


def test_read_claude_advices_just_within_ttl(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path, ttl_minutes=60)
    write_claude_advice(tmp_path, "cid-1", age_minutes=59)
    result = advisor._read_claude_advices()
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Paper beslissingslogica
# ---------------------------------------------------------------------------

def test_high_win_rate_triggers_kapitaal_verhogen(tmp_path: Path) -> None:
    """win_rate > 0.60 over 20+ trades → kapitaal_verhogen."""
    queen = make_queen(tmp_path)
    mission_mock = MagicMock()
    mission_mock.ant_type = "paper_ant"
    mission_mock.market_scope.symbols = ["BTC-EUR"]
    queen.active_missions = {"paper-mission-001": mission_mock}
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="BTC-EUR")
    for _ in range(16):    # 16 wins
        write_trade(tmp_path, "BTC-EUR", pnl=5.0)
    for _ in range(4):     # 4 losses → win_rate = 0.80
        write_trade(tmp_path, "BTC-EUR", pnl=-2.0)

    decision = advisor.advise()
    assert "paper-mission-001" in decision.kapitaal_verhogen
    assert len(decision.adviezen_gevolgd) >= 1


def test_low_win_rate_triggers_verlagen_and_deprioriteer(tmp_path: Path) -> None:
    """win_rate < 0.30 over 10+ trades → kapitaal_verlagen + deprioriteer."""
    queen = make_queen(tmp_path)
    mission_mock = MagicMock()
    mission_mock.ant_type = "paper_ant"
    mission_mock.market_scope.symbols = ["ETH-EUR"]
    queen.active_missions = {"paper-mission-002": mission_mock}
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="ETH-EUR")
    for _ in range(2):     # 2 wins
        write_trade(tmp_path, "ETH-EUR", pnl=5.0, mission_id="paper-mission-002")
    for _ in range(8):     # 8 losses → win_rate = 0.20
        write_trade(tmp_path, "ETH-EUR", pnl=-3.0, mission_id="paper-mission-002")

    decision = advisor.advise()
    assert "paper-mission-002" in decision.kapitaal_verlagen
    assert cid in decision.deprioriteer_kandidaten


def test_insufficient_trades_neutral(tmp_path: Path) -> None:
    """Minder dan 10 trades → geen actie."""
    advisor = make_advisor(tmp_path)
    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid)
    for _ in range(5):
        write_trade(tmp_path, "BTC-EUR", pnl=-5.0)   # 5 trades, zou anders verlagen

    decision = advisor.advise()
    assert decision.kapitaal_verlagen == []
    assert decision.deprioriteer_kandidaten == []


# ---------------------------------------------------------------------------
# Claude beslissingslogica
# ---------------------------------------------------------------------------

def test_claude_advies_bevestigd_door_paper_prioriteit(tmp_path: Path) -> None:
    """Claude advies + paper bevestigt → prioriteit_kandidaten."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="BTC-EUR")
    write_claude_advice(tmp_path, cid, symbol="BTC-EUR", action="promote", confidence=8)
    for _ in range(15):   # 15 wins uit 20 → win_rate = 0.75
        write_trade(tmp_path, "BTC-EUR", pnl=10.0)
    for _ in range(5):
        write_trade(tmp_path, "BTC-EUR", pnl=-3.0)

    decision = advisor.advise()
    assert cid in decision.prioriteit_kandidaten
    assert any("bevestigd" in msg for msg in decision.adviezen_gevolgd)


def test_claude_advies_tegengesproken_door_paper(tmp_path: Path) -> None:
    """Claude advies maar paper tegenspreekt → adviezen_genegeerd."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="SOL-EUR")
    write_claude_advice(tmp_path, cid, symbol="SOL-EUR", action="promote", confidence=9)
    for _ in range(2):    # 2 wins uit 10 → win_rate = 0.20
        write_trade(tmp_path, "SOL-EUR", pnl=5.0)
    for _ in range(8):
        write_trade(tmp_path, "SOL-EUR", pnl=-3.0)

    decision = advisor.advise()
    assert cid not in decision.prioriteit_kandidaten
    assert any("tegenspreekt" in msg for msg in decision.adviezen_genegeerd)


def test_claude_advies_geen_paper_data_genegeerd(tmp_path: Path) -> None:
    """Claude advies maar geen paper data → genegeerd."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="BTC-EUR")
    write_claude_advice(tmp_path, cid, symbol="BTC-EUR", action="promote", confidence=8)
    # Geen paper trades geschreven

    decision = advisor.advise()
    assert cid not in decision.prioriteit_kandidaten
    assert any("geen paper-data" in msg for msg in decision.adviezen_genegeerd)


def test_claude_confidence_te_laag_genegeerd(tmp_path: Path) -> None:
    """Confidence < 6 → genegeerd."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="BTC-EUR")
    write_claude_advice(tmp_path, cid, symbol="BTC-EUR", action="promote", confidence=4)

    decision = advisor.advise()
    assert cid not in decision.prioriteit_kandidaten
    assert any("te laag" in msg for msg in decision.adviezen_genegeerd)


def test_claude_advies_verlopen_genegeerd(tmp_path: Path) -> None:
    """Verlopen advies wordt niet meegenomen."""
    queen = make_queen(tmp_path)
    advisor = QueenAdvisor(queen=queen, logs_root=tmp_path, advice_ttl_minutes=30)

    cid = str(uuid.uuid4())
    write_research_record(tmp_path, cid, symbol="BTC-EUR")
    write_claude_advice(tmp_path, cid, symbol="BTC-EUR", action="promote",
                        confidence=9, age_minutes=31)   # 31 min oud → verlopen
    for _ in range(15):
        write_trade(tmp_path, "BTC-EUR", pnl=10.0)
    for _ in range(5):
        write_trade(tmp_path, "BTC-EUR", pnl=-3.0)

    decision = advisor.advise()
    assert cid not in decision.prioriteit_kandidaten


# ---------------------------------------------------------------------------
# advise() algemeen
# ---------------------------------------------------------------------------

def test_advise_no_data_returns_empty(tmp_path: Path) -> None:
    advisor = make_advisor(tmp_path)
    decision = advisor.advise()
    assert isinstance(decision, QueenDecision)
    assert decision.is_empty()


def test_advise_logs_root_none_no_crash() -> None:
    queen = make_queen(Path("/nonexistent"))
    advisor = QueenAdvisor(queen=queen, logs_root=None)
    decision = advisor.advise()
    assert isinstance(decision, QueenDecision)


# ---------------------------------------------------------------------------
# Queen.apply_advisor_decision
# ---------------------------------------------------------------------------

def _make_real_queen(tmp_path: Path):
    from ant_colony.queen.queen import Queen
    from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
    scheduler = MagicMock()
    scheduler.enqueue_mission = MagicMock()
    queen = Queen(capital_total=10_000.0, scheduler=scheduler, logs_root=tmp_path)
    return queen


def test_apply_empty_decision_no_crash(tmp_path: Path) -> None:
    queen = _make_real_queen(tmp_path)
    decision = QueenDecision()
    queen.apply_advisor_decision(decision)   # should not raise


def test_apply_decision_logs_to_decisions_jsonl(tmp_path: Path) -> None:
    queen = _make_real_queen(tmp_path)
    decision = QueenDecision(
        adviezen_gevolgd=["paper:BTC-EUR win_rate=75.00% → kapitaal verhogen"],
        kapitaal_verhogen=["paper-mission-001"],
    )
    queen.apply_advisor_decision(decision)
    records = read_decisions(tmp_path)
    assert len(records) == 1
    assert records[0]["kapitaal_verhogen"] == ["paper-mission-001"]
    assert len(records[0]["adviezen_gevolgd"]) == 1


def test_apply_decision_with_allocatie_aanpassingen(tmp_path: Path) -> None:
    queen = _make_real_queen(tmp_path)
    queen.set_biome_capital("crypto", 5_000.0)
    decision = QueenDecision(allocatie_aanpassingen={"crypto": 0.6})
    queen.apply_advisor_decision(decision)
    records = read_decisions(tmp_path)
    assert len(records) == 1
    assert records[0]["allocatie_toegepast"] is True


# ---------------------------------------------------------------------------
# select_diverse_top_n
# ---------------------------------------------------------------------------

def _make_candidate(symbol: str, strategy_type: str, sharpe: float) -> dict:
    return {"symbol": symbol, "strategy_type": strategy_type, "sharpe": sharpe,
            "best_regime": "bull", "candidate_id": f"{symbol}-{strategy_type}"}


def test_diverse_top_n_sorts_by_sharpe() -> None:
    """Hoogste sharpe staat bovenaan."""
    cands = [
        _make_candidate("BTC-EUR", "sma_crossover",  0.5),
        _make_candidate("ETH-EUR", "rsi_momentum",   0.9),
        _make_candidate("SOL-EUR", "mean_reversion", 0.7),
    ]
    result = select_diverse_top_n(cands, n=3)
    assert result[0]["sharpe"] == 0.9
    assert result[1]["sharpe"] == 0.7


def test_diverse_top_n_max_one_per_symbol() -> None:
    """Maximaal 1 kandidaat per symbool in de top-N."""
    cands = [
        _make_candidate("BTC-EUR", "sma_crossover",  0.9),
        _make_candidate("BTC-EUR", "rsi_momentum",   0.8),   # zelfde symbool, hogere 2e
        _make_candidate("ETH-EUR", "mean_reversion", 0.7),
    ]
    result = select_diverse_top_n(cands, n=3)
    symbols = [c["symbol"] for c in result]
    assert symbols.count("BTC-EUR") == 1
    assert len(result) == 2   # BTC-EUR (0.9) + ETH-EUR (0.7), 2e BTC-EUR uitgesloten


def test_diverse_top_n_max_one_per_strategy_type() -> None:
    """Maximaal 1 kandidaat per strategy_type in de top-N."""
    cands = [
        _make_candidate("BTC-EUR", "sma_crossover", 0.9),
        _make_candidate("ETH-EUR", "sma_crossover", 0.8),   # zelfde type, ander symbool
        _make_candidate("SOL-EUR", "rsi_momentum",  0.7),
    ]
    result = select_diverse_top_n(cands, n=3)
    types = [c["strategy_type"] for c in result]
    assert types.count("sma_crossover") == 1
    assert len(result) == 2   # BTC sma (0.9) + SOL rsi (0.7), ETH sma uitgesloten


def test_diverse_top_n_picks_diverse_set() -> None:
    """Top-3 bevat unieke symbolen én strategy_types."""
    cands = [
        _make_candidate("BTC-EUR", "sma_crossover",  0.9),
        _make_candidate("BTC-EUR", "rsi_momentum",   0.85),  # zelfde symbool
        _make_candidate("BTC-EUR", "mean_reversion", 0.80),  # zelfde symbool
        _make_candidate("ETH-EUR", "rsi_momentum",   0.75),
        _make_candidate("SOL-EUR", "mean_reversion", 0.70),
        _make_candidate("ADA-EUR", "breakout",        0.65),
    ]
    result = select_diverse_top_n(cands, n=3)
    assert len(result) == 3
    assert len({c["symbol"] for c in result}) == 3          # alle 3 unieke symbolen
    assert len({c["strategy_type"] for c in result}) == 3   # alle 3 unieke types


def test_diverse_top_n_unknown_type_not_deduplicated() -> None:
    """strategy_type='unknown' telt niet als duplicate — meerdere unknowns zijn toegestaan."""
    cands = [
        _make_candidate("BTC-EUR", "unknown", 0.9),
        _make_candidate("ETH-EUR", "unknown", 0.8),
        _make_candidate("SOL-EUR", "unknown", 0.7),
    ]
    result = select_diverse_top_n(cands, n=3)
    assert len(result) == 3   # alle 3 toegelaten ondanks zelfde type


def test_diverse_top_n_fewer_than_n_candidates() -> None:
    """Minder dan N kandidaten → retourneer wat beschikbaar is."""
    cands = [_make_candidate("BTC-EUR", "sma_crossover", 0.8)]
    result = select_diverse_top_n(cands, n=3)
    assert len(result) == 1


def test_diverse_top_n_empty_input() -> None:
    """Lege input → lege output."""
    assert select_diverse_top_n([], n=3) == []


def test_diverse_top_n_btc_dominantie_doorbroken() -> None:
    """Regressietest: BTC-EUR domineert niet meer de top-3 als er diverse kandidaten zijn."""
    cands = [
        _make_candidate("BTC-EUR", "hybrid", 0.23),
        _make_candidate("BTC-EUR", "hybrid", 0.23),
        _make_candidate("BTC-EUR", "hybrid", 0.23),
        _make_candidate("ETH-EUR", "momentum", 0.20),
        _make_candidate("SOL-EUR", "mean_reversion", 0.18),
    ]
    result = select_diverse_top_n(cands, n=3)
    symbols = [c["symbol"] for c in result]
    assert symbols.count("BTC-EUR") == 1
    assert "ETH-EUR" in symbols
    assert "SOL-EUR" in symbols


def test_apply_decision_logs_all_fields(tmp_path: Path) -> None:
    queen = _make_real_queen(tmp_path)
    decision = QueenDecision(
        prioriteit_kandidaten=["cid-a"],
        deprioriteer_kandidaten=["cid-b"],
        kapitaal_verhogen=["mid-x"],
        kapitaal_verlagen=["mid-y"],
        adviezen_gevolgd=["advies 1"],
        adviezen_genegeerd=["advies 2: reden"],
    )
    queen.apply_advisor_decision(decision)
    records = read_decisions(tmp_path)
    r = records[0]
    assert r["prioriteit_kandidaten"]   == ["cid-a"]
    assert r["deprioriteer_kandidaten"] == ["cid-b"]
    assert r["kapitaal_verhogen"]       == ["mid-x"]
    assert r["kapitaal_verlagen"]       == ["mid-y"]
    assert r["adviezen_gevolgd"]        == ["advies 1"]
    assert r["adviezen_genegeerd"]      == ["advies 2: reden"]


# ---------------------------------------------------------------------------
# Regime bepaling en schrijven naar ANT_LOGS/queen/regime.jsonl
# ---------------------------------------------------------------------------

def _write_research_with_regime(tmp_path: Path, candidate_id: str, best_regime: str) -> None:
    """Schrijf een research record met best_regime veld."""
    research_dir = tmp_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "payload": {
            "action":       "candidate_accepted",
            "candidate_id": candidate_id,
            "symbol":       "BTC-EUR",
            "sharpe":       0.5,
            "best_regime":  best_regime,
        }
    }
    with (research_dir / "ant-research-001.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _read_regime_signal(tmp_path: Path) -> dict | None:
    """Lees de laatste regel uit ANT_LOGS/queen/regime.jsonl."""
    path = tmp_path / "queen" / "regime.jsonl"
    if not path.exists():
        return None
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    return json.loads(last) if last else None


class TestDetermineRegime:
    def test_bull_maps_to_trending(self, tmp_path):
        advisor = make_advisor(tmp_path)
        candidates = [{"best_regime": "bull"}, {"best_regime": "bull"}]
        assert advisor._determine_regime(candidates) == "TRENDING"

    def test_sideways_maps_to_sideways(self, tmp_path):
        advisor = make_advisor(tmp_path)
        candidates = [{"best_regime": "sideways"}]
        assert advisor._determine_regime(candidates) == "SIDEWAYS"

    def test_bear_maps_to_volatile(self, tmp_path):
        advisor = make_advisor(tmp_path)
        candidates = [{"best_regime": "bear"}, {"best_regime": "bear"}]
        assert advisor._determine_regime(candidates) == "VOLATILE"

    def test_dominant_wins_when_mixed(self, tmp_path):
        advisor = make_advisor(tmp_path)
        # 3 bull, 2 sideways → TRENDING
        candidates = [
            {"best_regime": "bull"},
            {"best_regime": "bull"},
            {"best_regime": "bull"},
            {"best_regime": "sideways"},
            {"best_regime": "sideways"},
        ]
        assert advisor._determine_regime(candidates) == "TRENDING"

    def test_empty_candidates_returns_none(self, tmp_path):
        advisor = make_advisor(tmp_path)
        assert advisor._determine_regime([]) is None

    def test_candidates_without_best_regime_returns_none(self, tmp_path):
        advisor = make_advisor(tmp_path)
        candidates = [{"sharpe": 0.5}, {"symbol": "BTC-EUR"}]
        assert advisor._determine_regime(candidates) is None

    def test_unknown_regime_value_returns_none(self, tmp_path):
        advisor = make_advisor(tmp_path)
        candidates = [{"best_regime": "bullish"}]  # niet in _REGIME_MAP
        assert advisor._determine_regime(candidates) is None


class TestWriteRegimeSignal:
    def test_writes_file_on_first_call(self, tmp_path):
        advisor = make_advisor(tmp_path)
        advisor._write_regime_signal("TRENDING")

        rec = _read_regime_signal(tmp_path)
        assert rec is not None
        assert rec["payload"]["action"] == "regime_signal"
        assert rec["payload"]["regime"] == "TRENDING"

    def test_appends_on_subsequent_calls(self, tmp_path):
        advisor = make_advisor(tmp_path)
        advisor._write_regime_signal("SIDEWAYS")
        advisor._write_regime_signal("VOLATILE")

        lines = (tmp_path / "queen" / "regime.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(l) for l in lines if l.strip()]
        assert len(records) == 2
        assert records[0]["payload"]["regime"] == "SIDEWAYS"
        assert records[1]["payload"]["regime"] == "VOLATILE"

    def test_creates_queen_dir_if_missing(self, tmp_path):
        advisor = make_advisor(tmp_path)
        assert not (tmp_path / "queen").exists()
        advisor._write_regime_signal("TRENDING")
        assert (tmp_path / "queen" / "regime.jsonl").exists()

    def test_no_write_when_logs_root_is_none(self):
        queen = MagicMock()
        queen.active_missions = {}
        advisor = QueenAdvisor(queen=queen, logs_root=None)
        advisor._write_regime_signal("TRENDING")   # mag niet crashen

    def test_timestamp_is_present(self, tmp_path):
        advisor = make_advisor(tmp_path)
        advisor._write_regime_signal("SIDEWAYS")
        rec = _read_regime_signal(tmp_path)
        assert "timestamp" in rec
        assert rec["timestamp"]  # niet leeg


class TestAdviseWritesRegime:
    def test_advise_writes_regime_when_candidates_present(self, tmp_path):
        advisor = make_advisor(tmp_path)
        _write_research_with_regime(tmp_path, str(uuid.uuid4()), "bull")

        advisor.advise()

        rec = _read_regime_signal(tmp_path)
        assert rec is not None
        assert rec["payload"]["regime"] == "TRENDING"

    def test_advise_does_not_write_when_no_candidates(self, tmp_path):
        advisor = make_advisor(tmp_path)
        advisor.advise()
        assert not (tmp_path / "queen" / "regime.jsonl").exists()

    def test_advise_writes_at_every_cycle(self, tmp_path):
        advisor = make_advisor(tmp_path)
        _write_research_with_regime(tmp_path, str(uuid.uuid4()), "sideways")

        advisor.advise()
        advisor.advise()
        advisor.advise()

        lines = (tmp_path / "queen" / "regime.jsonl").read_text(encoding="utf-8").splitlines()
        records = [l for l in lines if l.strip()]
        assert len(records) == 3

    def test_advise_sideways_regime(self, tmp_path):
        advisor = make_advisor(tmp_path)
        _write_research_with_regime(tmp_path, str(uuid.uuid4()), "sideways")
        _write_research_with_regime(tmp_path, str(uuid.uuid4()), "sideways")

        advisor.advise()
        rec = _read_regime_signal(tmp_path)
        assert rec["payload"]["regime"] == "SIDEWAYS"

    def test_advise_volatile_regime_from_bear(self, tmp_path):
        advisor = make_advisor(tmp_path)
        _write_research_with_regime(tmp_path, str(uuid.uuid4()), "bear")

        advisor.advise()
        rec = _read_regime_signal(tmp_path)
        assert rec["payload"]["regime"] == "VOLATILE"


# ---------------------------------------------------------------------------
# Weekend / markturen protocol
# ---------------------------------------------------------------------------

from datetime import date as _date


def _ams_dt(weekday: int, hour: int, minute: int) -> datetime:
    """
    Maak een Amsterdam-timezone datetime voor de gegeven dag/tijd.
    weekday: 0=ma … 6=zo (relatief aan een vaste maandag 2026-04-27)
    """
    from zoneinfo import ZoneInfo
    base_monday = datetime(2026, 4, 27, tzinfo=ZoneInfo("Europe/Amsterdam"))
    from datetime import timedelta as _td
    return base_monday.replace(hour=hour, minute=minute, second=0, microsecond=0) + _td(days=weekday)


class TestIsMarketOpen:
    def _advisor_at(self, tmp_path, weekday: int, hour: int, minute: int) -> "QueenAdvisor":
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(weekday, hour, minute)
        advisor._now_amsterdam = lambda: fixed
        return advisor

    def test_open_weekday_during_hours(self, tmp_path):
        """Ma 16:00 Amsterdam → markt open."""
        advisor = self._advisor_at(tmp_path, 0, 16, 0)
        is_open, opens_in = advisor._is_market_open()
        assert is_open is True
        assert opens_in == 0

    def test_open_at_exactly_1530(self, tmp_path):
        """Ma 15:30 → markt net open."""
        advisor = self._advisor_at(tmp_path, 0, 15, 30)
        is_open, _ = advisor._is_market_open()
        assert is_open is True

    def test_closed_before_open(self, tmp_path):
        """Ma 10:00 → markt dicht, geeft minuten tot 15:30."""
        advisor = self._advisor_at(tmp_path, 0, 10, 0)
        is_open, opens_in = advisor._is_market_open()
        assert is_open is False
        assert opens_in == 5 * 60 + 30   # 5.5 uur = 330 min

    def test_closed_at_exactly_2200(self, tmp_path):
        """Ma 22:00 → markt dicht (gesloten grens is exclusief)."""
        advisor = self._advisor_at(tmp_path, 0, 22, 0)
        is_open, _ = advisor._is_market_open()
        assert is_open is False

    def test_closed_saturday(self, tmp_path):
        """Za 12:00 → markt dicht."""
        advisor = self._advisor_at(tmp_path, 5, 12, 0)
        is_open, opens_in = advisor._is_market_open()
        assert is_open is False
        assert opens_in > 0   # minuten tot maandag 15:30

    def test_closed_sunday(self, tmp_path):
        """Zo 10:00 → markt dicht."""
        advisor = self._advisor_at(tmp_path, 6, 10, 0)
        is_open, _ = advisor._is_market_open()
        assert is_open is False

    def test_friday_after_close_opens_monday(self, tmp_path):
        """Vr 23:00 → volgende open = maandag (~64.5 uur = 3870 min)."""
        advisor = self._advisor_at(tmp_path, 4, 23, 0)
        is_open, opens_in = advisor._is_market_open()
        assert is_open is False
        # 3 dagen × 24 uur - 7.5 uur = 64.5 uur = 3870 min
        assert opens_in == pytest.approx(3870, abs=2)


class TestIsInBriefingWindow:
    def _advisor_at(self, tmp_path, weekday: int, hour: int, minute: int) -> "QueenAdvisor":
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(weekday, hour, minute)
        advisor._now_amsterdam = lambda: fixed
        return advisor

    def test_true_at_1515(self, tmp_path):
        advisor = self._advisor_at(tmp_path, 0, 15, 15)   # ma 15:15
        assert advisor._is_in_briefing_window() is True

    def test_true_at_1529(self, tmp_path):
        advisor = self._advisor_at(tmp_path, 0, 15, 29)
        assert advisor._is_in_briefing_window() is True

    def test_false_at_1530(self, tmp_path):
        """15:30 = market open → buiten briefing-venster."""
        advisor = self._advisor_at(tmp_path, 0, 15, 30)
        assert advisor._is_in_briefing_window() is False

    def test_false_before_briefing(self, tmp_path):
        advisor = self._advisor_at(tmp_path, 0, 10, 0)
        assert advisor._is_in_briefing_window() is False

    def test_false_on_weekend(self, tmp_path):
        advisor = self._advisor_at(tmp_path, 6, 15, 15)   # zo 15:15
        assert advisor._is_in_briefing_window() is False


class TestBriefing:
    def _write_news_snapshot(self, tmp_path: Path, sentiment: str = "bullish",
                              headlines: list | None = None) -> None:
        news_dir = tmp_path / "news"
        news_dir.mkdir(parents=True, exist_ok=True)
        snap = {
            "timestamp":        "2026-04-28T15:00:00",
            "market_sentiment": sentiment,
            "sentiment_score":  0.2,
            "crypto_sentiment": "neutral",
            "equities_sentiment": sentiment,
            "top_headlines":    headlines or ["Headline A", "Headline B", "Headline C"],
        }
        (news_dir / "20260428-150000.json").write_text(json.dumps(snap), encoding="utf-8")

    def _write_sector_signal(self, tmp_path: Path, symbol: str, rank: int) -> None:
        scouts_dir = tmp_path / "scouts"
        scouts_dir.mkdir(parents=True, exist_ok=True)
        rec = {
            "payload": {
                "action":         "opportunity_detected",
                "signal_type":    "sector_rotation",
                "symbol":         symbol,
                "momentum_rank":  rank,
                "confidence":     0.8,
            }
        }
        with (scouts_dir / "sector-scout.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def _read_briefing(self, tmp_path: Path) -> dict | None:
        path = tmp_path / "queen" / "briefing.jsonl"
        if not path.exists():
            return None
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return json.loads(lines[-1]) if lines else None

    def test_briefing_written_in_window(self, tmp_path):
        """Briefing wordt geschreven als advise() wordt aangeroepen tijdens het briefing-venster."""
        self._write_news_snapshot(tmp_path, sentiment="bullish")
        self._write_sector_signal(tmp_path, "XLE", rank=1)
        self._write_sector_signal(tmp_path, "XLK", rank=2)

        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(0, 15, 20)   # ma 15:20 → in briefing-venster
        advisor._now_amsterdam = lambda: fixed

        advisor.advise()

        rec = self._read_briefing(tmp_path)
        assert rec is not None
        assert rec["action"] == "market_opening_briefing"
        assert rec["market_sentiment"] == "bullish"
        assert "XLE" in rec["top_sectors"]

    def test_briefing_not_written_twice_same_day(self, tmp_path):
        """Tweede advise() in hetzelfde venster schrijft geen tweede briefing."""
        self._write_news_snapshot(tmp_path)
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(0, 15, 20)
        advisor._now_amsterdam = lambda: fixed

        advisor.advise()
        advisor.advise()   # tweede cyclus

        lines = (tmp_path / "queen" / "briefing.jsonl").read_text(encoding="utf-8").splitlines()
        records = [l for l in lines if l.strip()]
        assert len(records) == 1

    def test_briefing_recommendation_contains_sentiment(self, tmp_path):
        """Recommendation-string bevat het sentiment."""
        self._write_news_snapshot(tmp_path, sentiment="bearish")
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(0, 15, 20)
        advisor._now_amsterdam = lambda: fixed

        advisor.advise()

        rec = self._read_briefing(tmp_path)
        assert "bearish" in rec["recommendation"]

    def test_briefing_no_news_neutral_fallback(self, tmp_path):
        """Geen nieuws-snapshot → market_sentiment='neutral'."""
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(0, 15, 20)
        advisor._now_amsterdam = lambda: fixed

        advisor.advise()

        rec = self._read_briefing(tmp_path)
        assert rec["market_sentiment"] == "neutral"

    def test_briefing_not_written_outside_window(self, tmp_path):
        """Buiten het briefing-venster wordt geen briefing geschreven."""
        self._write_news_snapshot(tmp_path)
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(0, 10, 0)   # ma 10:00 → buiten venster
        advisor._now_amsterdam = lambda: fixed

        advisor.advise()

        assert not (tmp_path / "queen" / "briefing.jsonl").exists()

    def test_top_sectors_sorted_by_rank(self, tmp_path):
        """Sector-signalen worden gesorteerd op momentum_rank (laagste eerst)."""
        self._write_news_snapshot(tmp_path)
        self._write_sector_signal(tmp_path, "XLU", rank=5)
        self._write_sector_signal(tmp_path, "XLE", rank=1)
        self._write_sector_signal(tmp_path, "XLK", rank=3)

        advisor = make_advisor(tmp_path)
        result = advisor._read_top_sector_signals(n=2)
        assert result[0] == "XLE"   # rank 1 = beste
        assert result[1] == "XLK"   # rank 3


class TestWeekendLogging:
    def test_advise_runs_on_weekend_without_crash(self, tmp_path):
        """advise() gooit geen exception op zaterdag."""
        advisor = make_advisor(tmp_path)
        fixed = _ams_dt(5, 12, 0)   # zaterdag
        advisor._now_amsterdam = lambda: fixed
        decision = advisor.advise()
        assert isinstance(decision, QueenDecision)

    def test_crypto_decisions_continue_on_weekend(self, tmp_path):
        """Op weekend worden crypto paper-stats nog steeds verwerkt."""
        queen = make_queen(tmp_path)
        mission_mock = MagicMock()
        mission_mock.ant_type = "paper_ant"
        mission_mock.market_scope.symbols = ["BTC-EUR"]
        queen.active_missions = {"paper-btc": mission_mock}
        advisor = QueenAdvisor(queen=queen, logs_root=tmp_path)

        # Zaterdag
        fixed = _ams_dt(5, 12, 0)
        advisor._now_amsterdam = lambda: fixed

        cid = str(uuid.uuid4())
        write_research_record(tmp_path, cid, symbol="BTC-EUR")
        for _ in range(16):
            write_trade(tmp_path, "BTC-EUR", pnl=5.0)
        for _ in range(4):
            write_trade(tmp_path, "BTC-EUR", pnl=-2.0)

        decision = advisor.advise()
        assert "paper-btc" in decision.kapitaal_verhogen
