"""
tests/test_sector_scout_ant.py

Tests voor SectorScoutAnt.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.sector_scout_ant import (
    SectorScoutAnt,
    _SPDR_ETFS,
    _TOP_N,
)
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
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
        market_scope=MarketScope(biome="equities", symbols=["XLK"]),
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
        open=open_, high=close + 1, low=max(open_ - 1, 0.01), close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def make_adapter(symbol_returns: dict[str, float]) -> MagicMock:
    """Mock adapter where get_candles simulates the given returns."""
    adapter = MagicMock()
    adapter.is_available.return_value = True

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
    adapter.get_market_data.side_effect = lambda sym, tf: make_candle(sym, 110.0)
    return adapter


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None) -> SectorScoutAnt:
    if adapter is None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_candles.return_value = []
        adapter.get_market_data.return_value = None
    return SectorScoutAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
    )


def _full_returns() -> dict[str, float]:
    symbols = list(_SPDR_ETFS.keys())
    return {s: (i - 5) * 0.01 for i, s in enumerate(symbols)}


# ---------------------------------------------------------------------------
# 1. 3-maands return berekening
# ---------------------------------------------------------------------------


class TestGet3moReturn:
    def test_positive_return_calculated(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLK", 100.0), make_candle("XLK", 115.0)]
        result = ant._get_3mo_return("XLK", lambda *a, **k: candles)
        assert result == pytest.approx(0.15)

    def test_negative_return_calculated(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLE", 200.0), make_candle("XLE", 180.0)]
        result = ant._get_3mo_return("XLE", lambda *a, **k: candles)
        assert result == pytest.approx(-0.10)

    def test_zero_first_close_returns_none(self) -> None:
        ant = make_ant()
        candles = [make_candle("XLV", 0.0), make_candle("XLV", 50.0)]
        result = ant._get_3mo_return("XLV", lambda *a, **k: candles)
        assert result is None

    def test_single_candle_returns_none(self) -> None:
        ant = make_ant()
        result = ant._get_3mo_return("XLK", lambda *a, **k: [make_candle("XLK", 100.0)])
        assert result is None

    def test_empty_candles_returns_none(self) -> None:
        ant = make_ant()
        result = ant._get_3mo_return("XLK", lambda *a, **k: [])
        assert result is None

    def test_get_candles_called_with_3mo_period(self) -> None:
        ant = make_ant()
        calls = []

        def recording_fn(sym, period="3mo", interval="1d"):
            calls.append((sym, period, interval))
            return []

        ant._get_3mo_return("XLK", recording_fn)
        assert calls == [("XLK", _SPDR_ETFS and "3mo", "1d")]
        assert calls[0][1] == "3mo"

    def test_exception_in_candles_returns_none(self) -> None:
        ant = make_ant()

        def boom(*a, **k):
            raise RuntimeError("api error")

        result = ant._get_3mo_return("XLK", boom)
        assert result is None


# ---------------------------------------------------------------------------
# 2. Signalen bouwen
# ---------------------------------------------------------------------------


class TestBuildSignals:
    def test_top_n_get_long_signal(self) -> None:
        ant = make_ant()
        ranking = [(sym, 0.10 - i * 0.02) for i, sym in enumerate(_SPDR_ETFS.keys())]
        signals = ant._build_signals(ranking)
        long_symbols = {s["symbol"] for s in signals if s["signal"] == "LONG"}
        expected_top = {sym for sym, _ in ranking[:_TOP_N]}
        assert long_symbols == expected_top

    def test_rest_get_neutral_signal(self) -> None:
        ant = make_ant()
        ranking = [(sym, 0.10 - i * 0.02) for i, sym in enumerate(_SPDR_ETFS.keys())]
        signals = ant._build_signals(ranking)
        neutral_symbols = {s["symbol"] for s in signals if s["signal"] == "NEUTRAL"}
        expected_rest = {sym for sym, _ in ranking[_TOP_N:]}
        assert neutral_symbols == expected_rest

    def test_signal_count_equals_ranking_count(self) -> None:
        ant = make_ant()
        ranking = [("XLK", 0.1), ("XLF", 0.05), ("XLE", -0.1)]
        signals = ant._build_signals(ranking)
        assert len(signals) == 3

    def test_signal_has_required_fields(self) -> None:
        ant = make_ant()
        signals = ant._build_signals([("XLK", 0.10)])
        for field in ("symbol", "sector", "return_3mo", "rank", "signal"):
            assert field in signals[0]

    def test_rank_starts_at_one(self) -> None:
        ant = make_ant()
        signals = ant._build_signals([("XLK", 0.10), ("XLE", -0.05)])
        assert signals[0]["rank"] == 1
        assert signals[1]["rank"] == 2

    def test_sector_name_populated(self) -> None:
        ant = make_ant()
        signals = ant._build_signals([("XLK", 0.10)])
        assert signals[0]["sector"] == _SPDR_ETFS["XLK"]

    def test_return_value_rounded(self) -> None:
        ant = make_ant()
        signals = ant._build_signals([("XLK", 0.123456789)])
        assert signals[0]["return_3mo"] == pytest.approx(0.123457, abs=1e-5)


# ---------------------------------------------------------------------------
# 3. Tick
# ---------------------------------------------------------------------------


class TestTick:
    def test_tick_returns_signals_list(self) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter)
        signals = ant._tick()
        assert isinstance(signals, list)
        assert len(signals) == len(_SPDR_ETFS)

    def test_tick_sets_last_action(self) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter)
        ant._tick()
        assert ant._last_action != "init"

    def test_tick_no_data_returns_empty(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_candles.return_value = []
        adapter.get_market_data.return_value = None
        ant = make_ant(adapter=adapter)
        result = ant._tick()
        assert result == []

    def test_tick_processes_yfinance_fallback_bars(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True

        def get_candles(symbol, period="3mo", interval="1d"):
            if symbol != "XLK":
                return []
            return [
                make_candle("XLK", 100.0),
                make_candle("XLK", 112.0),
            ]

        adapter.get_candles.side_effect = get_candles
        adapter.get_market_data.return_value = make_candle("XLK", 112.0)
        ant = make_ant(adapter=adapter)

        signals = ant._tick()

        assert len(signals) == 1
        assert signals[0]["symbol"] == "XLK"
        assert signals[0]["return_3mo"] == pytest.approx(0.12)
        assert signals[0]["signal"] == "LONG"

    def test_tick_no_adapter_returns_empty(self) -> None:
        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = SectorScoutAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
        )
        result = ant._tick()
        assert result == []

    def test_tick_unavailable_adapter_returns_empty(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = False
        ant = make_ant(adapter=adapter)
        result = ant._tick()
        assert result == []

    def test_tick_writes_signals_log(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_contains_opportunity_detected_events(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(records) == _TOP_N
        assert all(r["payload"]["action"] == "opportunity_detected" for r in records)

    def test_log_signal_has_required_payload_fields(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        payload = records[0]["payload"]
        for field in ("signal_id", "symbol", "current_price", "confidence",
                      "change_pct", "signal_type", "sector_name", "momentum_rank"):
            assert field in payload

    def test_partial_data_still_produces_signals(self) -> None:
        partial = {s: 0.01 * i for i, s in enumerate(list(_SPDR_ETFS.keys())[:5])}
        adapter = make_adapter(partial)
        ant = make_ant(adapter=adapter)
        signals = ant._tick()
        assert len(signals) == 5

    def test_no_current_price_skips_signal_log(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        adapter.get_market_data.side_effect = None
        adapter.get_market_data.return_value = None
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists() or log_path.read_text().strip() == ""


# ---------------------------------------------------------------------------
# 4. Deduplicatie
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_day_signal_not_emitted_twice(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        # Only _TOP_N signals total — duplicates suppressed
        assert len(records) == _TOP_N

    def test_signal_id_format(self, tmp_path: Path) -> None:
        from datetime import date
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        today = date.today().isoformat()
        for rec in records:
            sid = rec["payload"]["signal_id"]
            assert today in sid
            assert sid.startswith("sector-")


# ---------------------------------------------------------------------------
# 5. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_no_log_when_logs_root_none(self) -> None:
        adapter = make_adapter({"XLK": 0.10, "XLE": -0.05})
        ant = make_ant(adapter=adapter, logs_root=None)
        ant._tick()

    def test_sequence_increments(self, tmp_path: Path) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter, logs_root=tmp_path)
        # Clear dedup so second tick emits again
        ant._emitted_signal_ids.clear()
        ant._tick()
        ant._emitted_signal_ids.clear()
        ant._tick()
        log_path = tmp_path / "scouts" / f"{ant.ant_id}.jsonl"
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        seqs = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))


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

    def test_initial_status_is_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE


# ---------------------------------------------------------------------------
# 8. Tick throttling
# ---------------------------------------------------------------------------


class TestTickThrottling:
    def test_tick_not_called_before_interval(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=[])
        # last_tick_at almost at now — interval not expired yet
        ant._last_tick_at = 1e18  # far future monotonic

        with patch("ant_colony.ants.equities.sector_scout_ant.time.sleep"):
            with patch("ant_colony.ants.equities.sector_scout_ant.time.monotonic", return_value=1e18 + 1):
                with patch("ant_colony.ants.equities.sector_scout_ant.datetime") as mock_dt:
                    start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    expired = start + timedelta(seconds=ant.mission.ttl + 1)
                    mock_dt.now.side_effect = [start, start, expired]
                    ant.run()

        ant._tick.assert_not_called()

    def test_tick_called_after_interval_expired(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=[])
        ant._last_tick_at = 0.0  # interval always expired

        with patch("ant_colony.ants.equities.sector_scout_ant.time.sleep"):
            with patch("ant_colony.ants.equities.sector_scout_ant.time.monotonic", return_value=99999.0):
                with patch("ant_colony.ants.equities.sector_scout_ant.datetime") as mock_dt:
                    start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    expired = start + timedelta(seconds=ant.mission.ttl + 1)
                    mock_dt.now.side_effect = [start, start, expired]
                    ant.run()

        ant._tick.assert_called_once()

    def test_tick_interval_is_300(self) -> None:
        assert SectorScoutAnt._TICK_INTERVAL == 300

    def test_last_tick_at_initialized_zero(self) -> None:
        ant = make_ant()
        assert ant._last_tick_at == 0.0


# ---------------------------------------------------------------------------
# 9. Change detection logging
# ---------------------------------------------------------------------------


class TestChangeDetection:
    def test_info_logged_on_first_tick(self, caplog) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter)

        with caplog.at_level(logging.INFO, logger=f"ant.sector_scout.{ant.ant_id[:8]}"):
            ant._tick()

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "Sector ranking" in r.message]
        assert len(info_records) == 1

    def test_debug_logged_on_unchanged_tick(self, caplog) -> None:
        adapter = make_adapter(_full_returns())
        ant = make_ant(adapter=adapter)
        ant._tick()  # prime state

        with caplog.at_level(logging.DEBUG, logger=f"ant.sector_scout.{ant.ant_id[:8]}"):
            ant._tick()

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG
                         and "onveranderd" in r.message]
        assert len(debug_records) == 1

    def test_info_logged_on_top3_change(self, caplog) -> None:
        adapter1 = make_adapter({s: (i * 0.01) for i, s in enumerate(_SPDR_ETFS.keys())})
        ant = make_ant(adapter=adapter1)
        ant._tick()

        # Change the ranking order completely
        adapter2 = make_adapter({s: ((10 - i) * 0.01) for i, s in enumerate(_SPDR_ETFS.keys())})
        ant.biome_registry.get.return_value = adapter2

        with caplog.at_level(logging.INFO, logger=f"ant.sector_scout.{ant.ant_id[:8]}"):
            ant._tick()

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "Sector ranking" in r.message]
        assert len(info_records) >= 1
