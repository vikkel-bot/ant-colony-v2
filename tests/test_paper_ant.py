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

from ant_colony.ants.paper_ant import (
    BROKER_FEE_PCT,
    PaperAnt,
    _MAX_OPEN_POSITIONS,
    _SL_PCT,
    _TP_PCT,
    _TRADE_CAPITAL_FRACTION,
)
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


def _make_adapter_with_price(price: float) -> MagicMock:
    md = MagicMock()
    md.close = price
    md.is_valid_price = True
    md.is_stale.return_value = False
    adapter = MagicMock()
    adapter.is_available.return_value = True
    adapter.get_market_data.return_value = md
    return adapter


def make_ant(mission: Mission | None = None, logs_root: Path | None = None) -> PaperAnt:
    mission = mission or make_mission()
    scheduler = MagicMock()
    biome_registry = MagicMock()
    biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
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
    detected_at: str | None = None,
) -> str:
    signal_id = signal_id or str(uuid.uuid4())
    scout_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "action": "opportunity_detected",
        "signal_id": signal_id,
        "symbol": symbol,
        "current_price": price,
        "confidence": confidence,
        "change_pct": change_pct,
        "signal_type": signal_type,
        "biome": biome,
    }
    if detected_at is not None:
        payload["detected_at"] = detected_at
    record = {
        "event_type": "action_executed",
        "source": "scout-001",
        "payload": payload,
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


# ---------------------------------------------------------------------------
# 11. Max open posities cap
# ---------------------------------------------------------------------------


class TestMaxOpenPositions:
    def test_cap_blocks_fourth_position(self, tmp_path: Path) -> None:
        symbols = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "ADA-EUR"]
        mission = make_mission(capital=100_000.0, symbols=symbols)
        ant = make_ant(mission=mission, logs_root=tmp_path)

        for sym in symbols[:_MAX_OPEN_POSITIONS]:
            write_scout_signal(tmp_path / "scouts", symbol=sym,
                               price=1_000.0, filename=f"s_{sym}.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == _MAX_OPEN_POSITIONS

        # Fourth symbol signal — should be blocked by cap
        write_scout_signal(tmp_path / "scouts", symbol=symbols[3],
                           price=1_000.0, filename="s_extra.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == _MAX_OPEN_POSITIONS

    def test_cap_allows_open_after_close(self, tmp_path: Path) -> None:
        symbols = ["BTC-EUR", "ETH-EUR", "SOL-EUR"]
        mission = make_mission(capital=100_000.0, symbols=symbols + ["ADA-EUR"])
        ant = make_ant(mission=mission, logs_root=tmp_path)

        for sym in symbols:
            write_scout_signal(tmp_path / "scouts", symbol=sym,
                               price=1_000.0, filename=f"s_{sym}.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == _MAX_OPEN_POSITIONS

        # Simulate close of one position (BTC-EUR) directly via ledger
        btc_pos = next(p for p in ant._ledger.open_positions if p.symbol == "BTC-EUR")
        from ant_colony.exit_chain.position import PositionStatus
        closed_pos = btc_pos.model_copy(update={
            "status": PositionStatus.CLOSED_TAKE_PROFIT,
            "exit_price": btc_pos.take_profit_price + 1.0,
            "closed_at": datetime.now(tz=timezone.utc),
        })
        ant._open_symbols.discard("BTC-EUR")
        ant._ledger.record_closed(closed_pos)
        assert len(ant._ledger.open_positions) == _MAX_OPEN_POSITIONS - 1

        # Now a new symbol should open
        write_scout_signal(tmp_path / "scouts", symbol="ADA-EUR",
                           price=1_000.0, filename="s_ada.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == _MAX_OPEN_POSITIONS


# ---------------------------------------------------------------------------
# 12. Herstart-recovery via _load_open_symbols_from_logs
# ---------------------------------------------------------------------------


def _write_paper_log_event(paper_dir: Path, ant_id: str, action: str,
                            position_id: str, symbol: str) -> None:
    """Schrijf een nep trade_opened/trade_closed event naar paper log."""
    paper_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "source": ant_id,
        "payload": {
            "action": action,
            "position_id": position_id,
            "symbol": symbol,
        },
    }
    (paper_dir / f"{ant_id}.jsonl").open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )


class TestStartupRecovery:
    def test_open_symbol_recovered_from_log(self, tmp_path: Path) -> None:
        old_ant_id = str(uuid.uuid4())
        pos_id = str(uuid.uuid4())
        _write_paper_log_event(tmp_path / "paper", old_ant_id, "trade_opened", pos_id, "BTC-EUR")

        ant = make_ant(logs_root=tmp_path)
        assert "BTC-EUR" in ant._open_symbols

    def test_closed_symbol_not_in_recovery(self, tmp_path: Path) -> None:
        old_ant_id = str(uuid.uuid4())
        pos_id = str(uuid.uuid4())
        paper_dir = tmp_path / "paper"
        _write_paper_log_event(paper_dir, old_ant_id, "trade_opened", pos_id, "BTC-EUR")
        _write_paper_log_event(paper_dir, old_ant_id, "trade_closed", pos_id, "BTC-EUR")

        ant = make_ant(logs_root=tmp_path)
        assert "BTC-EUR" not in ant._open_symbols

    def test_recovered_symbol_blocks_new_open(self, tmp_path: Path) -> None:
        old_ant_id = str(uuid.uuid4())
        pos_id = str(uuid.uuid4())
        _write_paper_log_event(tmp_path / "paper", old_ant_id, "trade_opened", pos_id, "BTC-EUR")

        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", symbol="BTC-EUR")
        ant._tick()
        # Ledger starts empty; new signal must be blocked by _open_symbols
        assert len(ant._ledger.open_positions) == 0

    def test_no_logs_root_returns_empty_set(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._open_symbols == set()

    def test_empty_paper_dir_returns_empty_set(self, tmp_path: Path) -> None:
        (tmp_path / "paper").mkdir()
        ant = make_ant(logs_root=tmp_path)
        assert ant._open_symbols == set()

    def test_trades_jsonl_not_scanned(self, tmp_path: Path) -> None:
        # _trades.jsonl (closed-trades log) must be ignored during recovery
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir(parents=True, exist_ok=True)
        record = {"position_id": str(uuid.uuid4()), "symbol": "BTC-EUR"}
        (paper_dir / "mission_abc_trades.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        ant = make_ant(logs_root=tmp_path)
        assert "BTC-EUR" not in ant._open_symbols

    def test_open_symbols_cleared_after_close(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts", symbol="BTC-EUR")
        ant._tick()
        assert "BTC-EUR" in ant._open_symbols

        pos = ant._ledger.open_positions[0]
        ant.biome_registry.get.return_value = _make_adapter_with_price(
            pos.take_profit_price + 1.0
        )
        ant._process_exits()
        assert "BTC-EUR" not in ant._open_symbols

    def test_open_symbols_updated_on_open(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert "BTC-EUR" not in ant._open_symbols
        write_scout_signal(tmp_path / "scouts", symbol="BTC-EUR")
        ant._tick()
        assert "BTC-EUR" in ant._open_symbols

    def test_corrupt_paper_log_no_crash(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        (paper_dir / "corrupt.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        assert ant._open_symbols == set()


# ---------------------------------------------------------------------------
# 13. Research-kandidaten verwerken
# ---------------------------------------------------------------------------


def write_research_candidate(
    research_dir: Path,
    *,
    candidate_id: str | None = None,
    symbol: str = _SYMBOL,
    strategy_type: str = "sma_crossover",
    direction: str = "long",
    tp_pct: float = 0.06,
    sl_pct: float = 0.03,
    sharpe: float = 0.8,
    biome: str = _BIOME,
    ant_id: str = "ant-research-001",
    filename: str | None = None,
    timestamp: str | None = None,
) -> str:
    import uuid as _uuid
    candidate_id = candidate_id or f"candidate-{_uuid.uuid4().hex[:8]}"
    research_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {
        "event_type": "action_executed",
        "source": ant_id,
        "payload": {
            "action": "candidate_accepted",
            "candidate_id": candidate_id,
            "symbol": symbol,
            "strategy_type": strategy_type,
            "direction": direction,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "sharpe": sharpe,
            "biome": biome,
        },
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    fname = filename or f"{ant_id}.jsonl"
    (research_dir / fname).open("a", encoding="utf-8").write(
        json.dumps(record) + "\n"
    )
    return candidate_id


class TestResearchCandidates:
    def test_research_candidate_opens_position(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_research_candidate_uses_tp_pct_from_log(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", tp_pct=0.08, sl_pct=0.04)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        expected_tp = _PRICE * (1.0 + 0.08)
        assert abs(pos.take_profit_price - expected_tp) < 0.01

    def test_research_candidate_uses_sl_pct_from_log(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", tp_pct=0.08, sl_pct=0.04)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        expected_sl = _PRICE * (1.0 - 0.04)
        assert abs(pos.stop_loss_price - expected_sl) < 0.01

    def test_research_candidate_dedup(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        cid = write_research_candidate(tmp_path / "research")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        # Same candidate written again in second file
        write_research_candidate(tmp_path / "research", candidate_id=cid, filename="ant-r2.jsonl")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_different_strategy_type_allows_second_position(self, tmp_path: Path) -> None:
        """BTC-EUR met twee verschillende strategy_types mag twee posities openen."""
        mission = make_mission(capital=50_000.0)
        ant = make_ant(mission=mission, logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(
            tmp_path / "research", strategy_type="sma_crossover", filename="r1.jsonl"
        )
        write_research_candidate(
            tmp_path / "research", strategy_type="rsi_based", filename="r2.jsonl"
        )
        ant._tick()
        assert len(ant._ledger.open_positions) == 2

    def test_same_strategy_type_blocks_duplicate(self, tmp_path: Path) -> None:
        """BTC-EUR met zelfde strategy_type mag maar één keer openen."""
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(
            tmp_path / "research", strategy_type="sma_crossover", filename="r1.jsonl"
        )
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        # Verander candidate_id maar zelfde symbool + strategy_type
        write_research_candidate(
            tmp_path / "research", strategy_type="sma_crossover", filename="r2.jsonl"
        )
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_short_direction_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", direction="short")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_no_price_skips_candidate(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = None  # no adapter
        write_research_candidate(tmp_path / "research")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_trade_opened_log_has_strategy_type(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", strategy_type="momentum")
        ant._tick()
        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")
        assert opened.get("strategy_type") == "momentum"

    def test_trade_opened_log_has_sl_pct_and_tp_pct(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", tp_pct=0.07, sl_pct=0.035)
        ant._tick()
        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")
        assert abs(opened["tp_pct"] - 0.07) < 1e-9
        assert abs(opened["sl_pct"] - 0.035) < 1e-9

    def test_research_candidate_closes_and_frees_slot(self, tmp_path: Path) -> None:
        """Na sluiting van research positie mag dezelfde (symbool, strategy_type) opnieuw."""
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        write_research_candidate(tmp_path / "research", strategy_type="rsi_based")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        assert ("BTC-EUR", "rsi_based") in ant._open_research_keys

        # Sluit de positie via take-profit
        pos = ant._ledger.open_positions[0]
        ant.biome_registry.get.return_value = _make_adapter_with_price(
            pos.take_profit_price + 1.0
        )
        ant._process_exits()
        assert len(ant._ledger.open_positions) == 0
        assert ("BTC-EUR", "rsi_based") not in ant._open_research_keys

    def test_no_research_dir_no_crash(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._process_research_candidates()  # research/ does not exist — no crash

    def test_corrupt_research_log_no_crash(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir()
        (research_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        ant._process_research_candidates()  # should not crash

    def test_preload_marks_existing_candidates_as_seen(self, tmp_path: Path) -> None:
        """Kandidaten die al in de log staan vóór startup worden niet verwerkt."""
        cid = write_research_candidate(tmp_path / "research")
        # Ant aanmaken ná schrijven → preload markeert cid als gezien
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        assert cid in ant._seen_research_ids
        ant._tick()
        # Geen positie — kandidaat was al aanwezig bij startup
        assert len(ant._ledger.open_positions) == 0

    def test_new_candidate_after_startup_is_processed(self, tmp_path: Path) -> None:
        """Kandidaat die ná startup arriveert wordt wel verwerkt."""
        # Ant eerst aanmaken (lege research dir)
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(_PRICE)
        # Nu een nieuw kandidaat schrijven
        write_research_candidate(tmp_path / "research")
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_preload_none_logs_root_returns_empty(self) -> None:
        from ant_colony.ants.paper_ant import PaperAnt
        seen = PaperAnt._preload_seen_research_ids(None)
        assert seen == set()


# ---------------------------------------------------------------------------
# 11. Broker fees
# ---------------------------------------------------------------------------


class TestBrokerFees:
    def test_broker_fee_pct_value(self) -> None:
        assert BROKER_FEE_PCT == pytest.approx(0.0025)

    def test_trade_opened_has_effective_entry_and_fee(self, tmp_path: Path) -> None:
        """trade_opened event bevat effective_entry en broker_entry_fee."""
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        ant._emit_trade_opened(pos, {})

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        opened = next(r["payload"] for r in records if r["payload"]["action"] == "trade_opened")

        assert "effective_entry"  in opened
        assert "broker_entry_fee" in opened
        assert opened["effective_entry"]  == pytest.approx(_PRICE * (1 + BROKER_FEE_PCT))
        assert opened["broker_entry_fee"] == pytest.approx(_PRICE * pos.quantity * BROKER_FEE_PCT)

    def test_trade_closed_has_fee_fields(self, tmp_path: Path) -> None:
        """trade_closed event bevat broker_fee_cost, realized_pnl_gross, effective_exit."""
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0
        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        closed = next(r["payload"] for r in records if r["payload"]["action"] == "trade_closed")

        assert "broker_fee_cost"    in closed
        assert "realized_pnl_gross" in closed
        assert "effective_exit"     in closed
        assert closed["broker_fee_cost"] > 0

    def test_trade_closed_pnl_net_of_fees(self, tmp_path: Path) -> None:
        """realized_pnl in trade_closed is gecorrigeerd voor brokerkosten."""
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0
        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        closed = next(r["payload"] for r in records if r["payload"]["action"] == "trade_closed")

        gross = closed["realized_pnl_gross"]
        fee   = closed["broker_fee_cost"]
        net   = closed["realized_pnl"]
        assert net == pytest.approx(gross - fee, rel=1e-5)
        assert net < gross  # fees reduceren altijd de PnL

    def test_fee_reduces_pnl_pct(self, tmp_path: Path) -> None:
        """pnl_pct is berekend op basis van netto PnL, niet bruto."""
        ant = make_ant(logs_root=tmp_path)
        pos = make_open_position(ant, entry_price=_PRICE)
        tp_price = pos.take_profit_price + 1.0
        ant.biome_registry.get.return_value = _make_adapter_with_price(tp_price)
        ant._process_exits()

        log_path = tmp_path / "paper" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        closed = next(r["payload"] for r in records if r["payload"]["action"] == "trade_closed")

        expected_pnl_pct = round(
            closed["realized_pnl"] / (_PRICE * pos.quantity) * 100, 4
        )
        assert closed["pnl_pct"] == pytest.approx(expected_pnl_pct, rel=1e-4)


# ---------------------------------------------------------------------------
# 15. Staleness-filter en live-price gedrag
# ---------------------------------------------------------------------------


class TestStalenessAndLivePrice:
    def test_stale_scout_signal_not_processed(self, tmp_path: Path) -> None:
        """Scout signal met detected_at > 5 min oud wordt niet geopend."""
        ant = make_ant(logs_root=tmp_path)
        stale_ts = (datetime.now(tz=timezone.utc) - timedelta(minutes=10)).isoformat()
        sid = write_scout_signal(tmp_path / "scouts", detected_at=stale_ts)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0
        assert sid in ant._processed_signals  # wel als gezien gemarkeerd

    def test_fresh_scout_signal_processed(self, tmp_path: Path) -> None:
        """Scout signal met recent detected_at wordt wel geopend."""
        ant = make_ant(logs_root=tmp_path)
        fresh_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=30)).isoformat()
        write_scout_signal(tmp_path / "scouts", detected_at=fresh_ts)
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_scout_signal_no_timestamp_treated_as_fresh(self, tmp_path: Path) -> None:
        """Scout signal zonder detected_at wordt als vers behandeld (fail-open)."""
        ant = make_ant(logs_root=tmp_path)
        write_scout_signal(tmp_path / "scouts")  # geen detected_at
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_stale_research_candidate_not_processed(self, tmp_path: Path) -> None:
        """Research kandidaat met timestamp > 5 min oud wordt niet verwerkt."""
        ant = make_ant(logs_root=tmp_path)
        stale_ts = (datetime.now(tz=timezone.utc) - timedelta(minutes=10)).isoformat()
        cid = write_research_candidate(tmp_path / "research", timestamp=stale_ts)
        ant._tick()
        assert len(ant._ledger.open_positions) == 0
        assert cid in ant._seen_research_ids  # wel als gezien gemarkeerd

    def test_fresh_research_candidate_processed(self, tmp_path: Path) -> None:
        """Research kandidaat met recent timestamp wordt wel verwerkt."""
        ant = make_ant(logs_root=tmp_path)
        fresh_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=30)).isoformat()
        write_research_candidate(tmp_path / "research", timestamp=fresh_ts)
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_research_candidate_no_timestamp_treated_as_fresh(self, tmp_path: Path) -> None:
        """Research kandidaat zonder timestamp wordt als vers behandeld (fail-open)."""
        ant = make_ant(logs_root=tmp_path)
        write_research_candidate(tmp_path / "research")  # geen timestamp
        ant._tick()
        assert len(ant._ledger.open_positions) == 1

    def test_try_open_position_uses_live_price_not_signal_price(self, tmp_path: Path) -> None:
        """Positie wordt geopend op live marktprijs, niet op de prijs uit het signaal."""
        live_price = 50_000.0
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(live_price)
        write_scout_signal(tmp_path / "scouts", price=99_999.0)  # signal prijs genegeerd
        ant._tick()
        assert len(ant._ledger.open_positions) == 1
        pos = ant._ledger.open_positions[0]
        assert abs(pos.entry_price - live_price) < 0.01

    def test_no_live_price_blocks_scout_entry(self, tmp_path: Path) -> None:
        """Geen live prijs beschikbaar → geen positie geopend."""
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = None
        write_scout_signal(tmp_path / "scouts")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_no_live_price_blocks_research_entry(self, tmp_path: Path) -> None:
        """Geen live prijs voor research kandidaat → geen positie geopend."""
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = None
        write_research_candidate(tmp_path / "research")
        ant._tick()
        assert len(ant._ledger.open_positions) == 0

    def test_sl_tp_based_on_live_price(self, tmp_path: Path) -> None:
        """SL en TP worden berekend op basis van live prijs, niet signal prijs."""
        live_price = 50_000.0
        ant = make_ant(logs_root=tmp_path)
        ant.biome_registry.get.return_value = _make_adapter_with_price(live_price)
        write_scout_signal(tmp_path / "scouts", price=99_999.0)
        ant._tick()
        pos = ant._ledger.open_positions[0]
        from ant_colony.ants.paper_ant import _SL_PCT, _TP_PCT
        assert abs(pos.stop_loss_price - live_price * (1.0 - _SL_PCT)) < 0.01
        assert abs(pos.take_profit_price - live_price * (1.0 + _TP_PCT)) < 0.01
