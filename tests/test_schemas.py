"""
tests/test_schemas.py

Basic schema construction and field validation for all colony schemas.
These tests verify that valid objects can be built and that obviously
wrong values are rejected at the schema level.
"""

import pytest
from datetime import datetime, timezone

from ant_colony.schemas.ant import Ant, AntStatus, AntType
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    StrategyCandidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_runtime_paths(**overrides) -> RuntimePaths:
    defaults = dict(
        output=r"C:\Trading\ANT_OUT",
        live=r"C:\Trading\ANT_LIVE",
        logs=r"C:\Trading\ANT_LOGS",
    )
    return RuntimePaths(**{**defaults, **overrides})


def make_node(**overrides) -> Node:
    defaults = dict(
        node_id="pc2-desktop",
        hostname="DESKTOP",
        allowed_biomes=["crypto"],
        allowed_ant_types=["scout_ant", "paper_ant"],
        heartbeat_interval=30,
        runtime_paths=make_runtime_paths(),
    )
    return Node(**{**defaults, **overrides})


def make_ant(**overrides) -> Ant:
    defaults = dict(
        ant_id="ant-001",
        ant_type=AntType.SCOUT,
        mission_id="m-001",
        node_id="pc2-desktop",
        ttl=3600,
    )
    return Ant(**{**defaults, **overrides})


def make_candidate(**overrides) -> StrategyCandidate:
    defaults = dict(
        candidate_id="cand-001",
        name="Simple MA crossover",
        source="internal",
        biome="crypto",
        logic_summary="Buy on golden cross, sell on death cross.",
        exit_conditions={"stop_loss_pct": 0.02, "take_profit_pct": 0.05},
    )
    return StrategyCandidate(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class TestNode:
    def test_valid_node(self):
        node = make_node()
        assert node.node_id == "pc2-desktop"
        assert node.status == NodeStatus.ACTIVE

    def test_allowed_biomes_must_not_be_empty(self):
        with pytest.raises(Exception):
            make_node(allowed_biomes=[])

    def test_allowed_ant_types_must_not_be_empty(self):
        with pytest.raises(Exception):
            make_node(allowed_ant_types=[])

    def test_runtime_paths_stored(self):
        node = make_node()
        assert "ANT_OUT" in node.runtime_paths.output
        assert "ANT_LIVE" in node.runtime_paths.live
        assert "ANT_LOGS" in node.runtime_paths.logs

    def test_default_status_is_active(self):
        assert make_node().status == NodeStatus.ACTIVE

    def test_last_heartbeat_defaults_to_none(self):
        assert make_node().last_heartbeat is None


# ---------------------------------------------------------------------------
# Ant
# ---------------------------------------------------------------------------

class TestAnt:
    def test_valid_ant(self):
        ant = make_ant()
        assert ant.ant_id == "ant-001"
        assert ant.status == AntStatus.IDLE

    def test_all_ant_types_are_valid(self):
        for ant_type in AntType:
            ant = make_ant(ant_type=ant_type)
            assert ant.ant_type == ant_type

    def test_ttl_must_be_positive(self):
        with pytest.raises(Exception):
            make_ant(ttl=0)

    def test_budget_used_defaults_to_zero(self):
        assert make_ant().budget_used == 0.0

    def test_budget_used_cannot_be_negative(self):
        with pytest.raises(Exception):
            make_ant(budget_used=-1.0)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_valid_heartbeat(self):
        hb = Heartbeat(
            ant_id="ant-001",
            mission_id="m-001",
            node_id="pc2-desktop",
            budget_used=0.0,
        )
        assert hb.status == HeartbeatStatus.RUNNING

    def test_budget_used_cannot_be_negative(self):
        with pytest.raises(Exception):
            Heartbeat(
                ant_id="ant-001",
                mission_id="m-001",
                node_id="pc2-desktop",
                budget_used=-5.0,
            )

    def test_timestamp_defaults_to_utcnow(self):
        hb = Heartbeat(
            ant_id="ant-001",
            mission_id="m-001",
            node_id="pc2-desktop",
            budget_used=0.0,
        )
        assert isinstance(hb.timestamp, datetime)


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------

class TestAuditEvent:
    def test_valid_event(self):
        event = AuditEvent(
            event_type=AuditEventType.MISSION_ISSUED,
            source="queen",
            sequence=0,
        )
        assert event.event_id  # uuid generated
        assert event.payload == {}

    def test_event_id_is_unique(self):
        e1 = AuditEvent(event_type=AuditEventType.HEARTBEAT, source="ant-001", sequence=1)
        e2 = AuditEvent(event_type=AuditEventType.HEARTBEAT, source="ant-001", sequence=2)
        assert e1.event_id != e2.event_id

    def test_source_must_not_be_empty(self):
        with pytest.raises(Exception):
            AuditEvent(event_type=AuditEventType.HEARTBEAT, source="  ", sequence=0)

    def test_sequence_cannot_be_negative(self):
        with pytest.raises(Exception):
            AuditEvent(event_type=AuditEventType.HEARTBEAT, source="scheduler", sequence=-1)

    def test_all_event_types_are_constructable(self):
        for et in AuditEventType:
            event = AuditEvent(event_type=et, source="scheduler", sequence=0)
            assert event.event_type == et


# ---------------------------------------------------------------------------
# StrategyCandidate
# ---------------------------------------------------------------------------

class TestStrategyCandidate:
    def test_valid_candidate(self):
        c = make_candidate()
        assert c.status == CandidateStatus.RESEARCH
        assert c.approved_by is None

    def test_exit_conditions_are_required(self):
        with pytest.raises(Exception):
            make_candidate(exit_conditions={})

    def test_approved_by_must_be_queen(self):
        with pytest.raises(Exception):
            make_candidate(approved_by="rogue_agent")

    def test_live_status_requires_queen_approval(self):
        with pytest.raises(Exception):
            make_candidate(status=CandidateStatus.LIVE, approved_by=None)

    def test_live_status_with_queen_approval_is_valid(self):
        c = make_candidate(status=CandidateStatus.LIVE, approved_by="queen")
        assert c.status == CandidateStatus.LIVE

    def test_provenance_defaults_to_empty_list(self):
        assert make_candidate().provenance == []

    def test_fitness_score_defaults_to_none(self):
        assert make_candidate().fitness_score is None
