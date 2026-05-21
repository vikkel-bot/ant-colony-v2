"""
tests/test_pipeline_approved_to_paper.py

Tests voor de koppeling: StrategyPromoter → PaperAnt (APPROVED kandidaten)

Scenarios:
  1.  APPROVED kandidaat met live prijs → paper positie geopend
  2.  Geen approved directory → geen crash
  3.  candidate_id al gezien → niet opnieuw verwerkt
  4.  Geen prijs beschikbaar → positie niet geopend
  5.  Kandidaat met direction=short → overgeslagen
  6.  Al open positie voor symbool → nieuwe positie overgeslagen
  7.  logs_root=None → geen crash
  8.  Ongeldige JSON-regel → overgeslagen zonder crash
  9.  Meerdere APPROVED kandidaten → elk apart verwerkt
  10. Paper log bevat trade_opened event na positie openen
  11. Stale approved kandidaat (>24u) wordt overgeslagen
  12. Verse approved kandidaat (promoted_at recent) wordt verwerkt
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.ants.paper_ant import PaperAnt, _MAX_APPROVED_PER_TICK
from ant_colony.exit_chain.position import PaperPosition, PositionSide
from ant_colony.lab.backtester import BacktestResults
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.strategy_candidate import CandidateStatus, StrategyCandidate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"
_PRICE  = 40_000.0


def make_mission(symbols: list[str] | None = None, capital: float = 10_000.0) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="paper_ant",
        allowed_node="pc2",
        allowed_actions=["open_position", "close_position"],
        market_scope=MarketScope(biome=_BIOME, symbols=symbols or [_SYMBOL]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
            stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="pipeline paper test"),
    )


def make_adapter(price: float | None = _PRICE) -> MagicMock:
    adapter = MagicMock()
    adapter.is_available.return_value = price is not None
    if price is not None:
        md = MagicMock()
        md.close = price
        md.is_valid_price = True
        md.is_stale.return_value = False
        adapter.get_market_data.return_value = md
    else:
        adapter.get_market_data.return_value = None
    return adapter


def make_registry(price: float | None = _PRICE) -> MagicMock:
    registry = MagicMock()
    registry.get.return_value = make_adapter(price)
    return registry


def make_ant(
    tmp_path: Path,
    *,
    price: float | None = _PRICE,
    capital: float = 10_000.0,
) -> PaperAnt:
    return PaperAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(capital=capital),
        scheduler=MagicMock(),
        biome_registry=make_registry(price),
        logs_root=tmp_path,
    )


def make_approved_candidate(
    *,
    candidate_id: str | None = None,
    symbol: str = _SYMBOL,
    direction: str = "long",
    fitness_score: float = 0.8,
) -> StrategyCandidate:
    return StrategyCandidate(
        candidate_id=candidate_id or f"cand-{uuid.uuid4().hex[:8]}",
        name="Test APPROVED",
        source="test",
        biome=_BIOME,
        market_scope={"symbol": symbol, "timeframe": "1h"},
        logic_summary="Test",
        parameters={"direction": direction},
        entry_conditions={"direction": direction},
        exit_conditions={"take_profit_pct": 0.06, "stop_loss_pct": 0.03},
        backtest_results=BacktestResults(
            sharpe_ratio=fitness_score,
            win_rate=0.6,
            total_trades=25,
            max_drawdown_pct=0.05,
        ),
        fitness_score=fitness_score,
        status=CandidateStatus.APPROVED,
        approved_by="queen",
    )


def write_approved(
    approved_dir: Path,
    candidate: StrategyCandidate,
    *,
    promoted_at: str | None = None,
) -> None:
    approved_dir.mkdir(parents=True, exist_ok=True)
    log_path = approved_dir / f"{candidate.candidate_id}.jsonl"
    record = candidate.model_dump(mode="json")
    now_iso = datetime.now(timezone.utc).isoformat()
    record["timestamp"] = promoted_at or now_iso
    record["promoted_at"] = promoted_at or now_iso
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_paper_log(tmp_path: Path) -> list[dict]:
    paper_dir = tmp_path / "paper"
    if not paper_dir.exists():
        return []
    records = []
    for path in paper_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_approved_candidate_opens_position(tmp_path: Path) -> None:
    """APPROVED kandidaat met live prijs → paper positie geopend."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 1


def test_no_approved_dir_no_crash(tmp_path: Path) -> None:
    """Geen approved directory → geen crash."""
    ant = make_ant(tmp_path)
    ant._process_approved_candidates()  # should not raise


def test_duplicate_candidate_id_not_reprocessed(tmp_path: Path) -> None:
    """Dezelfde candidate_id twee keer → slechts één positie geopend."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    ant._process_approved_candidates()
    # tweede tick — kandidaat al in _seen_approved_ids
    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 1


def test_no_price_no_position(tmp_path: Path) -> None:
    """Geen prijs beschikbaar → positie niet geopend."""
    ant       = make_ant(tmp_path, price=None)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 0


def test_short_direction_skipped(tmp_path: Path) -> None:
    """Kandidaat met direction=short → overgeslagen (PaperAnt gaat alleen long)."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate(direction="short")
    write_approved(tmp_path / "approved", candidate)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 0


def test_existing_position_skips_new_entry(tmp_path: Path) -> None:
    """Al open positie voor symbool → nieuwe positie overgeslagen."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    # Handmatig een open positie registreren
    pos = PaperPosition(
        position_id=str(uuid.uuid4()),
        symbol=_SYMBOL,
        biome=_BIOME,
        mission_id=ant.mission.mission_id,
        ant_id=ant.ant_id,
        side=PositionSide.LONG,
        entry_price=_PRICE,
        quantity=0.01,
        stop_loss_price=_PRICE * 0.98,
        take_profit_price=_PRICE * 1.03,
        ttl=ant.mission.ttl,
        current_price=_PRICE,
        peak_price=_PRICE,
        opened_at=datetime.now(tz=timezone.utc),
    )
    ant._ledger.record_opened(pos)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 1  # geen tweede positie


def test_logs_root_none_no_crash() -> None:
    """logs_root=None → geen crash."""
    ant = PaperAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_registry(),
        logs_root=None,
    )
    ant._process_approved_candidates()  # should not raise


def test_invalid_json_skipped(tmp_path: Path) -> None:
    """Ongeldige JSON-regel → overgeslagen, rest verwerkt."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()

    approved_dir = tmp_path / "approved"
    approved_dir.mkdir(parents=True, exist_ok=True)
    log_path = approved_dir / f"{candidate.candidate_id}.jsonl"
    with log_path.open("w", encoding="utf-8") as fh:
        fh.write("niet geldig json\n")
        fh.write(json.dumps(candidate.model_dump(mode="json"), default=str) + "\n")

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 1


def test_multiple_candidates_each_considered(tmp_path: Path) -> None:
    """Meerdere APPROVED kandidaten voor verschillende symbolen → elk apart verwerkt."""
    symbols   = ["BTC-EUR", "ETH-EUR", "XRP-EUR"]
    adapter   = MagicMock()
    adapter.is_available.return_value = True
    md = MagicMock()
    md.close = _PRICE
    md.is_valid_price = True
    md.is_stale.return_value = False
    adapter.get_market_data.return_value = md
    registry = MagicMock()
    registry.get.return_value = adapter

    ant = PaperAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(symbols=symbols),
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    for sym in symbols:
        write_approved(tmp_path / "approved", make_approved_candidate(symbol=sym))

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 3


def test_approved_processing_stops_after_max_per_tick(tmp_path: Path) -> None:
    """Approved-loop verwerkt per tick maximaal _MAX_APPROVED_PER_TICK kandidaten."""
    ant = make_ant(tmp_path, price=_PRICE)
    symbols = [f"SYM{i}-EUR" for i in range(_MAX_APPROVED_PER_TICK + 3)]
    for symbol in symbols:
        write_approved(tmp_path / "approved", make_approved_candidate(symbol=symbol))

    ant._open_from_candidate = MagicMock(return_value=False)

    count = ant._process_approved_candidates()

    assert count == _MAX_APPROVED_PER_TICK
    assert ant._open_from_candidate.call_count == _MAX_APPROVED_PER_TICK


def test_trade_opened_event_in_log(tmp_path: Path) -> None:
    """Na positie openen staat een trade_opened event in de paper log."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    ant._process_approved_candidates()

    logs    = read_paper_log(tmp_path)
    actions = [r.get("payload", {}).get("action") for r in logs]
    assert "trade_opened" in actions


def test_rejected_candidate_retried_after_position_closes(tmp_path: Path) -> None:
    """Geweigerde kandidaat (al open positie) wordt opnieuw geprobeerd als de positie gesloten is."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    write_approved(tmp_path / "approved", candidate)

    # Tick 1: simuleer een open positie — _open_from_candidate geeft False terug
    ant._has_open_position = MagicMock(return_value=True)
    ant._process_approved_candidates()
    # Kandidaat geblokkeerd → niet in _seen_approved_ids
    assert candidate.candidate_id not in ant._seen_approved_ids

    # Tick 2: positie weg → kandidaat moet nu wél worden geprobeerd
    ant._has_open_position = MagicMock(return_value=False)
    ant._process_approved_candidates()
    assert candidate.candidate_id in ant._seen_approved_ids
    assert len(ant._ledger.open_positions) == 1


def test_stale_approved_candidate_not_processed(tmp_path: Path) -> None:
    """Approved kandidaat ouder dan 24 uur wordt overgeslagen."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    stale_ts  = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat()
    write_approved(tmp_path / "approved", candidate, promoted_at=stale_ts)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 0
    # Niet in seen — mag opnieuw geprobeerd als timestamp ververst
    assert candidate.candidate_id not in ant._seen_approved_ids


def test_fresh_approved_candidate_within_24h_processed(tmp_path: Path) -> None:
    """Approved kandidaat met promoted_at < 24 uur oud wordt wél verwerkt."""
    ant       = make_ant(tmp_path, price=_PRICE)
    candidate = make_approved_candidate()
    fresh_ts  = (datetime.now(tz=timezone.utc) - timedelta(hours=23)).isoformat()
    write_approved(tmp_path / "approved", candidate, promoted_at=fresh_ts)

    ant._process_approved_candidates()

    assert len(ant._ledger.open_positions) == 1
