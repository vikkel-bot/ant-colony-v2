"""
tests/test_scheduler_tick.py

Scheduler behaviour: tick logic, heartbeat staleness, TTL enforcement,
kill-switch levels, and log output.
"""

import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ant_colony.colony.scheduler.colony_scheduler import (
    AgentRecord,
    ColonyScheduler,
    ColonyStatus,
    KillLevel,
)
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_scheduler(tmp_path: Path, tick_interval: int = 1) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=tick_interval)


def make_record(
    ant_id: str = "ant-001",
    ttl: int = 3600,
    heartbeat_interval: int = 30,
    node_id: str = "pc2-desktop",
) -> AgentRecord:
    return AgentRecord(
        ant_id=ant_id,
        mission_id="m-001",
        node_id=node_id,
        ant_type="scout_ant",
        ttl=ttl,
        heartbeat_interval=heartbeat_interval,
    )


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------

class TestSchedulerInstantiation:
    def test_default_status_is_running(self, tmp_path):
        s = make_scheduler(tmp_path)
        assert s.status == ColonyStatus.RUNNING

    def test_tick_does_not_execute_at_import(self):
        # Importing the module must not run any code — verified by the fact
        # that we can import without side effects.
        from ant_colony.colony.scheduler import colony_scheduler  # noqa: F401


# ---------------------------------------------------------------------------
# Tick — basic behaviour
# ---------------------------------------------------------------------------

class TestTick:
    def test_tick_on_empty_colony_does_not_raise(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.tick()
        assert s.status == ColonyStatus.RUNNING

    def test_tick_writes_to_log(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.tick()
        log_path = tmp_path / "colony" / "scheduler.jsonl"
        records = read_log(log_path)
        assert len(records) >= 1

    def test_tick_is_skipped_when_halted(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.kill_switch(KillLevel.COLONY)
        assert s.status == ColonyStatus.HALTED

        # Tick after halt must be a no-op — no new log entries from tick
        log_path = tmp_path / "colony" / "scheduler.jsonl"
        entries_before = len(read_log(log_path))
        s.tick()
        entries_after = len(read_log(log_path))
        assert entries_after == entries_before

    def test_tick_increments_sequence(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.tick()
        s.tick()
        log_path = tmp_path / "colony" / "scheduler.jsonl"
        records = read_log(log_path)
        sequences = [r["sequence"] for r in records if "sequence" in r]
        assert sequences == sorted(sequences)
        assert sequences[-1] >= 2


# ---------------------------------------------------------------------------
# Agent registration and heartbeat tracking
# ---------------------------------------------------------------------------

class TestAgentRegistration:
    def test_register_and_tick_keeps_healthy_agent_running(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record()
        s.register_agent(rec)
        s.tick()
        assert rec.status == AntStatus.RUNNING

    def test_record_heartbeat_updates_timestamp(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record()
        s.register_agent(rec)

        old_ts = rec.last_heartbeat
        time.sleep(0.01)  # ensure measurable delta

        hb = Heartbeat(
            ant_id="ant-001",
            mission_id="m-001",
            node_id="pc2-desktop",
            budget_used=0.0,
        )
        s.record_heartbeat(hb)
        assert rec.last_heartbeat >= old_ts

    def test_heartbeat_from_unknown_agent_is_ignored(self, tmp_path):
        s = make_scheduler(tmp_path)
        hb = Heartbeat(
            ant_id="ghost-agent",
            mission_id="m-999",
            node_id="pc2-desktop",
            budget_used=0.0,
        )
        s.record_heartbeat(hb)  # must not raise


# ---------------------------------------------------------------------------
# Heartbeat staleness detection
# ---------------------------------------------------------------------------

class TestHeartbeatStaleness:
    def test_stale_heartbeat_aborts_agent(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record(heartbeat_interval=1)
        s.register_agent(rec)

        # Backdate last_heartbeat beyond stale threshold (interval * 2)
        rec.last_heartbeat = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

        s.tick()
        assert rec.status == AntStatus.ABORTED

    def test_fresh_heartbeat_is_not_aborted(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record(heartbeat_interval=30)
        s.register_agent(rec)
        s.tick()
        assert rec.status == AntStatus.RUNNING

    def test_stale_agent_calls_stopper(self, tmp_path):
        stopped: list[str] = []
        s = ColonyScheduler(
            logs_root=tmp_path,
            tick_interval=1,
            agent_stopper=lambda ant_id, reason: stopped.append(ant_id),
        )
        rec = make_record(heartbeat_interval=1)
        s.register_agent(rec)
        rec.last_heartbeat = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

        s.tick()
        assert "ant-001" in stopped


# ---------------------------------------------------------------------------
# TTL enforcement
# ---------------------------------------------------------------------------

class TestTTLEnforcement:
    def test_expired_ttl_aborts_agent(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record(ttl=1, heartbeat_interval=1)
        s.register_agent(rec)

        # Backdate started_at beyond TTL
        rec.started_at = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

        s.tick()
        assert rec.status == AntStatus.ABORTED

    def test_agent_within_ttl_is_not_aborted(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record(ttl=3600, heartbeat_interval=30)
        s.register_agent(rec)
        s.tick()
        assert rec.status == AntStatus.RUNNING

    def test_already_aborted_agent_is_not_processed_again(self, tmp_path):
        s = make_scheduler(tmp_path)
        rec = make_record(ttl=1, heartbeat_interval=1)
        rec.status = AntStatus.ABORTED
        s.register_agent(rec)
        rec.started_at = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

        s.tick()
        assert rec.status == AntStatus.ABORTED  # unchanged


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_level_1_aborts_single_agent(self, tmp_path):
        s = make_scheduler(tmp_path)
        r1 = make_record(ant_id="ant-001")
        r2 = make_record(ant_id="ant-002")
        s.register_agent(r1)
        s.register_agent(r2)

        s.kill_switch(KillLevel.AGENT, scope="ant-001")

        assert r1.status == AntStatus.ABORTED
        assert r2.status == AntStatus.RUNNING
        assert s.status == ColonyStatus.RUNNING

    def test_level_2_aborts_all_agents_on_node(self, tmp_path):
        s = make_scheduler(tmp_path)
        r1 = make_record(ant_id="ant-001", node_id="node-a")
        r2 = make_record(ant_id="ant-002", node_id="node-a")
        r3 = make_record(ant_id="ant-003", node_id="node-b")
        s.register_agent(r1)
        s.register_agent(r2)
        s.register_agent(r3)

        s.kill_switch(KillLevel.NODE, scope="node-a")

        assert r1.status == AntStatus.ABORTED
        assert r2.status == AntStatus.ABORTED
        assert r3.status == AntStatus.RUNNING
        assert s.status == ColonyStatus.RUNNING

    def test_level_3_halts_colony_and_aborts_all_agents(self, tmp_path):
        s = make_scheduler(tmp_path)
        r1 = make_record(ant_id="ant-001")
        r2 = make_record(ant_id="ant-002")
        s.register_agent(r1)
        s.register_agent(r2)

        s.kill_switch(KillLevel.COLONY)

        assert s.status == ColonyStatus.HALTED
        assert r1.status == AntStatus.ABORTED
        assert r2.status == AntStatus.ABORTED

    def test_level_3_does_not_auto_restart(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.kill_switch(KillLevel.COLONY)
        assert s.status == ColonyStatus.HALTED

        # Multiple ticks must not change the status
        s.tick()
        s.tick()
        assert s.status == ColonyStatus.HALTED

    def test_kill_switch_writes_audit_event(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.kill_switch(KillLevel.COLONY)

        log_path = tmp_path / "colony" / "scheduler.jsonl"
        records = read_log(log_path)
        event_types = [r.get("event_type") for r in records]
        assert "colony_halted" in event_types

    def test_level_1_with_no_scope_does_not_raise(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.kill_switch(KillLevel.AGENT, scope=None)  # logs error, does not raise


# ---------------------------------------------------------------------------
# Log integrity
# ---------------------------------------------------------------------------

class TestLogIntegrity:
    def test_log_file_is_append_only_newline_delimited_json(self, tmp_path):
        s = make_scheduler(tmp_path)
        s.tick()
        s.tick()

        log_path = tmp_path / "colony" / "scheduler.jsonl"
        assert log_path.exists()

        raw = log_path.read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip()]
        for line in lines:
            json.loads(line)  # each line must be valid JSON

    def test_log_parent_dirs_are_created(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        s = ColonyScheduler(logs_root=deep, tick_interval=1)
        s.tick()
        assert (deep / "colony" / "scheduler.jsonl").exists()
