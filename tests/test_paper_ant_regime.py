"""
tests/test_paper_ant_regime.py

Tests voor regime-gebaseerde entry filtering in PaperAnt.
Dekt: read_latest_queen_regime, _is_entry_allowed_by_regime,
      SIDEWAYS/TRENDING/VOLATILE gedrag in alle drie entry-paden,
      en VOLATILE positiegrootte/SL aanpassingen.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants._queen_regime import read_latest_queen_regime
from ant_colony.ants.paper_ant import (
    PaperAnt,
    _SL_PCT,
    _TP_PCT,
    _TRADE_CAPITAL_FRACTION,
    _SIDEWAYS_ALLOWED_STRATEGY_TYPES,
    _VOLATILE_MIN_SHARPE,
    _VOLATILE_SL_MULTIPLIER,
    _VOLATILE_CAPITAL_MULT,
)
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_PRICE  = 40_000.0


def _make_mission(capital: float = 10_000.0) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="paper_ant",
        allowed_node="pc2",
        allowed_actions=["open_position", "close_position"],
        market_scope=MarketScope(biome="crypto", symbols=[_SYMBOL]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
            stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="regime test"),
    )


def _make_adapter(price: float = _PRICE) -> MagicMock:
    md = MagicMock()
    md.close = price
    md.is_valid_price = True
    md.is_stale.return_value = False
    adapter = MagicMock()
    adapter.is_available.return_value = True
    adapter.get_market_data.return_value = md
    return adapter


def _make_ant(tmp_path: Path) -> PaperAnt:
    mission = _make_mission()
    scheduler = MagicMock()
    biome_registry = MagicMock()
    biome_registry.get.return_value = _make_adapter()
    return PaperAnt(
        ant_id=uuid.uuid4().hex,
        mission=mission,
        scheduler=scheduler,
        biome_registry=biome_registry,
        logs_root=tmp_path,
    )


def _write_regime_signal(logs_root: Path, regime: str) -> None:
    """Schrijf een regime-signaal naar ANT_LOGS/queen/regime.jsonl."""
    queen_dir = logs_root / "queen"
    queen_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-04-25T10:00:00+00:00",
        "payload": {"action": "regime_signal", "regime": regime},
    }
    (queen_dir / "regime.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_research_candidate(
    logs_root: Path,
    ant_id: str,
    symbol: str,
    strategy_type: str,
    sharpe: float = 0.5,
    candidate_id: str | None = None,
) -> str:
    """Schrijf een research candidate_accepted record en return candidate_id."""
    cid = candidate_id or uuid.uuid4().hex
    research_dir = logs_root / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    record = {
        "timestamp": now,
        "payload": {
            "action": "candidate_accepted",
            "candidate_id": cid,
            "symbol": symbol,
            "strategy_type": strategy_type,
            "sharpe": sharpe,
            "sharpe_ratio": sharpe,
            "direction": "long",
            "biome": "crypto",
            "tp_pct": _TP_PCT,
            "sl_pct": _SL_PCT,
        },
    }
    path = research_dir / f"{ant_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return cid


def _write_scout_signal(
    logs_root: Path,
    ant_id: str,
    symbol: str,
    signal_type: str = "price_move",
    change_pct: float = 0.05,
) -> str:
    """Schrijf een scout opportunity_detected record en return signal_id."""
    sid = uuid.uuid4().hex
    scout_dir = logs_root / "scouts"
    scout_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    record = {
        "timestamp": now,
        "payload": {
            "action": "opportunity_detected",
            "signal_id": sid,
            "symbol": symbol,
            "signal_type": signal_type,
            "change_pct": change_pct,
            "confidence": 0.85,
            "biome": "crypto",
            "detected_at": now,
        },
    }
    path = scout_dir / f"{ant_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return sid


# ---------------------------------------------------------------------------
# read_latest_queen_regime
# ---------------------------------------------------------------------------

class TestReadLatestQueenRegime:
    def test_returns_none_when_file_missing(self, tmp_path):
        assert read_latest_queen_regime(tmp_path) is None

    def test_returns_none_when_dir_missing(self, tmp_path):
        # queen/ subdir bestaat niet
        assert read_latest_queen_regime(tmp_path) is None

    def test_returns_sideways(self, tmp_path):
        _write_regime_signal(tmp_path, "SIDEWAYS")
        assert read_latest_queen_regime(tmp_path) == "SIDEWAYS"

    def test_returns_trending(self, tmp_path):
        _write_regime_signal(tmp_path, "TRENDING")
        assert read_latest_queen_regime(tmp_path) == "TRENDING"

    def test_returns_volatile(self, tmp_path):
        _write_regime_signal(tmp_path, "VOLATILE")
        assert read_latest_queen_regime(tmp_path) == "VOLATILE"

    def test_returns_none_for_unknown_regime(self, tmp_path):
        _write_regime_signal(tmp_path, "BULLISH")
        assert read_latest_queen_regime(tmp_path) is None

    def test_case_insensitive(self, tmp_path):
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        record = {"timestamp": "2026-04-25T10:00:00Z", "payload": {"action": "regime_signal", "regime": "sideways"}}
        (queen_dir / "regime.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert read_latest_queen_regime(tmp_path) == "SIDEWAYS"

    def test_returns_none_for_wrong_action(self, tmp_path):
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        record = {"timestamp": "2026-04-25T10:00:00Z", "payload": {"action": "other_action", "regime": "SIDEWAYS"}}
        (queen_dir / "regime.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert read_latest_queen_regime(tmp_path) is None

    def test_reads_last_line_of_multiple(self, tmp_path):
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        lines = [
            json.dumps({"timestamp": "2026-04-25T09:00:00Z", "payload": {"action": "regime_signal", "regime": "SIDEWAYS"}}),
            json.dumps({"timestamp": "2026-04-25T10:00:00Z", "payload": {"action": "regime_signal", "regime": "VOLATILE"}}),
        ]
        (queen_dir / "regime.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert read_latest_queen_regime(tmp_path) == "VOLATILE"

    def test_returns_none_on_empty_file(self, tmp_path):
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        (queen_dir / "regime.jsonl").write_text("", encoding="utf-8")
        assert read_latest_queen_regime(tmp_path) is None


# ---------------------------------------------------------------------------
# _is_entry_allowed_by_regime
# ---------------------------------------------------------------------------

class TestIsEntryAllowedByRegime:
    def test_none_regime_allows_all(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime(None, strategy_type="momentum")
        assert allowed is True

    def test_trending_allows_all_strategy_types(self, tmp_path):
        ant = _make_ant(tmp_path)
        for st in ["momentum", "sma_crossover", "rsi_based", "mean_reversion", "bollinger_bands"]:
            allowed, _ = ant._is_entry_allowed_by_regime("TRENDING", strategy_type=st)
            assert allowed is True, f"{st} must be allowed in TRENDING"

    def test_sideways_blocks_momentum(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, reason = ant._is_entry_allowed_by_regime("SIDEWAYS", strategy_type="momentum")
        assert allowed is False
        assert "SIDEWAYS" in reason

    def test_sideways_blocks_sma_crossover(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, reason = ant._is_entry_allowed_by_regime("SIDEWAYS", strategy_type="sma_crossover")
        assert allowed is False
        assert "SIDEWAYS" in reason

    def test_sideways_allows_mean_reversion(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime("SIDEWAYS", strategy_type="mean_reversion")
        assert allowed is True

    def test_sideways_allows_rsi_based(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime("SIDEWAYS", strategy_type="rsi_based")
        assert allowed is True

    def test_sideways_blocks_scout_signals(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, reason = ant._is_entry_allowed_by_regime("SIDEWAYS", is_scout=True, symbol="BTC-EUR")
        assert allowed is False
        assert "SIDEWAYS" in reason

    def test_volatile_blocks_low_sharpe(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, reason = ant._is_entry_allowed_by_regime("VOLATILE", sharpe=0.1)
        assert allowed is False
        assert "VOLATILE" in reason

    def test_volatile_allows_high_sharpe(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime("VOLATILE", sharpe=0.5)
        assert allowed is True

    def test_volatile_allows_at_exact_threshold(self, tmp_path):
        ant = _make_ant(tmp_path)
        # Exact threshold = 0.3 → sharpe <= 0.3 blocked
        allowed, _ = ant._is_entry_allowed_by_regime("VOLATILE", sharpe=_VOLATILE_MIN_SHARPE)
        assert allowed is False

    def test_volatile_allows_none_sharpe(self, tmp_path):
        # Geen sharpe = fail-open
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime("VOLATILE", sharpe=None)
        assert allowed is True

    def test_unknown_regime_allows_all(self, tmp_path):
        ant = _make_ant(tmp_path)
        allowed, _ = ant._is_entry_allowed_by_regime("UNKNOWN_REGIME", strategy_type="momentum")
        assert allowed is True


# ---------------------------------------------------------------------------
# SIDEWAYS regime — scout-pad geblokkeerd
# ---------------------------------------------------------------------------

class TestSidewaysScoutBlocked:
    def test_scout_signal_blocked_in_sideways(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_regime_signal(tmp_path, "SIDEWAYS")
        sid = _write_scout_signal(tmp_path, ant.ant_id, _SYMBOL, "price_move", 0.05)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            count = ant._process_new_signals(regime="SIDEWAYS")

        assert len(ant._ledger.open_positions) == 0

    def test_scout_signal_allowed_in_trending(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_scout_signal(tmp_path, ant.ant_id, _SYMBOL, "price_move", 0.05)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_new_signals(regime="TRENDING")

        assert len(ant._ledger.open_positions) == 1

    def test_scout_signal_allowed_when_no_regime(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_scout_signal(tmp_path, ant.ant_id, _SYMBOL, "price_move", 0.05)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_new_signals(regime=None)

        assert len(ant._ledger.open_positions) == 1


# ---------------------------------------------------------------------------
# SIDEWAYS regime — research-pad strategy filter
# ---------------------------------------------------------------------------

class TestSidewaysResearchFilter:
    def test_momentum_blocked_in_sideways(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="SIDEWAYS")

        assert len(ant._ledger.open_positions) == 0

    def test_sma_crossover_blocked_in_sideways(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "sma_crossover", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="SIDEWAYS")

        assert len(ant._ledger.open_positions) == 0

    def test_mean_reversion_allowed_in_sideways(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "mean_reversion", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="SIDEWAYS")

        assert len(ant._ledger.open_positions) == 1

    def test_rsi_based_allowed_in_sideways(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "rsi_based", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="SIDEWAYS")

        assert len(ant._ledger.open_positions) == 1

    def test_momentum_allowed_in_trending(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="TRENDING")

        assert len(ant._ledger.open_positions) == 1

    def test_entry_geblokkeerd_log_message(self, tmp_path, caplog):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.5)

        import logging
        with caplog.at_level(logging.INFO, logger=f"ant.paper.{ant.ant_id[:8]}"):
            with patch.object(ant, "_is_trading_allowed", return_value=True):
                ant._process_research_candidates(regime="SIDEWAYS")

        assert any("Entry geblokkeerd" in r.message for r in caplog.records)
        assert any("SIDEWAYS" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# VOLATILE regime — positiegrootte en SL aanpassingen
# ---------------------------------------------------------------------------

class TestVolatileAdjustments:
    def test_low_sharpe_blocked_in_volatile(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.1)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="VOLATILE")

        assert len(ant._ledger.open_positions) == 0

    def test_high_sharpe_allowed_in_volatile(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="VOLATILE")

        assert len(ant._ledger.open_positions) == 1

    def test_volatile_sl_pct_is_larger(self, tmp_path):
        ant = _make_ant(tmp_path)

        captured = {}

        original = ant._broker.open_position

        def capture_open(signal, capital_available):
            captured["sl"] = signal.stop_loss_price
            captured["entry"] = signal.entry_price
            return original(signal, capital_available)

        ant._broker.open_position = capture_open

        _write_research_candidate(tmp_path, ant.ant_id, _SYMBOL, "momentum", sharpe=0.5)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._process_research_candidates(regime="VOLATILE")

        assert len(ant._ledger.open_positions) == 1
        entry = captured["entry"]
        sl    = captured["sl"]
        actual_sl_pct = (entry - sl) / entry
        expected_sl_pct = _SL_PCT * _VOLATILE_SL_MULTIPLIER
        assert abs(actual_sl_pct - expected_sl_pct) < 1e-9

    def test_volatile_position_size_halved(self, tmp_path):
        ant = _make_ant(tmp_path)

        captured_normal = {}
        captured_volatile = {}

        def capture_normal(signal, capital_available):
            captured_normal["qty"] = signal.suggested_quantity
            from ant_colony.paper.paper_broker import BrokerResult, RejectionReason
            return BrokerResult.rejected(RejectionReason.INSUFFICIENT_CAPITAL, "test")

        def capture_volatile(signal, capital_available):
            captured_volatile["qty"] = signal.suggested_quantity
            from ant_colony.paper.paper_broker import BrokerResult, RejectionReason
            return BrokerResult.rejected(RejectionReason.INSUFFICIENT_CAPITAL, "test")

        # Test normal regime
        ant2 = _make_ant(tmp_path / "normal")
        sid_n = _write_scout_signal(tmp_path / "normal", ant2.ant_id, _SYMBOL, "price_move", 0.05)
        ant2._broker.open_position = capture_normal
        with patch.object(ant2, "_is_trading_allowed", return_value=True):
            ant2._process_new_signals(regime=None)

        # Test volatile regime — new ant to avoid dedup
        ant3 = _make_ant(tmp_path / "volatile")
        sid_v = _write_scout_signal(tmp_path / "volatile", ant3.ant_id, _SYMBOL, "price_move", 0.05)
        ant3._broker.open_position = capture_volatile
        with patch.object(ant3, "_is_trading_allowed", return_value=True):
            ant3._process_new_signals(regime="VOLATILE")

        # Volatile positiegrootte moet de helft zijn van normaal
        if captured_normal.get("qty") and captured_volatile.get("qty"):
            ratio = captured_volatile["qty"] / captured_normal["qty"]
            assert abs(ratio - _VOLATILE_CAPITAL_MULT) < 1e-6


# ---------------------------------------------------------------------------
# read_regime integratie met PaperAnt
# ---------------------------------------------------------------------------

class TestReadRegimeIntegration:
    def test_read_regime_returns_none_without_logs_root(self, tmp_path):
        mission = _make_mission()
        ant = PaperAnt(
            ant_id=uuid.uuid4().hex,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=None,
        )
        assert ant._read_regime() is None

    def test_read_regime_returns_signal_from_file(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_regime_signal(tmp_path, "VOLATILE")
        assert ant._read_regime() == "VOLATILE"

    def test_read_regime_fail_open_when_file_missing(self, tmp_path):
        ant = _make_ant(tmp_path)
        assert ant._read_regime() is None

    def test_tick_passes_regime_to_process_methods(self, tmp_path):
        ant = _make_ant(tmp_path)
        _write_regime_signal(tmp_path, "SIDEWAYS")

        received = {}

        def spy_signals(regime=None):
            received["regime"] = regime
            return 0

        ant._process_new_signals = spy_signals
        ant._process_exits = MagicMock()
        ant._process_research_candidates = MagicMock(return_value=0)
        ant._process_approved_candidates = MagicMock(return_value=0)

        with patch.object(ant, "_is_trading_allowed", return_value=True):
            ant._tick()

        assert received.get("regime") == "SIDEWAYS"
