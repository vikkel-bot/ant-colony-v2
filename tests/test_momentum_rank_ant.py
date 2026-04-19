"""
tests/test_momentum_rank_ant.py

Tests voor MomentumRankAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.momentum_rank_ant import (
    MomentumRankAnt,
    _SPDR_ETFS,
    _W1M, _W3M, _W6M,
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
        ant_type="momentum_rank_ant",
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
        success_conditions=SuccessConditions(description="momentum rank test"),
    )


def make_candle(symbol: str, close: float) -> MarketData:
    return MarketData(
        symbol=symbol, timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=close * 0.99, high=close * 1.01, low=close * 0.98, close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def make_adapter(returns_by_period: dict[str, dict[str, float]] | None = None) -> MagicMock:
    """
    returns_by_period: {symbol: {period: return_value}}
    e.g. {"XLK": {"1mo": 0.05, "3mo": 0.10, "6mo": 0.08}}
    """
    adapter = MagicMock()
    adapter.is_available.return_value = True

    def get_candles(symbol, period="3mo", interval="1d"):
        if returns_by_period is None:
            return []
        sym_data = (returns_by_period or {}).get(symbol, {})
        r = sym_data.get(period)
        if r is None:
            return []
        first = 100.0
        last  = first * (1 + r)
        return [make_candle(symbol, first), make_candle(symbol, last)]

    adapter.get_candles.side_effect = get_candles
    return adapter


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None) -> MomentumRankAnt:
    if adapter is None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_candles.return_value = []
    return MomentumRankAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
    )


def write_scout_signal(scouts_dir: Path, symbol: str) -> None:
    scouts_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "payload": {
            "action": "opportunity_detected",
            "symbol": symbol,
            "signal_type": "sector_rotation",
        },
    }
    (scouts_dir / "scout.jsonl").open("a").write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# 1. Period return berekening
# ---------------------------------------------------------------------------


class TestPeriodReturn:
    def test_positive_return(self) -> None:
        candles = [make_candle("XLK", 100.0), make_candle("XLK", 110.0)]
        result = MomentumRankAnt._period_return("XLK", "1mo", lambda *a, **k: candles)
        assert result == pytest.approx(0.10)

    def test_negative_return(self) -> None:
        candles = [make_candle("XLE", 200.0), make_candle("XLE", 180.0)]
        result = MomentumRankAnt._period_return("XLE", "3mo", lambda *a, **k: candles)
        assert result == pytest.approx(-0.10)

    def test_empty_candles_returns_none(self) -> None:
        result = MomentumRankAnt._period_return("XLK", "1mo", lambda *a, **k: [])
        assert result is None

    def test_single_candle_returns_none(self) -> None:
        result = MomentumRankAnt._period_return(
            "XLK", "1mo", lambda *a, **k: [make_candle("XLK", 100.0)]
        )
        assert result is None

    def test_zero_first_close_returns_none(self) -> None:
        result = MomentumRankAnt._period_return(
            "XLK", "1mo", lambda *a, **k: [make_candle("XLK", 0.0), make_candle("XLK", 50.0)]
        )
        assert result is None

    def test_exception_returns_none(self) -> None:
        result = MomentumRankAnt._period_return(
            "XLK", "1mo", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api"))
        )
        assert result is None


# ---------------------------------------------------------------------------
# 2. Score symbool
# ---------------------------------------------------------------------------


class TestScoreSymbol:
    def _fn(self, returns: dict[str, float]):
        def get_candles(symbol, period="3mo", interval="1d"):
            r = returns.get(period)
            if r is None:
                return []
            return [make_candle(symbol, 100.0), make_candle(symbol, 100.0 * (1 + r))]
        return get_candles

    def test_weighted_score_calculated(self) -> None:
        ant = make_ant()
        fn  = self._fn({"1mo": 0.10, "3mo": 0.12, "6mo": 0.06})
        row = ant._score_symbol("XLK", fn)
        expected = 0.10 * _W1M + 0.12 * _W3M + 0.06 * _W6M
        assert row is not None
        assert row["score"] == pytest.approx(expected, abs=1e-5)

    def test_missing_period_normalizes_weights(self) -> None:
        ant = make_ant()
        fn  = self._fn({"1mo": 0.10, "3mo": 0.10})  # 6mo missing
        row = ant._score_symbol("XLK", fn)
        assert row is not None
        # weights renormalized to (0.40 + 0.40) = 0.80
        expected = (0.10 * _W1M + 0.10 * _W3M) / (_W1M + _W3M)
        assert row["score"] == pytest.approx(expected, abs=1e-5)

    def test_no_data_returns_none(self) -> None:
        ant = make_ant()
        row = ant._score_symbol("XLK", lambda *a, **k: [])
        assert row is None

    def test_row_has_required_fields(self) -> None:
        ant = make_ant()
        fn  = self._fn({"1mo": 0.05, "3mo": 0.08, "6mo": 0.04})
        row = ant._score_symbol("XLK", fn)
        assert row is not None
        for field in ("symbol", "sector", "score", "r1m", "r3m", "r6m", "rank"):
            assert field in row

    def test_sector_name_populated(self) -> None:
        ant = make_ant()
        fn  = self._fn({"1mo": 0.05, "3mo": 0.08, "6mo": 0.04})
        row = ant._score_symbol("XLK", fn)
        assert row is not None
        assert row["sector"] == _SPDR_ETFS["XLK"]

    def test_missing_period_recorded_as_none(self) -> None:
        ant = make_ant()
        fn  = self._fn({"3mo": 0.10})
        row = ant._score_symbol("XLK", fn)
        assert row is not None
        assert row["r1m"] is None
        assert row["r3m"] == pytest.approx(0.10)
        assert row["r6m"] is None


# ---------------------------------------------------------------------------
# 3. Sectoruniversum laden
# ---------------------------------------------------------------------------


class TestLoadSectorUniverse:
    def test_no_logs_root_returns_all_spdrs(self) -> None:
        ant = make_ant(logs_root=None)
        universe = ant._load_sector_universe()
        assert set(universe) == set(_SPDR_ETFS.keys())

    def test_no_scouts_dir_returns_all_spdrs(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        universe = ant._load_sector_universe()
        assert set(universe) == set(_SPDR_ETFS.keys())

    def test_scouts_dir_symbols_included(self, tmp_path: Path) -> None:
        write_scout_signal(tmp_path / "scouts", "XLK")
        write_scout_signal(tmp_path / "scouts", "XLF")
        ant = make_ant(logs_root=tmp_path)
        universe = ant._load_sector_universe()
        assert "XLK" in universe
        assert "XLF" in universe

    def test_scouts_dir_merges_with_spdr_universe(self, tmp_path: Path) -> None:
        write_scout_signal(tmp_path / "scouts", "XLK")
        ant = make_ant(logs_root=tmp_path)
        universe = ant._load_sector_universe()
        # Full SPDR list always included
        assert set(_SPDR_ETFS.keys()).issubset(set(universe))

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        scouts_dir = tmp_path / "scouts"
        scouts_dir.mkdir()
        (scouts_dir / "bad.jsonl").write_text("not valid json\n")
        ant = make_ant(logs_root=tmp_path)
        universe = ant._load_sector_universe()
        assert set(universe) == set(_SPDR_ETFS.keys())

    def test_non_opportunity_events_ignored(self, tmp_path: Path) -> None:
        scouts_dir = tmp_path / "scouts"
        scouts_dir.mkdir()
        record = {"payload": {"action": "something_else", "symbol": "FAKE"}}
        (scouts_dir / "other.jsonl").write_text(json.dumps(record) + "\n")
        ant = make_ant(logs_root=tmp_path)
        universe = ant._load_sector_universe()
        assert "FAKE" not in universe


# ---------------------------------------------------------------------------
# 4. Tick
# ---------------------------------------------------------------------------


class TestTick:
    def _returns_for(self, symbols: list[str], r: float = 0.05) -> dict:
        return {s: {"1mo": r, "3mo": r * 1.2, "6mo": r * 0.8} for s in symbols}

    def test_tick_returns_ranking_list(self) -> None:
        data    = self._returns_for(list(_SPDR_ETFS.keys()))
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter)
        ranking = ant._tick()
        assert isinstance(ranking, list)
        assert len(ranking) == len(_SPDR_ETFS)

    def test_ranking_sorted_by_score_descending(self) -> None:
        data    = {s: {"1mo": i * 0.01, "3mo": i * 0.01, "6mo": i * 0.01}
                   for i, s in enumerate(_SPDR_ETFS.keys())}
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter)
        ranking = ant._tick()
        scores  = [r["score"] for r in ranking]
        assert scores == sorted(scores, reverse=True)

    def test_rank_field_starts_at_one(self) -> None:
        data    = self._returns_for(["XLK", "XLE"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter)
        ranking = ant._tick()
        assert ranking[0]["rank"] == 1

    def test_tick_no_adapter_returns_empty(self) -> None:
        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = MomentumRankAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
        )
        assert ant._tick() == []

    def test_tick_unavailable_adapter_returns_empty(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = False
        ant     = make_ant(adapter=adapter)
        assert ant._tick() == []

    def test_tick_no_scores_returns_empty(self) -> None:
        adapter = make_adapter({})
        ant     = make_ant(adapter=adapter)
        assert ant._tick() == []

    def test_tick_dedup_same_day(self) -> None:
        data    = self._returns_for(["XLK"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter)
        ant._tick()
        result = ant._tick()
        assert result == []

    def test_tick_writes_log(self, tmp_path: Path) -> None:
        data    = self._returns_for(["XLK", "XLE"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "momentum_rank" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_momentum_ranking_action(self, tmp_path: Path) -> None:
        data    = self._returns_for(["XLK", "XLE"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "momentum_rank" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["payload"]["action"] == "momentum_ranking"

    def test_log_has_ranking_list(self, tmp_path: Path) -> None:
        data    = self._returns_for(["XLK", "XLE"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "momentum_rank" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload  = records[0]["payload"]
        assert "ranking" in payload
        assert isinstance(payload["ranking"], list)

    def test_log_has_weights(self, tmp_path: Path) -> None:
        data    = self._returns_for(["XLK"])
        adapter = make_adapter(data)
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "momentum_rank" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        weights  = records[0]["payload"]["weights"]
        assert weights["1m"] == pytest.approx(_W1M)
        assert weights["3m"] == pytest.approx(_W3M)
        assert weights["6m"] == pytest.approx(_W6M)

    def test_symbol_exception_does_not_abort_tick(self) -> None:
        call_count = [0]

        def flaky_candles(symbol, period="3mo", interval="1d"):
            call_count[0] += 1
            if call_count[0] % 3 == 0:
                raise RuntimeError("flaky")
            return []

        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_candles.side_effect = flaky_candles
        ant     = make_ant(adapter=adapter)
        result  = ant._tick()
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
        with patch("ant_colony.ants.equities.momentum_rank_ant.time.sleep"):
            with patch("ant_colony.ants.equities.momentum_rank_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status  = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.momentum_rank_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.momentum_rank_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        assert make_ant()._status == AntStatus.IDLE
