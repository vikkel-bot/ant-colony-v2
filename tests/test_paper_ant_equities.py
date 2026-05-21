"""
tests/test_paper_ant_equities.py

Tests voor EquitiesPaperAnt.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.paper_ant_equities import (
    EquitiesPaperAnt,
    _estimate_trading_seconds_since,
    _HARD_SL_PCT,
    _TRAILING_STOP_PCT,
    _TTL_TRADING_SECONDS,
    _TRADING_SECONDS_PER_DAY,
    _EQUITY_MAX_TTL_DAYS,
    _WATCHTOWER_TTL_DAYS,
    _MOMENTUM_EXIT_CONSECUTIVE_TICKS,
    _MOMENTUM_EXIT_COOLDOWN_SECONDS,
    _MOMENTUM_EXIT_MIN_HOLD_SECONDS,
    _MOMENTUM_EXIT_TYPE,
)
from ant_colony.exit_chain.position import PaperPosition, PositionSide
from ant_colony.schemas.mission import (
    AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_mission(logs_root: Path | None = None) -> Mission:
    return Mission(
        mission_id=f"eq-paper-test-{uuid.uuid4().hex[:8]}",
        ant_type="paper_ant_equities",
        allowed_node="test-node",
        allowed_actions=["read_data", "paper_trade"],
        market_scope=MarketScope(
            biome="equities",
            symbols=["AAPL", "MSFT", "JNJ", "KO", "XLK"],
        ),
        capital_limit=500.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.25,
            max_position_size=500.0,
            daily_loss_limit=50.0,
            stop_loss_required=False,
        ),
        ttl=86400,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(
            description="Equities paper test.",
        ),
    )


def _make_ant(tmp_path: Path) -> EquitiesPaperAnt:
    mission       = _make_mission()
    scheduler     = MagicMock()
    biome_registry = MagicMock()
    biome_registry.get.return_value = None   # geen live prijs standaard
    return EquitiesPaperAnt(
        ant_id=uuid.uuid4().hex,
        mission=mission,
        scheduler=scheduler,
        biome_registry=biome_registry,
        logs_root=tmp_path,
    )


def _write_paper_log(logs_root: Path, ant_id: str, payload: dict) -> None:
    """Schrijf een AuditEvent naar paper/{ant_id}.jsonl."""
    paper_dir = logs_root / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "source":    ant_id,
        "payload":   payload,
    }
    (paper_dir / f"{ant_id}.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )


def _write_trade_opened(
    logs_root: Path,
    ant_id: str,
    *,
    position_id: str,
    symbol: str = "AAPL",
    entry_price: float = 200.0,
    quantity: float = 1.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    ts: datetime | None = None,
    biome: str = "equities",
    source: str = "sector_scout",
    watchtower_signal_id: str | None = None,
    ttl_seconds: int | None = None,
) -> None:
    sl = stop_loss  if stop_loss  is not None else entry_price * (1.0 - _HARD_SL_PCT)
    tp = take_profit if take_profit is not None else entry_price * 2.0
    paper_dir = logs_root / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": (ts or datetime.now(tz=timezone.utc)).isoformat(),
        "source":    ant_id,
        "payload": {
            "action":      "trade_opened",
            "position_id": position_id,
            "symbol":      symbol,
            "biome":       biome,
            "side":        "long",
            "entry_price": entry_price,
            "quantity":    quantity,
            "stop_loss":   sl,
            "take_profit": tp,
            "source":      source,
            "watchtower_signal_id": watchtower_signal_id,
            "ttl_seconds": ttl_seconds,
        },
    }
    (paper_dir / f"{ant_id}.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )


def _write_trade_closed(
    logs_root: Path,
    ant_id: str,
    *,
    position_id: str,
    exit_type: str = "ttl_trading_days",
    ts: datetime | None = None,
) -> None:
    paper_dir = logs_root / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": (ts or datetime.now(tz=timezone.utc)).isoformat(),
        "source": ant_id,
        "payload": {
            "action": "trade_closed",
            "position_id": position_id,
            "exit_type": exit_type,
            "exit_reason": exit_type,
        },
    }
    (paper_dir / f"{ant_id}.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )


def _write_position_update(
    logs_root: Path, ant_id: str, *,
    position_id: str,
    peak_price: float,
    trading_seconds: float = 0.0,
) -> None:
    _write_paper_log(logs_root, ant_id, {
        "action":          "position_update",
        "position_id":     position_id,
        "peak_price":      peak_price,
        "trading_seconds": trading_seconds,
    })


def _write_sector_ranking(logs_root: Path, symbols: list[str]) -> None:
    ranking_dir = logs_root / "equities" / "sector_scout"
    ranking_dir.mkdir(parents=True, exist_ok=True)
    ranking = [
        {
            "symbol": symbol,
            "sector": symbol,
            "return_3mo": round(0.2 - index * 0.01, 6),
            "rank": index + 1,
            "signal": "LONG" if index < 3 else "NEUTRAL",
        }
        for index, symbol in enumerate(symbols)
    ]
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "source": "sector-scout",
        "payload": {
            "action": "sector_ranking",
            "ranking_date": datetime.now(tz=timezone.utc).date().isoformat(),
            "top5": symbols[:5],
            "ranking": ranking,
        },
    }
    (ranking_dir / "sector_scout.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )


# ---------------------------------------------------------------------------
# Tests — _is_market_open
# ---------------------------------------------------------------------------

class TestIsMarketOpen:
    def test_tuesday_16h_amsterdam_is_open(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        # Dinsdag 16:00 AMS — open
        dt = datetime(2026, 4, 21, 16, 0, 0, tzinfo=AMS)   # dinsdag
        ant = MagicMock(spec=EquitiesPaperAnt)
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = dt
            result = EquitiesPaperAnt._is_market_open(ant)
        assert result is True

    def test_saturday_is_closed(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        dt = datetime(2026, 4, 25, 16, 0, 0, tzinfo=AMS)   # zaterdag
        ant = MagicMock(spec=EquitiesPaperAnt)
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = dt
            result = EquitiesPaperAnt._is_market_open(ant)
        assert result is False

    def test_monday_14h_amsterdam_is_before_open(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        dt = datetime(2026, 4, 20, 14, 0, 0, tzinfo=AMS)   # maandag 14:00 AMS
        ant = MagicMock(spec=EquitiesPaperAnt)
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = dt
            result = EquitiesPaperAnt._is_market_open(ant)
        assert result is False

    def test_friday_22h_amsterdam_is_closed(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        dt = datetime(2026, 4, 24, 22, 5, 0, tzinfo=AMS)   # vrijdag 22:05
        ant = MagicMock(spec=EquitiesPaperAnt)
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = dt
            result = EquitiesPaperAnt._is_market_open(ant)
        assert result is False


# ---------------------------------------------------------------------------
# Tests — _estimate_trading_seconds_since
# ---------------------------------------------------------------------------

class TestEstimateTradingSeconds:
    def test_zero_for_future_datetime(self):
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        assert _estimate_trading_seconds_since(future) == 0.0

    def test_reasonable_estimate_for_one_trading_day(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        # Precies het begin van NYSE-open op een dinsdag, nu is einde van die dag
        # opened_at = dinsdag 15:30; nu = dinsdag 22:00 → 6.5h = 23400s
        opened = datetime(2026, 4, 21, 15, 30, 0, tzinfo=AMS)
        close  = datetime(2026, 4, 21, 22,  0, 0, tzinfo=AMS)
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = close.astimezone(timezone.utc)
            result = _estimate_trading_seconds_since(opened)
        assert abs(result - _TRADING_SECONDS_PER_DAY) < 60   # ±1 minuut tolerantie

    def test_weekend_not_counted(self):
        from zoneinfo import ZoneInfo
        AMS = ZoneInfo("Europe/Amsterdam")
        # Vrijdag 22:00 → maandag 15:30: weekend (zat+zon) telt niet mee
        opened = datetime(2026, 4, 24, 22, 0, 0, tzinfo=AMS)  # vrijdag einde
        monday = datetime(2026, 4, 27, 15, 30, 0, tzinfo=AMS)  # maandag open
        with patch("ant_colony.ants.paper_ant_equities.datetime") as mock_dt:
            mock_dt.now.return_value = monday.astimezone(timezone.utc)
            result = _estimate_trading_seconds_since(opened)
        assert result == pytest.approx(0.0, abs=60)


# ---------------------------------------------------------------------------
# Tests — exit-logica (trailing stop, harde SL, TTL)
# ---------------------------------------------------------------------------

class TestExitLogic:
    def _make_position(
        self,
        ant: EquitiesPaperAnt,
        entry_price: float = 100.0,
        symbol: str = "AAPL",
        source: str = "sector_scout",
        watchtower_signal_id: str | None = None,
        opened_at: datetime | None = None,
    ) -> PaperPosition:
        ttl = 14 * 24 * 3600 if source == "watchtower" or watchtower_signal_id else _TTL_TRADING_SECONDS
        pos = PaperPosition(
            position_id=uuid.uuid4().hex,
            symbol=symbol,
            biome="equities",
            mission_id=ant.mission.mission_id,
            ant_id=ant.ant_id,
            side=PositionSide.LONG,
            entry_price=entry_price,
            quantity=1.0,
            stop_loss_price=entry_price * (1 - _HARD_SL_PCT),
            take_profit_price=entry_price * 2.0,
            ttl=ttl,
            current_price=entry_price,
            peak_price=entry_price,
            opened_at=opened_at or datetime.now(tz=timezone.utc),
            watchtower_signal_id=watchtower_signal_id,
        )
        ant._ledger.record_opened(pos)
        ant._open_symbols.add(symbol)
        ant._peak_prices[pos.position_id] = entry_price
        ant._trading_seconds[pos.position_id] = 0.0
        ant._position_sources[pos.position_id] = source
        return pos

    def test_trailing_stop_triggers(self, tmp_path):
        ant = _make_ant(tmp_path)
        pos = self._make_position(ant, entry_price=100.0)

        # Simuleer: price stijgt naar 110, dan daalt 6% → trailing stop geraakt
        ant._peak_prices[pos.position_id] = 110.0
        trigger_price = 110.0 * (1 - _TRAILING_STOP_PCT) - 0.01   # net onder trailing stop

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = trigger_price
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 0
        assert len(ant._ledger.closed_trades) == 1
        assert ant._ledger.closed_trades[0].exit_reason == "trailing_stop"

    def test_hard_sl_triggers(self, tmp_path):
        ant = _make_ant(tmp_path)
        pos = self._make_position(ant, entry_price=100.0)

        # Prijs daalt 8% — onder hard SL van 7%
        trigger_price = 100.0 * (1 - _HARD_SL_PCT) - 0.01

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = trigger_price
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.closed_trades) == 1
        assert ant._ledger.closed_trades[0].exit_reason == "hard_stop_loss"

    def test_ttl_triggers_after_equity_max_ttl_days(self, tmp_path):
        ant = _make_ant(tmp_path)
        opened_at = datetime.now(tz=timezone.utc) - timedelta(days=_EQUITY_MAX_TTL_DAYS, seconds=1)
        self._make_position(ant, entry_price=100.0, opened_at=opened_at)

        # Prijs stabiel — geen SL/TP
        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0   # prijs is goed
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.closed_trades) == 1
        assert ant._ledger.closed_trades[0].exit_reason == "ttl_trading_days"

    def test_watchtower_ttl_stays_14_days(self, tmp_path):
        ant = _make_ant(tmp_path)
        opened_at = datetime.now(tz=timezone.utc) - timedelta(days=_WATCHTOWER_TTL_DAYS, seconds=1)
        pos = self._make_position(
            ant,
            entry_price=100.0,
            source="watchtower",
            watchtower_signal_id="wt-ttl",
            opened_at=opened_at,
        )

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert pos.ttl == _WATCHTOWER_TTL_DAYS * 24 * 3600
        assert len(ant._ledger.closed_trades) == 1
        assert ant._ledger.closed_trades[0].exit_reason == "ttl_trading_days"

    def test_sector_scout_position_marks_and_closes_after_momentum_lost(self, tmp_path):
        ant = _make_ant(tmp_path)
        opened_at = datetime.now(tz=timezone.utc) - timedelta(
            seconds=_MOMENTUM_EXIT_MIN_HOLD_SECONDS + 1
        )
        self._make_position(
            ant,
            entry_price=100.0,
            symbol="XLK",
            source="sector_scout",
            opened_at=opened_at,
        )
        _write_sector_ranking(tmp_path, ["XLE", "XLF", "XLI", "XLY", "XLU", "XLK"])

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        for _ in range(_MOMENTUM_EXIT_CONSECUTIVE_TICKS):
            ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 1
        assert ant._momentum_exit_pending

        ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 0
        assert ant._ledger.closed_trades[0].exit_reason == _MOMENTUM_EXIT_TYPE
        assert ant._momentum_cooldown_until("XLK") is not None

    def test_momentum_lost_waits_for_minimum_hold_time(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._make_position(ant, entry_price=100.0, symbol="XLK", source="sector_scout")
        _write_sector_ranking(tmp_path, ["XLE", "XLF", "XLI", "XLY", "XLU", "XLK"])

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        for _ in range(_MOMENTUM_EXIT_CONSECUTIVE_TICKS + 2):
            ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 1
        assert not ant._momentum_exit_pending

    def test_momentum_lost_requires_three_consecutive_misses(self, tmp_path):
        ant = _make_ant(tmp_path)
        opened_at = datetime.now(tz=timezone.utc) - timedelta(
            seconds=_MOMENTUM_EXIT_MIN_HOLD_SECONDS + 1
        )
        self._make_position(
            ant,
            entry_price=100.0,
            symbol="XLK",
            source="sector_scout",
            opened_at=opened_at,
        )
        _write_sector_ranking(tmp_path, ["XLE", "XLF", "XLI", "XLY", "XLU", "XLK"])

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        for _ in range(_MOMENTUM_EXIT_CONSECUTIVE_TICKS - 1):
            ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 1
        assert not ant._momentum_exit_pending

        ant._process_exits(advance_trading_time=False)

        assert ant._momentum_exit_pending

    def test_watchtower_position_ignores_sector_momentum_exit(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._make_position(
            ant,
            entry_price=100.0,
            symbol="AAPL",
            source="watchtower",
            watchtower_signal_id="wt-ignore-momentum",
        )
        _write_sector_ranking(tmp_path, ["XLE", "XLF", "XLI", "XLY", "XLU"])

        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 101.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        for _ in range(_MOMENTUM_EXIT_CONSECUTIVE_TICKS + 1):
            ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 1

    def test_no_exit_within_normal_range(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._make_position(ant, entry_price=100.0)

        # Prijs daalt 3% — veilig (boven trailing stop 5% en hard SL 7%)
        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = 97.0
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert len(ant._ledger.open_positions) == 1
        assert len(ant._ledger.closed_trades) == 0

    def test_peak_price_updated_on_new_high(self, tmp_path):
        ant = _make_ant(tmp_path)
        pos = self._make_position(ant, entry_price=100.0)

        new_high = 115.0
        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = new_high
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

        ant._process_exits(advance_trading_time=False)

        assert ant._peak_prices[pos.position_id] == new_high
        assert len(ant._ledger.open_positions) == 1   # niet gesloten


# ---------------------------------------------------------------------------
# Tests — ledger-herstel bij herstart
# ---------------------------------------------------------------------------

class TestLedgerRecovery:
    def test_open_position_restored_on_restart(self, tmp_path):
        mission  = _make_mission()
        ant_id   = uuid.uuid4().hex
        pos_id   = uuid.uuid4().hex

        _write_trade_opened(tmp_path, ant_id, position_id=pos_id, symbol="AAPL", entry_price=200.0)

        scheduler = MagicMock()
        registry  = MagicMock()
        registry.get.return_value = None

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=scheduler,
            biome_registry=registry,
            logs_root=tmp_path,
        )

        assert "AAPL" in ant._open_symbols
        assert len(ant._ledger.open_positions) == 1
        assert ant._ledger.open_positions[0].position_id == pos_id

    def test_closed_position_not_restored(self, tmp_path):
        mission = _make_mission()
        ant_id  = uuid.uuid4().hex
        pos_id  = uuid.uuid4().hex

        _write_trade_opened(tmp_path, ant_id, position_id=pos_id, symbol="MSFT")
        _write_trade_closed(tmp_path, ant_id, position_id=pos_id)

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=tmp_path,
        )

        assert "MSFT" not in ant._open_symbols
        assert len(ant._ledger.open_positions) == 0

    def test_recent_momentum_lost_close_restores_cooldown(self, tmp_path):
        mission = _make_mission()
        ant_id = uuid.uuid4().hex
        pos_id = uuid.uuid4().hex
        closed_at = datetime.now(tz=timezone.utc) - timedelta(hours=1)

        _write_trade_opened(tmp_path, ant_id, position_id=pos_id, symbol="XLB")
        _write_trade_closed(
            tmp_path,
            ant_id,
            position_id=pos_id,
            exit_type=_MOMENTUM_EXIT_TYPE,
            ts=closed_at,
        )

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=tmp_path,
        )

        cooldown_until = ant._momentum_cooldown_until("XLB")
        assert cooldown_until is not None
        assert cooldown_until > datetime.now(tz=timezone.utc)
        assert cooldown_until <= closed_at + timedelta(seconds=_MOMENTUM_EXIT_COOLDOWN_SECONDS)

    def test_peak_price_restored_from_position_update(self, tmp_path):
        mission = _make_mission()
        ant_id  = uuid.uuid4().hex
        pos_id  = uuid.uuid4().hex

        _write_trade_opened(tmp_path, ant_id, position_id=pos_id, symbol="AAPL", entry_price=200.0)
        _write_position_update(tmp_path, ant_id, position_id=pos_id, peak_price=230.0, trading_seconds=3600.0)

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=tmp_path,
        )

        assert ant._peak_prices[pos_id] == 230.0
        assert ant._trading_seconds[pos_id] == 3600.0

    def test_non_equities_trade_not_restored(self, tmp_path):
        """Crypto posities in paper/ worden niet hersteld door EquitiesPaperAnt."""
        mission = _make_mission()
        ant_id  = uuid.uuid4().hex
        pos_id  = uuid.uuid4().hex

        _write_trade_opened(
            tmp_path, ant_id, position_id=pos_id,
            symbol="BTC-EUR", biome="crypto", entry_price=40000.0,
        )

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=tmp_path,
        )

        assert len(ant._ledger.open_positions) == 0

    def test_multiple_positions_all_restored(self, tmp_path):
        mission = _make_mission()
        ant_id  = uuid.uuid4().hex

        for symbol, price in [("AAPL", 200.0), ("MSFT", 300.0), ("JNJ", 150.0)]:
            pos_id = uuid.uuid4().hex
            _write_trade_opened(
                tmp_path, ant_id, position_id=pos_id, symbol=symbol, entry_price=price
            )

        ant = EquitiesPaperAnt(
            ant_id=ant_id,
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=MagicMock(),
            logs_root=tmp_path,
        )

        assert len(ant._ledger.open_positions) == 3
        assert {"AAPL", "MSFT", "JNJ"}.issubset(ant._open_symbols)


# ---------------------------------------------------------------------------
# Tests — signaalverwerking
# ---------------------------------------------------------------------------

class TestSignalProcessing:
    def _write_scout_signal(self, logs_root: Path, *, symbol: str, signal_id: str) -> None:
        scout_dir = logs_root / "scouts"
        scout_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source": "sector-scout",
            "payload": {
                "action":     "opportunity_detected",
                "biome":      "equities",
                "symbol":     symbol,
                "signal_id":  signal_id,
                "confidence": 0.80,
                "side":       "long",
            },
        }
        (scout_dir / "sector_scout.jsonl").open("a", encoding="utf-8").write(
            json.dumps(record) + "\n"
        )

    def _write_breakout_signal(self, logs_root: Path, *, symbol: str, candidate_id: str) -> None:
        d = logs_root / "equities" / "breakout"
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source": "breakout-ant",
            "payload": {
                "action":       "breakout_signal",
                "symbol":       symbol,
                "candidate_id": candidate_id,
                "entry_price":  150.0,
                "sl_price":     139.5,
                "tp_price":     180.0,
            },
        }
        (d / "breakout.jsonl").open("a", encoding="utf-8").write(json.dumps(record) + "\n")

    def _write_dividend_signal(self, logs_root: Path, *, symbol: str, candidate_id: str) -> None:
        d = logs_root / "equities" / "dividend"
        d.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source": "dividend-scout",
            "payload": {
                "action":       "dividend_candidate",
                "symbol":       symbol,
                "candidate_id": candidate_id,
            },
        }
        (d / "dividend.jsonl").open("a", encoding="utf-8").write(json.dumps(record) + "\n")

    def _write_watchtower_candidate(
        self,
        logs_root: Path,
        *,
        symbol: str,
        signal_id: str,
        biome: str = "equities",
    ) -> None:
        d = logs_root / "watchtower"
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.now(tz=timezone.utc).isoformat()
        record = {
            "timestamp": now,
            "source": "watchtower-ant",
            "payload": {
                "action": "watchtower_candidate",
                "asset": symbol,
                "symbol": symbol,
                "biome": biome,
                "direction": "long",
                "entry_score": 0.72,
                "confidence": 0.66,
                "signal_id": signal_id,
                "source": "watchtower",
                "created_at": now,
                "reason": "accepted Watchtower candidate",
            },
        }
        (d / "candidates.jsonl").open("a", encoding="utf-8").write(json.dumps(record) + "\n")

    def _mock_price(self, ant: EquitiesPaperAnt, price: float) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = True
        md = MagicMock()
        md.is_valid_price = True
        md.is_stale.return_value = False
        md.close = price
        adapter.get_market_data.return_value = md
        ant.biome_registry.get.return_value = adapter

    def test_scout_signal_opens_position(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 200.0)
        self._write_scout_signal(tmp_path, symbol="AAPL", signal_id="sig-001")
        stats = ant._process_scout_signals()
        assert "AAPL" in ant._open_symbols
        assert stats["received"] == 1
        assert stats["opened"] == 1

        records = [
            json.loads(line)
            for line in (tmp_path / "paper" / f"{ant.ant_id}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")
        assert opened["strategy_type"] == "momentum"

    def test_watchtower_trade_opened_logs_strategy_type(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 200.0)
        self._write_watchtower_candidate(tmp_path, symbol="AAPL", signal_id="wt-001")

        stats = ant._process_watchtower_candidates()

        assert stats["opened"] == 1
        records = [
            json.loads(line)
            for line in (tmp_path / "paper" / f"{ant.ant_id}.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")
        assert opened["strategy_type"] == "watchtower_signal"

    def test_sector_ranking_snapshot_opens_position_when_opportunity_log_missing(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        _write_sector_ranking(tmp_path, ["XLK", "XLE", "XLU", "XLF"])

        stats = ant._process_scout_signals()

        assert {"XLK", "XLE", "XLU"}.issubset(ant._open_symbols)
        assert "XLF" not in ant._open_symbols
        assert stats["received"] == 3
        assert stats["opened"] == 3

    def test_sector_ranking_snapshot_logs_existing_position_filter(self, tmp_path, caplog):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        ant._open_symbols.add("XLK")
        _write_sector_ranking(tmp_path, ["XLK", "XLE", "XLU"])

        with caplog.at_level("INFO", logger=f"ant.eq_paper.{ant.ant_id[:8]}"):
            stats = ant._process_scout_signals()

        assert stats["received"] == 3
        assert stats["filtered"] == 1
        assert stats["opened"] == 2
        assert any("EqPaper sector_scout ranking ontvangen" in rec.message for rec in caplog.records)
        assert any("reden=already_open" in rec.message for rec in caplog.records)

    def test_sector_ranking_respects_momentum_lost_cooldown(self, tmp_path, caplog):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        ant._set_momentum_cooldown(
            "XLB",
            datetime.now(tz=timezone.utc) - timedelta(seconds=60),
        )
        _write_sector_ranking(tmp_path, ["XLB", "XLK", "XLE"])

        with caplog.at_level("INFO", logger=f"ant.eq_paper.{ant.ant_id[:8]}"):
            stats = ant._process_scout_signals()

        assert "XLB" not in ant._open_symbols
        assert stats["received"] == 3
        assert stats["filtered"] == 1
        assert stats["opened"] == 2
        assert any("momentum_lost_cooldown_until" in rec.message for rec in caplog.records)

    def test_breakout_signal_opens_position(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 150.0)
        self._write_breakout_signal(tmp_path, symbol="MSFT", candidate_id="br-001")
        ant._process_breakout_signals()
        assert "MSFT" in ant._open_symbols

    def test_dividend_signal_opens_position(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 160.0)
        self._write_dividend_signal(tmp_path, symbol="JNJ", candidate_id="div-001")
        ant._process_dividend_candidates()
        assert "JNJ" in ant._open_symbols

    def test_duplicate_signal_not_processed_twice(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 200.0)
        self._write_scout_signal(tmp_path, symbol="AAPL", signal_id="sig-dup")
        ant._process_scout_signals()
        assert len(ant._ledger.open_positions) == 1

        # Tweede keer verwerken — zelfde signal_id → geen tweede positie
        ant._process_scout_signals()
        assert len(ant._ledger.open_positions) == 1

    def test_low_confidence_signal_ignored(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 200.0)
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source": "sector-scout",
            "payload": {
                "action":     "opportunity_detected",
                "biome":      "equities",
                "symbol":     "XLK",
                "signal_id":  "low-conf",
                "confidence": 0.40,   # onder drempel
                "side":       "long",
            },
        }
        (scout_dir / "sector_scout.jsonl").open("a").write(json.dumps(record) + "\n")
        ant._process_scout_signals()
        assert "XLK" not in ant._open_symbols

    def test_crypto_scout_signal_ignored_in_equities_ant(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 200.0)
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source": "crypto-scout",
            "payload": {
                "action":     "opportunity_detected",
                "biome":      "crypto",       # ← verkeerde biome
                "symbol":     "BTC-EUR",
                "signal_id":  "crypto-sig",
                "confidence": 0.90,
                "side":       "long",
            },
        }
        (scout_dir / "crypto_scout.jsonl").open("a").write(json.dumps(record) + "\n")
        ant._process_scout_signals()
        assert "BTC-EUR" not in ant._open_symbols

    def test_watchtower_candidate_opens_scaled_position(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        self._write_watchtower_candidate(tmp_path, symbol="AAPL", signal_id="wt-aapl-1")

        ant._process_watchtower_candidates()

        assert "AAPL" in ant._open_symbols
        pos = ant._ledger.open_positions[0]
        assert pos.watchtower_signal_id == "wt-aapl-1"
        # 500 capital * 10% normal trade fraction * 0.5 Watchtower scale = 25 EUR
        assert pos.quantity == pytest.approx(0.25)

    def test_watchtower_candidate_duplicate_not_processed_twice(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        self._write_watchtower_candidate(tmp_path, symbol="AAPL", signal_id="wt-dup")

        ant._process_watchtower_candidates()
        ant._process_watchtower_candidates()

        assert len(ant._ledger.open_positions) == 1

    def test_watchtower_candidate_wrong_biome_is_filtered(self, tmp_path):
        ant = _make_ant(tmp_path)
        self._mock_price(ant, 100.0)
        self._write_watchtower_candidate(
            tmp_path,
            symbol="NATGAS",
            signal_id="wt-natgas-1",
            biome="commodity",
        )

        stats = ant._process_watchtower_candidates()

        assert stats["received"] == 1
        assert stats["filtered"] == 1
        assert len(ant._ledger.open_positions) == 0


class TestWatchtowerFeedback:
    def test_feedback_sent_after_watchtower_position_close(self, tmp_path):
        ant = _make_ant(tmp_path)
        client = MagicMock()
        ant._watchtower_client = client
        opened_at = datetime.now(tz=timezone.utc) - timedelta(hours=3)
        pos = PaperPosition(
            position_id=uuid.uuid4().hex,
            symbol="AAPL",
            biome="equities",
            mission_id=ant.mission.mission_id,
            ant_id=ant.ant_id,
            side=PositionSide.LONG,
            entry_price=100.0,
            quantity=1.0,
            stop_loss_price=93.0,
            take_profit_price=200.0,
            ttl=ant.mission.ttl,
            current_price=105.0,
            peak_price=105.0,
            opened_at=opened_at,
            closed_at=datetime.now(tz=timezone.utc),
            exit_price=105.0,
            exit_reason="ttl_trading_days",
            watchtower_signal_id="wt-feedback-1",
        )

        class ImmediateThread:
            def __init__(self, target, args=(), kwargs=None, **_):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        with patch("ant_colony.ants.paper_ant_equities.threading.Thread", ImmediateThread):
            ant._send_watchtower_feedback(pos, "ttl_trading_days", pnl_pct=5.0, pnl_eur=5.0)

        client.post_outcome.assert_called_once()
        outcome = client.post_outcome.call_args.args[0]
        assert outcome["feedback_type"] == "TRADE_OUTCOME"
        assert outcome["signal_id"] == "wt-feedback-1"
        assert outcome["biome"] == "EQUITIES"
        assert outcome["exit_reason"] == "TTL"
        assert outcome["pnl_eur"] == 5.0
        assert outcome["duration_hours"] >= 2.9
