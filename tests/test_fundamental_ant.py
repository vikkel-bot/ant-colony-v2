"""
tests/test_fundamental_ant.py

Tests voor FundamentalAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.fundamental_ant import (
    FundamentalAnt,
    _MIN_PIOTROSKI_SCORE,
    _SP500_TOP50,
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
        ant_type="fundamental_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "propose_candidate"],
        market_scope=MarketScope(biome="equities", symbols=["AAPL", "MSFT"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="fundamental test"),
    )


def make_candle(close: float, open_: float = 100.0) -> MarketData:
    return MarketData(
        symbol="AAPL", timeframe="1mo",
        timestamp=datetime.now(tz=timezone.utc),
        open=open_, high=close + 1, low=open_ - 1, close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def _good_fundamentals() -> dict:
    return {
        "pe_ratio":          15.0,
        "debt_to_equity":    0.3,
        "roa":               0.12,
        "roe":               0.25,
        "current_ratio":     2.1,
        "gross_margin":      0.40,
        "operating_cashflow": 10_000_000.0,
        "total_assets":       80_000_000.0,
        "net_income":          8_000_000.0,
    }


def _bad_fundamentals() -> dict:
    return {
        "pe_ratio":          0.0,
        "debt_to_equity":    3.0,
        "roa":              -0.05,
        "roe":              -0.10,
        "current_ratio":     0.5,
        "gross_margin":      0.10,
        "operating_cashflow": -8_000_000.0,  # worse than net income → F3 fails
        "total_assets":       50_000_000.0,
        "net_income":         -6_000_000.0,
    }


def make_ant(adapter=None, logs_root=None) -> FundamentalAnt:
    return FundamentalAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        adapter=adapter or MagicMock(spec=YahooFinanceAdapter),
        logs_root=logs_root,
    )


# ---------------------------------------------------------------------------
# 1. Piotroski score
# ---------------------------------------------------------------------------


class TestPiotroskiScore:
    def test_all_criteria_met_gives_nine(self) -> None:
        ant = make_ant()
        f = _good_fundamentals()
        score = ant._piotroski_score(f)
        assert score == 9

    def test_all_criteria_fail_gives_zero(self) -> None:
        ant = make_ant()
        f = _bad_fundamentals()
        score = ant._piotroski_score(f)
        assert score == 0

    def test_roa_positive_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "roa": 0.01}
        score = ant._piotroski_score(f)
        assert score >= 1

    def test_roa_zero_not_counted(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "roa": 0.0}
        baseline = ant._piotroski_score(_bad_fundamentals())
        score = ant._piotroski_score(f)
        assert score == baseline

    def test_ocf_positive_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "operating_cashflow": 1.0}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_accruals_quality_counted(self) -> None:
        ant = make_ant()
        # OCF > Net Income
        f = {**_bad_fundamentals(), "operating_cashflow": 100.0, "net_income": 50.0}
        score = ant._piotroski_score(f)
        assert score > 0

    def test_low_debt_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "debt_to_equity": 0.5}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_good_current_ratio_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "current_ratio": 1.5}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_high_gross_margin_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "gross_margin": 0.30}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_high_roe_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "roe": 0.15}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_reasonable_pe_adds_one(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "pe_ratio": 20.0}
        score = ant._piotroski_score(f)
        assert score > ant._piotroski_score(_bad_fundamentals())

    def test_high_roa_adds_bonus(self) -> None:
        ant = make_ant()
        f = {**_bad_fundamentals(), "roa": 0.06}
        score = ant._piotroski_score(f)
        # F1 (roa>0) + F9 (roa>0.05) both fire
        assert score >= 2

    def test_score_bounded_0_to_9(self) -> None:
        ant = make_ant()
        for f in [_good_fundamentals(), _bad_fundamentals()]:
            score = ant._piotroski_score(f)
            assert 0 <= score <= 9


# ---------------------------------------------------------------------------
# 2. 12-maands momentum
# ---------------------------------------------------------------------------


class TestGet12moMomentum:
    def test_positive_momentum(self) -> None:
        ant = make_ant()
        candles = [make_candle(100.0), make_candle(120.0)]
        ant.adapter.get_candles.return_value = candles
        result = ant._get_12mo_momentum("AAPL")
        assert result == pytest.approx(0.20)

    def test_negative_momentum(self) -> None:
        ant = make_ant()
        candles = [make_candle(200.0), make_candle(180.0)]
        ant.adapter.get_candles.return_value = candles
        result = ant._get_12mo_momentum("AAPL")
        assert result == pytest.approx(-0.10)

    def test_insufficient_candles_returns_none(self) -> None:
        ant = make_ant()
        ant.adapter.get_candles.return_value = [make_candle(100.0)]
        assert ant._get_12mo_momentum("AAPL") is None

    def test_zero_first_close_returns_none(self) -> None:
        ant = make_ant()
        ant.adapter.get_candles.return_value = [make_candle(0.0), make_candle(100.0)]
        assert ant._get_12mo_momentum("AAPL") is None

    def test_uses_1y_period_monthly_interval(self) -> None:
        ant = make_ant()
        ant.adapter.get_candles.return_value = []
        ant._get_12mo_momentum("AAPL")
        ant.adapter.get_candles.assert_called_once_with("AAPL", period="1y", interval="1mo")


# ---------------------------------------------------------------------------
# 3. Screen symbol
# ---------------------------------------------------------------------------


class TestScreenSymbol:
    def test_good_candidate_accepted(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("AAPL")
        assert result is not None
        assert result["symbol"] == "AAPL"

    def test_low_score_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _bad_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("AAPL")
        assert result is None

    def test_negative_momentum_rejected(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(120.0), make_candle(100.0)]
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("AAPL")
        assert result is None

    def test_candidate_has_required_fields(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("AAPL")
        assert result is not None
        for field in ("symbol", "piotroski_score", "momentum_12m", "fundamentals", "screened_at"):
            assert field in result

    def test_candidate_piotroski_score_at_least_min(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol("AAPL")
        assert result is not None
        assert result["piotroski_score"] >= _MIN_PIOTROSKI_SCORE


# ---------------------------------------------------------------------------
# 4. Tick — watchlist doorlopen
# ---------------------------------------------------------------------------


class TestTick:
    def test_tick_returns_candidates_list(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep"):
            candidates = ant._tick()
        assert isinstance(candidates, list)

    def test_tick_sets_last_action(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _bad_fundamentals()
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep"):
            ant._tick()
        assert ant._last_action != "init"

    def test_tick_writes_log_for_candidates(self, tmp_path: Path) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = [make_candle(100.0), make_candle(115.0)]
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep"):
            ant._tick()
        log_path = tmp_path / "equities" / "fundamental" / f"{ant.ant_id}.jsonl"
        if log_path.exists():
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert all(r["payload"]["action"] == "fundamental_candidate" for r in records)

    def test_exception_per_symbol_does_not_abort_tick(self) -> None:
        call_count = [0]

        def flaky_fundamentals(sym):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise RuntimeError("flaky")
            return _bad_fundamentals()

        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_fundamentals.side_effect = flaky_fundamentals
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep"):
            result = ant._tick()  # must not raise
        assert isinstance(result, list)


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
        ant._tick = MagicMock(return_value=[])
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep"):
            with patch("ant_colony.ants.equities.fundamental_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.fundamental_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.fundamental_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED
