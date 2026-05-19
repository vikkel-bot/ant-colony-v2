"""
tests/test_research_ant.py

ResearchAnt — gedrag zonder echte API calls.

Scenarios:
  1.  SMA golden cross           → long kandidaat gegenereerd
  2.  SMA death cross            → short kandidaat gegenereerd
  3.  SMA geen crossover         → geen kandidaat
  4.  RSI oversold (< 30)        → rsi_based gedeactiveerd, geen kandidaat
  5.  RSI overbought (> 70)      → rsi_based gedeactiveerd, geen kandidaat
  6.  RSI neutraal (30–70)       → geen kandidaat
  7.  Bollinger onderband touch  → long kandidaat
  8.  Bollinger bovenband touch  → short kandidaat
  9.  Backtest sharpe < 0.5      → kandidaat niet gelogd
  10. Backtest win_rate < 0.45   → kandidaat niet gelogd
  11. Te weinig candles (< 52)   → tick overgeslagen, geen crash
  12. Adapter niet beschikbaar   → geen crash
  13. Adapter zonder get_candles → geen crash
  14. Heartbeat gerapporteerd na tick
  15. TTL verlopen               → ant stopt met AntStatus.COMPLETED
  16. Finale heartbeat bij exit
  17. Audit log geschreven bij kandidaat boven drempel
  18. Logbestand bevat vereiste velden
  19. Geen log bij kandidaat onder drempel
  20. Geen log als logs_root=None
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.research_ant import (
    ResearchAnt,
    _MIN_CANDLES,
    _SHARPE_THRESHOLD,
    _WIN_RATE_THRESHOLD,
    _COMMODITY_YFINANCE_TICKERS,
    _bollinger,
    _rsi,
    _sma,
    _strategy_type_from_signal,
    _strategy_type_from_signal_type,
)
from ant_colony.strategies import DISABLED_STRATEGY_TYPES
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
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

def make_mission(
    *,
    symbols: list[str] | None = None,
    ttl: int = 7200,
    heartbeat_interval: int = 120,
) -> Mission:
    return Mission(
        mission_id="m-research-001",
        ant_type="research_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["read_data", "backtest", "propose_candidate"],
        market_scope=MarketScope(
            biome="crypto",
            symbols=symbols or ["BTC-EUR"],
            timeframes=["1h"],
        ),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=ttl,
        heartbeat_interval=heartbeat_interval,
        success_conditions=SuccessConditions(description="Research missie"),
        abort_conditions=AbortConditions(),
    )


def make_candles(
    closes: list[float],
    symbol: str = "BTC-EUR",
) -> list[MarketData]:
    """Bouw een gesorteerde lijst van MarketData vanuit een lijst van sluitingsprijzen."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        MarketData(
            symbol=symbol,
            timeframe="1h",
            timestamp=base + timedelta(hours=i),
            open=c * 0.999,
            high=c * 1.001,
            low=c * 0.998,
            close=c,
            volume=1_000.0,
            biome_id="crypto",
        )
        for i, c in enumerate(closes)
    ]


def make_adapter(candles: list[MarketData] | None = None, available: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.biome_id = "crypto"
    adapter.is_available.return_value = available
    adapter.get_candles.return_value = candles or []
    return adapter


def make_registry(
    candles: list[MarketData] | None = None, available: bool = True
) -> BiomeRegistry:
    registry = BiomeRegistry()
    registry.register(make_adapter(candles=candles, available=available))
    return registry


def make_ant(
    tmp_path: Path,
    *,
    mission: Mission | None = None,
    registry: BiomeRegistry | None = None,
    scheduler: MagicMock | None = None,
) -> ResearchAnt:
    ant = ResearchAnt(
        ant_id="ant-research-test-0001",
        mission=mission or make_mission(),
        scheduler=scheduler or MagicMock(),
        biome_registry=registry or make_registry(),
        logs_root=tmp_path,
    )
    ant._lean_validator = MagicMock()
    ant._lean_validator.validate.return_value = lean_unavailable_result()
    return ant


def lean_unavailable_result() -> dict:
    return {
        "lean_sharpe": None,
        "lean_max_drawdown": None,
        "lean_win_rate": None,
        "lean_trades": None,
        "lean_status": "unavailable",
        "lean_reason": "test lean unavailable",
    }


def stub_backtester(
    ant: ResearchAnt,
    *,
    sharpe: float = 0.8,
    win_rate: float = 0.55,
    trades: int = 20,
) -> MagicMock:
    """Vervang de backtester door een mock met vooraf bepaalde resultaten."""
    mock_bt = MagicMock()
    mock_bt.run.return_value = BacktestResults(
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        total_trades=trades,
        max_drawdown_pct=0.05,
    )
    ant._backtester = mock_bt
    return mock_bt


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def candidate_records(path: Path) -> list[dict]:
    return [
        record
        for record in read_jsonl(path)
        if (record.get("payload") or {}).get("action") == "candidate_accepted"
    ]


def log_path(tmp_path: Path, ant: ResearchAnt) -> Path:
    return tmp_path / "research" / f"{ant.ant_id}.jsonl"


# ---------------------------------------------------------------------------
# Candle datasets voor specifieke signalen
# ---------------------------------------------------------------------------

# Golden cross: SMA20 kruist boven SMA50
# - closes[0..30] = 80.0  (31 bars laag)
# - closes[31..49] = 70.0 (19 bars nog lager → SMA20 zakt)
# - closes[50..51] = 200.0 (2 bars spike → SMA20 schiet omhoog, kruist SMA50)
# Verificatie: sma20[-2]=76.5 < sma50[-2]=78.6 ; sma20[-1]=83.0 > sma50[-1]=81.0
GOLDEN_CROSS_CLOSES = [80.0] * 179 + [70.0] * 19 + [200.0] * 2  # 200 bars
# Verificatie (identiek aan 52-bar versie): sma20[-2]=76.5 < sma50[-2]=78.6 ;
# sma20[-1]=83.0 > sma50[-1]=81.0

# Death cross: SMA20 kruist onder SMA50
DEATH_CROSS_CLOSES = [120.0] * 193 + [140.0] * 5 + [30.0] * 2  # 200 bars
# Verificatie: sma20[-2]=120.5 > sma50[-2]=120.2 ; sma20[-1]=116.0 < sma50[-1]=118.4

# RSI oversold: 186 neutrale bars + 14 dalende bars (elke stap -5)
# RSI = 0.0 (alle deltas negatief) → ruim onder 30
RSI_OVERSOLD_CLOSES = [100.0] * 186 + [float(100 - 5 * i) for i in range(14)]  # 200 bars

# RSI overbought: 186 neutrale bars + 14 stijgende bars (elke stap +5)
# RSI = 100.0 (alle deltas positief) → ruim boven 70
RSI_OVERBOUGHT_CLOSES = [100.0] * 186 + [float(100 + 5 * i) for i in range(14)]  # 200 bars

# Bollinger onderband touch: 199 stabiele bars + 1 crash naar 70
# mean≈99.85, std≈2.1, lower≈95.6 → 70 < lower ✓
BB_LOWER_CLOSES = [100.0] * 199 + [70.0]  # 200 bars

# Bollinger bovenband touch: 199 stabiele bars + 1 spike naar 130
# mean≈100.15, std≈2.1, upper≈104.4 → 130 > upper ✓
BB_UPPER_CLOSES = [100.0] * 199 + [130.0]  # 200 bars

# Neutraal: 200 bars op constante prijs → geen SMA crossover
FLAT_CLOSES = [100.0] * 200

# Neutraal alternererend: RSI ≈ 50, prijs middenin Bollinger bands
# 101/99 afwisselend → mean=100, std=1, upper=102, lower=98, RSI≈50
NEUTRAL_CLOSES = [101.0 if i % 2 == 0 else 99.0 for i in range(200)]


# ---------------------------------------------------------------------------
# Indicator hulpfuncties
# ---------------------------------------------------------------------------

class TestSMAHelper:
    def test_exact_period_returns_one_value(self):
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _sma(closes, 5)
        assert len(result) == 1
        assert abs(result[0] - 3.0) < 1e-9

    def test_rolling_sma_length(self):
        closes = [float(i) for i in range(10)]
        result = _sma(closes, 3)
        assert len(result) == 8   # 10 - 3 + 1

    def test_too_few_values_returns_empty(self):
        assert _sma([1.0, 2.0], 5) == []

    def test_sma_values_correct(self):
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _sma(closes, 3)
        assert abs(result[0] - 2.0) < 1e-9   # mean([1,2,3])
        assert abs(result[1] - 3.0) < 1e-9   # mean([2,3,4])
        assert abs(result[2] - 4.0) < 1e-9   # mean([3,4,5])


class TestRSIHelper:
    def test_all_gains_returns_100(self):
        closes = [float(100 + i) for i in range(15)]
        result = _rsi(closes, period=14)
        assert result == 100.0

    def test_all_losses_returns_zero(self):
        closes = [float(100 - i) for i in range(15)]
        result = _rsi(closes, period=14)
        assert result == 0.0

    def test_neutral_market_near_50(self):
        # Alternerend op/neer → gains ≈ losses → RSI ≈ 50
        closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(15)]
        result = _rsi(closes, period=14)
        assert result is not None
        assert 40.0 < result < 60.0

    def test_insufficient_data_returns_none(self):
        closes = [100.0] * 10
        assert _rsi(closes, period=14) is None

    def test_exact_boundary_returns_value(self):
        closes = [100.0] * 15   # 15 waarden → 14 deltas, exact genoeg
        result = _rsi(closes, period=14)
        assert result is not None


class TestBollingerHelper:
    def test_flat_prices_zero_std(self):
        closes = [100.0] * 20
        result = _bollinger(closes, period=20)
        assert result is not None
        upper, middle, lower = result
        assert abs(middle - 100.0) < 1e-9
        assert abs(upper - lower) < 1e-9   # std = 0, bands gelijk

    def test_upper_above_lower(self):
        closes = [90.0 + i for i in range(20)]
        result = _bollinger(closes, period=20)
        assert result is not None
        upper, middle, lower = result
        assert upper > middle > lower

    def test_insufficient_data_returns_none(self):
        assert _bollinger([100.0] * 10, period=20) is None

    def test_spike_outside_upper_band(self):
        closes = [100.0] * 51 + [130.0]
        upper, _, lower = _bollinger(closes[-20:], period=20)
        assert closes[-1] > upper

    def test_crash_outside_lower_band(self):
        closes = [100.0] * 51 + [70.0]
        upper, _, lower = _bollinger(closes[-20:], period=20)
        assert closes[-1] < lower


# ---------------------------------------------------------------------------
# 1–3  SMA crossover detectie
# ---------------------------------------------------------------------------

class TestSMACrossover:
    def test_golden_cross_generates_long_candidate(self, tmp_path):
        """Scenario 1: SMA20 kruist boven SMA50 → long kandidaat."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        sma_records = [
            r for r in records
            if "SMA_CROSSOVER" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(sma_records) >= 1
        assert (sma_records[0].get("payload") or {}).get("direction") == "long"

    def test_death_cross_generates_short_candidate(self, tmp_path):
        """Scenario 2: SMA20 kruist onder SMA50 → short kandidaat."""
        candles = make_candles(DEATH_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        sma_records = [
            r for r in records
            if "SMA_CROSSOVER" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(sma_records) >= 1
        assert (sma_records[0].get("payload") or {}).get("direction") == "short"

    def test_flat_market_no_crossover_candidate(self, tmp_path):
        """Scenario 3: geen crossover → geen SMA kandidaat."""
        candles = make_candles(FLAT_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        sma_records = [
            r for r in records
            if "SMA_CROSSOVER" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(sma_records) == 0


# ---------------------------------------------------------------------------
# 4–6  RSI detectie
# ---------------------------------------------------------------------------

class TestRSIDetection:
    def test_rsi_oversold_is_disabled(self, tmp_path):
        """Scenario 4: RSI < 30 → rsi_based is uitgeschakeld, geen kandidaat."""
        candles = make_candles(RSI_OVERSOLD_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._check_rsi("BTC-EUR", candles, [c.close for c in candles])

        records = read_jsonl(log_path(tmp_path, ant))
        rsi_records = [
            r for r in records
            if "RSI_OVERSOLD" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert rsi_records == []
        assert not ant._backtester.run.called

    def test_rsi_overbought_is_disabled(self, tmp_path):
        """Scenario 5: RSI > 70 → rsi_based is uitgeschakeld, geen kandidaat."""
        candles = make_candles(RSI_OVERBOUGHT_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._check_rsi("BTC-EUR", candles, [c.close for c in candles])

        records = read_jsonl(log_path(tmp_path, ant))
        rsi_records = [
            r for r in records
            if "RSI_OVERBOUGHT" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert rsi_records == []
        assert not ant._backtester.run.called

    def test_rsi_neutral_no_candidate(self, tmp_path):
        """Scenario 6: RSI neutraal (≈50) → geen RSI kandidaat."""
        candles = make_candles(NEUTRAL_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        rsi_records = [
            r for r in records
            if "RSI_" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(rsi_records) == 0


# ---------------------------------------------------------------------------
# 7–8  Bollinger band detectie
# ---------------------------------------------------------------------------

class TestBollingerDetection:
    def test_lower_band_touch_generates_long_candidate(self, tmp_path):
        """Scenario 7: prijs raakt onderband → long kandidaat."""
        candles = make_candles(BB_LOWER_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        bb_records = [
            r for r in records
            if "BB_LOWER_TOUCH" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(bb_records) >= 1
        assert (bb_records[0].get("payload") or {}).get("direction") == "long"

    def test_upper_band_touch_generates_short_candidate(self, tmp_path):
        """Scenario 8: prijs raakt bovenband → short kandidaat."""
        candles = make_candles(BB_UPPER_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        bb_records = [
            r for r in records
            if "BB_UPPER_TOUCH" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(bb_records) >= 1
        assert (bb_records[0].get("payload") or {}).get("direction") == "short"

    def test_price_within_bands_no_candidate(self, tmp_path):
        """Prijs middenin de bands → geen Bollinger kandidaat."""
        candles = make_candles(NEUTRAL_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        bb_records = [
            r for r in records
            if "BB_" in (r.get("payload") or {}).get("signal_type", "").upper()
        ]
        assert len(bb_records) == 0


# ---------------------------------------------------------------------------
# 9–10  Drempelfilter
# ---------------------------------------------------------------------------

class TestThresholdFilter:
    def test_sharpe_below_threshold_not_logged(self, tmp_path):
        """Scenario 9: sharpe < 0.5 → kandidaat niet gelogd."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant, sharpe=_SHARPE_THRESHOLD - 0.1, win_rate=0.60)

        ant._tick()

        assert candidate_records(log_path(tmp_path, ant)) == []

    def test_win_rate_below_threshold_not_logged(self, tmp_path):
        """Scenario 10: win_rate < 0.45 → kandidaat niet gelogd."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant, sharpe=1.2, win_rate=_WIN_RATE_THRESHOLD - 0.05)

        ant._tick()

        assert candidate_records(log_path(tmp_path, ant)) == []

    def test_both_thresholds_met_candidate_logged(self, tmp_path):
        """Precies boven beide drempels → kandidaat wel gelogd."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant, sharpe=_SHARPE_THRESHOLD + 0.01, win_rate=_WIN_RATE_THRESHOLD + 0.01)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        assert len(records) >= 1

    def test_none_sharpe_treated_as_zero(self, tmp_path):
        """sharpe=None (< 2 trades) → 0.0 → onder drempel → niet gelogd."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant, sharpe=None, win_rate=0.60)

        ant._tick()

        assert candidate_records(log_path(tmp_path, ant)) == []


# ---------------------------------------------------------------------------
# 11–13  Datavalidatie en robuustheid
# ---------------------------------------------------------------------------

class TestDataValidation:
    def test_too_few_candles_tick_skipped(self, tmp_path):
        """Scenario 11: < 200 candles → _tick slaat symbool over, geen crash."""
        short_closes = [100.0] * (_MIN_CANDLES - 1)  # één te weinig
        candles = make_candles(short_closes)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)

        ant._tick()  # mag niet raisen

        assert candidate_records(log_path(tmp_path, ant)) == []

    def test_empty_candles_no_crash(self, tmp_path):
        """Lege candle lijst → geen crash."""
        registry = make_registry(candles=[])
        ant = make_ant(tmp_path, registry=registry)

        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        assert any((r.get("payload") or {}).get("action") == "research_tick" for r in records)

    def test_unavailable_adapter_no_crash(self, tmp_path):
        """Scenario 12: adapter niet bereikbaar → geen crash."""
        registry = make_registry(available=False)
        ant = make_ant(tmp_path, registry=registry)

        ant._tick()

    def test_missing_biome_adapter_no_crash(self, tmp_path):
        """Lege BiomeRegistry (geen adapter) → geen crash."""
        empty_registry = BiomeRegistry()
        ant = make_ant(tmp_path, registry=empty_registry)

        ant._tick()

    def test_adapter_without_get_candles_no_crash(self, tmp_path):
        """Scenario 13: adapter heeft geen get_candles methode → geen crash."""
        adapter = MagicMock(spec=["biome_id", "is_available"])
        adapter.biome_id = "crypto"
        adapter.is_available.return_value = True

        registry = BiomeRegistry()
        registry.register(adapter)
        ant = make_ant(tmp_path, registry=registry)

        ant._tick()

    def test_backtester_exception_no_crash(self, tmp_path):
        """Crash in backtester → fail-closed, geen kandidaat, geen exception."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)

        mock_bt = MagicMock()
        mock_bt.run.side_effect = RuntimeError("backtester kapot")
        ant._backtester = mock_bt

        ant._tick()  # mag niet raisen

        assert candidate_records(log_path(tmp_path, ant)) == []


# ---------------------------------------------------------------------------
# 14  Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_send_heartbeat_calls_scheduler(self, tmp_path):
        """Scenario 14: heartbeat rapporteert aan scheduler."""
        scheduler = MagicMock()
        ant = make_ant(tmp_path, scheduler=scheduler)
        ant._status = AntStatus.RUNNING

        ant._send_heartbeat()

        scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_contains_ant_id(self, tmp_path):
        scheduler = MagicMock()
        ant = make_ant(tmp_path, scheduler=scheduler)
        ant._status = AntStatus.RUNNING

        ant._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id

    def test_heartbeat_contains_mission_id(self, tmp_path):
        scheduler = MagicMock()
        mission = make_mission()
        ant = make_ant(tmp_path, mission=mission, scheduler=scheduler)
        ant._status = AntStatus.RUNNING

        ant._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.mission_id == mission.mission_id

    def test_heartbeat_reports_last_action(self, tmp_path):
        scheduler = MagicMock()
        ant = make_ant(tmp_path, scheduler=scheduler)
        ant._status = AntStatus.RUNNING
        ant._last_action = "candidate:rsi_oversold:BTC-EUR"

        ant._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.last_action == "candidate:rsi_oversold:BTC-EUR"

    def test_heartbeat_exception_does_not_propagate(self, tmp_path):
        scheduler = MagicMock()
        scheduler.record_heartbeat.side_effect = RuntimeError("scheduler down")
        ant = make_ant(tmp_path, scheduler=scheduler)

        ant._send_heartbeat()  # mag niet raisen

    def test_run_sends_heartbeat_when_interval_elapsed(self, tmp_path):
        """Heartbeat wordt verzonden zodra heartbeat_interval verstreken is."""
        scheduler = MagicMock()
        mission = make_mission(ttl=300, heartbeat_interval=10, symbols=["BTC-EUR"])
        ant = make_ant(
            tmp_path,
            mission=mission,
            scheduler=scheduler,
            registry=make_registry(candles=[]),
        )

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("ant_colony.ants.research_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.research_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,                              # started_at
                base + timedelta(seconds=11),      # iter 1: elapsed=11<300, hb: 11>=10 → send
                base + timedelta(seconds=11),      # last_heartbeat update
                base + timedelta(seconds=301),     # iter 2: TTL → COMPLETED
            ]
            mock_time.sleep = MagicMock()
            ant.run()

        assert scheduler.record_heartbeat.call_count >= 1


# ---------------------------------------------------------------------------
# 15–16  TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_run_returns_completed_when_ttl_expires(self, tmp_path):
        """Scenario 15: TTL verlopen → AntStatus.COMPLETED."""
        mission = make_mission(ttl=10, heartbeat_interval=5)
        ant = make_ant(tmp_path, mission=mission, registry=make_registry(candles=[]))

        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch("ant_colony.ants.research_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.research_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,
                base + timedelta(seconds=1),   # iter 1: elapsed=1 < 10
                base + timedelta(seconds=11),  # iter 2: elapsed=11 >= 10 → COMPLETED
            ]
            mock_time.sleep = MagicMock()
            status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_run_sends_final_heartbeat_on_exit(self, tmp_path):
        """Scenario 16: finally-blok stuurt altijd een heartbeat."""
        scheduler = MagicMock()
        mission = make_mission(ttl=10, heartbeat_interval=5)
        ant = make_ant(
            tmp_path,
            mission=mission,
            scheduler=scheduler,
            registry=make_registry(candles=[]),
        )

        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch("ant_colony.ants.research_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.research_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,
                base + timedelta(seconds=11),  # direct TTL
            ]
            mock_time.sleep = MagicMock()
            ant.run()

        assert scheduler.record_heartbeat.called

    def test_status_is_idle_before_run(self, tmp_path):
        ant = make_ant(tmp_path)
        assert ant._status == AntStatus.IDLE

    def test_watchdog_restarts_when_tick_stalls(self, tmp_path, monkeypatch, caplog):
        """Als een research tick vastloopt, moet de run-loop een nieuwe cyclus starten."""
        scheduler = MagicMock()
        mission = make_mission(ttl=30, heartbeat_interval=1)
        ant = make_ant(
            tmp_path,
            mission=mission,
            scheduler=scheduler,
            registry=make_registry(candles=[]),
        )
        calls = {"count": 0}
        release_stale_tick = threading.Event()

        def fake_tick() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                release_stale_tick.wait(0.2)
                return
            ant._status = AntStatus.COMPLETED

        monkeypatch.setattr("ant_colony.ants.research_ant._RESEARCH_WATCHDOG_SECONDS", 0.02)
        monkeypatch.setattr("ant_colony.ants.research_ant.time.sleep", lambda *_args: None)
        ant._tick = fake_tick  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger=f"ant.research.{ant.ant_id[:8]}"):
            status = ant.run()

        assert status == AntStatus.COMPLETED
        assert calls["count"] >= 2
        assert ant._watchdog_restarts == 1
        assert ant._last_action == "tick_watchdog_restart"
        assert any("ResearchAnt watchdog" in record.message for record in caplog.records)
        records = read_jsonl(log_path(tmp_path, ant))
        assert any((r.get("payload") or {}).get("action") == "research_watchdog_restart" for r in records)


# ---------------------------------------------------------------------------
# 17–20  Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_candidate_written_to_research_subdir(self, tmp_path):
        """Scenario 17: kandidaat gelogd naar ANT_LOGS/research/."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = make_ant(tmp_path, registry=registry)
        stub_backtester(ant)

        ant._tick()

        assert log_path(tmp_path, ant).exists(), "Logbestand niet aangemaakt"

    def test_log_entry_is_valid_json(self, tmp_path):
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        assert len(records) >= 1

    def test_log_contains_required_fields(self, tmp_path):
        """Scenario 18: logrecord heeft AuditEvent structuur met alle payload-velden."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        # AuditEvent top-level velden
        for field in ("event_type", "source", "timestamp", "mission_id", "payload"):
            assert field in record, f"AuditEvent veld '{field}' ontbreekt"
        # payload velden
        payload = record["payload"]
        for field in ("action", "candidate_id", "symbol", "sharpe", "win_rate", "direction"):
            assert field in payload, f"Payload veld '{field}' ontbreekt"

    def test_log_candidate_status_is_research(self, tmp_path):
        """Geaccepteerde kandidaat heeft action=candidate_accepted in payload."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert record["payload"]["action"] == "candidate_accepted"

    def test_log_provenance_contains_ant_id(self, tmp_path):
        """source veld in AuditEvent bevat ant_id."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert record["source"] == ant.ant_id

    def test_log_provenance_contains_mission_id(self, tmp_path):
        """mission_id veld in AuditEvent bevat de mission_id."""
        mission = make_mission()
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, mission=mission, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert record["mission_id"] == mission.mission_id

    def test_no_log_when_below_threshold(self, tmp_path):
        """Scenario 19: onder drempel → geen logbestand."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant, sharpe=0.1, win_rate=0.3)

        ant._tick()

        assert candidate_records(log_path(tmp_path, ant)) == []

    def test_no_log_when_logs_root_is_none(self, tmp_path):
        """Scenario 20: logs_root=None → geen crash, geen bestand."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        registry = make_registry(candles=candles)
        ant = ResearchAnt(
            ant_id="ant-nolog",
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
            logs_root=None,
        )
        stub_backtester(ant)

        ant._tick()   # mag niet raisen

    def test_multiple_ticks_append_to_same_file(self, tmp_path):
        """Elke tick met signaal voegt een record toe aan hetzelfde bestand."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)

        ant._tick()
        ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        assert len(records) >= 2

    def test_exit_conditions_not_empty(self, tmp_path):
        """Geaccepteerde kandidaat heeft sharpe en win_rate ingevuld."""
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert record["payload"]["sharpe"] is not None
        assert record["payload"]["win_rate"] is not None

    def test_fitness_score_equals_sharpe(self, tmp_path):
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        stub_backtester(ant, sharpe=0.75)
        ant._tick()

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert abs(record["payload"]["sharpe"] - 0.75) < 1e-3

    def test_lean_validation_fields_logged_for_candidate(self, tmp_path):
        candles = make_candles(GOLDEN_CROSS_CLOSES)
        ant = make_ant(tmp_path, registry=make_registry(candles=candles))
        ant._lean_validator.validate.return_value = {
            "lean_sharpe": 0.7,
            "lean_max_drawdown": 0.04,
            "lean_win_rate": 0.58,
            "lean_trades": 24,
            "lean_status": "passed",
            "lean_reason": "Lean backtest voltooid",
        }
        stub_backtester(ant, sharpe=0.8)

        ant._tick()

        payload = read_jsonl(log_path(tmp_path, ant))[0]["payload"]
        assert payload["lean_status"] == "passed"
        assert payload["lean_validated"] is True
        assert payload["lean_sharpe"] == 0.7
        assert payload["lean_trades"] == 24


# ---------------------------------------------------------------------------
# _strategy_type_from_signal — Claude/improved variant + tp_pct classificatie
# ---------------------------------------------------------------------------

class TestStrategyTypeFromSignalClaudeVariants:

    def test_claude_keyword_high_tp_pct_is_momentum(self):
        result = _strategy_type_from_signal("LONG", ["claude", "sma"], tp_pct=0.08)
        assert result == "momentum"

    def test_claude_keyword_low_tp_pct_is_mean_reversion(self):
        result = _strategy_type_from_signal("LONG", ["claude", "rsi"], tp_pct=0.04)
        assert result == "mean_reversion"

    def test_claude_keyword_mid_tp_pct_is_hybrid(self):
        result = _strategy_type_from_signal("LONG", ["claude"], tp_pct=0.06)
        assert result == "hybrid"

    def test_claude_keyword_no_tp_pct_is_hybrid(self):
        result = _strategy_type_from_signal("LONG", ["claude"], tp_pct=None)
        assert result == "hybrid"

    def test_improved_keyword_high_tp_pct_is_momentum(self):
        result = _strategy_type_from_signal("LONG", ["improved", "breakout"], tp_pct=0.09)
        assert result == "momentum"

    def test_improved_keyword_low_tp_pct_is_mean_reversion(self):
        result = _strategy_type_from_signal("SHORT", ["improved"], tp_pct=0.03)
        assert result == "mean_reversion"

    def test_improved_keyword_no_tp_pct_is_hybrid(self):
        result = _strategy_type_from_signal("LONG", ["improved"], tp_pct=None)
        assert result == "hybrid"

    def test_tp_pct_boundary_007_is_still_hybrid(self):
        """tp_pct == 0.07 is niet > 0.07 en niet <= 0.04 → hybrid."""
        result = _strategy_type_from_signal("LONG", ["claude"], tp_pct=0.07)
        assert result == "hybrid"

    def test_tp_pct_boundary_004_is_mean_reversion(self):
        """tp_pct == 0.04 is <= 0.04 → mean_reversion."""
        result = _strategy_type_from_signal("LONG", ["claude"], tp_pct=0.04)
        assert result == "mean_reversion"

    def test_normal_keywords_not_affected_by_tp_pct(self):
        """Zonder 'claude'/'improved' wordt tp_pct genegeerd."""
        result = _strategy_type_from_signal("LONG", ["sma", "crossover"], tp_pct=0.02)
        assert result != "mean_reversion"


def test_disabled_strategy_types_for_slimming_phase() -> None:
    assert {"rsi_based", "sector_scout", "hybrid", "unknown"}.issubset(
        DISABLED_STRATEGY_TYPES
    )


# ---------------------------------------------------------------------------
# _check_strategy_diversity — waarschuwing bij homogene top 3
# ---------------------------------------------------------------------------

def _write_research_log(log_dir: Path, entries: list[dict]) -> None:
    p = log_dir / "research" / "r.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for e in entries:
            f.write(json.dumps({"payload": e}) + "\n")


class TestCheckStrategyDiversity:

    def _make_candidate(self, sharpe: float, strategy_type: str) -> dict:
        return {
            "action": "candidate_accepted",
            "candidate_id": f"c-{sharpe}",
            "sharpe": sharpe,
            "strategy_type": strategy_type,
        }

    def test_warns_when_top3_all_same_type(self, tmp_path, caplog):
        ant = make_ant(tmp_path)
        entries = [
            self._make_candidate(1.2, "momentum"),
            self._make_candidate(1.0, "momentum"),
            self._make_candidate(0.8, "momentum"),
        ]
        _write_research_log(tmp_path, entries)

        import logging
        with caplog.at_level(logging.WARNING, logger="ant_colony.ants.research_ant"):
            ant._check_strategy_diversity()

        assert any("diversiteit" in r.message.lower() or "strategy_type" in r.message
                   for r in caplog.records)

    def test_no_warn_when_types_differ(self, tmp_path, caplog):
        ant = make_ant(tmp_path)
        entries = [
            self._make_candidate(1.2, "momentum"),
            self._make_candidate(1.0, "mean_reversion"),
            self._make_candidate(0.8, "momentum"),
        ]
        _write_research_log(tmp_path, entries)

        import logging
        with caplog.at_level(logging.WARNING, logger="ant_colony.ants.research_ant"):
            ant._check_strategy_diversity()

        warning_msgs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("diversiteit" in r.message.lower() for r in warning_msgs)

    def test_no_warn_when_fewer_than_3_candidates(self, tmp_path, caplog):
        ant = make_ant(tmp_path)
        entries = [
            self._make_candidate(1.2, "momentum"),
            self._make_candidate(1.0, "momentum"),
        ]
        _write_research_log(tmp_path, entries)

        import logging
        with caplog.at_level(logging.WARNING, logger="ant_colony.ants.research_ant"):
            ant._check_strategy_diversity()

        warning_msgs = [r for r in caplog.records if r.levelname == "WARNING"
                        and "diversiteit" in r.message.lower()]
        assert not warning_msgs

    def test_no_warn_when_logs_root_none(self, caplog):
        ant = ResearchAnt(
            ant_id="ant-noroot",
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=make_registry(candles=[]),
            logs_root=None,
        )
        import logging
        with caplog.at_level(logging.WARNING, logger="ant_colony.ants.research_ant"):
            ant._check_strategy_diversity()

        assert not caplog.records

    def test_uses_top3_by_sharpe_not_all(self, tmp_path, caplog):
        """Top 3 op sharpe zijn divers, overige 2 zijn homogeen → geen warning."""
        ant = make_ant(tmp_path)
        entries = [
            self._make_candidate(1.5, "momentum"),
            self._make_candidate(1.3, "mean_reversion"),
            self._make_candidate(1.1, "hybrid"),
            self._make_candidate(0.9, "momentum"),
            self._make_candidate(0.8, "momentum"),
        ]
        _write_research_log(tmp_path, entries)

        import logging
        with caplog.at_level(logging.WARNING, logger="ant_colony.ants.research_ant"):
            ant._check_strategy_diversity()

        warning_msgs = [r for r in caplog.records if r.levelname == "WARNING"
                        and "diversiteit" in r.message.lower()]
        assert not warning_msgs


class TestRejectedCandidateCooldown:

    def test_same_rejected_candidate_suppressed_within_cooldown(self, tmp_path):
        ant = make_ant(tmp_path)

        kwargs = {
            "symbol": "BTC-EUR",
            "signal_type": "sma_crossover",
            "direction": "long",
            "parameters": {"short_window": 20, "long_window": 50},
            "sharpe": _SHARPE_THRESHOLD - 0.01,
            "win_rate": _WIN_RATE_THRESHOLD + 0.1,
            "total_trades": 20,
        }
        ant._record_rejected_candidate(**kwargs)
        ant._record_rejected_candidate(**kwargs)

        assert ant._reject_tick_new == 1
        assert ant._reject_tick_suppressed == 1
        assert ant._reject_tick_reasons["sharpe"] == 2

    def test_different_rejected_candidate_not_suppressed(self, tmp_path):
        ant = make_ant(tmp_path)

        base = {
            "symbol": "BTC-EUR",
            "signal_type": "sma_crossover",
            "direction": "long",
            "sharpe": _SHARPE_THRESHOLD + 0.2,
            "win_rate": _WIN_RATE_THRESHOLD - 0.01,
            "total_trades": 20,
        }
        ant._record_rejected_candidate(parameters={"short_window": 20}, **base)
        ant._record_rejected_candidate(parameters={"short_window": 21}, **base)

        assert ant._reject_tick_new == 2
        assert ant._reject_tick_suppressed == 0
        assert ant._reject_tick_reasons["win_rate"] == 2


# ---------------------------------------------------------------------------
# Commodity watchtower signalen
# ---------------------------------------------------------------------------

def _make_yfinance_df(prices: list[float]):
    """Bouw een minimaal pandas DataFrame dat yfinance.Ticker.history() imiteert."""
    import pandas as pd
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta as td
    index = [base + td(days=i) for i in range(len(prices))]
    return pd.DataFrame(
        {
            "Open": prices,
            "High": [p * 1.01 for p in prices],
            "Low":  [p * 0.99 for p in prices],
            "Close": prices,
            "Volume": [1000.0] * len(prices),
        },
        index=index,
    )


def _make_commodity_mission(symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id="m-commodity-001",
        ant_type="research_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["read_data", "backtest", "propose_candidate"],
        market_scope=MarketScope(
            biome="commodity",
            symbols=symbols or ["NATGAS", "COPPER", "SILVER"],
            timeframes=["1d"],
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
        success_conditions=SuccessConditions(description="Commodity research missie"),
        abort_conditions=AbortConditions(),
    )


class TestCommodityWatchtowerSignals:

    def test_non_commodity_biome_skips_processing(self, tmp_path: Path) -> None:
        """Als biome != 'commodity', doet _process_commodity_watchtower_signals niets."""
        ant = make_ant(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        ant._process_commodity_watchtower_signals()
        assert len(ant._last_emitted) == 0

    def test_commodity_ticker_map_contains_expected_symbols(self) -> None:
        assert _COMMODITY_YFINANCE_TICKERS["NATGAS"] == "NG=F"
        assert _COMMODITY_YFINANCE_TICKERS["COPPER"] == "HG=F"
        assert _COMMODITY_YFINANCE_TICKERS["SILVER"] == "SI=F"

    def test_commodity_generates_candidate_via_yfinance(self, tmp_path: Path) -> None:
        """Met voldoende yfinance candles wordt een watchtower_signal kandidaat geëmitteerd."""
        import sys

        prices = [3.5 + i * 0.001 for i in range(250)]
        mock_df = _make_yfinance_df(prices)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_df
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker

        mission = _make_commodity_mission(symbols=["NATGAS"])
        ant = ResearchAnt(
            ant_id="ant-commodity-test",
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=tmp_path,
        )
        ant._lean_validator = MagicMock()
        ant._lean_validator.validate.return_value = {
            "lean_sharpe": None, "lean_max_drawdown": None,
            "lean_win_rate": None, "lean_trades": None,
            "lean_status": "unavailable", "lean_reason": "test",
        }

        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            ant._process_commodity_watchtower_signals()

        assert ("NATGAS", "watchtower_signal") in ant._last_emitted

    def test_commodity_skips_symbol_not_in_mission(self, tmp_path: Path) -> None:
        """Symbolen niet in de missie worden overgeslagen."""
        import sys

        prices = [3.5] * 250
        mock_df = _make_yfinance_df(prices)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_df
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker

        mission = _make_commodity_mission(symbols=["SILVER"])  # alleen SILVER
        ant = ResearchAnt(
            ant_id="ant-commodity-scope",
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=tmp_path,
        )
        ant._lean_validator = MagicMock()
        ant._lean_validator.validate.return_value = {
            "lean_sharpe": None, "lean_max_drawdown": None,
            "lean_win_rate": None, "lean_trades": None,
            "lean_status": "unavailable", "lean_reason": "test",
        }

        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            ant._process_commodity_watchtower_signals()

        assert ("NATGAS", "watchtower_signal") not in ant._last_emitted
        assert ("COPPER", "watchtower_signal") not in ant._last_emitted

    def test_fetch_commodity_candles_returns_empty_on_import_error(self, tmp_path: Path) -> None:
        """Als yfinance niet geïnstalleerd is, retourneert _fetch_commodity_candles []."""
        import sys

        ant = ResearchAnt(
            ant_id="ant-commodity-noyf",
            mission=_make_commodity_mission(),
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=tmp_path,
        )
        # Simuleer ImportError door yfinance op None te zetten in sys.modules
        with patch.dict(sys.modules, {"yfinance": None}):
            result = ant._fetch_commodity_candles("NG=F", "NATGAS")
        assert result == []

    def test_fetch_commodity_candles_returns_empty_when_no_data(self, tmp_path: Path) -> None:
        """Leeg DataFrame → lege lijst, geen crash."""
        import sys
        import pandas as pd

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker

        ant = ResearchAnt(
            ant_id="ant-commodity-empty",
            mission=_make_commodity_mission(),
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=tmp_path,
        )
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            result = ant._fetch_commodity_candles("NG=F", "NATGAS")
        assert result == []

    def test_commodity_too_few_candles_skipped(self, tmp_path: Path) -> None:
        """Met te weinig candles (<200) wordt geen kandidaat geëmitteerd."""
        import sys

        prices = [3.5] * 50  # slechts 50 bars — onder _MIN_CANDLES
        mock_df = _make_yfinance_df(prices)
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_df
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker

        ant = ResearchAnt(
            ant_id="ant-commodity-fewbars",
            mission=_make_commodity_mission(symbols=["NATGAS"]),
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=tmp_path,
        )
        ant._lean_validator = MagicMock()
        ant._lean_validator.validate.return_value = {
            "lean_sharpe": None, "lean_max_drawdown": None,
            "lean_win_rate": None, "lean_trades": None,
            "lean_status": "unavailable", "lean_reason": "test",
        }
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            ant._process_commodity_watchtower_signals()
        assert ("NATGAS", "watchtower_signal") not in ant._last_emitted


class TestStrategyTypeWatchtower:

    def test_strategy_type_from_signal_watchtower(self) -> None:
        assert _strategy_type_from_signal("watchtower_signal NATGAS", []) == "watchtower_signal"

    def test_strategy_type_from_signal_type_watchtower(self) -> None:
        assert _strategy_type_from_signal_type("watchtower_signal") == "watchtower_signal"

    def test_strategy_type_from_signal_type_watchtower_prefix(self) -> None:
        assert _strategy_type_from_signal_type("watchtower_signal_v2") == "watchtower_signal"
