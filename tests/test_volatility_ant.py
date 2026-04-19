"""
tests/test_volatility_ant.py

Tests voor VolatilityAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.volatility_ant import (
    VolatilityAnt,
    _VIX_HEDGE_THRESHOLD,
    _VIX_REDUCE_THRESHOLD,
    _VIX_SEVERITY_MAX,
    _REGIME_NORMAL,
    _REGIME_HEDGE,
    _REGIME_REDUCE_EQUITY,
    _VIX_SYMBOL,
)
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission() -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="volatility_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["^VIX"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="volatility test"),
    )


def make_vix_candle(vix_close: float) -> MarketData:
    return MarketData(
        symbol=_VIX_SYMBOL, timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=vix_close - 0.5, high=vix_close + 1.0, low=vix_close - 1.0,
        close=vix_close, volume=0.0, biome_id="equities",
    )


def make_adapter(vix: float | None = 20.0) -> MagicMock:
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def get_candles(symbol, period="5d", interval="1d"):
        if vix is None:
            return []
        return [make_vix_candle(vix)]

    adapter.get_candles.side_effect = get_candles
    return adapter


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None) -> VolatilityAnt:
    if adapter is None:
        adapter = make_adapter()
    return VolatilityAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
    )


# ---------------------------------------------------------------------------
# 1. VIX classificatie
# ---------------------------------------------------------------------------


class TestClassifyVix:
    def test_normal_below_hedge_threshold(self) -> None:
        assert VolatilityAnt._classify_vix(_VIX_HEDGE_THRESHOLD - 0.1) == _REGIME_NORMAL

    def test_normal_at_hedge_threshold(self) -> None:
        assert VolatilityAnt._classify_vix(_VIX_HEDGE_THRESHOLD) == _REGIME_NORMAL

    def test_hedge_just_above_threshold(self) -> None:
        assert VolatilityAnt._classify_vix(_VIX_HEDGE_THRESHOLD + 0.1) == _REGIME_HEDGE

    def test_hedge_between_thresholds(self) -> None:
        vix = (_VIX_HEDGE_THRESHOLD + _VIX_REDUCE_THRESHOLD) / 2
        assert VolatilityAnt._classify_vix(vix) == _REGIME_HEDGE

    def test_reduce_equity_at_reduce_threshold(self) -> None:
        assert VolatilityAnt._classify_vix(_VIX_REDUCE_THRESHOLD + 0.1) == _REGIME_REDUCE_EQUITY

    def test_reduce_equity_very_high_vix(self) -> None:
        assert VolatilityAnt._classify_vix(60.0) == _REGIME_REDUCE_EQUITY


# ---------------------------------------------------------------------------
# 2. VIX ophalen
# ---------------------------------------------------------------------------


class TestGetVix:
    def test_returns_last_candle_close(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=22.5))
        vix = ant._get_vix()
        assert vix == pytest.approx(22.5)

    def test_no_adapter_returns_none(self) -> None:
        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = VolatilityAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
        )
        assert ant._get_vix() is None

    def test_unavailable_adapter_returns_none(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = False
        ant = make_ant(adapter=adapter)
        assert ant._get_vix() is None

    def test_empty_candles_returns_none(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=None))
        assert ant._get_vix() is None

    def test_adapter_exception_returns_none(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_candles.side_effect = RuntimeError("api error")
        ant = make_ant(adapter=adapter)
        assert ant._get_vix() is None

    def test_no_get_candles_method_returns_none(self) -> None:
        adapter = MagicMock(spec=[])  # no methods
        adapter.is_available = MagicMock(return_value=True)
        ant = make_ant(adapter=adapter)
        assert ant._get_vix() is None


# ---------------------------------------------------------------------------
# 3. Tick
# ---------------------------------------------------------------------------


class TestTick:
    def test_normal_regime_emits_signal(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=15.0))
        signal = ant._tick()
        assert signal is not None
        assert signal["regime"] == _REGIME_NORMAL

    def test_hedge_regime_emits_signal(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=_VIX_HEDGE_THRESHOLD + 5))
        signal = ant._tick()
        assert signal is not None
        assert signal["regime"] == _REGIME_HEDGE

    def test_reduce_equity_regime_emits_signal(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=_VIX_REDUCE_THRESHOLD + 5))
        signal = ant._tick()
        assert signal is not None
        assert signal["regime"] == _REGIME_REDUCE_EQUITY

    def test_signal_has_required_fields(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=20.0))
        signal = ant._tick()
        assert signal is not None
        for field in ("action", "signal_date", "vix_value", "regime", "severity", "biome"):
            assert field in signal, f"missing: {field}"

    def test_signal_action_is_vix_signal(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=20.0))
        signal = ant._tick()
        assert signal is not None
        assert signal["action"] == "vix_signal"

    def test_vix_value_in_signal(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=28.3))
        signal = ant._tick()
        assert signal is not None
        assert signal["vix_value"] == pytest.approx(28.3, abs=0.01)

    def test_severity_normalized_0_to_1(self) -> None:
        for vix in [0.0, 25.0, 50.0, 80.0]:
            ant    = make_ant(adapter=make_adapter(vix=vix))
            signal = ant._tick()
            if signal is not None:
                assert 0.0 <= signal["severity"] <= 1.0

    def test_severity_at_max_vix(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=_VIX_SEVERITY_MAX))
        signal = ant._tick()
        assert signal is not None
        assert signal["severity"] == pytest.approx(1.0)

    def test_no_vix_returns_none(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=None))
        assert ant._tick() is None

    def test_dedup_same_day(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0))
        ant._tick()
        assert ant._tick() is None

    def test_signal_date_is_today(self) -> None:
        ant    = make_ant(adapter=make_adapter(vix=20.0))
        signal = ant._tick()
        assert signal is not None
        assert signal["signal_date"] == date.today().isoformat()

    def test_last_action_updated(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0))
        ant._tick()
        assert ant._last_action != "init"

    def test_dedup_last_action_set(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0))
        ant._tick()
        ant._tick()
        assert "dedup" in ant._last_action


# ---------------------------------------------------------------------------
# 4. Log output
# ---------------------------------------------------------------------------


class TestLogOutput:
    def test_writes_log_file(self, tmp_path: Path) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0), logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "volatility" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_vix_signal_action(self, tmp_path: Path) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0), logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "volatility" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["payload"]["action"] == "vix_signal"

    def test_log_has_vix_value_and_regime(self, tmp_path: Path) -> None:
        ant = make_ant(adapter=make_adapter(vix=30.0), logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "volatility" / f"{ant.ant_id}.jsonl"
        payload  = json.loads(log_path.read_text().splitlines()[0])["payload"]
        assert payload["vix_value"] == pytest.approx(30.0, abs=0.01)
        assert payload["regime"] == _REGIME_HEDGE

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(adapter=make_adapter(vix=20.0), logs_root=None)
        ant._tick()  # must not crash

    def test_no_log_when_vix_unavailable(self, tmp_path: Path) -> None:
        ant = make_ant(adapter=make_adapter(vix=None), logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "volatility" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_sequence_increments(self, tmp_path: Path) -> None:
        adapter = make_adapter(vix=20.0)
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        # Two ticks on different "days"
        ant._tick()
        ant._emitted_dates.clear()
        ant._tick()
        log_path = tmp_path / "equities" / "volatility" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs     = [r["sequence"] for r in records]
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
        ant._tick = MagicMock(return_value=None)
        with patch("ant_colony.ants.equities.volatility_ant.time.sleep"):
            with patch("ant_colony.ants.equities.volatility_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status  = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.volatility_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.volatility_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        assert make_ant()._status == AntStatus.IDLE
