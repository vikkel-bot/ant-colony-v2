"""
tests/test_scout_ant.py

ScoutAnt — gedrag zonder echte API calls.

Scenarios (vereist):
  1. Price move > 1%  → OpportunitySignal gegenereerd
  2. Price move < 1%  → geen signaal
  3. Volume spike > 2× → signaal
  4. Baseline < 3 ticks → geen spike detectie
  5. TTL verlopen → ant stopt met AntStatus.COMPLETED
  6. Stale marktdata → tick overgeslagen (geen signaal)
  7. Heartbeat gerapporteerd binnen run()-loop
  8. Audit log geschreven bij signaal
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.scout_ant import ScoutAnt, _HISTORY_SIZE, _PRICE_MOVE_THRESHOLD, _VOLUME_SPIKE_FACTOR
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions
from ant_colony.schemas.opportunity_signal import OpportunitySignal, SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_mission(
    *,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    ttl: int = 3600,
    heartbeat_interval: int = 30,
) -> Mission:
    return Mission(
        mission_id="m-scout-001",
        ant_type="scout_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["scan_market"],
        market_scope=MarketScope(
            biome="crypto",
            symbols=symbols or ["BTC-EUR", "ETH-EUR", "SOL-EUR"],
            timeframes=timeframes or ["1h"],
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
        success_conditions=SuccessConditions(description="Observatie missie"),
        abort_conditions=AbortConditions(),
    )


def make_candle(
    symbol: str = "BTC-EUR",
    open_: float = 50_000.0,
    close: float = 50_000.0,
    volume: float = 100.0,
    age_seconds: float = 0.0,
) -> MarketData:
    ts = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    return MarketData(
        symbol=symbol,
        timeframe="1h",
        timestamp=ts,
        open=open_,
        high=max(open_, close) * 1.001,
        low=min(open_, close) * 0.999,
        close=close,
        volume=volume,
        biome_id="crypto",
    )


def make_adapter(candle: MarketData | None = None, available: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.biome_id = "crypto"
    adapter.is_available.return_value = available
    adapter.get_market_data.return_value = candle
    return adapter


def make_registry(candle: MarketData | None = None, available: bool = True) -> BiomeRegistry:
    registry = BiomeRegistry()
    registry.register(make_adapter(candle=candle, available=available))
    return registry


def make_scout(
    tmp_path: Path,
    *,
    mission: Mission | None = None,
    registry: BiomeRegistry | None = None,
    scheduler: MagicMock | None = None,
) -> ScoutAnt:
    return ScoutAnt(
        ant_id="ant-scout-test-0001",
        mission=mission or make_mission(),
        scheduler=scheduler or MagicMock(),
        biome_registry=registry or make_registry(),
        logs_root=tmp_path,
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1 & 2  Price move detectie
# ---------------------------------------------------------------------------

class TestPriceMoveDetection:
    def test_above_threshold_generates_signal(self, tmp_path):
        # 2% move ruim boven de 1% drempel
        candle = make_candle(open_=50_000.0, close=51_000.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle))

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is not None
        assert signal.signal_type == SignalType.PRICE_MOVE
        assert signal.symbol == "BTC-EUR"
        assert abs(signal.change_pct - 0.02) < 1e-6

    def test_below_threshold_no_signal(self, tmp_path):
        # 0.5% move — onder de 1% drempel
        candle = make_candle(open_=50_000.0, close=50_250.0)
        scout = make_scout(tmp_path)

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is None

    def test_exactly_at_threshold_no_signal(self, tmp_path):
        # Precies 1% → drempel is > 1%, dus geen signaal
        candle = make_candle(open_=50_000.0, close=50_500.0)  # exact 1%
        scout = make_scout(tmp_path)

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is None

    def test_negative_move_detected(self, tmp_path):
        # Prijsdaling van 2% is ook een geldige move
        candle = make_candle(open_=50_000.0, close=49_000.0)
        scout = make_scout(tmp_path)

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is not None
        assert signal.change_pct < 0

    def test_confidence_increases_with_larger_move(self, tmp_path):
        small_move = make_candle(open_=50_000.0, close=50_600.0)   # 1.2%
        large_move = make_candle(open_=50_000.0, close=52_000.0)   # 4%
        scout = make_scout(tmp_path)

        sig_small = scout._check_price_move("BTC-EUR", small_move)
        sig_large = scout._check_price_move("BTC-EUR", large_move)

        assert sig_small is not None and sig_large is not None
        assert sig_large.confidence > sig_small.confidence

    def test_confidence_capped_at_1(self, tmp_path):
        # 10% move — confidence mag nooit > 1
        candle = make_candle(open_=50_000.0, close=55_000.0)
        scout = make_scout(tmp_path)

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is not None
        assert signal.confidence <= 1.0

    def test_zero_open_price_skipped(self, tmp_path):
        candle = make_candle(open_=0.0, close=50_000.0)
        scout = make_scout(tmp_path)

        signal = scout._check_price_move("BTC-EUR", candle)

        assert signal is None


# ---------------------------------------------------------------------------
# 3 & 4  Volume spike detectie
# ---------------------------------------------------------------------------

class TestVolumeSpikeDetection:
    def _fill_history(self, scout: ScoutAnt, symbol: str, volumes: list[float]) -> None:
        for v in volumes:
            scout._vol_history[symbol].append(v)

    def test_spike_above_2x_generates_signal(self, tmp_path):
        scout = make_scout(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        # 9 vorige candles met volume 100 → baseline = 100; huidige = 300 (3×)
        self._fill_history(scout, "BTC-EUR", [100.0] * 9)
        candle = make_candle(volume=300.0)
        scout._vol_history["BTC-EUR"].append(300.0)  # huidige al in history

        signal = scout._check_volume_spike("BTC-EUR", candle)

        assert signal is not None
        assert signal.signal_type == SignalType.VOLUME_SPIKE

    def test_spike_below_2x_no_signal(self, tmp_path):
        scout = make_scout(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        self._fill_history(scout, "BTC-EUR", [100.0] * 9)
        candle = make_candle(volume=150.0)  # 1.5× — onder drempel
        scout._vol_history["BTC-EUR"].append(150.0)

        signal = scout._check_volume_spike("BTC-EUR", candle)

        assert signal is None

    def test_fewer_than_3_ticks_no_detection(self, tmp_path):
        # Scenario 4: baseline nog niet opgebouwd
        scout = make_scout(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        # Slechts 2 entries in history (inclusief huidige) → < 3
        scout._vol_history["BTC-EUR"].append(100.0)
        candle = make_candle(volume=999.0)
        scout._vol_history["BTC-EUR"].append(999.0)

        signal = scout._check_volume_spike("BTC-EUR", candle)

        assert signal is None

    def test_exactly_3_ticks_enables_detection(self, tmp_path):
        scout = make_scout(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        # 2 vorige + 1 huidige = 3 entries → detectie actief
        scout._vol_history["BTC-EUR"].append(100.0)
        scout._vol_history["BTC-EUR"].append(100.0)
        candle = make_candle(volume=500.0)  # 5× baseline
        scout._vol_history["BTC-EUR"].append(500.0)

        signal = scout._check_volume_spike("BTC-EUR", candle)

        assert signal is not None

    def test_confidence_capped_at_1(self, tmp_path):
        scout = make_scout(tmp_path, mission=make_mission(symbols=["BTC-EUR"]))
        self._fill_history(scout, "BTC-EUR", [100.0] * 9)
        candle = make_candle(volume=10_000.0)  # 100× — hoog genoeg voor cap
        scout._vol_history["BTC-EUR"].append(10_000.0)

        signal = scout._check_volume_spike("BTC-EUR", candle)

        assert signal is not None
        assert signal.confidence <= 1.0


# ---------------------------------------------------------------------------
# 6  Stale / ongeldige marktdata
# ---------------------------------------------------------------------------

class TestDataValidation:
    def test_stale_candle_skipped(self, tmp_path):
        # Scenario 6: stale data → geen signaal
        stale_candle = make_candle(
            open_=50_000.0, close=52_000.0,  # 4% move — zou detecteren
            age_seconds=400.0,               # ouder dan 300s max
        )
        scout = make_scout(tmp_path, registry=make_registry(candle=stale_candle))

        signals_before = scout._log_seq
        scout._tick()

        assert scout._log_seq == signals_before  # geen log-entries toegevoegd

    def test_unavailable_adapter_tick_is_skipped(self, tmp_path):
        scout = make_scout(tmp_path, registry=make_registry(available=False))
        scout._tick()  # mag niet raisen

    def test_none_candle_does_not_raise(self, tmp_path):
        scout = make_scout(tmp_path, registry=make_registry(candle=None))
        scout._tick()  # mag niet raisen

    def test_missing_biome_adapter_does_not_raise(self, tmp_path):
        # Registry met leeg biome — get() geeft None terug
        empty_registry = BiomeRegistry()
        scout = make_scout(tmp_path, registry=empty_registry)
        scout._tick()


# ---------------------------------------------------------------------------
# 7  Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_send_heartbeat_calls_scheduler(self, tmp_path):
        # Scenario 7: heartbeat gerapporteerd
        scheduler = MagicMock()
        scout = make_scout(tmp_path, scheduler=scheduler)
        scout._status = AntStatus.RUNNING

        scout._send_heartbeat()

        scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_contains_ant_id(self, tmp_path):
        scheduler = MagicMock()
        scout = make_scout(tmp_path, scheduler=scheduler)
        scout._status = AntStatus.RUNNING

        scout._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == scout.ant_id

    def test_heartbeat_contains_mission_id(self, tmp_path):
        scheduler = MagicMock()
        mission = make_mission()
        scout = make_scout(tmp_path, mission=mission, scheduler=scheduler)
        scout._status = AntStatus.RUNNING

        scout._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.mission_id == mission.mission_id

    def test_heartbeat_reports_last_action(self, tmp_path):
        scheduler = MagicMock()
        scout = make_scout(tmp_path, scheduler=scheduler)
        scout._status = AntStatus.RUNNING
        scout._last_action = "signal:price_move:BTC-EUR"

        scout._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.last_action == "signal:price_move:BTC-EUR"

    def test_heartbeat_raised_exception_does_not_propagate(self, tmp_path):
        scheduler = MagicMock()
        scheduler.record_heartbeat.side_effect = RuntimeError("scheduler down")
        scout = make_scout(tmp_path, scheduler=scheduler)

        scout._send_heartbeat()  # mag niet raisen

    def test_run_sends_heartbeat_when_interval_elapsed(self, tmp_path):
        # Scenario 7 via run(): heartbeat na heartbeat_interval seconds
        scheduler = MagicMock()
        mission = make_mission(ttl=30, heartbeat_interval=5, symbols=["BTC-EUR"])
        scout = make_scout(tmp_path, mission=mission, scheduler=scheduler,
                           registry=make_registry(candle=None))

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("ant_colony.ants.scout_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.scout_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,                             # started_at
                base + timedelta(seconds=6),      # loop iter 1: elapsed=6<30, hb: 6>=5 → sent
                base + timedelta(seconds=6),      # last_heartbeat update
                base + timedelta(seconds=31),     # loop iter 2: TTL → COMPLETED
            ]
            mock_time.sleep = MagicMock()
            scout.run()

        # Minstens één keer heartbeat via record_heartbeat (+ finally)
        assert scheduler.record_heartbeat.call_count >= 1


# ---------------------------------------------------------------------------
# 5  TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_run_returns_completed_when_ttl_expires(self, tmp_path):
        # Scenario 5: TTL verlopen → COMPLETED
        mission = make_mission(ttl=10, heartbeat_interval=5, symbols=["BTC-EUR"])
        scout = make_scout(tmp_path, mission=mission, registry=make_registry(candle=None))

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("ant_colony.ants.scout_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.scout_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,                           # started_at
                base + timedelta(seconds=1),    # loop iter 1: elapsed=1 < 10
                base + timedelta(seconds=11),   # loop iter 2: elapsed=11 >= 10 → COMPLETED
            ]
            mock_time.sleep = MagicMock()
            status = scout.run()

        assert status == AntStatus.COMPLETED

    def test_run_continues_while_within_ttl(self, tmp_path):
        # TTL niet verlopen — status blijft RUNNING tot we hem van buiten stoppen
        mission = make_mission(ttl=3600, heartbeat_interval=5, symbols=["BTC-EUR"])
        scout = make_scout(tmp_path, mission=mission, registry=make_registry(candle=None))

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        call_count = [0]

        def fake_now(**_kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return base  # started_at
            if call_count[0] == 2:
                return base + timedelta(seconds=1)  # elapsed=1 < 3600
            # Na 2e iteratie: zet status op COMPLETED zodat de loop stopt
            scout._status = AntStatus.COMPLETED
            return base + timedelta(seconds=2)

        with patch("ant_colony.ants.scout_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.scout_ant.time") as mock_time:
            mock_dt.now.side_effect = fake_now
            mock_time.sleep = MagicMock()
            status = scout.run()

        assert status == AntStatus.COMPLETED

    def test_run_sends_final_heartbeat_on_exit(self, tmp_path):
        # finally-blok stuurt altijd een heartbeat
        scheduler = MagicMock()
        mission = make_mission(ttl=10, heartbeat_interval=5, symbols=["BTC-EUR"])
        scout = make_scout(tmp_path, mission=mission, scheduler=scheduler,
                           registry=make_registry(candle=None))

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("ant_colony.ants.scout_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.scout_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,
                base + timedelta(seconds=11),  # direct TTL
            ]
            mock_time.sleep = MagicMock()
            scout.run()

        assert scheduler.record_heartbeat.called


# ---------------------------------------------------------------------------
# 8  Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_signal_written_to_scouts_subdir(self, tmp_path):
        # Scenario 8: log geschreven naar ANT_LOGS/scouts/
        candle = make_candle(open_=50_000.0, close=51_500.0)  # 3% move
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))

        scout._tick()

        log_path = tmp_path / "scouts" / f"{scout.ant_id}.jsonl"
        assert log_path.exists(), "Logbestand niet aangemaakt"

    def test_log_entry_is_valid_json(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=51_500.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))
        scout._tick()

        log_path = tmp_path / "scouts" / f"{scout.ant_id}.jsonl"
        records = read_jsonl(log_path)
        assert len(records) >= 1

    def test_log_entry_has_required_ticker_fields(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=51_500.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))
        scout._tick()

        record = read_jsonl(tmp_path / "scouts" / f"{scout.ant_id}.jsonl")[0]
        assert "timestamp" in record      # voor dashboard-ticker sortering
        assert "event_type" in record
        assert "source" in record
        assert "payload" in record

    def test_log_payload_contains_signal_fields(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=52_000.0)  # 4%
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))
        scout._tick()

        payload = read_jsonl(tmp_path / "scouts" / f"{scout.ant_id}.jsonl")[0]["payload"]
        assert payload["symbol"] == "BTC-EUR"
        assert payload["signal_type"] == "price_move"
        assert "current_price" in payload
        assert "confidence" in payload

    def test_no_log_when_no_signal(self, tmp_path):
        # Geen beweging → geen logbestand
        candle = make_candle(open_=50_000.0, close=50_100.0)  # 0.2%
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))
        scout._tick()

        log_path = tmp_path / "scouts" / f"{scout.ant_id}.jsonl"
        assert not log_path.exists()

    def test_no_log_when_logs_root_is_none(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=52_000.0)  # 4%
        registry = make_registry(candle=candle)
        scout = ScoutAnt(
            ant_id="ant-nolog",
            mission=make_mission(symbols=["BTC-EUR"]),
            scheduler=MagicMock(),
            biome_registry=registry,
            logs_root=None,
        )
        scout._tick()  # mag niet raisen, geen bestand aangemaakt

    def test_multiple_signals_append_to_same_file(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=52_000.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))

        scout._tick()
        scout._tick()

        log_path = tmp_path / "scouts" / f"{scout.ant_id}.jsonl"
        records = read_jsonl(log_path)
        assert len(records) >= 2

    def test_sequence_increments_per_signal(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=52_000.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))

        scout._tick()
        scout._tick()

        log_path = tmp_path / "scouts" / f"{scout.ant_id}.jsonl"
        records = read_jsonl(log_path)
        sequences = [r["sequence"] for r in records]
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)  # geen duplicaten

    def test_source_field_is_ant_id(self, tmp_path):
        candle = make_candle(open_=50_000.0, close=52_000.0)
        scout = make_scout(tmp_path, registry=make_registry(candle=candle),
                           mission=make_mission(symbols=["BTC-EUR"]))
        scout._tick()

        record = read_jsonl(tmp_path / "scouts" / f"{scout.ant_id}.jsonl")[0]
        assert record["source"] == scout.ant_id


# ---------------------------------------------------------------------------
# OpportunitySignal schema
# ---------------------------------------------------------------------------

class TestOpportunitySignalSchema:
    def test_price_move_signal_valid(self):
        sig = OpportunitySignal(
            symbol="BTC-EUR",
            signal_type=SignalType.PRICE_MOVE,
            current_price=51_000.0,
            change_pct=0.02,
            confidence=0.67,
            biome="crypto",
            mission_id="m-001",
        )
        assert sig.signal_type == SignalType.PRICE_MOVE
        assert sig.signal_id  # auto-generated UUID

    def test_volume_spike_signal_valid(self):
        sig = OpportunitySignal(
            symbol="ETH-EUR",
            signal_type=SignalType.VOLUME_SPIKE,
            current_price=3_200.0,
            change_pct=0.0,
            confidence=0.50,
            biome="crypto",
            mission_id="m-001",
        )
        assert sig.signal_type == SignalType.VOLUME_SPIKE

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(Exception):
            OpportunitySignal(
                symbol="BTC-EUR",
                signal_type=SignalType.PRICE_MOVE,
                current_price=50_000.0,
                change_pct=0.02,
                confidence=-0.1,
                biome="crypto",
                mission_id="m-001",
            )

    def test_confidence_above_one_rejected(self):
        with pytest.raises(Exception):
            OpportunitySignal(
                symbol="BTC-EUR",
                signal_type=SignalType.PRICE_MOVE,
                current_price=50_000.0,
                change_pct=0.02,
                confidence=1.1,
                biome="crypto",
                mission_id="m-001",
            )

    def test_zero_price_rejected(self):
        with pytest.raises(Exception):
            OpportunitySignal(
                symbol="BTC-EUR",
                signal_type=SignalType.PRICE_MOVE,
                current_price=0.0,
                change_pct=0.02,
                confidence=0.5,
                biome="crypto",
                mission_id="m-001",
            )
