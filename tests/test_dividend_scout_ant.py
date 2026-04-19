"""
tests/test_dividend_scout_ant.py

Tests voor DividendScoutAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.dividend_scout_ant import (
    DividendScoutAnt,
    _DIVIDEND_ARISTOCRATS,
    _MAX_PAYOUT,
    _MIN_YEARS,
    _MIN_YIELD,
    _VIX_THRESHOLD,
)
from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission() -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="dividend_scout_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["KO", "JNJ"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="dividend test"),
    )


def make_vix_candle(close: float) -> MarketData:
    return MarketData(
        symbol="^VIX", timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=close - 0.5, high=close + 1, low=close - 1, close=close,
        volume=0.0, biome_id="equities",
    )


def good_dividend_info() -> dict:
    return {
        "dividend_yield":    0.035,   # 3.5%
        "consecutive_years": 30,
        "payout_ratio":      0.55,
    }


def bad_dividend_info_low_yield() -> dict:
    return {
        "dividend_yield":    0.01,   # below 2%
        "consecutive_years": 30,
        "payout_ratio":      0.55,
    }


def bad_dividend_info_low_years() -> dict:
    return {
        "dividend_yield":    0.04,
        "consecutive_years": 10,     # below 25
        "payout_ratio":      0.55,
    }


def bad_dividend_info_high_payout() -> dict:
    return {
        "dividend_yield":    0.04,
        "consecutive_years": 30,
        "payout_ratio":      0.90,   # above 80%
    }


def make_ant(adapter=None, logs_root=None) -> DividendScoutAnt:
    return DividendScoutAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        adapter=adapter or MagicMock(spec=YahooFinanceAdapter),
        logs_root=logs_root,
    )


# ---------------------------------------------------------------------------
# 1. screen_symbol — kwaliteitsfilter
# ---------------------------------------------------------------------------


class TestScreenSymbol:
    def test_good_candidate_accepted(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = good_dividend_info()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("KO")
        assert result is not None
        assert result["symbol"] == "KO"

    def test_low_yield_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = bad_dividend_info_low_yield()
        ant = make_ant(adapter=adapter)
        assert ant._screen_symbol("KO") is None

    def test_insufficient_years_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = bad_dividend_info_low_years()
        ant = make_ant(adapter=adapter)
        assert ant._screen_symbol("KO") is None

    def test_high_payout_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = bad_dividend_info_high_payout()
        ant = make_ant(adapter=adapter)
        assert ant._screen_symbol("KO") is None

    def test_boundary_yield_exactly_min_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        info = {**good_dividend_info(), "dividend_yield": _MIN_YIELD}
        adapter.get_dividend_info.return_value = info
        ant = make_ant(adapter=adapter)
        assert ant._screen_symbol("KO") is None  # <= not <

    def test_candidate_has_required_fields(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = good_dividend_info()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("KO")
        assert result is not None
        for field in ("symbol", "dividend_yield", "consecutive_years", "payout_ratio", "screened_at"):
            assert field in result

    def test_candidate_values_correct(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        info = good_dividend_info()
        adapter.get_dividend_info.return_value = info
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("KO")
        assert result is not None
        assert result["dividend_yield"]   == pytest.approx(info["dividend_yield"])
        assert result["consecutive_years"] == info["consecutive_years"]
        assert result["payout_ratio"]     == pytest.approx(info["payout_ratio"])


# ---------------------------------------------------------------------------
# 2. VIX-check
# ---------------------------------------------------------------------------


class TestGetVix:
    def test_returns_vix_value(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.return_value = [make_vix_candle(18.5), make_vix_candle(20.3)]
        ant = make_ant(adapter=adapter)
        vix = ant._get_vix()
        assert vix == pytest.approx(20.3)

    def test_empty_candles_returns_none(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        assert ant._get_vix() is None

    def test_exception_returns_none(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.side_effect = RuntimeError("net")
        ant = make_ant(adapter=adapter)
        assert ant._get_vix() is None

    def test_vix_calls_correct_symbol(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.return_value = [make_vix_candle(15.0)]
        ant = make_ant(adapter=adapter)
        ant._get_vix()
        call_args = adapter.get_candles.call_args
        assert call_args[0][0] == "^VIX"


# ---------------------------------------------------------------------------
# 3. Tick — VIX hedge signaal
# ---------------------------------------------------------------------------


class TestTickVixSignal:
    def _make_adapter(self, vix: float, dividend_info: dict | None = None) -> MagicMock:
        adapter = MagicMock(spec=YahooFinanceAdapter)

        def get_candles(sym, **kwargs):
            if sym == "^VIX":
                return [make_vix_candle(vix)]
            return []

        adapter.get_candles.side_effect = get_candles
        adapter.get_dividend_info.return_value = dividend_info or bad_dividend_info_low_yield()
        return adapter

    def test_low_vix_gives_normal_signal(self) -> None:
        adapter = self._make_adapter(vix=15.0)
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            result = ant._tick()
        assert result["vix_signal"] == "NORMAL"

    def test_high_vix_gives_hedge_signal(self) -> None:
        adapter = self._make_adapter(vix=30.0)
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            result = ant._tick()
        assert result["vix_signal"] == "HEDGE"

    def test_vix_at_threshold_is_normal(self) -> None:
        adapter = self._make_adapter(vix=_VIX_THRESHOLD)
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            result = ant._tick()
        assert result["vix_signal"] == "NORMAL"

    def test_tick_returns_vix_value(self) -> None:
        adapter = self._make_adapter(vix=22.5)
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            result = ant._tick()
        assert result["vix"] == pytest.approx(22.5)

    def test_tick_returns_candidates_list(self) -> None:
        adapter = self._make_adapter(vix=15.0, dividend_info=good_dividend_info())
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            result = ant._tick()
        assert "candidates" in result
        assert isinstance(result["candidates"], list)


# ---------------------------------------------------------------------------
# 4. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def _make_adapter_with_good_info(self, vix: float = 15.0) -> MagicMock:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_dividend_info.return_value = good_dividend_info()

        def get_candles(sym, **kwargs):
            if sym == "^VIX":
                return [make_vix_candle(vix)]
            return []

        adapter.get_candles.side_effect = get_candles
        return adapter

    def test_log_file_created_for_candidate(self, tmp_path: Path) -> None:
        adapter = self._make_adapter_with_good_info()
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            ant._tick()
        log_path = tmp_path / "equities" / "dividend" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_hedge_flag(self, tmp_path: Path) -> None:
        adapter = self._make_adapter_with_good_info(vix=30.0)
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            ant._tick()
        log_path = tmp_path / "equities" / "dividend" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert any(r["payload"]["hedge_active"] is True for r in records)

    def test_log_action_is_dividend_candidate(self, tmp_path: Path) -> None:
        adapter = self._make_adapter_with_good_info()
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            ant._tick()
        log_path = tmp_path / "equities" / "dividend" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert all(r["payload"]["action"] == "dividend_candidate" for r in records)

    def test_no_log_when_logs_root_none(self) -> None:
        adapter = self._make_adapter_with_good_info()
        ant = make_ant(adapter=adapter, logs_root=None)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            ant._tick()  # must not crash

    def test_sequence_increments_across_candidates(self, tmp_path: Path) -> None:
        adapter = self._make_adapter_with_good_info()
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            ant._tick()
        log_path = tmp_path / "equities" / "dividend" / f"{ant.ant_id}.jsonl"
        if log_path.exists():
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            seqs = [r["sequence"] for r in records]
            assert seqs == list(range(len(seqs)))


# ---------------------------------------------------------------------------
# 5. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()


# ---------------------------------------------------------------------------
# 6. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value={"candidates": [], "vix": 15.0, "vix_signal": "NORMAL"})
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep"):
            with patch("ant_colony.ants.equities.dividend_scout_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.dividend_scout_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.dividend_scout_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED


# ---------------------------------------------------------------------------
# 7. Dividend Aristocrats lijst
# ---------------------------------------------------------------------------


class TestDividendAristocratsList:
    def test_list_not_empty(self) -> None:
        assert len(_DIVIDEND_ARISTOCRATS) > 0

    def test_known_aristocrats_present(self) -> None:
        for symbol in ("KO", "JNJ", "PG", "MMM"):
            assert symbol in _DIVIDEND_ARISTOCRATS, f"{symbol} missing from aristocrats"

    def test_no_duplicate_symbols(self) -> None:
        assert len(_DIVIDEND_ARISTOCRATS) == len(set(_DIVIDEND_ARISTOCRATS))
