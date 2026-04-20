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


def write_trade(tmp_path: Path, symbol: str, pnl: float,
                mission_id: str = "paper-mission-001") -> None:
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    trade = {
        "position_id":  str(uuid.uuid4()),
        "symbol":       symbol,
        "side":         "long",
        "status":       "closed",
        "realized_pnl": pnl,
        "exit_reason":  "take_profit",
    }
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
