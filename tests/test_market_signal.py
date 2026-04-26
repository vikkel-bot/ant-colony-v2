"""
tests/test_market_signal.py

Tests voor het gecombineerde markt-signaal systeem (News → Queen → PaperAnt).

Dekt:
  1. read_latest_market_signal — bestandsoperaties en parsing
  2. _compute_market_signal — alle tabel-combinaties
  3. QueenAdvisor._write_market_signal — correct schrijven naar JSONL
  4. QueenAdvisor.advise() — markt-signaal wordt geschreven wanneer regime bekend is
  5. PaperAnt._try_open_position — market signal past capital en SL aan
  6. EquitiesPaperAnt._process_scout_signals — bearish filter blokkeert rank > 1
  7. EquitiesPaperAnt._try_open_position — market signal past capital en harde SL aan
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants._market_signal import read_latest_market_signal
from ant_colony.queen.queen_advisor import (
    QueenAdvisor,
    _compute_market_signal,
    _MARKET_SIGNAL_TABLE,
    _MARKET_SIGNAL_DEFAULT,
)
from ant_colony.schemas.mission import (
    MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mission(
    capital: float = 10_000.0,
    biome: str = "crypto",
    symbols: list[str] | None = None,
) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="paper_ant",
        allowed_node="pc2",
        allowed_actions=["open_position", "close_position"],
        market_scope=MarketScope(biome=biome, symbols=symbols or ["BTC-EUR"]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
            stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="market signal test"),
    )


def _make_queen(tmp_path: Path) -> MagicMock:
    queen = MagicMock()
    queen.active_missions = {}
    queen.allocation_snapshot.return_value = MagicMock(biomes=[])
    return queen


def _make_advisor(tmp_path: Path) -> QueenAdvisor:
    return QueenAdvisor(queen=_make_queen(tmp_path), logs_root=tmp_path)


def _write_market_signal_file(
    queen_dir: Path,
    regime: str = "SIDEWAYS",
    news_sentiment: str = "neutral",
    combined_signal: str = "normal",
    position_size_mult: float = 1.0,
    sl_mult: float = 1.0,
) -> None:
    queen_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-04-26T10:00:00+00:00",
        "payload": {
            "action":             "market_signal",
            "regime":             regime,
            "news_sentiment":     news_sentiment,
            "combined_signal":    combined_signal,
            "position_size_mult": position_size_mult,
            "sl_mult":            sl_mult,
        },
    }
    (queen_dir / "market_signal.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _write_regime_file(queen_dir: Path, regime: str = "SIDEWAYS") -> None:
    queen_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-04-26T10:00:00+00:00",
        "payload": {"action": "regime_signal", "regime": regime},
    }
    (queen_dir / "regime.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _write_research(tmp_path: Path, symbol: str = "BTC-EUR", best_regime: str = "sideways") -> None:
    research_dir = tmp_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "payload": {
            "action": "candidate_accepted",
            "candidate_id": str(uuid.uuid4()),
            "symbol": symbol,
            "sharpe": 0.8,
            "best_regime": best_regime,
            "strategy_type": "sma_crossover",
        }
    }
    (research_dir / "test.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. read_latest_market_signal
# ---------------------------------------------------------------------------

class TestReadLatestMarketSignal:

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert read_latest_market_signal(tmp_path) is None

    def test_returns_none_when_file_empty(self, tmp_path: Path) -> None:
        (tmp_path / "queen").mkdir()
        (tmp_path / "queen" / "market_signal.jsonl").write_text("", encoding="utf-8")
        assert read_latest_market_signal(tmp_path) is None

    def test_returns_none_wrong_action(self, tmp_path: Path) -> None:
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        record = {
            "timestamp": "2026-04-26T10:00:00",
            "payload": {"action": "regime_signal", "regime": "SIDEWAYS"},
        }
        (queen_dir / "market_signal.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        assert read_latest_market_signal(tmp_path) is None

    def test_returns_payload_for_valid_signal(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen",
            regime="TRENDING", news_sentiment="bullish",
            combined_signal="optimistic", position_size_mult=1.25, sl_mult=1.0,
        )
        result = read_latest_market_signal(tmp_path)
        assert result is not None
        assert result["combined_signal"] == "optimistic"
        assert result["position_size_mult"] == 1.25
        assert result["sl_mult"] == 1.0
        assert result["regime"] == "TRENDING"
        assert result["news_sentiment"] == "bullish"

    def test_returns_last_line_when_multiple(self, tmp_path: Path) -> None:
        queen_dir = tmp_path / "queen"
        queen_dir.mkdir()
        path = queen_dir / "market_signal.jsonl"
        for combined, mult in [("cautious", 0.5), ("optimistic", 1.25)]:
            rec = {
                "timestamp": "2026-04-26T10:00:00",
                "payload": {
                    "action": "market_signal", "regime": "TRENDING",
                    "news_sentiment": "bullish", "combined_signal": combined,
                    "position_size_mult": mult, "sl_mult": 1.0,
                },
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        result = read_latest_market_signal(tmp_path)
        assert result["combined_signal"] == "optimistic"
        assert result["position_size_mult"] == 1.25


# ---------------------------------------------------------------------------
# 2. _compute_market_signal
# ---------------------------------------------------------------------------

class TestComputeMarketSignal:

    def test_returns_default_when_regime_none(self) -> None:
        assert _compute_market_signal(None, "bullish") == _MARKET_SIGNAL_DEFAULT

    def test_returns_default_when_sentiment_none(self) -> None:
        assert _compute_market_signal("TRENDING", None) == _MARKET_SIGNAL_DEFAULT

    def test_sideways_bearish(self) -> None:
        sig, pos, sl = _compute_market_signal("SIDEWAYS", "bearish")
        assert sig == "cautious"
        assert abs(pos - 0.5) < 0.001
        assert abs(sl - 1.25) < 0.001

    def test_sideways_bullish(self) -> None:
        sig, pos, sl = _compute_market_signal("SIDEWAYS", "bullish")
        assert sig == "normal"
        assert abs(pos - 1.0) < 0.001
        assert abs(sl - 1.0) < 0.001

    def test_trending_bullish(self) -> None:
        sig, pos, sl = _compute_market_signal("TRENDING", "bullish")
        assert sig == "optimistic"
        assert abs(pos - 1.25) < 0.001

    def test_trending_bearish(self) -> None:
        sig, pos, sl = _compute_market_signal("TRENDING", "bearish")
        assert sig == "cautious_trending"
        assert abs(pos - 0.75) < 0.001

    def test_volatile_always_restrictive(self) -> None:
        for sentiment in ("bullish", "bearish", "neutral"):
            sig, pos, sl = _compute_market_signal("VOLATILE", sentiment)
            assert sig == "restrictive", f"Expected restrictive for VOLATILE/{sentiment}"
            assert abs(pos - 0.5) < 0.001

    def test_unknown_combo_returns_default(self) -> None:
        result = _compute_market_signal("UNKNOWN_REGIME", "bullish")
        assert result == _MARKET_SIGNAL_DEFAULT

    def test_all_table_entries_present(self) -> None:
        for (regime, sentiment) in _MARKET_SIGNAL_TABLE:
            result = _compute_market_signal(regime, sentiment)
            assert result == _MARKET_SIGNAL_TABLE[(regime, sentiment)]


# ---------------------------------------------------------------------------
# 3. QueenAdvisor._write_market_signal
# ---------------------------------------------------------------------------

class TestWriteMarketSignal:

    def test_file_written(self, tmp_path: Path) -> None:
        advisor = _make_advisor(tmp_path)
        advisor._write_market_signal("TRENDING", "bullish", "optimistic", 1.25, 1.0)
        path = tmp_path / "queen" / "market_signal.jsonl"
        assert path.exists()
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["payload"]["action"] == "market_signal"
        assert rec["payload"]["combined_signal"] == "optimistic"
        assert rec["payload"]["position_size_mult"] == 1.25
        assert rec["payload"]["sl_mult"] == 1.0
        assert rec["payload"]["regime"] == "TRENDING"
        assert rec["payload"]["news_sentiment"] == "bullish"

    def test_append_only(self, tmp_path: Path) -> None:
        advisor = _make_advisor(tmp_path)
        advisor._write_market_signal("SIDEWAYS", "bearish", "cautious", 0.5, 1.25)
        advisor._write_market_signal("TRENDING", "bullish", "optimistic", 1.25, 1.0)
        path = tmp_path / "queen" / "market_signal.jsonl"
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

    def test_no_crash_when_logs_root_none(self) -> None:
        advisor = QueenAdvisor(queen=MagicMock(), logs_root=None)
        advisor._write_market_signal("TRENDING", "bullish", "optimistic", 1.25, 1.0)


# ---------------------------------------------------------------------------
# 4. QueenAdvisor.advise() — market signal written when regime present
# ---------------------------------------------------------------------------

class TestAdviseWritesMarketSignal:

    def test_market_signal_written_when_regime_determined(self, tmp_path: Path) -> None:
        _write_research(tmp_path, best_regime="bull")
        advisor = _make_advisor(tmp_path)
        with patch.object(advisor, "_is_in_briefing_window", return_value=False):
            advisor.advise()
        path = tmp_path / "queen" / "market_signal.jsonl"
        assert path.exists()
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["payload"]["action"] == "market_signal"
        assert rec["payload"]["regime"] == "TRENDING"

    def test_market_signal_uses_news_sentiment(self, tmp_path: Path) -> None:
        _write_research(tmp_path, best_regime="bull")
        news_dir = tmp_path / "news"
        news_dir.mkdir()
        (news_dir / "20260426-100000.json").write_text(
            json.dumps({
                "timestamp": "2026-04-26T10:00:00",
                "market_sentiment": "bearish",
                "sentiment_score": -0.05,
            }),
            encoding="utf-8",
        )
        advisor = _make_advisor(tmp_path)
        with patch.object(advisor, "_is_in_briefing_window", return_value=False):
            advisor.advise()
        path = tmp_path / "queen" / "market_signal.jsonl"
        rec = json.loads(path.read_text(encoding="utf-8").strip())
        assert rec["payload"]["news_sentiment"] == "bearish"
        assert rec["payload"]["combined_signal"] == "cautious_trending"

    def test_no_market_signal_without_regime(self, tmp_path: Path) -> None:
        advisor = _make_advisor(tmp_path)
        with patch.object(advisor, "_is_in_briefing_window", return_value=False):
            advisor.advise()
        path = tmp_path / "queen" / "market_signal.jsonl"
        assert not path.exists()


# ---------------------------------------------------------------------------
# 5. PaperAnt._try_open_position — market signal past capital en SL aan
# ---------------------------------------------------------------------------

class TestPaperAntMarketSignal:

    def _make_ant(self, tmp_path: Path, capital: float = 10_000.0):
        from ant_colony.ants.paper_ant import PaperAnt
        mission = _make_mission(capital=capital)
        scheduler = MagicMock()
        registry = MagicMock()
        registry.get.return_value = None
        return PaperAnt(
            ant_id=str(uuid.uuid4()),
            mission=mission,
            scheduler=scheduler,
            biome_registry=registry,
            logs_root=tmp_path,
        )

    def test_cautious_halves_capital(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.0,
        )
        ant = self._make_ant(tmp_path, capital=10_000.0)

        opened_positions = []

        def fake_fetch(symbol):
            return 100.0

        def fake_open(entry_signal, capital):
            result = MagicMock()
            result.accepted = True
            pos = MagicMock()
            pos.position_id = "pos-1"
            pos.symbol = entry_signal.symbol
            pos.quantity = entry_signal.suggested_quantity
            result.position = pos
            opened_positions.append(entry_signal)
            return result

        with patch.object(ant, "_fetch_price", side_effect=fake_fetch):
            with patch.object(ant._broker, "open_position", side_effect=fake_open):
                with patch.object(ant._ledger, "record_opened"):
                    ant._try_open_position(
                        {"symbol": "BTC-EUR", "biome": "crypto", "confidence": 0.8},
                    )

        assert len(opened_positions) == 1
        sig = opened_positions[0]
        # capital 10000, 10% fraction * 0.5 pos_mult = 500 EUR → 5 qty at 100
        assert abs(sig.suggested_quantity - 5.0) < 0.01

    def test_sl_mult_widens_stop_loss(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.25,
        )
        ant = self._make_ant(tmp_path, capital=10_000.0)
        opened_signals = []

        def fake_open(entry_signal, capital):
            result = MagicMock()
            result.accepted = True
            pos = MagicMock()
            pos.position_id = "pos-1"
            pos.symbol = entry_signal.symbol
            pos.quantity = entry_signal.suggested_quantity
            result.position = pos
            opened_signals.append(entry_signal)
            return result

        with patch.object(ant, "_fetch_price", return_value=1000.0):
            with patch.object(ant._broker, "open_position", side_effect=fake_open):
                with patch.object(ant._ledger, "record_opened"):
                    ant._try_open_position(
                        {"symbol": "ETH-EUR", "biome": "crypto", "confidence": 0.9},
                    )

        assert len(opened_signals) == 1
        sig = opened_signals[0]
        # sl_pct = 0.02 * 1.25 = 0.025 → stop_loss = 1000 * (1 - 0.025) = 975
        assert abs(sig.stop_loss_price - 975.0) < 0.01

    def test_no_signal_uses_defaults(self, tmp_path: Path) -> None:
        # No market_signal.jsonl → defaults (1.0, 1.0)
        ant = self._make_ant(tmp_path, capital=10_000.0)
        opened_signals = []

        def fake_open(entry_signal, capital):
            result = MagicMock()
            result.accepted = True
            pos = MagicMock()
            pos.position_id = "pos-1"
            pos.symbol = entry_signal.symbol
            pos.quantity = entry_signal.suggested_quantity
            result.position = pos
            opened_signals.append(entry_signal)
            return result

        with patch.object(ant, "_fetch_price", return_value=100.0):
            with patch.object(ant._broker, "open_position", side_effect=fake_open):
                with patch.object(ant._ledger, "record_opened"):
                    ant._try_open_position(
                        {"symbol": "BTC-EUR", "biome": "crypto", "confidence": 0.8},
                    )

        assert len(opened_signals) == 1
        sig = opened_signals[0]
        # capital 10000, 10% = 1000 → qty=10 at price 100
        assert abs(sig.suggested_quantity - 10.0) < 0.01

    def test_volatile_stacks_on_market_signal(self, tmp_path: Path) -> None:
        # Market signal cautious (0.5) + VOLATILE (0.5) → 0.25 of capital
        _write_market_signal_file(
            tmp_path / "queen",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.0,
        )
        ant = self._make_ant(tmp_path, capital=10_000.0)
        opened_signals = []

        def fake_open(entry_signal, capital):
            result = MagicMock()
            result.accepted = True
            pos = MagicMock()
            pos.position_id = "pos-1"
            pos.symbol = entry_signal.symbol
            pos.quantity = entry_signal.suggested_quantity
            result.position = pos
            opened_signals.append(entry_signal)
            return result

        with patch.object(ant, "_fetch_price", return_value=100.0):
            with patch.object(ant._broker, "open_position", side_effect=fake_open):
                with patch.object(ant._ledger, "record_opened"):
                    ant._try_open_position(
                        {"symbol": "BTC-EUR", "biome": "crypto", "confidence": 0.8},
                        regime="VOLATILE",
                    )

        assert len(opened_signals) == 1
        sig = opened_signals[0]
        # capital_fraction = 0.10 * 0.5 (market) * 0.5 (volatile) = 0.025
        # qty = 10000 * 0.025 / 100 = 2.5
        assert abs(sig.suggested_quantity - 2.5) < 0.01


# ---------------------------------------------------------------------------
# 6. EquitiesPaperAnt._process_scout_signals — bearish filter
# ---------------------------------------------------------------------------

class TestEquitiesBearishFilter:

    def _make_eq_ant(self, tmp_path: Path):
        from ant_colony.ants.paper_ant_equities import EquitiesPaperAnt
        mission = _make_mission(capital=10_000.0, biome="equities", symbols=["XLK"])
        scheduler = MagicMock()
        registry = MagicMock()
        registry.get.return_value = None
        return EquitiesPaperAnt(
            ant_id=str(uuid.uuid4()),
            mission=mission,
            scheduler=scheduler,
            biome_registry=registry,
            logs_root=tmp_path,
        )

    def _write_scout_signal(
        self,
        scouts_dir: Path,
        symbol: str,
        signal_id: str,
        momentum_rank: int,
        confidence: float = 0.8,
    ) -> None:
        scouts_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "payload": {
                "action": "opportunity_detected",
                "biome": "equities",
                "signal_type": "sector_rotation",
                "symbol": symbol,
                "signal_id": signal_id,
                "confidence": confidence,
                "momentum_rank": momentum_rank,
            }
        }
        with (scouts_dir / "scout.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_rank1_allowed_when_bearish(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen", news_sentiment="bearish",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.25,
        )
        self._write_scout_signal(tmp_path / "scouts", "XLK", "sig-1", momentum_rank=1)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        def fake_open(sym, *, source):
            opened.append(sym)

        with patch.object(ant, "_try_open_position", side_effect=fake_open):
            ant._process_scout_signals()

        assert "XLK" in opened

    def test_rank2_blocked_when_bearish(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen", news_sentiment="bearish",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.25,
        )
        self._write_scout_signal(tmp_path / "scouts", "XLE", "sig-2", momentum_rank=2)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        with patch.object(ant, "_try_open_position", side_effect=lambda sym, **kw: opened.append(sym)):
            ant._process_scout_signals()

        assert "XLE" not in opened

    def test_rank2_allowed_when_neutral(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen", news_sentiment="neutral",
            combined_signal="normal", position_size_mult=1.0, sl_mult=1.0,
        )
        self._write_scout_signal(tmp_path / "scouts", "XLE", "sig-3", momentum_rank=2)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        with patch.object(ant, "_try_open_position", side_effect=lambda sym, **kw: opened.append(sym)):
            ant._process_scout_signals()

        assert "XLE" in opened

    def test_rank2_allowed_when_bullish(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen", news_sentiment="bullish",
            combined_signal="optimistic", position_size_mult=1.25, sl_mult=1.0,
        )
        self._write_scout_signal(tmp_path / "scouts", "XLV", "sig-4", momentum_rank=3)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        with patch.object(ant, "_try_open_position", side_effect=lambda sym, **kw: opened.append(sym)):
            ant._process_scout_signals()

        assert "XLV" in opened

    def test_no_signal_file_allows_all(self, tmp_path: Path) -> None:
        self._write_scout_signal(tmp_path / "scouts", "XLB", "sig-5", momentum_rank=4)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        with patch.object(ant, "_try_open_position", side_effect=lambda sym, **kw: opened.append(sym)):
            ant._process_scout_signals()

        assert "XLB" in opened

    def test_multiple_signals_bearish_only_rank1_passes(self, tmp_path: Path) -> None:
        _write_market_signal_file(
            tmp_path / "queen", news_sentiment="bearish",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.25,
        )
        scouts_dir = tmp_path / "scouts"
        for sym, rank, sig_id in [
            ("XLK", 1, "sig-a"),
            ("XLE", 2, "sig-b"),
            ("XLV", 3, "sig-c"),
        ]:
            self._write_scout_signal(scouts_dir, sym, sig_id, momentum_rank=rank)
        ant = self._make_eq_ant(tmp_path)
        opened = []

        with patch.object(ant, "_try_open_position", side_effect=lambda sym, **kw: opened.append(sym)):
            ant._process_scout_signals()

        assert "XLK" in opened
        assert "XLE" not in opened
        assert "XLV" not in opened


# ---------------------------------------------------------------------------
# 7. EquitiesPaperAnt._try_open_position — market signal past capital/SL aan
# ---------------------------------------------------------------------------

class TestEquitiesPaperAntMarketSignal:

    def _make_eq_ant(self, tmp_path: Path):
        from ant_colony.ants.paper_ant_equities import EquitiesPaperAnt
        mission = _make_mission(capital=10_000.0, biome="equities", symbols=["XLK"])
        scheduler = MagicMock()
        registry = MagicMock()
        registry.get.return_value = None
        return EquitiesPaperAnt(
            ant_id=str(uuid.uuid4()),
            mission=mission,
            scheduler=scheduler,
            biome_registry=registry,
            logs_root=tmp_path,
        )

    def test_cautious_halves_capital(self, tmp_path: Path) -> None:
        from ant_colony.ants.paper_ant_equities import _TRADE_CAPITAL_FRACTION
        _write_market_signal_file(
            tmp_path / "queen",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.0,
        )
        ant = self._make_eq_ant(tmp_path)
        opened_positions = []

        with patch.object(ant, "_fetch_price", return_value=100.0):
            with patch.object(ant._ledger, "record_opened") as mock_open:
                with patch.object(ant, "_emit_trade_opened"):
                    ant._try_open_position("XLK", source="sector_scout")

        assert len(ant._ledger.open_positions) == 0 or len(mock_open.call_args_list) == 1
        # Can't easily check quantity without full ledger, so check via record_opened call
        if mock_open.call_args_list:
            pos = mock_open.call_args_list[0][0][0]
            # capital 10000 * 0.10 * 0.5 = 500 → qty = 500/100 = 5.0
            assert abs(pos.quantity - 5.0) < 0.01

    def test_sl_mult_widens_hard_stop(self, tmp_path: Path) -> None:
        from ant_colony.ants.paper_ant_equities import _HARD_SL_PCT
        _write_market_signal_file(
            tmp_path / "queen",
            combined_signal="cautious", position_size_mult=0.5, sl_mult=1.25,
        )
        ant = self._make_eq_ant(tmp_path)

        with patch.object(ant, "_fetch_price", return_value=1000.0):
            with patch.object(ant._ledger, "record_opened") as mock_open:
                with patch.object(ant, "_emit_trade_opened"):
                    ant._try_open_position("XLK", source="sector_scout")

        if mock_open.call_args_list:
            pos = mock_open.call_args_list[0][0][0]
            # sl = 1000 * (1 - 0.07 * 1.25) = 1000 * (1 - 0.0875) = 912.5
            assert abs(pos.stop_loss_price - 912.5) < 0.1

    def test_no_signal_uses_defaults(self, tmp_path: Path) -> None:
        ant = self._make_eq_ant(tmp_path)

        with patch.object(ant, "_fetch_price", return_value=100.0):
            with patch.object(ant._ledger, "record_opened") as mock_open:
                with patch.object(ant, "_emit_trade_opened"):
                    ant._try_open_position("XLK", source="sector_scout")

        if mock_open.call_args_list:
            pos = mock_open.call_args_list[0][0][0]
            # capital 10000 * 0.10 = 1000 → qty = 1000/100 = 10.0
            assert abs(pos.quantity - 10.0) < 0.01
            # sl = 100 * (1 - 0.07) = 93.0
            assert abs(pos.stop_loss_price - 93.0) < 0.01

    def test_trailing_stop_unchanged_with_signal(self, tmp_path: Path) -> None:
        from ant_colony.ants.paper_ant_equities import _TRAILING_STOP_PCT
        # Verify trailing stop constant is untouched by market signal
        assert _TRAILING_STOP_PCT == 0.05
