"""
tests/test_execution_gate.py

Tests voor LiveExecutionGate en PaperExecutionGate.

Dekt:
  - LiveExecutionGate: alle vijf pre-order checks
  - LiveExecutionGate: doorgeven van adapter-resultaat (accepted / None)
  - LiveExecutionGate: audit log schrijven
  - PaperExecutionGate: alle vier pre-order checks
  - PaperExecutionGate: MARKET fill op market_data.close
  - PaperExecutionGate: LIMIT fill op order.limit_price
  - PaperExecutionGate: nooit None teruggeven
  - Beide gates: kolonie HALTED blokkeert eerst
  - Beide gates: mission niet actief blokkeert
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.biome.biome_adapter import MarketData
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, ColonyStatus
from ant_colony.execution.live_gate import LiveExecutionGate
from ant_colony.execution.paper_gate import PaperExecutionGate
from ant_colony.queen.queen import Queen
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions
from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths
from ant_colony.schemas.order import (
    LiveOrder,
    OrderRejectionReason,
    OrderResult,
    OrderSide,
    OrderType,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_scheduler(status: ColonyStatus = ColonyStatus.RUNNING) -> ColonyScheduler:
    scheduler = MagicMock(spec=ColonyScheduler)
    scheduler.status = status
    return scheduler


def _make_node(node_id: str = "node-1") -> Node:
    return Node(
        node_id=node_id,
        hostname="test-host",
        status=NodeStatus.ACTIVE,
        allowed_biomes=["crypto"],
        allowed_ant_types=["execution_ant"],
        heartbeat_interval=30,
        runtime_paths=RuntimePaths(
            output="C:/tmp/out",
            live="C:/tmp/live",
            logs="C:/tmp/logs",
        ),
    )


def _make_queen(capital: float = 50_000.0) -> Queen:
    scheduler = _make_scheduler()
    queen = Queen(capital_total=capital, scheduler=scheduler)
    queen.register_node(_make_node())
    return queen


def _make_mission(
    mission_id: str = "m-1",
    capital_limit: float = 10_000.0,
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type="execution_ant",
        allowed_node="node-1",
        allowed_actions=["place_order"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
        capital_limit=capital_limit,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
        ),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="test"),
    )


def _issue_mission(queen: Queen, mission: Mission) -> None:
    result = queen.issue_mission(mission)
    assert result.accepted, f"Mission rejected: {result.rejection_reason}"


def _fresh_market_data(close: float = 30_000.0) -> MarketData:
    return MarketData(
        symbol="BTC-EUR",
        timeframe="1h",
        timestamp=datetime.now(tz=timezone.utc),
        open=close,
        high=close + 100,
        low=close - 100,
        close=close,
        volume=100.0,
        biome_id="crypto",
    )


def _stale_market_data() -> MarketData:
    return MarketData(
        symbol="BTC-EUR",
        timeframe="1h",
        timestamp=datetime.now(tz=timezone.utc) - timedelta(seconds=400),
        open=30_000.0,
        high=30_100.0,
        low=29_900.0,
        close=30_000.0,
        volume=100.0,
        biome_id="crypto",
    )


def _buy_order(
    mission_id: str = "m-1",
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
) -> LiveOrder:
    return LiveOrder(
        mission_id=mission_id,
        ant_id="a-1",
        symbol="BTC-EUR",
        biome="crypto",
        side=OrderSide.BUY,
        order_type=order_type,
        quantity=0.1,
        limit_price=limit_price,
        stop_loss_price=28_000.0,
        take_profit_price=35_000.0,
    )


def _make_adapter(available: bool = True, result: OrderResult | None = None):
    adapter = MagicMock()
    adapter.biome_id = "crypto"
    adapter.is_available.return_value = available
    adapter.place_order.return_value = result
    return adapter


# ---------------------------------------------------------------------------
# TestLiveGateColonyHalted
# ---------------------------------------------------------------------------

class TestLiveGateColonyHalted:

    def test_halted_colony_rejects_order(self):
        queen = _make_queen()
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        order = _buy_order()
        result = gate.execute(order, market_data=_fresh_market_data())
        assert result is not None
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.COLONY_HALTED

    def test_halted_colony_does_not_call_adapter(self):
        queen = _make_queen()
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        gate.execute(_buy_order(), market_data=_fresh_market_data())
        adapter.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# TestLiveGateMissionNotActive
# ---------------------------------------------------------------------------

class TestLiveGateMissionNotActive:

    def test_unknown_mission_rejected(self):
        queen = _make_queen()
        scheduler = _make_scheduler()
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        order = _buy_order(mission_id="unknown-mission")
        result = gate.execute(order, market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MISSION_NOT_ACTIVE

    def test_active_mission_passes_check(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter(result=OrderResult.accepted_result(
            order_id="o-1", exchange_order_id="exch-1",
            filled_quantity=0.1, avg_price=30_000.0,
        ))
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result is not None
        assert result.accepted is True


# ---------------------------------------------------------------------------
# TestLiveGateCapitalCheck
# ---------------------------------------------------------------------------

class TestLiveGateCapitalCheck:

    def test_zero_capital_limit_rejected(self):
        queen = _make_queen()
        mission = _make_mission(capital_limit=0.0)
        # Override by directly inserting (bypass Queen issue_mission validation)
        queen._active_missions["m-zero"] = mission.model_copy(
            update={"mission_id": "m-zero", "capital_limit": 0.0}
        )
        scheduler = _make_scheduler()
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        order = _buy_order(mission_id="m-zero")
        result = gate.execute(order, market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.CAPITAL_LIMIT_BREACHED


# ---------------------------------------------------------------------------
# TestLiveGateMarketDataStale
# ---------------------------------------------------------------------------

class TestLiveGateMarketDataStale:

    def test_none_market_data_rejected(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=None)
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MARKET_DATA_STALE

    def test_stale_market_data_rejected(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_stale_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MARKET_DATA_STALE

    def test_fresh_market_data_passes(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter(result=OrderResult.accepted_result(
            order_id="o-1", exchange_order_id="exch-1",
            filled_quantity=0.1, avg_price=30_000.0,
        ))
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.accepted is True


# ---------------------------------------------------------------------------
# TestLiveGateAdapterAvailability
# ---------------------------------------------------------------------------

class TestLiveGateAdapterAvailability:

    def test_unavailable_adapter_rejected(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter(available=False)
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.ADAPTER_UNAVAILABLE

    def test_available_adapter_delegates_order(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        accepted = OrderResult.accepted_result(
            order_id="o-1", exchange_order_id="exch-7",
            filled_quantity=0.1, avg_price=30_000.0,
        )
        adapter = _make_adapter(available=True, result=accepted)
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        adapter.place_order.assert_called_once()
        assert result.accepted is True
        assert result.exchange_order_id == "exch-7"

    def test_adapter_returns_none_propagated(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter(available=True, result=None)
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result is None

    def test_adapter_returns_rejected_propagated(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        exchange_rejected = OrderResult.rejected(
            order_id="o-1",
            reason=OrderRejectionReason.EXCHANGE_REJECTED,
            detail="insufficient funds at exchange",
        )
        adapter = _make_adapter(available=True, result=exchange_rejected)
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.EXCHANGE_REJECTED


# ---------------------------------------------------------------------------
# TestLiveGateCheckOrder
# ---------------------------------------------------------------------------

class TestLiveGateCheckOrder:

    def test_halted_before_mission_check(self):
        """Check 1 (HALTED) gaat vóór check 2 (mission actief)."""
        queen = _make_queen()
        # mission NOT issued — would fail check 2 if we reached it
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.rejection_reason == OrderRejectionReason.COLONY_HALTED

    def test_mission_check_before_market_data_check(self):
        """Check 2 (mission) gaat vóór check 4 (market data)."""
        queen = _make_queen()
        # mission NOT issued
        scheduler = _make_scheduler()
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=None)
        assert result.rejection_reason == OrderRejectionReason.MISSION_NOT_ACTIVE

    def test_market_data_before_adapter_check(self):
        """Check 4 (market data) gaat vóór check 5 (adapter)."""
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        adapter = _make_adapter(available=False)
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(_buy_order(), market_data=None)
        assert result.rejection_reason == OrderRejectionReason.MARKET_DATA_STALE


# ---------------------------------------------------------------------------
# TestLiveGateAuditLog
# ---------------------------------------------------------------------------

class TestLiveGateAuditLog:

    def test_rejection_written_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_root = Path(tmp)
            queen = _make_queen()
            scheduler = _make_scheduler(ColonyStatus.HALTED)
            adapter = _make_adapter()
            gate = LiveExecutionGate(
                queen=queen, scheduler=scheduler, adapter=adapter,
                logs_root=logs_root,
            )
            order = _buy_order()
            gate.execute(order, market_data=_fresh_market_data())
            log_file = logs_root / "execution" / f"{order.order_id}.jsonl"
            assert log_file.exists()
            lines = log_file.read_text().strip().split("\n")
            assert len(lines) == 1
            import json
            record = json.loads(lines[0])
            assert record["payload"]["outcome"] == "rejected"

    def test_accepted_written_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_root = Path(tmp)
            queen = _make_queen()
            mission = _make_mission()
            _issue_mission(queen, mission)
            scheduler = _make_scheduler()
            accepted = OrderResult.accepted_result(
                order_id="o-1", exchange_order_id="exch-log",
                filled_quantity=0.1, avg_price=30_000.0,
            )
            adapter = _make_adapter(result=accepted)
            gate = LiveExecutionGate(
                queen=queen, scheduler=scheduler, adapter=adapter,
                logs_root=logs_root,
            )
            order = _buy_order()
            gate.execute(order, market_data=_fresh_market_data())
            log_file = logs_root / "execution" / f"{order.order_id}.jsonl"
            import json
            record = json.loads(log_file.read_text().strip())
            assert record["payload"]["outcome"] == "accepted"

    def test_no_log_without_logs_root(self):
        queen = _make_queen()
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        adapter = _make_adapter()
        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter, logs_root=None)
        # Should not raise
        gate.execute(_buy_order(), market_data=_fresh_market_data())


# ---------------------------------------------------------------------------
# TestPaperGateChecks
# ---------------------------------------------------------------------------

class TestPaperGateChecks:

    def test_halted_colony_rejects(self):
        queen = _make_queen()
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.COLONY_HALTED

    def test_unknown_mission_rejected(self):
        queen = _make_queen()
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(mission_id="x"), market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MISSION_NOT_ACTIVE

    def test_none_market_data_rejected(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=None)
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MARKET_DATA_STALE

    def test_stale_market_data_rejected(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=_stale_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.MARKET_DATA_STALE

    def test_zero_capital_limit_rejected(self):
        queen = _make_queen()
        mission = _make_mission(capital_limit=0.0)
        queen._active_missions["m-zero"] = mission.model_copy(
            update={"mission_id": "m-zero", "capital_limit": 0.0}
        )
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(mission_id="m-zero"), market_data=_fresh_market_data())
        assert result.accepted is False
        assert result.rejection_reason == OrderRejectionReason.CAPITAL_LIMIT_BREACHED


# ---------------------------------------------------------------------------
# TestPaperGateFill
# ---------------------------------------------------------------------------

class TestPaperGateFill:

    def test_market_order_fills_at_close(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        data = _fresh_market_data(close=31_500.0)
        result = gate.execute(_buy_order(order_type=OrderType.MARKET), market_data=data)
        assert result.accepted is True
        assert result.avg_price == 31_500.0

    def test_limit_order_fills_at_limit_price(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        data = _fresh_market_data(close=31_500.0)
        order = _buy_order(order_type=OrderType.LIMIT, limit_price=29_500.0)
        result = gate.execute(order, market_data=data)
        assert result.accepted is True
        assert result.avg_price == 29_500.0

    def test_fill_quantity_equals_order_quantity(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.filled_quantity == 0.1

    def test_exchange_order_id_has_paper_prefix(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result.exchange_order_id is not None
        assert result.exchange_order_id.startswith("paper-")

    def test_paper_gate_never_returns_none(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(_buy_order(), market_data=_fresh_market_data())
        assert result is not None

    def test_two_fills_have_different_exchange_ids(self):
        queen = _make_queen()
        mission = _make_mission()
        _issue_mission(queen, mission)
        scheduler = _make_scheduler()
        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        data = _fresh_market_data()
        r1 = gate.execute(_buy_order(), market_data=data)
        r2 = gate.execute(_buy_order(), market_data=data)
        assert r1.exchange_order_id != r2.exchange_order_id


# ---------------------------------------------------------------------------
# TestPaperGateAuditLog
# ---------------------------------------------------------------------------

class TestPaperGateAuditLog:

    def test_accepted_written_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_root = Path(tmp)
            queen = _make_queen()
            mission = _make_mission()
            _issue_mission(queen, mission)
            scheduler = _make_scheduler()
            gate = PaperExecutionGate(queen=queen, scheduler=scheduler, logs_root=logs_root)
            order = _buy_order()
            gate.execute(order, market_data=_fresh_market_data())
            log_file = logs_root / "paper_execution" / f"{order.order_id}.jsonl"
            assert log_file.exists()
            import json
            record = json.loads(log_file.read_text().strip())
            assert record["payload"]["outcome"] == "accepted_paper"

    def test_rejection_written_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_root = Path(tmp)
            queen = _make_queen()
            scheduler = _make_scheduler(ColonyStatus.HALTED)
            gate = PaperExecutionGate(queen=queen, scheduler=scheduler, logs_root=logs_root)
            order = _buy_order()
            gate.execute(order, market_data=_fresh_market_data())
            log_file = logs_root / "paper_execution" / f"{order.order_id}.jsonl"
            import json
            record = json.loads(log_file.read_text().strip())
            assert record["payload"]["outcome"] == "rejected"
