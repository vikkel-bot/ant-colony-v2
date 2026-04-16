"""
tests/test_allocator.py

Fase 6 — Queen allocator upgrade.

Test-opzet:
  - TestAllocationPlan        validaties, properties, capital_for
  - TestAllocationResult      accepted/rejected constructie
  - TestAllocationSnapshot    colony-totalen, biome-lookup, utilization
  - TestQueenApplyPlan        apply_allocation_plan: happy path, atomiciteit,
                              partiële updates, hertoepassing
  - TestQueenSnapshot         allocation_snapshot: leeg, na missions, na revoke,
                              vers berekend (geen caching)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.queen import (
    AllocationPlan,
    AllocationResult,
    AllocationSnapshot,
    BiomeAllocationState,
    Queen,
)
from ant_colony.schemas.mission import (
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def _make_queen(scheduler: ColonyScheduler, capital: float = 100_000.0) -> Queen:
    return Queen(capital_total=capital, scheduler=scheduler)


def _make_mission(
    mission_id: str = "m-001",
    capital: float = 10_000.0,
    biome: str = "crypto",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type="research_ant",
        allowed_node="node-1",
        allowed_actions=["read"],
        market_scope=MarketScope(biome=biome, symbols=["BTC-EUR"]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=500.0,
            daily_loss_limit=200.0,
        ),
        ttl=86_400,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="test"),
    )


# ---------------------------------------------------------------------------
# TestAllocationPlan
# ---------------------------------------------------------------------------

class TestAllocationPlan:
    def test_valid_plan_accepted(self):
        plan = AllocationPlan(allocations={"crypto": 0.50, "equities": 0.30})
        assert plan.allocations["crypto"] == 0.50

    def test_single_biome_plan(self):
        plan = AllocationPlan(allocations={"crypto": 0.80})
        assert plan.total_fraction == pytest.approx(0.80)

    def test_fractions_sum_exactly_one(self):
        plan = AllocationPlan(allocations={"crypto": 0.60, "equities": 0.40})
        assert plan.total_fraction == pytest.approx(1.0)

    def test_fractions_sum_less_than_one(self):
        plan = AllocationPlan(allocations={"crypto": 0.50, "equities": 0.30})
        assert plan.total_fraction == pytest.approx(0.80)

    def test_unallocated_fraction_correct(self):
        plan = AllocationPlan(allocations={"crypto": 0.50, "equities": 0.30})
        assert plan.unallocated_fraction == pytest.approx(0.20)

    def test_unallocated_fraction_zero_when_fully_allocated(self):
        plan = AllocationPlan(allocations={"crypto": 0.60, "equities": 0.40})
        assert plan.unallocated_fraction == pytest.approx(0.0)

    def test_capital_for_known_biome(self):
        plan = AllocationPlan(allocations={"crypto": 0.50})
        assert plan.capital_for("crypto", 100_000.0) == pytest.approx(50_000.0)

    def test_capital_for_unknown_biome_returns_none(self):
        plan = AllocationPlan(allocations={"crypto": 0.50})
        assert plan.capital_for("equities", 100_000.0) is None

    def test_capital_for_scales_with_colony_total(self):
        plan = AllocationPlan(allocations={"crypto": 0.25})
        assert plan.capital_for("crypto", 200_000.0) == pytest.approx(50_000.0)
        assert plan.capital_for("crypto", 40_000.0) == pytest.approx(10_000.0)

    def test_empty_allocations_raises(self):
        with pytest.raises(ValueError):
            AllocationPlan(allocations={})

    def test_fraction_zero_raises(self):
        with pytest.raises(ValueError, match="> 0"):
            AllocationPlan(allocations={"crypto": 0.0})

    def test_fraction_negative_raises(self):
        with pytest.raises(ValueError, match="> 0"):
            AllocationPlan(allocations={"crypto": -0.1})

    def test_fraction_above_one_raises(self):
        with pytest.raises(ValueError, match="≤ 1.0"):
            AllocationPlan(allocations={"crypto": 1.1})

    def test_fraction_exactly_one_accepted(self):
        plan = AllocationPlan(allocations={"crypto": 1.0})
        assert plan.total_fraction == pytest.approx(1.0)

    def test_sum_above_one_raises(self):
        with pytest.raises(ValueError, match="≤ 1.0"):
            AllocationPlan(allocations={"crypto": 0.70, "equities": 0.50})

    def test_three_biomes_valid(self):
        plan = AllocationPlan(allocations={
            "crypto": 0.50,
            "equities": 0.30,
            "commodities": 0.10,
        })
        assert plan.unallocated_fraction == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# TestAllocationResult
# ---------------------------------------------------------------------------

class TestAllocationResult:
    def test_accepted_result(self):
        result = AllocationResult(
            applied=True,
            biome_limits={"crypto": 50_000.0, "equities": 30_000.0},
        )
        assert result.applied
        assert result.biome_limits["crypto"] == 50_000.0

    def test_rejected_classmethod(self):
        result = AllocationResult.rejected("sum too high")
        assert not result.applied
        assert result.rejection_reason == "sum too high"
        assert result.biome_limits == {}

    def test_accepted_empty_rejection_reason(self):
        result = AllocationResult(applied=True, biome_limits={"crypto": 50_000.0})
        assert result.rejection_reason == ""


# ---------------------------------------------------------------------------
# TestAllocationSnapshot
# ---------------------------------------------------------------------------

class TestAllocationSnapshot:
    def _make_snap(
        self,
        colony_total: float = 100_000.0,
        colony_allocated: float = 0.0,
        biomes: list[BiomeAllocationState] | None = None,
    ) -> AllocationSnapshot:
        available = max(0.0, colony_total - colony_allocated)
        return AllocationSnapshot(
            colony_total=colony_total,
            colony_allocated=colony_allocated,
            colony_available=available,
            biomes=biomes or [],
        )

    def test_colony_utilization_pct(self):
        snap = self._make_snap(colony_total=100_000.0, colony_allocated=30_000.0)
        assert snap.colony_utilization_pct == pytest.approx(30.0)

    def test_colony_utilization_pct_zero(self):
        snap = self._make_snap(colony_allocated=0.0)
        assert snap.colony_utilization_pct == pytest.approx(0.0)

    def test_colony_utilization_pct_none_when_total_zero(self):
        snap = AllocationSnapshot(
            colony_total=0.0, colony_allocated=0.0, colony_available=0.0
        )
        assert snap.colony_utilization_pct is None

    def test_biome_lookup_found(self):
        state = BiomeAllocationState(
            biome_id="crypto", limit=50_000.0, allocated=10_000.0,
            available=40_000.0, utilization_pct=20.0,
        )
        snap = self._make_snap(biomes=[state])
        assert snap.biome("crypto") is state

    def test_biome_lookup_not_found_returns_none(self):
        snap = self._make_snap()
        assert snap.biome("unknown") is None

    def test_empty_biomes_list(self):
        snap = self._make_snap()
        assert snap.biomes == []

    def test_biome_allocation_state_fields(self):
        state = BiomeAllocationState(
            biome_id="equities", limit=30_000.0, allocated=15_000.0,
            available=15_000.0, utilization_pct=50.0,
        )
        assert state.biome_id == "equities"
        assert state.limit == 30_000.0
        assert state.utilization_pct == 50.0

    def test_biome_allocation_state_unconstrained(self):
        state = BiomeAllocationState(
            biome_id="crypto", limit=None, allocated=0.0,
            available=None, utilization_pct=None,
        )
        assert state.limit is None
        assert state.utilization_pct is None


# ---------------------------------------------------------------------------
# TestQueenApplyPlan
# ---------------------------------------------------------------------------

class TestQueenApplyPlan:
    def test_apply_plan_returns_accepted(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.50})
        result = queen.apply_allocation_plan(plan)
        assert result.applied

    def test_apply_plan_sets_biome_limits(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.50, "equities": 0.30})
        queen.apply_allocation_plan(plan)
        assert queen.biome_capital_limit("crypto") == pytest.approx(50_000.0)
        assert queen.biome_capital_limit("equities") == pytest.approx(30_000.0)

    def test_apply_plan_result_contains_computed_limits(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.50, "equities": 0.30})
        result = queen.apply_allocation_plan(plan)
        assert result.biome_limits["crypto"] == pytest.approx(50_000.0)
        assert result.biome_limits["equities"] == pytest.approx(30_000.0)

    def test_apply_plan_scales_with_capital_total(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=40_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.25})
        queen.apply_allocation_plan(plan)
        assert queen.biome_capital_limit("crypto") == pytest.approx(10_000.0)

    def test_apply_plan_full_allocation(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.60, "equities": 0.40})
        result = queen.apply_allocation_plan(plan)
        assert result.applied
        assert queen.biome_capital_limit("crypto") == pytest.approx(60_000.0)
        assert queen.biome_capital_limit("equities") == pytest.approx(40_000.0)

    def test_apply_plan_does_not_touch_unlisted_biomes(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.set_biome_capital("commodities", 10_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.50})
        queen.apply_allocation_plan(plan)
        # commodities was niet in het plan — limiet ongewijzigd
        assert queen.biome_capital_limit("commodities") == pytest.approx(10_000.0)

    def test_apply_plan_overwrites_existing_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        plan = AllocationPlan(allocations={"crypto": 0.70})
        queen.apply_allocation_plan(plan)
        assert queen.biome_capital_limit("crypto") == pytest.approx(70_000.0)

    def test_apply_plan_twice_second_wins(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.30}))
        assert queen.biome_capital_limit("crypto") == pytest.approx(30_000.0)

    def test_apply_plan_issue_mission_respects_new_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.10}))
        # 15_000 > 10_000 limiet → afgewezen
        result = queen.issue_mission(_make_mission("m1", capital=15_000.0, biome="crypto"))
        assert not result.accepted

    def test_apply_plan_zero_capital_total_sets_zero_limits(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=0.0)
        plan = AllocationPlan(allocations={"crypto": 0.50})
        result = queen.apply_allocation_plan(plan)
        assert result.applied
        assert queen.biome_capital_limit("crypto") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestQueenSnapshot
# ---------------------------------------------------------------------------

class TestQueenSnapshot:
    def test_snapshot_before_any_setup(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        snap = queen.allocation_snapshot()
        assert snap.colony_total == 100_000.0
        assert snap.colony_allocated == 0.0
        assert snap.colony_available == 100_000.0
        assert snap.biomes == []

    def test_snapshot_contains_biomes_after_plan(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={
            "crypto": 0.50, "equities": 0.30,
        }))
        snap = queen.allocation_snapshot()
        assert len(snap.biomes) == 2

    def test_snapshot_biomes_sorted_by_biome_id(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={
            "equities": 0.30, "crypto": 0.50, "commodities": 0.10,
        }))
        snap = queen.allocation_snapshot()
        ids = [b.biome_id for b in snap.biomes]
        assert ids == sorted(ids)

    def test_snapshot_biome_limit_correct(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").limit == pytest.approx(50_000.0)

    def test_snapshot_biome_allocated_zero_before_missions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").allocated == 0.0

    def test_snapshot_biome_allocated_after_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.issue_mission(_make_mission("m1", capital=20_000.0, biome="crypto"))
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").allocated == pytest.approx(20_000.0)

    def test_snapshot_biome_available_after_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.issue_mission(_make_mission("m1", capital=20_000.0, biome="crypto"))
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").available == pytest.approx(30_000.0)

    def test_snapshot_biome_utilization_pct_after_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.issue_mission(_make_mission("m1", capital=25_000.0, biome="crypto"))
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").utilization_pct == pytest.approx(50.0)

    def test_snapshot_colony_allocated_after_missions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={
            "crypto": 0.50, "equities": 0.30,
        }))
        queen.issue_mission(_make_mission("m1", capital=20_000.0, biome="crypto"))
        queen.issue_mission(_make_mission("m2", capital=10_000.0, biome="equities"))
        snap = queen.allocation_snapshot()
        assert snap.colony_allocated == pytest.approx(30_000.0)
        assert snap.colony_available == pytest.approx(70_000.0)

    def test_snapshot_fresh_after_revoke(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.issue_mission(_make_mission("m1", capital=20_000.0, biome="crypto"))
        queen.revoke_mission("m1")
        snap = queen.allocation_snapshot()
        assert snap.biome("crypto").allocated == 0.0
        assert snap.biome("crypto").utilization_pct == pytest.approx(0.0)

    def test_snapshot_is_freshly_computed(self, tmp_path):
        # Twee opeenvolgende snapshots geven verschillende waarden na een mutatie
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        snap1 = queen.allocation_snapshot()
        queen.issue_mission(_make_mission("m1", capital=10_000.0, biome="crypto"))
        snap2 = queen.allocation_snapshot()
        assert snap1.biome("crypto").allocated == 0.0
        assert snap2.biome("crypto").allocated == pytest.approx(10_000.0)

    def test_snapshot_colony_utilization_pct(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100_000.0)
        queen.apply_allocation_plan(AllocationPlan(allocations={"crypto": 0.50}))
        queen.issue_mission(_make_mission("m1", capital=40_000.0, biome="crypto"))
        snap = queen.allocation_snapshot()
        assert snap.colony_utilization_pct == pytest.approx(40.0)
