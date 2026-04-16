"""
tests/test_multi_node_sim.py

Fase 7 — Paper-mode multi-node kolonie simulatie.

Test-opzet:
  - TestSimulationReport    NodeReport properties, SimulationReport aggregaten
  - TestMultiNodeSimSetup   add_node validatie, node_count, Queen-koppeling
  - TestMultiNodeSimRun     tick-verwerking, stale prijzen, signalen,
                            multi-node isolatie, lege run
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ant_colony.colony.multi_node_sim import (
    MultiNodeSimulator,
    NodeReport,
    SimulationReport,
)
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.entry.entry_signal import EntrySignal, SignalSource
from ant_colony.queen import MissionRejectionReason, Queen
from ant_colony.schemas.mission import (
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_node(
    node_id: str,
    biomes: list[str] | None = None,
    ant_types: list[str] | None = None,
    status: NodeStatus = NodeStatus.ACTIVE,
) -> Node:
    return Node(
        node_id=node_id,
        hostname=f"host-{node_id}",
        allowed_biomes=biomes or ["crypto"],
        allowed_ant_types=ant_types or ["paper_ant"],
        heartbeat_interval=60,
        status=status,
        runtime_paths=RuntimePaths(output="/out", live="/live", logs="/logs"),
    )


def _make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def _make_queen(
    scheduler: ColonyScheduler,
    capital: float = 100_000.0,
    nodes: list[Node] | None = None,
) -> Queen:
    queen = Queen(capital_total=capital, scheduler=scheduler)
    for node in (nodes or []):
        queen.register_node(node)
    return queen


def _make_mission(
    mission_id: str,
    node: str,
    capital: float = 10_000.0,
    biome: str = "crypto",
    ant_type: str = "paper_ant",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type=ant_type,
        allowed_node=node,
        allowed_actions=["paper_trade"],
        capital_limit=capital,
        market_scope=MarketScope(biome=biome, symbols=["BTC-EUR"]),
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=5_000.0,
            daily_loss_limit=500.0,
        ),
        ttl=86_400,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="test"),
    )


def _make_signal(
    mission_id: str,
    entry: float = 30_000.0,
    sl: float = 29_000.0,
    tp: float = 32_000.0,
) -> EntrySignal:
    return EntrySignal(
        symbol="BTC-EUR",
        biome="crypto",
        mission_id=mission_id,
        ant_id="ant-test",
        side="long",
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        valid_until=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        source=SignalSource.MANUAL,
    )


def _make_sim(
    tmp_path: Path,
    nodes: list[Node],
    capital: float = 100_000.0,
) -> tuple[Queen, MultiNodeSimulator]:
    sched = _make_scheduler(tmp_path)
    queen = _make_queen(sched, capital=capital, nodes=nodes)
    sim = MultiNodeSimulator(queen=queen)
    return queen, sim


# ---------------------------------------------------------------------------
# TestSimulationReport
# ---------------------------------------------------------------------------

class TestSimulationReport:
    def _node_report(
        self,
        node_id: str = "node-a",
        mission_id: str = "m-a",
        ticks: int = 5,
        trades: int = 1,
        pnl: float = 100.0,
        capital_start: float = 10_000.0,
        capital_end: float = 10_100.0,
    ) -> NodeReport:
        return NodeReport(
            node_id=node_id,
            mission_id=mission_id,
            ticks_run=ticks,
            trades_closed=trades,
            total_pnl=pnl,
            capital_start=capital_start,
            capital_end=capital_end,
        )

    def test_node_report_return_pct(self):
        r = self._node_report(pnl=500.0, capital_start=10_000.0)
        assert r.return_pct == pytest.approx(5.0)

    def test_node_report_return_pct_negative(self):
        r = self._node_report(pnl=-200.0, capital_start=10_000.0)
        assert r.return_pct == pytest.approx(-2.0)

    def test_node_report_return_pct_none_when_zero_capital(self):
        r = self._node_report(pnl=0.0, capital_start=0.0)
        assert r.return_pct is None

    def test_simulation_report_total_pnl(self):
        snap = SimulationReport(
            nodes=[
                self._node_report("a", pnl=200.0),
                self._node_report("b", pnl=150.0),
            ],
            total_ticks=10,
            stale_ticks=0,
        )
        assert snap.total_pnl == pytest.approx(350.0)

    def test_simulation_report_total_trades(self):
        snap = SimulationReport(
            nodes=[
                self._node_report("a", trades=3),
                self._node_report("b", trades=2),
            ],
            total_ticks=10,
            stale_ticks=0,
        )
        assert snap.total_trades == 5

    def test_simulation_report_active_nodes(self):
        snap = SimulationReport(
            nodes=[self._node_report("a"), self._node_report("b")],
            total_ticks=10,
            stale_ticks=0,
        )
        assert snap.active_nodes == 2

    def test_simulation_report_node_lookup_found(self):
        nr = self._node_report("node-a")
        snap = SimulationReport(nodes=[nr], total_ticks=5, stale_ticks=0)
        assert snap.node("node-a") is nr

    def test_simulation_report_node_lookup_not_found(self):
        snap = SimulationReport(nodes=[], total_ticks=0, stale_ticks=0)
        assert snap.node("unknown") is None

    def test_simulation_report_empty_nodes(self):
        snap = SimulationReport(nodes=[], total_ticks=0, stale_ticks=0)
        assert snap.total_pnl == 0.0
        assert snap.total_trades == 0
        assert snap.active_nodes == 0


# ---------------------------------------------------------------------------
# TestMultiNodeSimSetup
# ---------------------------------------------------------------------------

class TestMultiNodeSimSetup:
    def test_node_count_zero_at_start(self, tmp_path):
        _, sim = _make_sim(tmp_path, nodes=[])
        assert sim.node_count == 0

    def test_add_node_increases_count(self, tmp_path):
        _, sim = _make_sim(tmp_path, nodes=[_make_node("node-a")])
        sim.add_node("node-a", _make_mission("m-a", "node-a"))
        assert sim.node_count == 1

    def test_add_two_nodes(self, tmp_path):
        _, sim = _make_sim(tmp_path, nodes=[_make_node("node-a"), _make_node("node-b")])
        sim.add_node("node-a", _make_mission("m-a", "node-a"))
        sim.add_node("node-b", _make_mission("m-b", "node-b"))
        assert sim.node_count == 2

    def test_add_node_unregistered_raises(self, tmp_path):
        _, sim = _make_sim(tmp_path, nodes=[])
        with pytest.raises(ValueError, match="Queen rejected"):
            sim.add_node("unknown", _make_mission("m-x", "unknown"))

    def test_add_node_stale_node_raises(self, tmp_path):
        _, sim = _make_sim(
            tmp_path,
            nodes=[_make_node("node-a", status=NodeStatus.STALE)],
        )
        with pytest.raises(ValueError, match="Queen rejected"):
            sim.add_node("node-a", _make_mission("m-a", "node-a"))

    def test_add_node_wrong_ant_type_raises(self, tmp_path):
        _, sim = _make_sim(
            tmp_path,
            nodes=[_make_node("node-a", ant_types=["research_ant"])],
        )
        with pytest.raises(ValueError, match="Queen rejected"):
            sim.add_node("node-a", _make_mission("m-a", "node-a", ant_type="paper_ant"))

    def test_add_node_wrong_biome_raises(self, tmp_path):
        _, sim = _make_sim(
            tmp_path,
            nodes=[_make_node("node-a", biomes=["equities"])],
        )
        with pytest.raises(ValueError, match="Queen rejected"):
            sim.add_node("node-a", _make_mission("m-a", "node-a", biome="crypto"))

    def test_add_node_capital_exceeded_raises(self, tmp_path):
        _, sim = _make_sim(
            tmp_path,
            nodes=[_make_node("node-a")],
            capital=500.0,
        )
        with pytest.raises(ValueError, match="Queen rejected"):
            sim.add_node("node-a", _make_mission("m-a", "node-a", capital=10_000.0))


# ---------------------------------------------------------------------------
# TestMultiNodeSimRun
# ---------------------------------------------------------------------------

class TestMultiNodeSimRun:
    def _sim_with_nodes(
        self,
        tmp_path: Path,
        node_ids: list[str],
        capital: float = 100_000.0,
    ) -> tuple[MultiNodeSimulator, dict[str, str]]:
        """Helper: bouw een sim met n nodes, geef sim + mission_ids terug."""
        nodes = [_make_node(nid) for nid in node_ids]
        _, sim = _make_sim(tmp_path, nodes=nodes, capital=capital)
        mission_ids: dict[str, str] = {}
        for i, nid in enumerate(node_ids):
            mid = f"m-{i}"
            sim.add_node(nid, _make_mission(mid, nid))
            mission_ids[nid] = mid
        return sim, mission_ids

    def test_empty_prices_returns_zero_ticks(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([])
        assert report.total_ticks == 0
        assert report.stale_ticks == 0

    def test_total_ticks_matches_price_list(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0] * 5)
        assert report.total_ticks == 5

    def test_stale_price_counted(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0, -1.0, 0.0, 30_000.0])
        assert report.stale_ticks == 2

    def test_stale_price_not_counted_as_node_tick(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0, -1.0, 30_000.0])
        # 2 valid ticks, 1 stale
        assert report.node("node-a").ticks_run == 2

    def test_no_signal_no_trade(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0] * 5)
        assert report.total_trades == 0
        assert report.node("node-a").trades_closed == 0

    def test_signal_opens_and_closes_position(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a"])
        signal = _make_signal(mission_ids["node-a"], entry=30_000.0, sl=29_000.0, tp=32_000.0)
        # tick 0: signal → open; ticks 1-3: prijs stijgt naar tp
        prices = [30_000.0, 30_500.0, 31_000.0, 32_000.0, 32_001.0]
        signals = {"node-a": [signal, None, None, None, None]}
        report = sim.run(prices, signals)
        assert report.total_trades == 1
        assert report.node("node-a").trades_closed == 1

    def test_trade_pnl_positive_on_take_profit(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a"])
        signal = _make_signal(mission_ids["node-a"], entry=30_000.0, sl=29_000.0, tp=31_000.0)
        prices = [30_000.0, 30_500.0, 31_001.0]
        signals = {"node-a": [signal, None, None]}
        report = sim.run(prices, signals)
        assert report.node("node-a").total_pnl > 0

    def test_two_nodes_independent_pnl(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a", "node-b"])
        sig_a = _make_signal(mission_ids["node-a"], entry=30_000.0, sl=29_000.0, tp=31_000.0)
        sig_b = _make_signal(mission_ids["node-b"], entry=30_000.0, sl=29_000.0, tp=31_000.0)
        prices = [30_000.0, 30_500.0, 31_001.0]
        signals = {
            "node-a": [sig_a, None, None],
            "node-b": [sig_b, None, None],
        }
        report = sim.run(prices, signals)
        nr_a = report.node("node-a")
        nr_b = report.node("node-b")
        assert nr_a.trades_closed == 1
        assert nr_b.trades_closed == 1
        assert nr_a.total_pnl == pytest.approx(nr_b.total_pnl)

    def test_two_nodes_ticks_run_equal(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a", "node-b"])
        prices = [30_000.0] * 6
        report = sim.run(prices)
        assert report.node("node-a").ticks_run == 6
        assert report.node("node-b").ticks_run == 6

    def test_node_report_sorted_by_node_id(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-c", "node-a", "node-b"])
        report = sim.run([30_000.0])
        ids = [r.node_id for r in report.nodes]
        assert ids == sorted(ids)

    def test_all_stale_prices_no_trades(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a"])
        signal = _make_signal(mission_ids["node-a"])
        report = sim.run([-1.0, 0.0, -5.0], signals={"node-a": [signal, signal, signal]})
        assert report.total_trades == 0
        assert report.stale_ticks == 3
        assert report.node("node-a").ticks_run == 0

    def test_no_signals_dict_accepted(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0, 31_000.0], signals=None)
        assert report.total_ticks == 2
        assert report.total_trades == 0

    def test_partial_signals_list_fills_none(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a"])
        # signals list shorter than prices — missing positions treated as None
        signal = _make_signal(mission_ids["node-a"], entry=30_000.0, sl=29_000.0, tp=32_000.0)
        prices = [30_000.0, 30_500.0, 31_000.0]
        signals = {"node-a": [signal]}  # only one signal for 3 ticks
        report = sim.run(prices, signals)
        assert report.total_ticks == 3

    def test_capital_start_equals_mission_capital_limit(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        report = sim.run([30_000.0])
        assert report.node("node-a").capital_start == pytest.approx(10_000.0)

    def test_no_nodes_run_returns_empty_report(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        sim = MultiNodeSimulator(queen=queen)
        report = sim.run([30_000.0, 31_000.0])
        assert report.total_ticks == 2
        assert report.active_nodes == 0
        assert report.total_trades == 0
        assert report.total_pnl == 0.0

    def test_tick_results_stored_per_node(self, tmp_path):
        sim, _ = self._sim_with_nodes(tmp_path, ["node-a"])
        prices = [30_000.0, 31_000.0, 32_000.0]
        report = sim.run(prices)
        assert len(report.node("node-a").tick_results) == 3

    def test_stop_loss_closes_position(self, tmp_path):
        sim, mission_ids = self._sim_with_nodes(tmp_path, ["node-a"])
        # entry at 30k, sl at 29k — price drops below sl
        signal = _make_signal(mission_ids["node-a"], entry=30_000.0, sl=29_000.0, tp=35_000.0)
        prices = [30_000.0, 30_100.0, 28_900.0]
        signals = {"node-a": [signal, None, None]}
        report = sim.run(prices, signals)
        assert report.node("node-a").trades_closed == 1
        assert report.node("node-a").total_pnl < 0
