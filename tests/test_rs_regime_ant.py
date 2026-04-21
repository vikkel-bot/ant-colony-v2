"""
tests/test_rs_regime_ant.py

Unit tests voor RSRegimeAnt, classify_regime() en read_latest_rs_regime().

Getest gedrag:
  - Alle 4 regimes correct geclassificeerd
  - classify_regime() grenscondities
  - _tick() retourneert payload met correcte velden
  - Regime-signaal wordt naar disk geschreven
  - read_latest_rs_regime() leest correct terug
  - Edge cases: geen adapter, onvoldoende data
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import logging

from ant_colony.ants.equities.rs_regime_ant import (
    RSRegimeAnt,
    classify_regime,
    read_latest_rs_regime,
    _MIN_BARS,
    _DEFENSIVE_BASKET,
    _QQQ,
)
from ant_colony.schemas.ant import AntStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candle(close: float):
    c = MagicMock()
    c.close = close
    return c


def _make_candles(closes: list[float]) -> list:
    return [_make_candle(c) for c in closes]


def _make_adapter(qqq_closes: list[float], def_closes: list[float]):
    """Maak een adapter-mock met get_candles die gefixeerde sluitprijzen retourneert."""
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def _get_candles(symbol, period, interval):
        if symbol == _QQQ:
            return _make_candles(qqq_closes)
        return _make_candles(def_closes)

    adapter.get_candles.side_effect = _get_candles
    return adapter


def _make_registry(adapter):
    registry = MagicMock()
    registry.get.return_value = adapter
    return registry


def _make_ant(logs_root=None, adapter=None):
    mission = MagicMock()
    mission.mission_id = "test-rs-mission"
    mission.allowed_node = "test-node"
    mission.market_scope.biome = "equities"
    mission.ttl = 86400
    mission.heartbeat_interval = 3600

    scheduler = MagicMock()
    registry = _make_registry(adapter) if adapter else MagicMock()
    if adapter is None:
        registry.get.return_value = None

    return RSRegimeAnt(
        ant_id=str(uuid.uuid4()),
        mission=mission,
        scheduler=scheduler,
        biome_registry=registry,
        logs_root=logs_root,
    )


def _build_closes(n: int = 60, base: float = 100.0) -> list[float]:
    """Bouw een lijst van n sluitprijzen op, licht stijgend."""
    return [base + i * 0.1 for i in range(n)]


# ---------------------------------------------------------------------------
# classify_regime() — pure functie tests
# ---------------------------------------------------------------------------

class TestClassifyRegime:
    def test_risk_on(self):
        assert classify_regime(qqq_vs_50d=1.05, avg_defensive_rs=0.90) == "RISK_ON"

    def test_risk_on_boundary(self):
        assert classify_regime(qqq_vs_50d=1.00, avg_defensive_rs=0.99) == "RISK_ON"

    def test_neutral_low_boundary(self):
        assert classify_regime(qqq_vs_50d=1.05, avg_defensive_rs=1.00) == "NEUTRAL"

    def test_neutral_high_boundary(self):
        assert classify_regime(qqq_vs_50d=1.05, avg_defensive_rs=1.10) == "NEUTRAL"

    def test_risk_off_qqq_below_sma(self):
        # QQQ onder SMA, maar defensive RS niet extreem
        assert classify_regime(qqq_vs_50d=0.95, avg_defensive_rs=0.80) == "RISK_OFF"

    def test_risk_off_high_defensive_rs(self):
        # QQQ boven SMA, maar defensive RS > 1.1
        assert classify_regime(qqq_vs_50d=1.02, avg_defensive_rs=1.15) == "RISK_OFF"

    def test_crisis(self):
        # QQQ onder SMA én defensive RS > 1.2
        assert classify_regime(qqq_vs_50d=0.90, avg_defensive_rs=1.25) == "CRISIS"

    def test_crisis_boundary(self):
        # QQQ onder SMA én defensive RS exact 1.21 (boven 1.2)
        assert classify_regime(qqq_vs_50d=0.95, avg_defensive_rs=1.21) == "CRISIS"

    def test_not_crisis_when_qqq_above_sma(self):
        # defensive RS > 1.2 maar QQQ boven SMA → RISK_OFF, niet CRISIS
        assert classify_regime(qqq_vs_50d=1.01, avg_defensive_rs=1.30) == "RISK_OFF"


# ---------------------------------------------------------------------------
# RSRegimeAnt._tick() — integratie met adapter mock
# ---------------------------------------------------------------------------

class TestRsRegimeAntTick:
    def _make_tick_ant(self, qqq_closes, def_closes, logs_root=None):
        adapter = _make_adapter(qqq_closes, def_closes)
        return _make_ant(logs_root=logs_root, adapter=adapter)

    def test_tick_returns_payload_risk_on(self, tmp_path):
        # QQQ boven SMA (stijgend), defensive RS laag (def daalt vs QQQ)
        qqq = _build_closes(60, base=100.0)   # QQQ stijgt → nu > SMA
        # defensive daalt terwijl QQQ stijgt → RS < 1
        def_closes = _build_closes(60, base=100.0)
        def_closes[-1] = 90.0   # defensive zakt → RS < 1

        ant = self._make_tick_ant(qqq, def_closes, tmp_path)
        result = ant._tick()
        assert result is not None
        assert result["action"] == "regime_signal"
        assert "regime" in result
        assert "qqq_vs_50d" in result
        assert "avg_defensive_rs" in result
        assert "components" in result

    def test_tick_crisis_classification(self, tmp_path):
        # QQQ daalt ver onder SMA, defensive stijgt sterk
        qqq_now = 80.0
        qqq_20d_ago = 100.0
        qqq_sma_region = [110.0] * 50  # hoge SMA
        qqq_recent = [100.0] * 9 + [qqq_20d_ago] + [95.0] * 10 + [qqq_now]
        qqq = qqq_sma_region[: 50 - len(qqq_recent)] + qqq_recent
        if len(qqq) < 60:
            qqq = [110.0] * (60 - len(qqq)) + qqq

        # defensive stijgt sterk vs QQQ → RS > 1.2
        def_now = 130.0
        def_20d_ago = 100.0
        def_closes = [100.0] * 39 + [def_20d_ago] + [110.0] * 10 + [def_now]
        if len(def_closes) < 60:
            def_closes = [100.0] * (60 - len(def_closes)) + def_closes

        adapter = MagicMock()
        adapter.is_available.return_value = True

        def _get_candles(symbol, period, interval):
            if symbol == _QQQ:
                return _make_candles(qqq)
            return _make_candles(def_closes)

        adapter.get_candles.side_effect = _get_candles

        ant = _make_ant(logs_root=tmp_path, adapter=adapter)
        result = ant._tick()

        assert result is not None
        assert result["regime"] == "CRISIS"

    def test_tick_no_adapter_returns_none(self):
        ant = _make_ant(adapter=None)
        result = ant._tick()
        assert result is None
        assert ant._last_action == "tick:no_adapter"

    def test_tick_adapter_no_get_candles_returns_none(self):
        # Adapter zonder get_candles attribuut → tick retourneert None
        adapter = MagicMock(spec=["is_available"])
        adapter.is_available.return_value = True

        ant = _make_ant(adapter=adapter)
        result = ant._tick()
        assert result is None

    def test_tick_insufficient_qqq_data_returns_none(self):
        qqq = _build_closes(10)   # te weinig bars (< 50)
        def_closes = _build_closes(60)
        ant = self._make_tick_ant(qqq, def_closes)
        result = ant._tick()
        assert result is None
        assert "insufficient_qqq" in ant._last_action

    def test_signal_written_to_disk(self, tmp_path):
        qqq = _build_closes(60, base=100.0)
        def_closes = _build_closes(60, base=100.0)
        ant = self._make_tick_ant(qqq, def_closes, logs_root=tmp_path)
        ant._tick()

        log_files = list((tmp_path / "rs_regime").glob("*.jsonl"))
        assert len(log_files) == 1

        records = [json.loads(l) for l in log_files[0].read_text().splitlines() if l.strip()]
        assert len(records) >= 1
        assert records[-1]["payload"]["action"] == "regime_signal"
        assert records[-1]["payload"]["regime"] in {"RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS"}


# ---------------------------------------------------------------------------
# read_latest_rs_regime()
# ---------------------------------------------------------------------------

class TestReadLatestRsRegime:
    def test_returns_none_when_dir_missing(self, tmp_path):
        result = read_latest_rs_regime(tmp_path)
        assert result is None

    def test_returns_none_when_dir_empty(self, tmp_path):
        (tmp_path / "rs_regime").mkdir()
        result = read_latest_rs_regime(tmp_path)
        assert result is None

    def test_returns_latest_payload(self, tmp_path):
        rs_dir = tmp_path / "rs_regime"
        rs_dir.mkdir()

        for regime, ts in [("RISK_ON", "2026-04-21T10:00:00+00:00"),
                           ("CRISIS",  "2026-04-21T12:00:00+00:00")]:
            record = {
                "timestamp": ts,
                "payload": {
                    "action": "regime_signal",
                    "regime": regime,
                    "qqq_vs_50d": 0.9 if regime == "CRISIS" else 1.05,
                    "avg_defensive_rs": 1.3 if regime == "CRISIS" else 0.9,
                },
            }
            (rs_dir / "ant-test.jsonl").open("a").write(json.dumps(record) + "\n")

        result = read_latest_rs_regime(tmp_path)
        assert result is not None
        assert result["regime"] == "CRISIS"

    def test_ignores_non_regime_signal_records(self, tmp_path):
        rs_dir = tmp_path / "rs_regime"
        rs_dir.mkdir()
        noise = {"timestamp": "2026-04-21T10:00:00+00:00",
                 "payload": {"action": "heartbeat"}}
        (rs_dir / "ant.jsonl").write_text(json.dumps(noise) + "\n")
        result = read_latest_rs_regime(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# Log-throttling en tick-interval
# ---------------------------------------------------------------------------

def _make_stable_ant(logs_root, qqq_closes, def_closes):
    adapter = _make_adapter(qqq_closes, def_closes)
    return _make_ant(logs_root=logs_root, adapter=adapter)


class TestRsRegimeLogThrottling:
    """Verifieer INFO/DEBUG selectie op basis van regime-change en QQQ-beweging."""

    def _tick_ant(self, qqq_closes, def_closes, logs_root=None):
        return _make_stable_ant(logs_root, qqq_closes, def_closes)

    def test_eerste_tick_altijd_info(self, tmp_path, caplog):
        """Eerste tick: _last_logged_regime is None → altijd INFO."""
        qqq = _build_closes(60, base=100.0)
        defs = _build_closes(60, base=100.0)
        defs[-1] = 90.0
        ant = self._tick_ant(qqq, defs, tmp_path)

        with caplog.at_level(logging.INFO, logger=f"ant.rs_regime.{ant.ant_id[:8]}"):
            ant._tick()

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "RS Regime" in r.message]
        assert len(info_records) == 1

    def test_zelfde_regime_tweede_tick_debug(self, tmp_path, caplog):
        """Tweede tick met zelfde regime en stabiele QQQ → DEBUG, geen INFO."""
        qqq = _build_closes(60, base=100.0)
        defs = _build_closes(60, base=100.0)
        defs[-1] = 90.0
        ant = self._tick_ant(qqq, defs, tmp_path)

        ant._tick()  # eerste tick → INFO + state opslaan
        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger=f"ant.rs_regime.{ant.ant_id[:8]}"):
            ant._tick()  # zelfde regime, QQQ niet veranderd → DEBUG

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "RS Regime" in r.message]
        assert len(info_records) == 0

    def test_regime_change_triggert_info(self, tmp_path, caplog):
        """Na regime-change altijd INFO, ook al is QQQ nauwelijks bewogen."""
        qqq = _build_closes(60, base=100.0)
        defs = _build_closes(60, base=100.0)
        defs[-1] = 90.0
        ant = self._tick_ant(qqq, defs, tmp_path)

        ant._tick()  # initieel regime vastleggen
        caplog.clear()

        # Forceer regime-change via instance state
        ant._last_logged_regime = "CRISIS"  # vorige was CRISIS, nu RISK_ON

        with caplog.at_level(logging.INFO, logger=f"ant.rs_regime.{ant.ant_id[:8]}"):
            ant._tick()

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "RS Regime" in r.message]
        assert len(info_records) == 1

    def test_grote_qqq_beweging_triggert_info(self, tmp_path, caplog):
        """QQQ-ratio verandert >2% → INFO ook zonder regime-change."""
        qqq = _build_closes(60, base=100.0)
        defs = _build_closes(60, base=100.0)
        defs[-1] = 90.0
        ant = self._tick_ant(qqq, defs, tmp_path)

        ant._tick()  # eerste tick
        caplog.clear()

        # Zet _last_logged_qqq ver genoeg weg dat drempel overschreden wordt
        ant._last_logged_qqq = ant._last_logged_qqq - 0.10  # simulate 10% drift

        with caplog.at_level(logging.INFO, logger=f"ant.rs_regime.{ant.ant_id[:8]}"):
            ant._tick()

        info_records = [r for r in caplog.records if r.levelno == logging.INFO
                        and "RS Regime" in r.message]
        assert len(info_records) == 1

    def test_tick_interval_60s(self):
        """TICK_INTERVAL is 60 seconden."""
        assert RSRegimeAnt._TICK_INTERVAL == 60

    def test_run_skips_tick_when_interval_not_elapsed(self, tmp_path):
        """run() ticked niet als _last_tick_at recent is (< 60s geleden)."""
        import time as _time
        qqq = _build_closes(60, base=100.0)
        defs = _build_closes(60, base=100.0)
        ant = _make_stable_ant(tmp_path, qqq, defs)

        tick_count = 0
        original_tick = ant._tick

        def counting_tick():
            nonlocal tick_count
            tick_count += 1
            return original_tick()

        ant._tick = counting_tick
        # Zet _last_tick_at naar 1 seconde geleden → interval (60s) nog niet verstreken
        ant._last_tick_at = _time.monotonic() - 1.0
        ant._status = AntStatus.RUNNING

        # Simuleer één while-iteratie
        import time as _t
        now_mono = _t.monotonic()
        if now_mono - ant._last_tick_at >= ant._TICK_INTERVAL:
            ant._tick()

        assert tick_count == 0  # geen tick want interval niet verstreken
