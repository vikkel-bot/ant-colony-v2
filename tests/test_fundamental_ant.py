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
    _TP_PCT,
    _SL_PCT,
)
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission(symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="fundamental_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "propose_candidate"],
        market_scope=MarketScope(biome="equities", symbols=symbols or ["AAPL", "MSFT"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="fundamental test"),
    )


def make_candle(close: float, open_: float = 100.0, high: float | None = None) -> MarketData:
    return MarketData(
        symbol="AAPL", timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=open_, high=high if high is not None else (close + 1),
        low=max(open_ - 1, 0.01), close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def _good_fundamentals() -> dict:
    return {
        "pe_ratio":           15.0,
        "debt_to_equity":     0.3,
        "roa":                0.12,
        "roe":                0.25,
        "current_ratio":      2.1,
        "gross_margin":       0.40,
        "operating_cashflow": 10_000_000.0,
        "total_assets":       80_000_000.0,
        "net_income":          8_000_000.0,
    }


def _bad_fundamentals() -> dict:
    return {
        "pe_ratio":           0.0,
        "debt_to_equity":     3.0,
        "roa":               -0.05,
        "roe":               -0.10,
        "current_ratio":      0.5,
        "gross_margin":       0.10,
        "operating_cashflow": -8_000_000.0,
        "total_assets":       50_000_000.0,
        "net_income":         -6_000_000.0,
    }


def _breakout_candles() -> list[MarketData]:
    """Candles with positive momentum AND near-52w-high (within 5%)."""
    # first: 100, last: 115 → momentum +15%
    # high of last candle = 116, (116-115)/116 = 0.86% < 5% → breakout
    return [make_candle(100.0, open_=99.0), make_candle(115.0, open_=100.0, high=116.0)]


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None, symbols=None) -> FundamentalAnt:
    if adapter is None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
    return FundamentalAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(symbols=symbols),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
    )


# ---------------------------------------------------------------------------
# 1. Piotroski F-Score (staticmethod)
# ---------------------------------------------------------------------------


class TestPiotroskiScore:
    def test_all_criteria_met_gives_nine(self) -> None:
        assert FundamentalAnt.piotroski_score(_good_fundamentals()) == 9

    def test_all_criteria_fail_gives_zero(self) -> None:
        assert FundamentalAnt.piotroski_score(_bad_fundamentals()) == 0

    def test_f1_roa_positive(self) -> None:
        f = {**_bad_fundamentals(), "roa": 0.01}
        assert FundamentalAnt.piotroski_score(f) >= 1

    def test_f1_roa_zero_not_counted(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "roa": 0.0}
        assert FundamentalAnt.piotroski_score(f) == baseline

    def test_f2_ocf_positive(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "operating_cashflow": 1.0}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f3_accruals_ocf_gt_ni(self) -> None:
        f = {**_bad_fundamentals(), "operating_cashflow": 100.0, "net_income": 50.0}
        # F2 and F3 both fire
        assert FundamentalAnt.piotroski_score(f) >= 2

    def test_f4_low_debt_to_equity(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "debt_to_equity": 0.5}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f4_dte_just_above_zero_counts(self) -> None:
        # 0.0 is treated as missing data by the `or 999.0` guard; use a small positive value
        f = {**_bad_fundamentals(), "debt_to_equity": 0.01}
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f5_current_ratio_gt_one(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "current_ratio": 1.5}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f6_gross_margin_gt_25pct(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "gross_margin": 0.30}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f7_roe_gt_10pct(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "roe": 0.15}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f8_reasonable_pe(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "pe_ratio": 20.0}
        assert FundamentalAnt.piotroski_score(f) > baseline

    def test_f8_pe_above_30_not_counted(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "pe_ratio": 35.0}
        assert FundamentalAnt.piotroski_score(f) == baseline

    def test_f8_pe_negative_not_counted(self) -> None:
        baseline = FundamentalAnt.piotroski_score(_bad_fundamentals())
        f = {**_bad_fundamentals(), "pe_ratio": -5.0}
        assert FundamentalAnt.piotroski_score(f) == baseline

    def test_f9_roa_gt_5pct_gives_two_points(self) -> None:
        f = {**_bad_fundamentals(), "roa": 0.06}
        # F1 (roa>0) + F9 (roa>0.05) both fire
        assert FundamentalAnt.piotroski_score(f) >= 2

    def test_score_bounded_0_to_9(self) -> None:
        for f in [_good_fundamentals(), _bad_fundamentals()]:
            score = FundamentalAnt.piotroski_score(f)
            assert 0 <= score <= 9

    def test_none_values_treated_as_zero(self) -> None:
        f = {k: None for k in _good_fundamentals()}
        score = FundamentalAnt.piotroski_score(f)
        assert 0 <= score <= 9


# ---------------------------------------------------------------------------
# 2. 12-maands momentum (staticmethod)
# ---------------------------------------------------------------------------


class TestGet12moMomentum:
    def test_positive_momentum(self) -> None:
        candles = [make_candle(100.0), make_candle(120.0)]
        result = FundamentalAnt._get_12mo_momentum("AAPL", lambda *a, **k: candles)
        assert result == pytest.approx(0.20)

    def test_negative_momentum(self) -> None:
        candles = [make_candle(200.0), make_candle(180.0)]
        result = FundamentalAnt._get_12mo_momentum("AAPL", lambda *a, **k: candles)
        assert result == pytest.approx(-0.10)

    def test_insufficient_candles_returns_none(self) -> None:
        result = FundamentalAnt._get_12mo_momentum(
            "AAPL", lambda *a, **k: [make_candle(100.0)]
        )
        assert result is None

    def test_empty_candles_returns_none(self) -> None:
        result = FundamentalAnt._get_12mo_momentum("AAPL", lambda *a, **k: [])
        assert result is None

    def test_zero_first_close_returns_none(self) -> None:
        result = FundamentalAnt._get_12mo_momentum(
            "AAPL", lambda *a, **k: [make_candle(0.0), make_candle(100.0)]
        )
        assert result is None

    def test_uses_1y_period_monthly_interval(self) -> None:
        calls = []

        def recording_fn(sym, period="1y", interval="1mo"):
            calls.append((sym, period, interval))
            return []

        FundamentalAnt._get_12mo_momentum("AAPL", recording_fn)
        assert calls[0] == ("AAPL", "1y", "1mo")

    def test_exception_returns_none(self) -> None:
        result = FundamentalAnt._get_12mo_momentum(
            "AAPL", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api"))
        )
        assert result is None


# ---------------------------------------------------------------------------
# 3. 52-weeks high breakout (staticmethod)
# ---------------------------------------------------------------------------


class TestAt52wHighBreakout:
    def test_price_near_high_is_breakout(self) -> None:
        # current=115, 52w high=116 → (116-115)/116 = 0.86% < 5%
        candles = [make_candle(100.0), make_candle(115.0, high=116.0)]
        result = FundamentalAnt._at_52w_high_breakout("AAPL", lambda *a, **k: candles)
        assert result is True

    def test_price_far_from_high_is_not_breakout(self) -> None:
        # current=90, 52w high=120 → (120-90)/120 = 25% > 5%
        candles = [make_candle(120.0, high=121.0), make_candle(90.0)]
        result = FundamentalAnt._at_52w_high_breakout("AAPL", lambda *a, **k: candles)
        assert result is False

    def test_price_at_exact_high_is_breakout(self) -> None:
        candles = [make_candle(100.0), make_candle(120.0, high=120.0)]
        result = FundamentalAnt._at_52w_high_breakout("AAPL", lambda *a, **k: candles)
        assert result is True

    def test_empty_candles_returns_false(self) -> None:
        result = FundamentalAnt._at_52w_high_breakout("AAPL", lambda *a, **k: [])
        assert result is False

    def test_single_candle_returns_false(self) -> None:
        result = FundamentalAnt._at_52w_high_breakout(
            "AAPL", lambda *a, **k: [make_candle(100.0)]
        )
        assert result is False

    def test_exception_returns_false(self) -> None:
        result = FundamentalAnt._at_52w_high_breakout(
            "AAPL", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api"))
        )
        assert result is False


# ---------------------------------------------------------------------------
# 4. Screen symbol
# ---------------------------------------------------------------------------


class TestScreenSymbol:
    def _make_adapter(self, fundamentals=None, candles=None):
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = fundamentals or _good_fundamentals()
        adapter.get_candles.return_value = candles if candles is not None else _breakout_candles()
        return adapter

    def test_good_candidate_accepted(self) -> None:
        adapter = self._make_adapter()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is not None
        assert result["symbol"] == "AAPL"

    def test_low_f_score_rejected(self) -> None:
        adapter = self._make_adapter(fundamentals=_bad_fundamentals())
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is None

    def test_negative_momentum_rejected(self) -> None:
        falling = [make_candle(120.0), make_candle(100.0)]
        adapter = self._make_adapter(candles=falling)
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is None

    def test_not_near_breakout_rejected(self) -> None:
        # positive momentum but far from 52w high
        far = [make_candle(80.0), make_candle(100.0, high=101.0)]
        # inject a higher high by modifying: create candle with high=200
        far_high = [make_candle(80.0, high=200.0), make_candle(100.0, high=101.0)]
        adapter = self._make_adapter(candles=far_high)
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is None

    def test_candidate_has_required_fields(self) -> None:
        adapter = self._make_adapter()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is not None
        for field in ("candidate_id", "symbol", "f_score", "momentum_12m",
                      "fundamentals", "screened_at"):
            assert field in result

    def test_f_score_at_least_minimum(self) -> None:
        adapter = self._make_adapter()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is not None
        assert result["f_score"] >= _MIN_PIOTROSKI_SCORE

    def test_dedup_same_symbol_same_day(self) -> None:
        adapter = self._make_adapter()
        ant = make_ant(adapter=adapter)
        r1 = ant._screen_symbol("AAPL", adapter.get_fundamentals, adapter.get_candles)
        r2 = ant._screen_symbol("AAPL", adapter.get_fundamentals, adapter.get_candles)
        assert r1 is not None
        assert r2 is None  # deduplicated

    def test_candidate_id_format(self) -> None:
        from datetime import date
        adapter = self._make_adapter()
        ant = make_ant(adapter=adapter)
        result = ant._screen_symbol(
            "AAPL", adapter.get_fundamentals, adapter.get_candles
        )
        assert result is not None
        today = date.today().isoformat()
        assert result["candidate_id"] == f"fundamental-aapl-{today}"


# ---------------------------------------------------------------------------
# 5. Tick — watchlist screening
# ---------------------------------------------------------------------------


class TestTick:
    def test_tick_returns_candidates_list(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = _breakout_candles()
        ant = make_ant(adapter=adapter)
        candidates = ant._tick()
        assert isinstance(candidates, list)

    def test_tick_sets_last_action(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _bad_fundamentals()
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        ant._tick()
        assert ant._last_action != "init"

    def test_tick_no_adapter_returns_empty(self) -> None:
        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = FundamentalAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
        )
        result = ant._tick()
        assert result == []

    def test_tick_writes_log_for_accepted_candidate(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = _breakout_candles()
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        candidates = ant._tick()
        if candidates:
            log_path = tmp_path / "research" / f"{ant.ant_id}.jsonl"
            assert log_path.exists()
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert all(r["payload"]["action"] == "candidate_accepted" for r in records)

    def test_tick_log_has_strategy_type(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = _breakout_candles()
        ant = make_ant(adapter=adapter, logs_root=tmp_path, symbols=["AAPL"])
        candidates = ant._tick()
        if candidates:
            log_path = tmp_path / "research" / f"{ant.ant_id}.jsonl"
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            assert records[0]["payload"]["strategy_type"] == "piotroski_breakout"

    def test_tick_log_has_tp_and_sl(self, tmp_path: Path) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _good_fundamentals()
        adapter.get_candles.return_value = _breakout_candles()
        ant = make_ant(adapter=adapter, logs_root=tmp_path, symbols=["AAPL"])
        candidates = ant._tick()
        if candidates:
            log_path = tmp_path / "research" / f"{ant.ant_id}.jsonl"
            records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
            payload = records[0]["payload"]
            assert payload["tp_pct"] == pytest.approx(_TP_PCT)
            assert payload["sl_pct"] == pytest.approx(_SL_PCT)

    def test_exception_per_symbol_does_not_abort_tick(self) -> None:
        call_count = [0]

        def flaky_fundamentals(sym):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise RuntimeError("flaky")
            return _bad_fundamentals()

        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.side_effect = flaky_fundamentals
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        result = ant._tick()
        assert isinstance(result, list)

    def test_default_watchlist_used_when_mission_has_no_symbols(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_fundamentals.return_value = _bad_fundamentals()
        adapter.get_candles.return_value = []

        mission = MagicMock()
        mission.market_scope.symbols = []  # empty → triggers _SP500_TOP50 fallback
        mission.market_scope.biome = "equities"
        mission.mission_id = str(uuid.uuid4())
        mission.allowed_node = "pc2"
        mission.ttl = 3600
        mission.heartbeat_interval = 300

        ant = FundamentalAnt(
            ant_id=str(uuid.uuid4()),
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=make_biome_registry(adapter),
        )
        ant._tick()
        assert adapter.get_fundamentals.call_count == len(_SP500_TOP50)


# ---------------------------------------------------------------------------
# 6. Heartbeat
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
# 7. Lifecycle
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

    def test_initial_status_is_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE
