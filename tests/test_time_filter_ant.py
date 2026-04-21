"""
tests/test_time_filter_ant.py

TimeFilterAnt — ICT Kill Zone filter.

Scenarios:
  _classify_session:
  1.  London Kill Zone (03:00 UTC) → TRADE
  2.  NY AM Kill Zone (14:00 UTC) → TRADE
  3.  NY PM (15:30 UTC) → AVOID
  4.  Asian Session (21:00 UTC) → DO NOT TRADE
  5.  Neutral voor London (01:00 UTC) → AVOID
  6.  Neutral na NY PM (17:00 UTC) → AVOID
  7.  Grensgeval London start (02:00 exact) → TRADE
  8.  Grensgeval London einde (05:00 exact) → neutral/AVOID
  9.  Grensgeval NY AM start (13:30 exact) → TRADE
  10. Grensgeval NY AM einde / NY PM start (15:00 exact) → AVOID
  11. Grensgeval Asian start (19:00 exact) → DO NOT TRADE
  12. Middernacht (00:00) → neutral/AVOID

  TimeFilterAnt:
  13. _tick() schrijft TimeSignal naar disk
  14. read_latest_time_signal() leest meest recente signal terug
  15. read_latest_time_signal() retourneert None als dir niet bestaat
  16. read_latest_time_signal() retourneert None als enkel niet-time_signal records bestaan
  17. run() stopt bij TTL expiry met COMPLETED
  18. logs_root=None → geen crash

  ScoutAnt integratie:
  19. ScoutAnt overslaat signalen als trade_allowed=False
  20. ScoutAnt emitteert signalen als trade_allowed=True
  21. ScoutAnt emitteert normaal als geen time_filter log aanwezig (fail-open)

  PaperAnt integratie:
  22. PaperAnt opent geen nieuwe posities als trade_allowed=False
  23. PaperAnt opent wel posities als trade_allowed=True
  24. PaperAnt verwerkt exits ongeacht trade_allowed
  25. PaperAnt gedraagt normaal als geen time_filter log aanwezig (fail-open)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.time_filter_ant import (
    TimeFilterAnt,
    _classify_session,
    read_latest_time_signal,
)
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mission(ttl: int = 3600, heartbeat_interval: int = 60) -> Mission:
    return Mission(
        mission_id=f"m-tf-{uuid.uuid4().hex[:8]}",
        ant_type="time_filter_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=ttl,
        heartbeat_interval=heartbeat_interval,
        success_conditions=SuccessConditions(description="time filter test"),
        abort_conditions=AbortConditions(),
    )


def make_ant(logs_root: Path | None = None, ttl: int = 3600) -> TimeFilterAnt:
    return TimeFilterAnt(
        ant_id=f"tf-{uuid.uuid4().hex[:12]}",
        mission=make_mission(ttl=ttl),
        scheduler=MagicMock(),
        logs_root=logs_root,
    )


def write_time_signal(
    time_filter_dir: Path,
    *,
    ant_id: str = "test-ant",
    session: str = "neutral",
    trade_allowed: bool = False,
    reason: str = "test",
    ts: str | None = None,
) -> None:
    """Schrijf een nep-TimeSignal naar disk."""
    ts = ts or datetime.now(tz=timezone.utc).isoformat()
    record = {
        "timestamp": ts,
        "payload": {
            "action": "time_signal",
            "session": session,
            "trade_allowed": trade_allowed,
            "reason": reason,
        },
    }
    time_filter_dir.mkdir(parents=True, exist_ok=True)
    path = time_filter_dir / f"{ant_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# 1–12: _classify_session — pure functie
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hour,minute,expected_session,expected_trade", [
    # 1. London Kill Zone
    (3, 0,  "london_kill_zone", True),
    # 2. NY AM Kill Zone
    (14, 0, "ny_am_kill_zone",  True),
    # 3. NY PM (aansluitend op NY AM)
    (15, 30, "ny_pm",           False),
    # 4. Asian Session
    (21, 0, "asian",            False),
    # 5. Neutral voor London
    (1, 0,  "neutral",          False),
    # 6. Neutral na NY PM
    (17, 0, "neutral",          False),
    # 7. Grensgeval London start (02:00 exact)
    (2, 0,  "london_kill_zone", True),
    # 8. Grensgeval London einde (05:00 exact) → neutral
    (5, 0,  "neutral",          False),
    # 9. Grensgeval NY AM start (13:30 exact)
    (13, 30, "ny_am_kill_zone", True),
    # 10. Grensgeval NY AM einde / NY PM start (15:00 exact)
    (15, 0,  "ny_pm",           False),
    # 11. Grensgeval Asian start (19:00 exact)
    (19, 0,  "asian",           False),
    # 12. Middernacht (00:00) → neutral
    (0, 0,   "neutral",         False),
])
def test_classify_session(hour, minute, expected_session, expected_trade):
    session, trade_allowed, reason = _classify_session(hour, minute)
    assert session == expected_session
    assert trade_allowed == expected_trade
    assert isinstance(reason, str) and len(reason) > 0


# ---------------------------------------------------------------------------
# 13: _tick() schrijft TimeSignal naar disk
# ---------------------------------------------------------------------------

def test_tick_writes_signal(tmp_path):
    ant = make_ant(logs_root=tmp_path)
    ant._tick()

    log_path = tmp_path / "time_filter" / f"{ant.ant_id}.jsonl"
    assert log_path.exists()

    lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1

    rec = json.loads(lines[0])
    payload = rec["payload"]
    assert payload["action"] == "time_signal"
    assert "session" in payload
    assert "trade_allowed" in payload
    assert isinstance(payload["trade_allowed"], bool)
    assert "reason" in payload
    assert "utc_time" in payload


# ---------------------------------------------------------------------------
# 14: read_latest_time_signal leest meest recente signal terug
# ---------------------------------------------------------------------------

def test_read_latest_time_signal_returns_most_recent(tmp_path):
    tf_dir = tmp_path / "time_filter"

    ts_old = "2024-06-01T02:00:00+00:00"
    ts_new = "2024-06-01T14:00:00+00:00"

    write_time_signal(tf_dir, ant_id="ant-a", session="neutral", trade_allowed=False, ts=ts_old)
    write_time_signal(tf_dir, ant_id="ant-a", session="ny_am_kill_zone", trade_allowed=True, ts=ts_new)

    result = read_latest_time_signal(tmp_path)
    assert result is not None
    assert result["session"] == "ny_am_kill_zone"
    assert result["trade_allowed"] is True


# ---------------------------------------------------------------------------
# 15: read_latest_time_signal retourneert None als dir niet bestaat
# ---------------------------------------------------------------------------

def test_read_latest_time_signal_no_dir(tmp_path):
    result = read_latest_time_signal(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 16: read_latest_time_signal negeert niet-time_signal records
# ---------------------------------------------------------------------------

def test_read_latest_time_signal_ignores_other_actions(tmp_path):
    tf_dir = tmp_path / "time_filter"
    tf_dir.mkdir(parents=True, exist_ok=True)

    other_record = {"timestamp": "2024-06-01T12:00:00+00:00", "payload": {"action": "heartbeat"}}
    path = tf_dir / "other.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(other_record) + "\n")

    result = read_latest_time_signal(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# 17: run() stopt bij TTL expiry
# ---------------------------------------------------------------------------

def test_run_stops_on_ttl():
    mission = make_mission(ttl=2, heartbeat_interval=1)
    ant = TimeFilterAnt(
        ant_id="tf-ttl-test",
        mission=mission,
        scheduler=MagicMock(),
        logs_root=None,
    )
    with patch("time.sleep"):
        status = ant.run()
    assert status == AntStatus.COMPLETED


# ---------------------------------------------------------------------------
# 18: logs_root=None → geen crash
# ---------------------------------------------------------------------------

def test_tick_no_logs_root():
    ant = make_ant(logs_root=None)
    ant._tick()  # geen crash verwacht


# ---------------------------------------------------------------------------
# ScoutAnt integratie
# ---------------------------------------------------------------------------

def _make_scout_mission() -> Mission:
    return Mission(
        mission_id="m-scout-tf-001",
        ant_type="scout_ant",
        allowed_node="pc2",
        allowed_actions=["scan_market"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"], timeframes=["1h"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="scout test"),
        abort_conditions=AbortConditions(),
    )


# 19: ScoutAnt overslaat signalen als trade_allowed=False
def test_scout_skips_signals_outside_kill_zone(tmp_path):
    from ant_colony.ants.scout_ant import ScoutAnt
    from ant_colony.biome.biome_adapter import MarketData
    from ant_colony.biome.biome_registry import BiomeRegistry

    write_time_signal(
        tmp_path / "time_filter",
        session="neutral",
        trade_allowed=False,
        reason="test — buiten kill zone",
    )

    mission = _make_scout_mission()
    registry = MagicMock(spec=BiomeRegistry)
    scout = ScoutAnt(
        ant_id="scout-tf-test",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    emitted: list = []
    scout._emit = lambda sig: emitted.append(sig)

    scout._tick()

    assert emitted == [], "Geen signalen verwacht buiten kill zone"
    assert "buiten_kill_zone" in scout._last_action


# 20: ScoutAnt emitteert signalen als trade_allowed=True
def test_scout_emits_signals_inside_kill_zone(tmp_path):
    from ant_colony.ants.scout_ant import ScoutAnt
    from ant_colony.biome.biome_adapter import MarketData
    from ant_colony.biome.biome_registry import BiomeRegistry
    from ant_colony.schemas.opportunity_signal import OpportunitySignal, SignalType

    write_time_signal(
        tmp_path / "time_filter",
        session="london_kill_zone",
        trade_allowed=True,
        reason="London Kill Zone",
    )

    candle = MarketData(
        symbol="BTC-EUR",
        timeframe="1h",
        timestamp=datetime.now(tz=timezone.utc),
        open=50_000.0,
        high=51_500.0,
        low=49_500.0,
        close=51_500.0,
        volume=200.0,
        biome_id="crypto",
    )

    adapter_mock = MagicMock()
    adapter_mock.is_available.return_value = True
    adapter_mock.get_market_data.return_value = candle
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter_mock

    mission = _make_scout_mission()
    scout = ScoutAnt(
        ant_id="scout-tf-test-inside",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    emitted: list = []
    scout._emit = lambda sig: emitted.append(sig)

    scout._tick()

    assert len(emitted) > 0, "Signalen verwacht binnen kill zone"


# 21: ScoutAnt emitteert normaal als geen time_filter log aanwezig (fail-open)
def test_scout_fail_open_no_time_filter(tmp_path):
    from ant_colony.ants.scout_ant import ScoutAnt
    from ant_colony.biome.biome_adapter import MarketData
    from ant_colony.biome.biome_registry import BiomeRegistry

    candle = MarketData(
        symbol="BTC-EUR",
        timeframe="1h",
        timestamp=datetime.now(tz=timezone.utc),
        open=50_000.0,
        high=52_000.0,
        low=49_000.0,
        close=52_000.0,
        volume=200.0,
        biome_id="crypto",
    )

    adapter_mock = MagicMock()
    adapter_mock.is_available.return_value = True
    adapter_mock.get_market_data.return_value = candle
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter_mock

    mission = _make_scout_mission()
    scout = ScoutAnt(
        ant_id="scout-tf-fail-open",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    emitted: list = []
    scout._emit = lambda sig: emitted.append(sig)
    scout._tick()

    assert len(emitted) > 0, "Signalen verwacht als geen time filter aanwezig (fail-open)"


# ---------------------------------------------------------------------------
# PaperAnt integratie
# ---------------------------------------------------------------------------

def _make_paper_mission() -> Mission:
    return Mission(
        mission_id=f"m-paper-tf-{uuid.uuid4().hex[:8]}",
        ant_type="paper_ant",
        allowed_node="pc2",
        allowed_actions=["open_position", "close_position"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
        capital_limit=10_000.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10, max_position_size=5_000.0,
            daily_loss_limit=500.0, stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="paper tf test"),
    )


def _make_paper_ant(tmp_path: Path):
    from ant_colony.ants.paper_ant import PaperAnt
    from ant_colony.biome.biome_registry import BiomeRegistry

    mission = _make_paper_mission()
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = None
    return PaperAnt(
        ant_id=f"paper-{uuid.uuid4().hex[:12]}",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )


def _write_scout_signal(scouts_dir: Path, symbol: str = "BTC-EUR", price: float = 50_000.0) -> None:
    scouts_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "source": "scout-abc",
        "payload": {
            "action": "opportunity_detected",
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "signal_type": "price_move",
            "current_price": price,
            "change_pct": 0.04,
            "confidence": 0.9,
            "biome": "crypto",
            "detected_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    }
    path = scouts_dir / "scout-abc.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# 22: PaperAnt opent geen nieuwe posities als trade_allowed=False
def test_paper_skips_entry_outside_kill_zone(tmp_path):
    write_time_signal(
        tmp_path / "time_filter",
        session="neutral",
        trade_allowed=False,
    )
    _write_scout_signal(tmp_path / "scouts")

    ant = _make_paper_ant(tmp_path)
    initial_positions = len(ant._ledger.open_positions)

    ant._process_new_signals()

    assert len(ant._ledger.open_positions) == initial_positions, \
        "Geen nieuwe posities verwacht buiten kill zone"


# 23: PaperAnt opent wel posities als trade_allowed=True
def test_paper_opens_position_inside_kill_zone(tmp_path):
    write_time_signal(
        tmp_path / "time_filter",
        session="london_kill_zone",
        trade_allowed=True,
    )
    _write_scout_signal(tmp_path / "scouts", price=50_000.0)

    price = 50_000.0
    adapter_mock = MagicMock()
    adapter_mock.is_available.return_value = True
    md_mock = MagicMock(close=price, is_valid_price=True)
    md_mock.is_stale.return_value = False
    adapter_mock.get_market_data.return_value = md_mock

    from ant_colony.ants.paper_ant import PaperAnt
    from ant_colony.biome.biome_registry import BiomeRegistry

    mission = _make_paper_mission()
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter_mock

    ant = PaperAnt(
        ant_id=f"paper-{uuid.uuid4().hex[:12]}",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    ant._process_new_signals()

    assert len(ant._ledger.open_positions) > 0, \
        "Positie verwacht binnen kill zone met geldig signaal"


# 24: PaperAnt verwerkt exits ongeacht trade_allowed
def test_paper_exits_processed_outside_kill_zone(tmp_path):
    write_time_signal(
        tmp_path / "time_filter",
        session="neutral",
        trade_allowed=False,
    )

    from ant_colony.ants.paper_ant import PaperAnt
    from ant_colony.biome.biome_registry import BiomeRegistry
    from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus
    from ant_colony.schemas.mission import Mission

    price = 50_000.0
    sl_hit_price = price * 0.9   # ruim onder stop_loss (price * 0.98)

    md_mock = MagicMock()
    md_mock.close = sl_hit_price
    md_mock.is_valid_price = True
    md_mock.is_stale.return_value = False

    adapter_mock = MagicMock()
    adapter_mock.is_available.return_value = True
    adapter_mock.get_market_data.return_value = md_mock

    mission = _make_paper_mission()
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter_mock

    ant = PaperAnt(
        ant_id=f"paper-{uuid.uuid4().hex[:12]}",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    # Zet een open positie direct in de ledger
    pos = PaperPosition(
        position_id=str(uuid.uuid4()),
        symbol="BTC-EUR",
        biome="crypto",
        mission_id=mission.mission_id,
        ant_id=ant.ant_id,
        side=PositionSide.LONG,
        entry_price=price,
        quantity=0.01,
        stop_loss_price=price * 0.98,
        take_profit_price=price * 1.03,
        ttl=mission.ttl,
        current_price=price,
        peak_price=price,
        opened_at=datetime.now(tz=timezone.utc),
    )
    ant._ledger.record_opened(pos)
    ant._open_symbols.add("BTC-EUR")

    initial_open = len(ant._ledger.open_positions)
    assert initial_open == 1

    ant._process_exits()

    # Bij prijs 90% van entry (10% daling) → stop loss geraakt → positie gesloten
    assert len(ant._ledger.open_positions) == 0, \
        "Positie moet gesloten zijn ondanks trade_allowed=False (exits altijd actief)"


# 25: PaperAnt gedraagt normaal als geen time_filter log aanwezig (fail-open)
def test_paper_fail_open_no_time_filter(tmp_path):
    _write_scout_signal(tmp_path / "scouts", price=50_000.0)

    price = 50_000.0
    adapter_mock = MagicMock()
    adapter_mock.is_available.return_value = True
    md_mock = MagicMock(close=price, is_valid_price=True)
    md_mock.is_stale.return_value = False
    adapter_mock.get_market_data.return_value = md_mock

    from ant_colony.ants.paper_ant import PaperAnt
    from ant_colony.biome.biome_registry import BiomeRegistry

    mission = _make_paper_mission()
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter_mock

    ant = PaperAnt(
        ant_id=f"paper-{uuid.uuid4().hex[:12]}",
        mission=mission,
        scheduler=MagicMock(),
        biome_registry=registry,
        logs_root=tmp_path,
    )

    ant._process_new_signals()

    assert len(ant._ledger.open_positions) > 0, \
        "Positie verwacht zonder time filter (fail-open)"
