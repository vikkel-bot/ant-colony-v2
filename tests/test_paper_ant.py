"""
tests/test_paper_ant.py

Volledige coverage voor PaperAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.paper_ant import PaperAnt, _SL_PCT, _TP_PCT, _TRADE_CAPITAL_FRACTION
from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus
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

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"
_PRICE  = 40_000.0


def make_mission(capital: float = 10_000.0, symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="paper_ant",
        allowed_node="pc2",
        allowed_actions=["open_position", "close_position"],
        market_scope=MarketScope(biome=_BIOME, symbols=symbols or [_SYMBOL]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
            stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="paper test"),
    )


def make_ant(mission: Mission | None = None, logs_root: Path | None = None) -> PaperAnt:
    mission = mission or make_mission()
    scheduler = MagicMock()
    biome_registry = MagicMock()
    biome_registry.get.return_value = None  # no live prices by default
    return PaperAnt(
        ant_id=str(uuid.uuid4()),
        mission=mission,
        scheduler=scheduler,
        biome_registry=biome_registry,
        logs_root=logs_root,
    )


def write_scout_signal(
    scout_dir: Path,
    *,
    signal_id: str | None = None,
    symbol: str = _SYMBOL,
    price: float = _PRICE,
    confidence: float = 0.8,
    change_pct: float = 0.05,
    signal_type: str = "price_move",
    biome: str = _BIOME,
    filename: str = "scout_1.jsonl",
) -> str:
    signal_id = signal_id or str(uuid.uuid4())
    scout_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "source": "scout-001",
        "payload": {
            "action": "opportunity_detected",
            "signal_id": signal_id,
            "symbol": symbol,
            "current_price": price,
            "confidence": confidence,
            "change_pct": change_pct,
            "signal_type": signal_type,
            "biome": biome,
        },
    }
    (scout_dir / filename).open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )
    return signal_id


def make_open_position(
    ant: PaperAnt,
    *,
    symbol: str = _SYMBOL,
    entry_price: float = _PRICE,
) -> PaperPosition:
    mission = ant.mission
    pos = PaperPosition(
        position_id=str(uuid.uuid4()),
        symbol=symbol,
        biome=_BIOME,
        mission_id=mission.mission_id,
        ant_id=ant.ant_id,
        side=PositionSide.LONG,
        entry_price=entry_price,
        quantity=0.01,
        stop_loss_price=entry_price * 0.98,
        take_profit_price=entry_price * 1.03,
        ttl=mission.ttl,
        current_price=entry_price,
        peak_price=entry_price,
        opened_at=datetime.now(tz=timezone.utc),
    )
    ant._ledger.record_opened(pos)
    return pos


# ---------------------------------------------------------------------------
# 1. Signaalverwerking — basispad
# ---------------------------------------------------------------------------


class TestSignalProcessing:
    def test_high_confidence_opens_trade(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", confidence=0.9)
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_low_confidence_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", confidence=0.5)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_exact_threshold_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", confidence=0.6)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_duplicate_signal_id_no_second_trade(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        sid = write_scout_signal(tmp_path / "scouts")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        # Write same signal again
        write_scout_signal(tmp_path / "scouts", signal_id=sid, filename="scout_2.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_symbol_already_open_skips_new_signal(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        make_open_position(ant)
        write_scout_signal(tmp_path / "scouts")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_negative_price_move_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", change_pct=-0.05, signal_type="price_move")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_no_scout_dir_no_crash(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()  # scouts/ does not exist
        assert len(ant._ledger.open_positions) == 0

    def test_logs_root_none_no_crash(self) -> None:
        ant = make_ant(logs_root=None)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0


# ---------------------------------------------------------------------------
# 2. Entry-positie parameters
# ---------------------------------------------------------------------------


class TestEntryParameters:
    def test_stop_loss_set_correctly(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", price=_PRICE)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        expected_sl = round(_PRICE * (1.0 - _SL_PCT), 8)
        assert abs(pos.stop_loss_price - expected_sl) < 0.01

    def test_take_profit_set_correctly(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", price=_PRICE)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        expected_tp = round(_PRICE * (1.0 + _TP_PCT), 8)
        assert abs(pos.take_profit_price - expected_tp) < 0.01

    def test_side_is_long(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts")
        ant._tick()
        pos = ant._ledger.open_positions[0]
        assert pos.side == PositionSide.LONG

    def test_quantity_uses_capital_fraction(self, tmp_path: Path) -> None:
        capital = 10_000.0
        ant = make_ant(mission=make_mission(capital=capital), logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", price=_PRICE)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        expected_qty = (capital * _TRADE_CAPITAL_FRACTION) / _PRICE
        # broker may cap, but should be in the right ballpark
        assert pos.quantity > 0
        assert pos.quantity <= expected_qty + 1e-8

    def test_capital_limit_zero_rejects_trade(self, tmp_path: Path) -> None:
        ant = make_ant(mission=make_mission(capital=0.0), logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0


# ---------------------------------------------------------------------------
# 3. Exit verwerking
# ---------------------------------------------------------------------------


def _make_adapter_with_price(price: float) -> MagicMock:
    md = MagicMock()
    md.close = price
    md.is_valid_price = True
    md.is_stale.return_value = False
    adapter = MagicMock()
    adapter.is_available.return_value = True
    adapter.get_market_data.return_value = md
    return adapter


class TestExitProcessing:
    def test_take_profit_closes_position(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0  # above TP

        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        assert len(ant._ledger.open_positions) == 0
        assert len(ant._ledger.closed_trades) == 1
        closed = ant._ledger.closed_trades[0]
        assert closed.status == PositionStatus.CLOSED_TAKE_PROFIT

    def test_stop_loss_closes_position(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        sl_price = pos.stop_loss_price - 1.0  # below SL

        ant.biome_registry.get.return_value = _make_adapter_with_price(sl_price)
        ant._process_exits()

        assert len(ant._ledger.open_positions) == 0
        closed = ant._ledger.closed_trades[0]
        assert closed.status == PositionStatus.CLOSED_STOP_LOSS

    def test_no_price_position_stays_open(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        make_open_position(ant)
        ant.biome_registry.get.return_value = None  # no adapter
        ant._process_exits()
        assert len(ant._ledger.open_positions) == 1

    def test_price_between_sl_tp_stays_open(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        mid_price = (pos.stop_loss_price + pos.take_profit_price) / 2

        ant.biome_registry.get.return_value = _make_adapter_with_price(mid_price)
        ant._process_exits()

        assert len(ant._ledger.open_positions) == 1


# ---------------------------------------------------------------------------
# 4. Exit-first doctrine (P3)
# ---------------------------------------------------------------------------


class TestExitFirstDoctrine:
    def test_exits_evaluated_before_entries(self, tmp_path: Path) -> None:
        call_order: list[str] = []
        ant = make_ant(logs_root=tmp_path)

        original_exits = ant._process_exits
        original_entries = ant._process_new_signals

        def exits_spy():
            call_order.append("exits")
            original_exits()

        def entries_spy():
            call_order.append("entries")
            original_entries()

        ant._process_exits = exits_spy
        ant._process_new_signals = entries_spy

        ant._tick()
        assert call_order == ["exits", "entries"]


# ---------------------------------------------------------------------------
# 5. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_trade_opened_event_written(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts")
        ant._tick()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions = [r["payload"]["action"] for r in records]
        assert "trade_opened" in actions

    def test_trade_opened_event_has_required_fields(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts")
        ant._tick()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")
        for field in ("position_id", "symbol", "side", "entry_price", "quantity",
                      "stop_loss", "take_profit"):
            assert field in opened, f"Missing field: {field}"

    def test_trade_closed_event_written(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0
        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions = [r["payload"]["action"] for r in records]
        assert "trade_closed" in actions

    def test_trade_closed_event_has_pnl(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0
        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        closed = next(r["payload"] for r in records if r["payload"]["action"] == "trade_closed")
        assert "realized_pnl" in closed
        assert "pnl_pct" in closed

    def test_pnl_summary_on_exit(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._emit_pnl_summary()
        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions = [r["payload"]["action"] for r in records]
        assert "pnl_summary" in actions

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        ant._emit_pnl_summary()  # should not crash


# ---------------------------------------------------------------------------
# 6. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent_on_send_heartbeat(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()  # must not propagate

    def test_heartbeat_contains_ant_id(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        hb = ant.scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id


# ---------------------------------------------------------------------------
# 7. TTL en lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        mission = make_mission()
        ant = make_ant(mission=mission)
        ant._status = AntStatus.RUNNING

        with patch("ant_colony.ants.paper_ant.time.sleep"):
            with patch("ant_colony.ants.paper_ant.datetime") as mock_dt:
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_final_heartbeat_sent_on_exit(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)

        with patch("ant_colony.ants.paper_ant.time.sleep"):
            with patch("ant_colony.ants.paper_ant.datetime") as mock_dt:
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]

                ant.run()

        # At least 1 heartbeat (from finally block)
        assert ant.scheduler.record_heartbeat.call_count >= 1

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()

        with patch("ant_colony.ants.paper_ant.time.sleep", side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.paper_ant.datetime") as mock_dt:
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                mock_dt.now.return_value = start

                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_exception_in_tick_returns_aborted(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(side_effect=RuntimeError("tick boom"))

        with patch("ant_colony.ants.paper_ant.time.sleep", side_effect=Exception("stop")):
            with patch("ant_colony.ants.paper_ant.datetime") as mock_dt:
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                mock_dt.now.return_value = start

                status = ant.run()

        assert status == AntStatus.ABORTED


# ---------------------------------------------------------------------------
# 8. Verwerkte signalen deduplicatie
# ---------------------------------------------------------------------------


class TestProcessedSignals:
    def test_processed_signals_set_grows(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        sid = write_scout_signal(tmp_path / "scouts")
        ant._tick()
        assert sid in ant._processed_signals

    def test_multiple_different_signals_all_tracked(self, tmp_path: Path) -> None:
        mission = make_mission(symbols=[_SYMBOL, "ETH-EUR"])
        ant = make_ant(mission=mission, logs_root=tmp_path)
        sid1 = write_scout_signal(tmp_path / "scouts", symbol=_SYMBOL, filename="s1.jsonl")
        sid2 = write_scout_signal(tmp_path / "scouts", symbol="ETH-EUR", filename="s2.jsonl")
        ant._tick()
        assert sid1 in ant._processed_signals
        assert sid2 in ant._processed_signals


# ---------------------------------------------------------------------------
# 9. Meerdere symbolen
# ---------------------------------------------------------------------------


class TestMultipleSymbols:
    def test_two_signals_different_symbols_both_open(self, tmp_path: Path) -> None:
        mission = make_mission(symbols=[_SYMBOL, "ETH-EUR"])
        ant = make_ant(mission=mission, logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", symbol=_SYMBOL, filename="s1.jsonl")
        write_scout_signal(tmp_path / "scouts", symbol="ETH-EUR", price=2_000.0, filename="s2.jsonl")
        ant._tick()
        symbols = {p.symbol for p in ant._ledger.open_positions}
        assert symbols == {_SYMBOL, "ETH-EUR"}

    def test_second_signal_same_symbol_blocked(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", filename="s1.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        write_scout_signal(tmp_path / "scouts", filename="s2.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1


# ---------------------------------------------------------------------------
# 10. Scout log corrupt / leeg
# ---------------------------------------------------------------------------


class TestScoutLogEdgeCases:
    def test_corrupt_json_line_skipped(self, tmp_path: Path) -> None:
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir()
        (scout_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()  # should not crash
        assert len(ant._ledger.open_positions) == 0

    def test_signal_missing_signal_id_skipped(self, tmp_path: Path) -> None:
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir()
        record = {"payload": {"action": "opportunity_detected", "symbol": _SYMBOL,
                               "current_price": _PRICE, "confidence": 0.9}}
        (scout_dir / "s.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_empty_jsonl_no_crash(self, tmp_path: Path) -> None:
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir()
        (scout_dir / "empty.jsonl").write_text("", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_unreadable_file_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        scout_dir = tmp_path / "scouts"
        scout_dir.mkdir()
        with patch.object(Path, "read_text", side_effect=OSError("perm")):
            ant._tick()
        assert len(ant._ledger.open_positions) == 0
