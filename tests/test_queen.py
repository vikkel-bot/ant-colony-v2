"""
tests/test_queen.py

Fase 3 — Queen governance, mission systeem, kill-switch.

Test-opzet:
  - TestQueenCapital           kapitaalallocatie, beschikbaarheid, herstel
  - TestQueenMissionIssuance   acceptatie, afwijzing, scheduler dispatch, logging
  - TestQueenKillSwitch        level 1/2/3, colony status, audit event
  - TestKillSwitchSimulation   multi-agent simulatie — gate-kern
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ant_colony.colony.scheduler.colony_scheduler import (
    AgentRecord,
    ColonyScheduler,
    ColonyStatus,
    KillLevel,
)
from ant_colony.queen import MissionIssueResult, MissionRejectionReason, Queen
from ant_colony.schemas.ant import AntStatus
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

def make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def make_queen(
    scheduler: ColonyScheduler,
    capital: float = 50_000.0,
    logs_root: Path | None = None,
) -> Queen:
    queen = Queen(capital_total=capital, scheduler=scheduler, logs_root=logs_root)
    # Registreer standaard node die overeenkomt met make_mission() defaults
    queen.register_node(Node(
        node_id="pc2-desktop",
        hostname="DESKTOP",
        allowed_biomes=["crypto"],
        allowed_ant_types=["paper_ant", "research_ant"],
        heartbeat_interval=60,
        runtime_paths=RuntimePaths(output="/out", live="/live", logs="/logs"),
    ))
    return queen


def make_mission(
    mission_id: str = "m-001",
    capital: float = 10_000.0,
    node: str = "pc2-desktop",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type="paper_ant",
        allowed_node=node,
        allowed_actions=["paper_trade"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
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


def make_agent(
    ant_id: str = "ant-001",
    mission_id: str = "m-001",
    node_id: str = "pc2-desktop",
) -> AgentRecord:
    return AgentRecord(
        ant_id=ant_id,
        mission_id=mission_id,
        node_id=node_id,
        ant_type="paper_ant",
        ttl=86_400,
        heartbeat_interval=60,
    )


# ---------------------------------------------------------------------------
# TestQueenCapital
# ---------------------------------------------------------------------------

class TestQueenCapital:
    def test_capital_total_set_at_init(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=75_000.0)
        assert queen.capital_total == 75_000.0

    def test_capital_allocated_zero_before_missions(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        assert queen.capital_allocated == 0.0

    def test_capital_available_equals_total_before_missions(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=50_000.0)
        assert queen.capital_available == 50_000.0

    def test_capital_allocated_increases_after_issue(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=50_000.0)
        queen.issue_mission(make_mission(capital=10_000.0))
        assert queen.capital_allocated == 10_000.0

    def test_capital_available_decreases_after_issue(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=50_000.0)
        queen.issue_mission(make_mission(capital=15_000.0))
        assert queen.capital_available == 35_000.0

    def test_capital_available_restored_after_revoke(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=50_000.0)
        queen.issue_mission(make_mission(mission_id="m-001", capital=10_000.0))
        queen.revoke_mission("m-001")
        assert queen.capital_available == 50_000.0

    def test_two_missions_allocate_combined_capital(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=50_000.0)
        queen.issue_mission(make_mission(mission_id="m-001", capital=10_000.0))
        queen.issue_mission(make_mission(mission_id="m-002", capital=20_000.0))
        assert queen.capital_allocated == 30_000.0
        assert queen.capital_available == 20_000.0

    def test_negative_capital_total_raises(self, tmp_path):
        sched = make_scheduler(tmp_path)
        with pytest.raises(ValueError, match="capital_total"):
            Queen(capital_total=-1.0, scheduler=sched)


# ---------------------------------------------------------------------------
# TestQueenMissionIssuance
# ---------------------------------------------------------------------------

class TestQueenMissionIssuance:
    def test_valid_mission_accepted(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        r = queen.issue_mission(make_mission())
        assert r.accepted
        assert r.mission_id == "m-001"
        assert r.rejection_reason is None

    def test_accepted_mission_appears_in_active_missions(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.issue_mission(make_mission(mission_id="m-001"))
        assert "m-001" in queen.active_missions

    def test_revoked_mission_removed_from_active(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.issue_mission(make_mission(mission_id="m-001"))
        queen.revoke_mission("m-001")
        assert "m-001" not in queen.active_missions

    def test_duplicate_mission_rejected(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.issue_mission(make_mission(mission_id="m-dup"))
        r = queen.issue_mission(make_mission(mission_id="m-dup"))
        assert not r.accepted
        assert r.rejection_reason == MissionRejectionReason.DUPLICATE_MISSION_ID

    def test_capital_exceeded_rejected(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=5_000.0)
        r = queen.issue_mission(make_mission(capital=10_000.0))
        assert not r.accepted
        assert r.rejection_reason == MissionRejectionReason.CAPITAL_EXCEEDED

    def test_capital_exactly_available_is_accepted(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=10_000.0)
        r = queen.issue_mission(make_mission(capital=10_000.0))
        assert r.accepted

    def test_mission_enqueued_in_scheduler(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.issue_mission(make_mission(mission_id="m-001"))
        assert any(m.mission_id == "m-001" for m in sched._pending_missions)

    def test_revoke_unknown_mission_is_safe(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.revoke_mission("nonexistent")  # moet geen exception gooien

    def test_mission_issued_log_written(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, logs_root=tmp_path)
        queen.issue_mission(make_mission(mission_id="m-log"))
        log = tmp_path / "missions" / "m-log.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["event_type"] == "mission_issued"
        assert record["mission_id"] == "m-log"
        assert record["source"] == "queen"

    def test_mission_rejected_log_written(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=100.0, logs_root=tmp_path)
        queen.issue_mission(make_mission(mission_id="m-rej", capital=999_999.0))
        log = tmp_path / "missions" / "m-rej.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["event_type"] == "mission_rejected"

    def test_no_log_when_logs_root_none(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, logs_root=None)
        queen.issue_mission(make_mission())  # mag geen exception gooien


# ---------------------------------------------------------------------------
# TestQueenKillSwitch
# ---------------------------------------------------------------------------

class TestQueenKillSwitch:
    def test_kill_level1_aborts_target_agent(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        agent = make_agent(ant_id="ant-target", node_id="pc2-desktop")
        sched.register_agent(agent)
        queen.kill_switch(KillLevel.AGENT, scope="ant-target")
        assert sched._agents["ant-target"].status == AntStatus.ABORTED

    def test_kill_level1_leaves_other_agents_running(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        sched.register_agent(make_agent(ant_id="ant-target"))
        sched.register_agent(make_agent(ant_id="ant-safe"))
        queen.kill_switch(KillLevel.AGENT, scope="ant-target")
        assert sched._agents["ant-safe"].status == AntStatus.RUNNING

    def test_kill_level2_aborts_all_agents_on_node(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        sched.register_agent(make_agent(ant_id="ant-a", node_id="node-x"))
        sched.register_agent(make_agent(ant_id="ant-b", node_id="node-x"))
        queen.kill_switch(KillLevel.NODE, scope="node-x")
        assert sched._agents["ant-a"].status == AntStatus.ABORTED
        assert sched._agents["ant-b"].status == AntStatus.ABORTED

    def test_kill_level2_leaves_other_nodes_running(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        sched.register_agent(make_agent(ant_id="ant-x", node_id="node-x"))
        sched.register_agent(make_agent(ant_id="ant-y", node_id="node-y"))
        queen.kill_switch(KillLevel.NODE, scope="node-x")
        assert sched._agents["ant-y"].status == AntStatus.RUNNING

    def test_kill_level3_halts_colony(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.kill_switch(KillLevel.COLONY)
        assert sched.status == ColonyStatus.HALTED

    def test_kill_level3_aborts_all_agents(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        sched.register_agent(make_agent(ant_id="ant-1"))
        sched.register_agent(make_agent(ant_id="ant-2"))
        sched.register_agent(make_agent(ant_id="ant-3"))
        queen.kill_switch(KillLevel.COLONY)
        assert all(
            r.status == AntStatus.ABORTED
            for r in sched._agents.values()
        )

    def test_kill_level3_tick_skipped_after_halt(self, tmp_path):
        """Scheduler verwerkt geen tick meer na colony kill."""
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched)
        queen.kill_switch(KillLevel.COLONY)
        tick_before = sched._tick_sequence
        sched.tick()
        assert sched._tick_sequence == tick_before  # geen tick uitgevoerd

    def test_kill_event_logged(self, tmp_path):
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, logs_root=tmp_path)
        queen.kill_switch(KillLevel.COLONY)
        log = tmp_path / "colony" / "kill_switch.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["event_type"] == "kill_switch_activated"
        assert record["source"] == "queen"
        assert record["payload"]["level"] == KillLevel.COLONY.value


# ---------------------------------------------------------------------------
# TestKillSwitchSimulation — gate-kern
# ---------------------------------------------------------------------------

class TestKillSwitchSimulation:
    """
    Bewijst dat de kill-switch correct werkt in een gesimuleerde multi-agent omgeving.
    Elke test bouwt een mini-kolonie op en verifieert het kill-gedrag end-to-end.
    """

    def _build_colony(
        self,
        tmp_path: Path,
        n_agents: int = 3,
        node_ids: list[str] | None = None,
        capital: float = 100_000.0,
    ) -> tuple[Queen, ColonyScheduler, list[AgentRecord]]:
        sched = make_scheduler(tmp_path)
        queen = make_queen(sched, capital=capital, logs_root=tmp_path)
        agents = []
        for i in range(n_agents):
            node = (node_ids[i] if node_ids else "pc2-desktop")
            agent = make_agent(ant_id=f"ant-{i+1}", node_id=node)
            sched.register_agent(agent)
            agents.append(agent)
        return queen, sched, agents

    def test_simulation_level3_halts_all(self, tmp_path):
        """Level 3: 3 agents op 2 nodes → allemaal gestopt, kolonie HALTED."""
        queen, sched, agents = self._build_colony(
            tmp_path,
            n_agents=3,
            node_ids=["node-a", "node-a", "node-b"],
        )
        # Controleer: voor kill zijn alle agents RUNNING
        assert all(a.status == AntStatus.RUNNING for a in agents)

        queen.kill_switch(KillLevel.COLONY)

        assert sched.status == ColonyStatus.HALTED
        assert all(a.status == AntStatus.ABORTED for a in agents)

    def test_simulation_level1_selective(self, tmp_path):
        """Level 1: kill ant-2 van 3 agents → ant-1 en ant-3 nog RUNNING."""
        queen, sched, agents = self._build_colony(tmp_path, n_agents=3)

        queen.kill_switch(KillLevel.AGENT, scope="ant-2")

        assert sched._agents["ant-1"].status == AntStatus.RUNNING
        assert sched._agents["ant-2"].status == AntStatus.ABORTED
        assert sched._agents["ant-3"].status == AntStatus.RUNNING
        assert sched.status == ColonyStatus.RUNNING  # kolonie draait door

    def test_simulation_level2_node_targeted(self, tmp_path):
        """Level 2: kill node-a → agents op node-b onberoerd."""
        queen, sched, agents = self._build_colony(
            tmp_path,
            n_agents=4,
            node_ids=["node-a", "node-a", "node-b", "node-b"],
        )
        queen.kill_switch(KillLevel.NODE, scope="node-a")

        assert sched._agents["ant-1"].status == AntStatus.ABORTED
        assert sched._agents["ant-2"].status == AntStatus.ABORTED
        assert sched._agents["ant-3"].status == AntStatus.RUNNING
        assert sched._agents["ant-4"].status == AntStatus.RUNNING
        assert sched.status == ColonyStatus.RUNNING

    def test_simulation_audit_trail_complete(self, tmp_path):
        """Na level-3 kill staat het event correct in kill_switch.jsonl."""
        queen, sched, _ = self._build_colony(tmp_path, n_agents=2)
        queen.kill_switch(KillLevel.COLONY)

        log = tmp_path / "colony" / "kill_switch.jsonl"
        assert log.exists()
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        last = json.loads(lines[-1])
        assert last["event_type"] == "kill_switch_activated"
        assert last["payload"]["level"] == KillLevel.COLONY.value
        assert last["source"] == "queen"
        # sequence is monotoon oplopend (≥ 1)
        assert last["sequence"] >= 1
