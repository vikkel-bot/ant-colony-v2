"""
tests/test_sector_scout_ant.py

Tests voor SectorScoutAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.sector_scout_ant import (
    SectorScoutAnt,
    _SPDR_ETFS,
    _BOTTOM_N,
    _TOP_N,
)
from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission() -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="sector_scout_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=list(_SPDR_ETFS.keys())[:3]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="sector rank test"),
    )


def make_candle(symbol: str, close: float, open_: float = 100.0) -> MarketData:
    return MarketData(
        symbol=symbol, timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=open_, high=close + 1, low=open_ - 1, close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def make_adapter_with_returns(symbol_returns: dict[str, float]) -> MagicMock:
    """
    Bouw een mock-adapter waar get_candles de opgegeven returns simuleert.
    Return r = (last - first) / first  →  last = first * (1 + r).
    """
    adapter = MagicMock(spec=YahooFinanceAdapter)

    def get_candles(symbol, period="3mo", interval="1d"):
        r = symbol_returns.get(symbol)
        if r is None:
            return []
        first_close = 100.0
        last_close  = first_close * (1 + r)
        return [
            make_candle(symbol, first_close, open_=99.0),
            make_candle(symbol, last_close,  open_=first_close),
        ]

    adapter.get_candles.side_effect = get_candles
    return adapter


def make_ant(adapter=None, logs_root=None) -> SectorScoutAnt:
    return SectorScoutAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        adapter=adapter or MagicMock(spec=YahooFinanceAdapter),
        logs_root=logs_root,
    )


# ---------------------------------------------------------------------------
# 1. 3-maands return berekening
# ---------------------------------------------------------------------------


class TestGet3moReturn:
    def test_positive_return_calculated(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLK", 100.0), make_candle("XLK", 115.0)]
        ant.adapter.get_candles.return_value = candles
        result = ant._get_3mo_return("XLK")
        assert result == pytest.approx(0.15)

    def test_negative_return_calculated(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLE", 200.0), make_candle("XLE", 180.0)]
        ant.adapter.get_candles.return_value = candles
        result = ant._get_3mo_return("XLE")
        assert result == pytest.approx(-0.10)

    def test_zero_first_close_returns_none(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLV", 0.0), make_candle("XLV", 50.0)]
        ant.adapter.get_candles.return_value = candles
        result = ant._get_3mo_return("XLV")
        assert result is None

    def test_single_candle_returns_none(self) -> None:
        ant = make_ant()
        ant.adapter.get_candles.return_value = [make_candle("XLK", 100.0)]
        result = ant._get_3mo_return("XLK")
        assert result is None

    def test_empty_candles_returns_none(self) -> None:
        ant = make_ant()
        ant.adapter.get_candles.return_value = []
        result = ant._get_3mo_return("XLK")
        assert result is None


# ---------------------------------------------------------------------------
# 2. Signalen bouwen
# ---------------------------------------------------------------------------


class TestBuildSignals:
    def test_top3_get_long_signal(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.20), ("XLF", 0.15), ("XLV", 0.10),
                   ("XLI", 0.05), ("XLB", 0.02), ("XLP", 0.01),
                   ("XLY", -0.01), ("XLU", -0.02), ("XLRE", -0.05),
                   ("XLC", -0.08), ("XLE", -0.12)]
        signals = ant._build_signals(ranking)
        long_symbols = {s["symbol"] for s in signals if s["signal"] == "LONG"}
        assert long_symbols == {"XLK", "XLF", "XLV"}

    def test_bottom3_get_short_signal(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.20), ("XLF", 0.15), ("XLV", 0.10),
                   ("XLI", 0.05), ("XLB", 0.02), ("XLP", 0.01),
                   ("XLY", -0.01), ("XLU", -0.02), ("XLRE", -0.05),
                   ("XLC", -0.08), ("XLE", -0.12)]
        signals = ant._build_signals(ranking)
        short_symbols = {s["symbol"] for s in signals if s["signal"] == "SHORT"}
        assert short_symbols == {"XLRE", "XLC", "XLE"}

    def test_middle_get_neutral_signal(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.20), ("XLF", 0.15), ("XLV", 0.10),
                   ("XLI", 0.05), ("XLB", 0.02), ("XLP", 0.01),
                   ("XLY", -0.01), ("XLU", -0.02), ("XLRE", -0.05),
                   ("XLC", -0.08), ("XLE", -0.12)]
        signals = ant._build_signals(ranking)
        neutral_symbols = {s["symbol"] for s in signals if s["signal"] == "NEUTRAL"}
        assert neutral_symbols == {"XLI", "XLB", "XLP", "XLY", "XLU"}

    def test_signal_count_equals_ranking_count(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.1), ("XLF", 0.05), ("XLE", -0.1)]
        signals = ant._build_signals(ranking)
        assert len(signals) == 3

    def test_signal_has_required_fields(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.10)]
        signals = ant._build_signals(ranking)
        for field in ("symbol", "sector", "return_3mo", "rank", "signal"):
            assert field in signals[0]

    def test_rank_starts_at_one(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.10), ("XLE", -0.05)]
        signals = ant._build_signals(ranking)
        assert signals[0]["rank"] == 1
        assert signals[1]["rank"] == 2


# ---------------------------------------------------------------------------
# 3. Tick
# ---------------------------------------------------------------------------


class TestTick:
    def _make_full_returns(self) -> dict[str, float]:
        symbols = list(_SPDR_ETFS.keys())
        return {s: (i - 5) * 0.01 for i, s in enumerate(symbols)}

    def test_tick_returns_signals_list(self) -> None:
        adapter = make_adapter_with_returns(self._make_full_returns())
        ant = make_ant(adapter=adapter)
        signals = ant._tick()
        assert isinstance(signals, list)
        assert len(signals) == len(_SPDR_ETFS)

    def test_tick_sets_last_action(self) -> None:
        adapter = make_adapter_with_returns(self._make_full_returns())
        ant = make_ant(adapter=adapter)
        ant._tick()
        assert ant._last_action != "init"

    def test_tick_no_data_returns_empty(self) -> None:
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        result = ant._tick()
        assert result == []

    def test_tick_writes_log(self, tmp_path: Path) -> None:
        adapter = make_adapter_with_returns(self._make_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "sector_scout" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_ranking_and_signals(self, tmp_path: Path) -> None:
        adapter = make_adapter_with_returns(self._make_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "sector_scout" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload = records[0]["payload"]
        assert "ranking" in payload
        assert "signals" in payload

    def test_partial_data_still_produces_signals(self) -> None:
        # Only 5 of 11 ETFs have data
        partial = {s: 0.01 * i for i, s in enumerate(list(_SPDR_ETFS.keys())[:5])}
        adapter = make_adapter_with_returns(partial)
        ant = make_ant(adapter=adapter)
        signals = ant._tick()
        assert len(signals) == 5


# ---------------------------------------------------------------------------
# 4. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_no_log_when_logs_root_none(self) -> None:
        adapter = make_adapter_with_returns({"XLK": 0.10, "XLE": -0.05})
        ant = make_ant(adapter=adapter, logs_root=None)
        ant._tick()  # should not crash

    def test_sequence_increments(self, tmp_path: Path) -> None:
        adapter = make_adapter_with_returns({"XLK": 0.10, "XLE": -0.05})
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        ant._tick()
        log_path = tmp_path / "equities" / "sector_scout" / f"{ant.ant_id}.jsonl"
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
        adapter = MagicMock(spec=YahooFinanceAdapter)
        adapter.get_candles.return_value = []
        ant = make_ant(adapter=adapter)
        ant._tick = MagicMock(return_value=[])

        with patch("ant_colony.ants.equities.sector_scout_ant.time.sleep"):
            with patch("ant_colony.ants.equities.sector_scout_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.sector_scout_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.sector_scout_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED
