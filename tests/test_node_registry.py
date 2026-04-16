"""
tests/test_node_registry.py

Fase 7 — Paper-mode multi-node kolonie simulatie.

Test-opzet:
  - TestNodeRegistryBasic        register, unregister, get, count, list
  - TestNodeRegistryTrust        is_trusted, can_run_ant, can_run_biome
  - TestNodeRegistryStatus       set_status, record_heartbeat
  - TestQueenNodeGovernance      register_node, unregister_node, trusted_nodes,
                                 issue_mission node-checks
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ant_colony.colony.node_registry import NodeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
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
    node_id: str = "pc2-desktop",
    status: NodeStatus = NodeStatus.ACTIVE,
    biomes: list[str] | None = None,
    ant_types: list[str] | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        hostname=f"host-{node_id}",
        allowed_biomes=biomes or ["crypto"],
        allowed_ant_types=ant_types or ["paper_ant", "research_ant"],
        heartbeat_interval=60,
        status=status,
        runtime_paths=RuntimePaths(output="/out", live="/live", logs="/logs"),
    )


def _make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def _make_queen(scheduler: ColonyScheduler, capital: float = 50_000.0) -> Queen:
    return Queen(capital_total=capital, scheduler=scheduler)


def _make_mission(
    mission_id: str = "m-001",
    node: str = "pc2-desktop",
    ant_type: str = "paper_ant",
    biome: str = "crypto",
    capital: float = 5_000.0,
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
            max_position_size=500.0,
            daily_loss_limit=200.0,
        ),
        ttl=86_400,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="test"),
    )


# ---------------------------------------------------------------------------
# TestNodeRegistryBasic
# ---------------------------------------------------------------------------

class TestNodeRegistryBasic:
    def test_empty_registry_count_zero(self):
        registry = NodeRegistry()
        assert registry.count == 0

    def test_register_increases_count(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        assert registry.count == 1

    def test_get_returns_registered_node(self):
        registry = NodeRegistry()
        node = _make_node("pc2")
        registry.register(node)
        assert registry.get("pc2") is node

    def test_get_unknown_returns_none(self):
        registry = NodeRegistry()
        assert registry.get("unknown") is None

    def test_is_registered_true_after_register(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        assert registry.is_registered("pc2")

    def test_is_registered_false_before_register(self):
        registry = NodeRegistry()
        assert not registry.is_registered("pc2")

    def test_register_overwrites_existing(self):
        registry = NodeRegistry()
        node1 = _make_node("pc2", biomes=["crypto"])
        node2 = _make_node("pc2", biomes=["equities"])
        registry.register(node1)
        registry.register(node2)
        assert registry.get("pc2").allowed_biomes == ["equities"]
        assert registry.count == 1

    def test_unregister_removes_node(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        registry.unregister("pc2")
        assert registry.get("pc2") is None
        assert registry.count == 0

    def test_unregister_unknown_is_idempotent(self):
        registry = NodeRegistry()
        registry.unregister("nonexistent")  # should not raise
        assert registry.count == 0

    def test_list_nodes_returns_sorted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc3"))
        registry.register(_make_node("pc1"))
        registry.register(_make_node("pc2"))
        assert registry.list_nodes() == ["pc1", "pc2", "pc3"]

    def test_list_nodes_empty_when_no_nodes(self):
        registry = NodeRegistry()
        assert registry.list_nodes() == []

    def test_empty_node_id_raises(self):
        registry = NodeRegistry()
        with pytest.raises(ValueError, match="node_id"):
            registry.register(_make_node(""))

    def test_multiple_nodes_independent(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        registry.register(_make_node("pc3"))
        assert registry.count == 2
        assert registry.get("pc2") is not None
        assert registry.get("pc3") is not None


# ---------------------------------------------------------------------------
# TestNodeRegistryTrust
# ---------------------------------------------------------------------------

class TestNodeRegistryTrust:
    def test_active_node_is_trusted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.ACTIVE))
        assert registry.is_trusted("pc2")

    def test_stale_node_is_not_trusted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.STALE))
        assert not registry.is_trusted("pc2")

    def test_suspended_node_is_not_trusted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.SUSPENDED))
        assert not registry.is_trusted("pc2")

    def test_untrusted_node_is_not_trusted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.UNTRUSTED))
        assert not registry.is_trusted("pc2")

    def test_unknown_node_is_not_trusted(self):
        registry = NodeRegistry()
        assert not registry.is_trusted("unknown")

    def test_can_run_ant_allowed_type(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", ant_types=["paper_ant", "research_ant"]))
        assert registry.can_run_ant("pc2", "paper_ant")
        assert registry.can_run_ant("pc2", "research_ant")

    def test_can_run_ant_disallowed_type(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", ant_types=["paper_ant"]))
        assert not registry.can_run_ant("pc2", "execution_ant")

    def test_can_run_ant_unknown_node(self):
        registry = NodeRegistry()
        assert not registry.can_run_ant("unknown", "paper_ant")

    def test_can_run_ant_stale_node(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.STALE, ant_types=["paper_ant"]))
        assert not registry.can_run_ant("pc2", "paper_ant")

    def test_can_run_biome_allowed(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", biomes=["crypto", "equities"]))
        assert registry.can_run_biome("pc2", "crypto")
        assert registry.can_run_biome("pc2", "equities")

    def test_can_run_biome_disallowed(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", biomes=["crypto"]))
        assert not registry.can_run_biome("pc2", "equities")

    def test_can_run_biome_unknown_node(self):
        registry = NodeRegistry()
        assert not registry.can_run_biome("unknown", "crypto")

    def test_can_run_biome_stale_node(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.STALE, biomes=["crypto"]))
        assert not registry.can_run_biome("pc2", "crypto")

    def test_list_trusted_only_active(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.ACTIVE))
        registry.register(_make_node("pc3", status=NodeStatus.STALE))
        registry.register(_make_node("pc4", status=NodeStatus.SUSPENDED))
        assert registry.list_trusted() == ["pc2"]

    def test_list_trusted_sorted(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc3"))
        registry.register(_make_node("pc1"))
        assert registry.list_trusted() == ["pc1", "pc3"]


# ---------------------------------------------------------------------------
# TestNodeRegistryStatus
# ---------------------------------------------------------------------------

class TestNodeRegistryStatus:
    def test_set_status_stale(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.ACTIVE))
        registry.set_status("pc2", NodeStatus.STALE)
        assert registry.get("pc2").status == NodeStatus.STALE
        assert not registry.is_trusted("pc2")

    def test_set_status_active(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.STALE))
        registry.set_status("pc2", NodeStatus.ACTIVE)
        assert registry.is_trusted("pc2")

    def test_set_status_unknown_node_ignored(self):
        registry = NodeRegistry()
        registry.set_status("unknown", NodeStatus.STALE)  # should not raise

    def test_record_heartbeat_sets_active(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2", status=NodeStatus.STALE))
        registry.record_heartbeat("pc2")
        assert registry.is_trusted("pc2")

    def test_record_heartbeat_sets_timestamp(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        assert registry.get("pc2").last_heartbeat is None
        registry.record_heartbeat("pc2")
        assert registry.get("pc2").last_heartbeat is not None

    def test_record_heartbeat_unknown_node_ignored(self):
        registry = NodeRegistry()
        registry.record_heartbeat("unknown")  # should not raise

    def test_set_status_suspended_blocks_trust(self):
        registry = NodeRegistry()
        registry.register(_make_node("pc2"))
        registry.set_status("pc2", NodeStatus.SUSPENDED)
        assert not registry.is_trusted("pc2")
        assert not registry.can_run_ant("pc2", "paper_ant")


# ---------------------------------------------------------------------------
# TestQueenNodeGovernance
# ---------------------------------------------------------------------------

class TestQueenNodeGovernance:
    def test_no_registered_node_rejects_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        result = queen.issue_mission(_make_mission())
        assert not result.accepted
        assert result.rejection_reason == MissionRejectionReason.NODE_NOT_TRUSTED

    def test_registered_active_node_accepts_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop"))
        result = queen.issue_mission(_make_mission())
        assert result.accepted

    def test_stale_node_rejects_mission(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", status=NodeStatus.STALE))
        result = queen.issue_mission(_make_mission())
        assert result.rejection_reason == MissionRejectionReason.NODE_NOT_TRUSTED

    def test_ant_type_not_allowed_rejects(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", ant_types=["research_ant"]))
        result = queen.issue_mission(_make_mission(ant_type="paper_ant"))
        assert result.rejection_reason == MissionRejectionReason.ANT_TYPE_NOT_ALLOWED

    def test_biome_not_allowed_on_node_rejects(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", biomes=["equities"]))
        result = queen.issue_mission(_make_mission(biome="crypto"))
        assert result.rejection_reason == MissionRejectionReason.BIOME_NOT_ALLOWED_ON_NODE

    def test_rejection_detail_contains_node_id(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        result = queen.issue_mission(_make_mission(node="pc2-desktop"))
        assert "pc2-desktop" in result.rejection_detail

    def test_rejection_detail_contains_ant_type(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", ant_types=["research_ant"]))
        result = queen.issue_mission(_make_mission(ant_type="paper_ant"))
        assert "paper_ant" in result.rejection_detail

    def test_rejection_detail_contains_biome(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", biomes=["equities"]))
        result = queen.issue_mission(_make_mission(biome="crypto"))
        assert "crypto" in result.rejection_detail

    def test_node_check_before_capital_check(self, tmp_path):
        # Kapitaal te laag én node niet vertrouwd → NODE_NOT_TRUSTED wint
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=100.0)
        result = queen.issue_mission(_make_mission(capital=5_000.0))
        assert result.rejection_reason == MissionRejectionReason.NODE_NOT_TRUSTED

    def test_unregister_node_blocks_subsequent_missions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop"))
        queen.issue_mission(_make_mission("m-001"))
        queen.unregister_node("pc2-desktop")
        result = queen.issue_mission(_make_mission("m-002"))
        assert result.rejection_reason == MissionRejectionReason.NODE_NOT_TRUSTED

    def test_trusted_nodes_empty_before_register(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        assert queen.trusted_nodes() == []

    def test_trusted_nodes_returns_active_only(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2"))
        queen.register_node(_make_node("pc3", status=NodeStatus.STALE))
        assert queen.trusted_nodes() == ["pc2"]

    def test_trusted_nodes_sorted(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc3"))
        queen.register_node(_make_node("pc1"))
        queen.register_node(_make_node("pc2"))
        assert queen.trusted_nodes() == ["pc1", "pc2", "pc3"]

    def test_hot_swap_node_updates_permissions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("pc2-desktop", biomes=["crypto"]))
        queen.register_node(_make_node("pc2-desktop", biomes=["equities"]))
        # na hot-swap: crypto niet meer toegestaan
        result = queen.issue_mission(_make_mission(biome="crypto"))
        assert result.rejection_reason == MissionRejectionReason.BIOME_NOT_ALLOWED_ON_NODE

    def test_multiple_nodes_each_scoped(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.register_node(_make_node("node-crypto", biomes=["crypto"],   ant_types=["paper_ant"]))
        queen.register_node(_make_node("node-eq",     biomes=["equities"], ant_types=["paper_ant"]))
        r1 = queen.issue_mission(_make_mission("m1", node="node-crypto", biome="crypto"))
        r2 = queen.issue_mission(_make_mission("m2", node="node-eq",     biome="equities"))
        assert r1.accepted
        assert r2.accepted
