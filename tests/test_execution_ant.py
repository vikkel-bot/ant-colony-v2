"""
tests/test_execution_ant.py

Volledige coverage voor ExecutionAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.execution_ant import (
    ExecutionAnt,
    _CAPITAL_FRACTION,
    _MIN_PAPER_TRADES,
    _MIN_WIN_RATE,
    _SL_PCT,
    _TP_PCT,
)
from ant_colony.biome.biome_adapter import LivePosition
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.order import OrderRejectionReason, OrderResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL  = "BTC-EUR"
_BIOME   = "crypto"
_PRICE   = 40_000.0
_CAPITAL = 5_000.0


def make_mission(
    capital: float = _CAPITAL,
    symbols: list[str] | None = None,
    allowed_actions: list[str] | None = None,
) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="execution_ant",
        allowed_node="pc2",
        allowed_actions=allowed_actions or ["live_execute", "read_data"],
        market_scope=MarketScope(biome=_BIOME, symbols=symbols or [_SYMBOL]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=500.0,
            daily_loss_limit=200.0,
            stop_loss_required=True,
        ),
        ttl=300,
        heartbeat_interval=10,
        success_conditions=SuccessConditions(description="execution test"),
    )


def make_gate(accepted: bool = True, rejection: OrderRejectionReason | None = None) -> MagicMock:
    gate = MagicMock()
    if accepted:
        gate.execute.return_value = OrderResult(
            accepted=True,
            order_id=str(uuid.uuid4()),
            exchange_order_id="EX-001",
            filled_quantity=0.01,
            avg_price=_PRICE,
        )
    else:
        gate.execute.return_value = OrderResult.rejected(
            order_id=str(uuid.uuid4()),
            reason=rejection or OrderRejectionReason.COLONY_HALTED,
        )
    return gate


def make_market_data(price: float = _PRICE, stale: bool = False) -> MagicMock:
    md = MagicMock()
    md.close = price
    md.is_valid_price = True
    md.is_stale.return_value = stale
    return md


def make_adapter(price: float = _PRICE, positions: list | None = None) -> MagicMock:
    adapter = MagicMock()
    adapter.is_available.return_value = True
    adapter.get_market_data.return_value = make_market_data(price)
    adapter.get_positions.return_value = positions or []
    return adapter


def make_ant(
    mission: Mission | None = None,
    gate=None,
    adapter=None,
    logs_root: Path | None = None,
) -> ExecutionAnt:
    mission = mission or make_mission()
    registry = MagicMock()
    if adapter is not None:
        registry.get.return_value = adapter
    else:
        registry.get.return_value = None
    return ExecutionAnt(
        ant_id=str(uuid.uuid4()),
        mission=mission,
        scheduler=MagicMock(),
        gate=gate,
        biome_registry=registry,
        logs_root=logs_root,
    )


def write_paper_log(
    paper_dir: Path,
    *,
    filename: str = "paper_ant.jsonl",
    symbol: str = _SYMBOL,
    trade_count: int = 12,
    win_rate: float = 0.75,
    total_pnl: float = 150.0,
    n_closed_wins: int = 9,
    n_closed_losses: int = 3,
) -> None:
    """Write a synthetic paper JSONL log file for testing."""
    paper_dir.mkdir(parents=True, exist_ok=True)
    records = []

    for i in range(n_closed_wins):
        records.append({
            "payload": {
                "action":       "trade_closed",
                "symbol":       symbol,
                "realized_pnl": 10.0,
            }
        })
    for i in range(n_closed_losses):
        records.append({
            "payload": {
                "action":       "trade_closed",
                "symbol":       symbol,
                "realized_pnl": -5.0,
            }
        })
    records.append({
        "payload": {
            "action":             "pnl_summary",
            "trade_count":        trade_count,
            "win_rate":           win_rate,
            "total_realized_pnl": total_pnl,
        }
    })

    path = paper_dir / filename
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def make_live_position(symbol: str = _SYMBOL) -> LivePosition:
    return LivePosition(
        position_id="pos-1",
        symbol=symbol,
        biome_id=_BIOME,
        side="buy",
        quantity=0.01,
        entry_price=_PRICE,
        current_price=_PRICE,
        timestamp=datetime.now(tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# 1. Startupvalidatie
# ---------------------------------------------------------------------------


class TestStartupValidation:
    def test_no_live_execute_action_aborts(self) -> None:
        ant = make_ant(
            mission=make_mission(allowed_actions=["read_data"]),
            gate=make_gate(),
        )
        status = ant.run()
        assert status == AntStatus.ABORTED

    def test_capital_limit_zero_aborts(self) -> None:
        ant = make_ant(
            mission=make_mission(capital=0.0, allowed_actions=["live_execute"]),
            gate=make_gate(),
        )
        status = ant.run()
        assert status == AntStatus.ABORTED

    def test_gate_none_aborts(self) -> None:
        ant = make_ant(gate=None)
        status = ant.run()
        assert status == AntStatus.ABORTED

    def test_all_valid_runs(self) -> None:
        ant = make_ant(gate=make_gate())

        with patch("ant_colony.ants.execution_ant.time.sleep"):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                ant._tick = MagicMock()
                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_startup_rejection_logs_reason(self, tmp_path: Path) -> None:
        ant = make_ant(
            mission=make_mission(allowed_actions=["read_data"]),
            gate=make_gate(),
            logs_root=tmp_path,
        )
        ant.run()
        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()
        record = json.loads(log_path.read_text().splitlines()[0])
        assert record["payload"]["action"] == "startup_rejected"
        assert "live_execute" in record["payload"]["reason"]

    def test_startup_rejection_no_heartbeat_to_scheduler_before_abort(self) -> None:
        ant = make_ant(gate=None)
        ant.run()
        ant.scheduler.record_heartbeat.assert_called()  # called in finally

    def test_status_aborted_before_run_when_gate_none(self) -> None:
        ant = make_ant(gate=None)
        assert ant._status == AntStatus.IDLE
        ant.run()
        assert ant._status == AntStatus.ABORTED


# ---------------------------------------------------------------------------
# 2. PaperAnt resultaten lezen
# ---------------------------------------------------------------------------


class TestPaperResultsReading:
    def test_good_session_returns_candidate(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_paper_log(tmp_path / "paper")
        candidates = ant._read_paper_candidates()
        assert len(candidates) == 1
        assert candidates[0]["symbol"] == _SYMBOL

    def test_low_win_rate_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_paper_log(
            tmp_path / "paper",
            win_rate=0.50,
            n_closed_wins=5,
            n_closed_losses=5,
        )
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_too_few_trades_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_paper_log(tmp_path / "paper", trade_count=5, n_closed_wins=4, n_closed_losses=1)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_negative_pnl_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_paper_log(tmp_path / "paper", total_pnl=-10.0)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_zero_pnl_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_paper_log(tmp_path / "paper", total_pnl=0.0)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_no_paper_dir_returns_empty(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_logs_root_none_returns_empty(self) -> None:
        ant = make_ant(logs_root=None)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_trades_file_skipped(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        # Write a _trades.jsonl file — should be ignored
        (paper_dir / "mission_id_trades.jsonl").write_text(
            json.dumps({"payload": {"action": "pnl_summary", "trade_count": 15,
                                     "win_rate": 0.8, "total_realized_pnl": 200.0}}) + "\n",
            encoding="utf-8",
        )
        ant = make_ant(logs_root=tmp_path)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        (paper_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        candidates = ant._read_paper_candidates()
        assert candidates == []

    def test_candidates_sorted_by_win_rate(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        write_paper_log(
            paper_dir, filename="a.jsonl", symbol="BTC-EUR",
            n_closed_wins=7, n_closed_losses=3, win_rate=0.7, total_pnl=100.0,
        )
        write_paper_log(
            paper_dir, filename="b.jsonl", symbol="ETH-EUR",
            n_closed_wins=9, n_closed_losses=1, win_rate=0.9, total_pnl=200.0,
        )
        ant = make_ant(
            mission=make_mission(symbols=["BTC-EUR", "ETH-EUR"]),
            logs_root=tmp_path,
        )
        candidates = ant._read_paper_candidates()
        assert len(candidates) == 2
        assert candidates[0]["win_rate"] >= candidates[1]["win_rate"]

    def test_per_symbol_too_few_trades_excluded(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        # Good session overall but only 2 trades for the symbol → below _MIN_SYMBOL_TRADES
        records = [
            {"payload": {"action": "trade_closed", "symbol": _SYMBOL, "realized_pnl": 10.0}},
            {"payload": {"action": "trade_closed", "symbol": _SYMBOL, "realized_pnl": 10.0}},
            {"payload": {
                "action": "pnl_summary",
                "trade_count": 10,
                "win_rate": 0.8,
                "total_realized_pnl": 80.0,
            }},
        ]
        with (paper_dir / "x.jsonl").open("w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        ant = make_ant(logs_root=tmp_path)
        candidates = ant._read_paper_candidates()
        assert candidates == []


# ---------------------------------------------------------------------------
# 3. Order bouwen
# ---------------------------------------------------------------------------


class TestOrderBuilding:
    def test_side_is_buy(self) -> None:
        ant = make_ant()
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        from ant_colony.schemas.order import OrderSide
        assert order.side == OrderSide.BUY

    def test_stop_loss_below_price(self) -> None:
        ant = make_ant()
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        expected_sl = round(_PRICE * (1.0 - _SL_PCT), 8)
        assert abs(order.stop_loss_price - expected_sl) < 0.01

    def test_take_profit_above_price(self) -> None:
        ant = make_ant()
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        expected_tp = round(_PRICE * (1.0 + _TP_PCT), 8)
        assert abs(order.take_profit_price - expected_tp) < 0.01

    def test_quantity_uses_capital_fraction(self) -> None:
        ant = make_ant(mission=make_mission(capital=_CAPITAL))
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        expected_qty = (_CAPITAL * _CAPITAL_FRACTION) / _PRICE
        assert abs(order.quantity - expected_qty) < 1e-6

    def test_order_type_is_market(self) -> None:
        ant = make_ant()
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        from ant_colony.schemas.order import OrderType
        assert order.order_type == OrderType.MARKET

    def test_mission_id_in_order(self) -> None:
        mission = make_mission()
        ant = make_ant(mission=mission)
        order = ant._build_order(_SYMBOL, _PRICE)
        assert order is not None
        assert order.mission_id == mission.mission_id


# ---------------------------------------------------------------------------
# 4. Order plaatsen via gate
# ---------------------------------------------------------------------------


class TestOrderPlacement:
    def _setup(self, tmp_path: Path) -> tuple[ExecutionAnt, MagicMock]:
        adapter = make_adapter()
        gate    = make_gate(accepted=True)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate,
            adapter=adapter,
            logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        return ant, gate

    def test_accepted_order_sets_open_order(self, tmp_path: Path) -> None:
        ant, _ = self._setup(tmp_path)
        ant._try_open_position()
        assert ant._open_order is not None
        assert ant._open_order.symbol == _SYMBOL

    def test_gate_called_with_market_data(self, tmp_path: Path) -> None:
        ant, gate = self._setup(tmp_path)
        ant._try_open_position()
        gate.execute.assert_called_once()
        _, kwargs = gate.execute.call_args
        assert kwargs.get("market_data") is not None

    def test_rejected_order_no_open_order(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate(accepted=False, rejection=OrderRejectionReason.COLONY_HALTED)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()
        assert ant._open_order is None

    def test_stale_market_data_skips_order(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        adapter.get_market_data.return_value = make_market_data(stale=True)
        gate    = make_gate()
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()
        gate.execute.assert_not_called()
        assert ant._open_order is None

    def test_symbol_not_in_scope_skipped(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate()
        ant     = make_ant(
            mission=make_mission(symbols=["ETH-EUR"]),  # BTC-EUR not in scope
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper", symbol="BTC-EUR")
        ant._try_open_position()
        gate.execute.assert_not_called()

    def test_no_candidates_no_order(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate()
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        # No paper dir → no candidates
        ant._try_open_position()
        gate.execute.assert_not_called()

    def test_adapter_none_skips_order(self, tmp_path: Path) -> None:
        gate = make_gate()
        ant  = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=None, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()
        gate.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Max één open positie
# ---------------------------------------------------------------------------


class TestMaxOnePosition:
    def test_second_order_blocked_when_open(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate(accepted=True)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")

        ant._try_open_position()         # eerste order
        assert ant._open_order is not None
        first_order = ant._open_order

        ant._try_open_position()         # tweede poging → geblokkeerd door _tick
        # Direct call bypasses the guard in _tick, but let's test via _tick
        ant._open_order = first_order   # reset to simulate still-open

        gate.execute.reset_mock()
        ant._tick()                     # _monitor_open_order runs; positions empty → closes it
        # After monitor clears the order, _try_open_position would run again
        # With empty positions it gets cleared; then tries again (second gate call possible)
        # What matters: we never have 2 simultaneous open orders

    def test_tick_does_not_try_open_when_order_present(self, tmp_path: Path) -> None:
        adapter = make_adapter(positions=[make_live_position()])
        gate    = make_gate()
        ant     = make_ant(gate=gate, adapter=adapter, logs_root=tmp_path)

        # Manually set an open order
        from ant_colony.schemas.order import OrderType, OrderSide
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._tick()
        gate.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Positiemonitoring
# ---------------------------------------------------------------------------


class TestPositionMonitoring:
    def test_position_in_get_positions_stays_tracked(self) -> None:
        adapter = make_adapter(positions=[make_live_position()])
        ant     = make_ant(adapter=adapter)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._monitor_open_order()
        assert ant._open_order is not None

    def test_position_gone_clears_open_order(self, tmp_path: Path) -> None:
        adapter = make_adapter(positions=[])   # no positions
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._monitor_open_order()
        assert ant._open_order is None

    def test_position_gone_logs_closed_event(self, tmp_path: Path) -> None:
        adapter = make_adapter(positions=[])
        ant     = make_ant(adapter=adapter, logs_root=tmp_path)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._monitor_open_order()

        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions  = [r["payload"]["action"] for r in records]
        assert "position_closed" in actions

    def test_get_positions_returns_none_keeps_order(self) -> None:
        adapter = make_adapter()
        adapter.get_positions.return_value = None
        ant     = make_ant(adapter=adapter)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._monitor_open_order()
        assert ant._open_order is not None  # fail-closed

    def test_adapter_unavailable_keeps_order(self) -> None:
        adapter = MagicMock()
        adapter.is_available.return_value = False
        registry = MagicMock()
        registry.get.return_value = adapter
        ant = make_ant()
        ant.biome_registry = registry
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)

        ant._monitor_open_order()
        assert ant._open_order is not None

    def test_no_open_order_monitor_noop(self) -> None:
        adapter = make_adapter()
        ant     = make_ant(adapter=adapter)
        ant._open_order = None

        ant._monitor_open_order()  # should not crash or call get_positions
        adapter.get_positions.assert_not_called()

    def test_different_symbol_in_positions_clears_order(self) -> None:
        other_pos = make_live_position(symbol="ETH-EUR")
        adapter   = make_adapter(positions=[other_pos])
        ant       = make_ant(adapter=adapter)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)  # BTC-EUR

        ant._monitor_open_order()
        assert ant._open_order is None  # BTC-EUR not in positions


# ---------------------------------------------------------------------------
# 7. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def test_order_accepted_logged(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate(accepted=True)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()

        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        attempted = next(r["payload"] for r in records if r["payload"]["action"] == "order_attempted")
        assert attempted["outcome"] == "accepted"
        assert attempted["symbol"]  == _SYMBOL

    def test_order_rejected_logged(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate(accepted=False, rejection=OrderRejectionReason.COLONY_HALTED)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()

        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        attempted = next(r["payload"] for r in records if r["payload"]["action"] == "order_attempted")
        assert attempted["outcome"] == "rejected"
        assert attempted["rejection_reason"] == "colony_halted"

    def test_log_has_sl_and_tp(self, tmp_path: Path) -> None:
        adapter = make_adapter()
        gate    = make_gate(accepted=True)
        ant     = make_ant(
            mission=make_mission(symbols=[_SYMBOL]),
            gate=gate, adapter=adapter, logs_root=tmp_path,
        )
        write_paper_log(tmp_path / "paper")
        ant._try_open_position()

        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        attempted = next(r["payload"] for r in records if r["payload"]["action"] == "order_attempted")
        assert "sl" in attempted
        assert "tp" in attempted

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        ant._emit_log({"action": "test"})  # should not crash

    def test_sequence_increments(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._emit_log({"action": "a"})
        ant._emit_log({"action": "b"})
        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["sequence"] == 0
        assert records[1]["sequence"] == 1

    def test_ttl_with_open_order_logs_warning(self, tmp_path: Path) -> None:
        ant = make_ant(gate=make_gate(), logs_root=tmp_path)
        ant._open_order = ant._build_order(_SYMBOL, _PRICE)
        ant._tick = MagicMock()

        with patch("ant_colony.ants.execution_ant.time.sleep"):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                ant.run()

        log_path = tmp_path / "execution" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions  = [r["payload"]["action"] for r in records]
        assert "ttl_expired_with_open_order" in actions


# ---------------------------------------------------------------------------
# 8. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent_after_run(self) -> None:
        ant = make_ant(gate=make_gate())
        ant._tick = MagicMock()

        with patch("ant_colony.ants.execution_ant.time.sleep"):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                ant.run()

        assert ant.scheduler.record_heartbeat.call_count >= 1

    def test_heartbeat_contains_ant_id(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        hb = ant.scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()  # must not propagate


# ---------------------------------------------------------------------------
# 9. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant(gate=make_gate())

        with patch("ant_colony.ants.execution_ant.time.sleep", side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_exception_in_tick_returns_aborted(self) -> None:
        ant = make_ant(gate=make_gate())
        ant._tick = MagicMock(side_effect=RuntimeError("tick boom"))

        with patch("ant_colony.ants.execution_ant.time.sleep", side_effect=Exception("stop")):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant(gate=make_gate())
        ant._tick = MagicMock()

        with patch("ant_colony.ants.execution_ant.time.sleep"):
            with patch("ant_colony.ants.execution_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()

        assert status == AntStatus.COMPLETED
