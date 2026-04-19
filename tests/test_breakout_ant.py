"""
tests/test_breakout_ant.py

Tests voor BreakoutAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.breakout_ant import (
    BreakoutAnt,
    _BREAKOUT_MARGIN,
    _READ_ACTION,
    _WRITE_ACTION,
    _SL_PCT,
    _TP_PCT,
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
        ant_type="breakout_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["AAPL"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="breakout test"),
    )


def make_candle(symbol: str, close: float, high: float | None = None) -> MarketData:
    h = high if high is not None else close + 1
    return MarketData(
        symbol=symbol, timeframe="1d",
        timestamp=datetime.now(tz=timezone.utc),
        open=close - 1, high=h, low=close - 2, close=close,
        volume=1_000_000.0, biome_id="equities",
    )


def make_candles_near_high(symbol: str, current: float, high_52w: float) -> list[MarketData]:
    """Return 2 candles: an old one at the 52w high, and a recent one near it."""
    return [
        make_candle(symbol, high_52w, high=high_52w),
        make_candle(symbol, current, high=current + 0.5),
    ]


def make_adapter(candles_fn=None, available: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.is_available.return_value = available
    if candles_fn is not None:
        adapter.get_candles.side_effect = candles_fn
    else:
        adapter.get_candles.return_value = []
    return adapter


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None, piotroski_log_dir=None,
             breakout_margin=_BREAKOUT_MARGIN) -> BreakoutAnt:
    if adapter is None:
        adapter = make_adapter()
    return BreakoutAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
        piotroski_log_dir=piotroski_log_dir,
        breakout_margin=breakout_margin,
    )


def write_piotroski_log(log_dir: Path, candidate_id: str, symbol: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "payload": {
            "action": _READ_ACTION,
            "candidate_id": candidate_id,
            "symbol": symbol,
        }
    }
    (log_dir / "piotroski_ant.jsonl").open("a").write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# 1. Initialisatie
# ---------------------------------------------------------------------------


class TestInit:
    def test_status_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE

    def test_piotroski_log_dir_defaults_to_logs_root(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._piotroski_log_dir == tmp_path / "equities" / "piotroski"

    def test_piotroski_log_dir_override(self, tmp_path: Path) -> None:
        override = tmp_path / "custom"
        ant = make_ant(piotroski_log_dir=override)
        assert ant._piotroski_log_dir == override

    def test_piotroski_log_dir_none_when_no_logs_root(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._piotroski_log_dir is None

    def test_seen_candidates_empty(self) -> None:
        ant = make_ant()
        assert len(ant._seen_candidate_ids) == 0


# ---------------------------------------------------------------------------
# 2. Tick — geen log-map of adapter
# ---------------------------------------------------------------------------


class TestTickNoLogDirOrAdapter:
    def test_no_log_dir_returns_empty(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._tick() == []

    def test_missing_log_dir_returns_empty(self, tmp_path: Path) -> None:
        ant = make_ant(piotroski_log_dir=tmp_path / "nonexistent")
        assert ant._tick() == []

    def test_no_adapter_returns_empty(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = BreakoutAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
            piotroski_log_dir=piotroski_dir,
        )
        result = ant._tick()
        assert result == []

    def test_unavailable_adapter_returns_empty(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        ant = make_ant(adapter=make_adapter(available=False), piotroski_log_dir=piotroski_dir)
        assert ant._tick() == []

    def test_adapter_missing_get_candles_returns_empty(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        adapter = MagicMock()
        adapter.is_available.return_value = True
        del adapter.get_candles  # remove method
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert result == []


# ---------------------------------------------------------------------------
# 3. Breakout detectie
# ---------------------------------------------------------------------------


class TestBreakoutDetection:
    def test_within_margin_emits_signal(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        cid = str(uuid.uuid4())
        write_piotroski_log(piotroski_dir, cid, "AAPL")

        # current=98, high=100 → distance=(100-98)/100=2% < 5%
        candles = make_candles_near_high("AAPL", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert len(result) == 1
        assert result[0]["symbol"] == "AAPL"

    def test_outside_margin_no_signal(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        cid = str(uuid.uuid4())
        write_piotroski_log(piotroski_dir, cid, "AAPL")

        # current=90, high=100 → distance=10% > 5%
        candles = make_candles_near_high("AAPL", current=90.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert result == []

    def test_exact_margin_boundary_emits_signal(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        # distance exactly 5% → should pass (<=)
        candles = make_candles_near_high("AAPL", current=95.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir, breakout_margin=0.05)
        result = ant._tick()
        assert len(result) == 1

    def test_custom_breakout_margin(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        # distance=8%, custom margin=10% → should pass
        candles = make_candles_near_high("AAPL", current=92.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir, breakout_margin=0.10)
        result = ant._tick()
        assert len(result) == 1

    def test_too_few_candles_no_signal(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        adapter = make_adapter(candles_fn=lambda sym, **kw: [make_candle("AAPL", 100.0)])
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert result == []

    def test_empty_candles_no_signal(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        adapter = make_adapter(candles_fn=lambda sym, **kw: [])
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert result == []


# ---------------------------------------------------------------------------
# 4. Signal inhoud
# ---------------------------------------------------------------------------


class TestSignalContent:
    def _make_signal(self, tmp_path: Path, current: float = 98.0, high_52w: float = 100.0) -> dict:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")
        candles = make_candles_near_high("AAPL", current=current, high_52w=high_52w)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        return ant._tick()[0]

    def test_entry_price_is_last_close(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path, current=98.0)
        assert sig["entry_price"] == pytest.approx(98.0, abs=0.001)

    def test_sl_price_is_8pct_below_entry(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path, current=100.0)
        expected_sl = round(100.0 * (1 - _SL_PCT), 4)
        assert sig["sl_price"] == pytest.approx(expected_sl, abs=0.001)

    def test_tp_price_is_20pct_above_entry(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path, current=100.0)
        expected_tp = round(100.0 * (1 + _TP_PCT), 4)
        assert sig["tp_price"] == pytest.approx(expected_tp, abs=0.001)

    def test_high_52w_in_signal(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path, current=98.0, high_52w=100.0)
        assert sig["high_52w"] == pytest.approx(100.0, abs=0.001)

    def test_breakout_confirmed_true(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path)
        assert sig["breakout_confirmed"] is True

    def test_signal_has_required_fields(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path)
        for field in ("signal_id", "candidate_id", "symbol", "entry_price",
                      "sl_price", "tp_price", "sl_pct", "tp_pct",
                      "high_52w", "distance_to_high", "breakout_confirmed", "emitted_at"):
            assert field in sig, f"missing field: {field}"

    def test_signal_id_contains_today(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path)
        assert date.today().isoformat() in sig["signal_id"]

    def test_signal_id_starts_with_breakout(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path)
        assert sig["signal_id"].startswith("breakout-")

    def test_distance_to_high_correct(self, tmp_path: Path) -> None:
        sig = self._make_signal(tmp_path, current=95.0, high_52w=100.0)
        assert sig["distance_to_high"] == pytest.approx(0.05, abs=1e-5)


# ---------------------------------------------------------------------------
# 5. Deduplicatie
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_symbol_not_emitted_twice_same_day(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        candles = make_candles_near_high("AAPL", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        first  = ant._tick()
        second = ant._tick()
        assert len(first) == 1
        assert len(second) == 0

    def test_different_symbols_both_emitted(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "MSFT")

        candles = make_candles_near_high("X", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert len(result) == 2


# ---------------------------------------------------------------------------
# 6. Log output
# ---------------------------------------------------------------------------


class TestLogOutput:
    def test_write_signal_creates_file(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        candles = make_candles_near_high("AAPL", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, logs_root=tmp_path, piotroski_log_dir=piotroski_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "breakout" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_contains_breakout_signal_action(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        candles = make_candles_near_high("AAPL", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, logs_root=tmp_path, piotroski_log_dir=piotroski_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "breakout" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert len(records) == 1
        assert records[0]["payload"]["action"] == _WRITE_ACTION

    def test_no_log_when_logs_root_none(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        write_piotroski_log(piotroski_dir, str(uuid.uuid4()), "AAPL")

        candles = make_candles_near_high("AAPL", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, logs_root=None, piotroski_log_dir=piotroski_dir)
        ant._tick()  # Should not raise

    def test_sequence_increments(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        for i in range(3):
            write_piotroski_log(piotroski_dir, str(uuid.uuid4()), f"SYM{i}")

        candles = make_candles_near_high("X", current=98.0, high_52w=100.0)
        adapter = make_adapter(candles_fn=lambda sym, **kw: candles)
        ant = make_ant(adapter=adapter, logs_root=tmp_path, piotroski_log_dir=piotroski_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "breakout" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))

    def test_symbol_missing_in_payload_skipped(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        piotroski_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": _READ_ACTION, "candidate_id": str(uuid.uuid4())}}
        (piotroski_dir / "bad.jsonl").write_text(json.dumps(record) + "\n")

        adapter = make_adapter()
        ant = make_ant(adapter=adapter, piotroski_log_dir=piotroski_dir)
        result = ant._tick()
        assert result == []


# ---------------------------------------------------------------------------
# 7. Scan log files
# ---------------------------------------------------------------------------


class TestScanLogFiles:
    def test_skips_wrong_action(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        piotroski_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "wrong_action", "candidate_id": "x", "symbol": "AAPL"}}
        (piotroski_dir / "test.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(piotroski_log_dir=piotroski_dir)
        results = list(ant._scan_log_dir(piotroski_dir, _READ_ACTION))
        assert results == []

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        piotroski_dir.mkdir(parents=True, exist_ok=True)
        (piotroski_dir / "bad.jsonl").write_text("not json\n")

        ant = make_ant(piotroski_log_dir=piotroski_dir)
        results = list(ant._scan_log_dir(piotroski_dir, _READ_ACTION))
        assert results == []

    def test_reads_multiple_files(self, tmp_path: Path) -> None:
        piotroski_dir = tmp_path / "piotroski"
        piotroski_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            record = {"payload": {"action": _READ_ACTION, "candidate_id": f"cid-{i}", "symbol": f"SYM{i}"}}
            (piotroski_dir / f"ant_{i}.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(piotroski_log_dir=piotroski_dir)
        results = list(ant._scan_log_dir(piotroski_dir, _READ_ACTION))
        assert len(results) == 3


# ---------------------------------------------------------------------------
# 8. Heartbeat
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
# 9. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=[])

        with patch("ant_colony.ants.equities.breakout_ant.time.sleep"):
            with patch("ant_colony.ants.equities.breakout_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.breakout_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.breakout_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE
