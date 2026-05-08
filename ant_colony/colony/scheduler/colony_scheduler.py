"""
Colony Scheduler — infrastructure layer for ANT COLONY v2.

Responsibilities:
  - Heartbeat monitoring for all active agents
  - TTL enforcement (hard cutoff)
  - Abort condition polling
  - Mission dispatch
  - Colony status management
  - Append-only tick and dashboard heartbeat log to ANT_LOGS\\colony\\scheduler.jsonl

Rules:
  - Runs always; is never an afterthought
  - No own capital or mission authority
  - Pauses on Colony kill (Level 3); never auto-restarts after Level 3
  - No code executes at import (P7)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol

from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat
from ant_colony.schemas.mission import Mission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Colony status
# ---------------------------------------------------------------------------

class ColonyStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    HALTED = "halted"   # Level-3 kill — requires manual operator restart


# ---------------------------------------------------------------------------
# Kill-switch levels (mirrors COLONY_GOVERNANCE.md §6)
# ---------------------------------------------------------------------------

class KillLevel(int, Enum):
    AGENT = 1   # single agent
    NODE = 2    # all agents on one node
    COLONY = 3  # entire colony — operator only, no auto-restart


# ---------------------------------------------------------------------------
# Minimal state records (in-memory; authoritative state lives in artifact files)
# ---------------------------------------------------------------------------

class AgentRecord:
    """Lightweight in-memory view of a running agent."""

    __slots__ = (
        "ant_id", "mission_id", "node_id", "ant_type",
        "started_at", "ttl", "heartbeat_interval",
        "last_heartbeat", "status",
    )

    def __init__(
        self,
        ant_id: str,
        mission_id: str,
        node_id: str,
        ant_type: str,
        ttl: int,
        heartbeat_interval: int,
    ) -> None:
        self.ant_id = ant_id
        self.mission_id = mission_id
        self.node_id = node_id
        self.ant_type = ant_type
        self.ttl = ttl
        self.heartbeat_interval = heartbeat_interval
        self.started_at: datetime = datetime.now(tz=timezone.utc)
        self.last_heartbeat: datetime = self.started_at
        self.status: AntStatus = AntStatus.RUNNING

    def age_seconds(self) -> float:
        return (datetime.now(tz=timezone.utc) - self.started_at).total_seconds()

    def seconds_since_heartbeat(self) -> float:
        return (datetime.now(tz=timezone.utc) - self.last_heartbeat).total_seconds()

    def is_ttl_expired(self) -> bool:
        return self.age_seconds() >= self.ttl

    def is_heartbeat_stale(self) -> bool:
        return self.seconds_since_heartbeat() > self.heartbeat_interval * 2


# ---------------------------------------------------------------------------
# Agent stop callback protocol
# ---------------------------------------------------------------------------

class AgentStopper(Protocol):
    """Callable that signals an agent to stop. Implemented by the runtime layer."""

    def __call__(self, ant_id: str, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class ColonyScheduler:
    """
    Tick-based colony scheduler.

    Usage::

        scheduler = ColonyScheduler(
            logs_root=Path(r"C:\\Trading\\ANT_LOGS"),
            tick_interval=5,
        )
        scheduler.start()          # blocking loop
        # or:
        # scheduler.tick()         # single tick, useful in tests
    """

    def __init__(
        self,
        logs_root: Path,
        tick_interval: int = 5,
        agent_stopper: AgentStopper | None = None,
    ) -> None:
        self._logs_root = logs_root
        self._tick_interval = tick_interval
        self._agent_stopper = agent_stopper

        self._agents: dict[str, AgentRecord] = {}
        self._pending_missions: list[Mission] = []
        self._status: ColonyStatus = ColonyStatus.RUNNING
        self._tick_sequence: int = 0
        self._tick_lock = threading.RLock()
        now = datetime.now(tz=timezone.utc)
        self._last_tick_started_at: datetime | None = None
        self._last_tick_completed_at: datetime = now
        self._watchdog_threshold_seconds = int(os.getenv("COLONY_TICK_WATCHDOG_SECONDS", "60"))
        self._watchdog_check_seconds = max(5, int(os.getenv("COLONY_TICK_WATCHDOG_CHECK_SECONDS", "30")))
        self._slow_tick_threshold_seconds = int(os.getenv("COLONY_SLOW_TICK_SECONDS", "60"))
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._last_watchdog_recovery_at: datetime | None = None
        self._dashboard_heartbeat_seconds = max(
            1,
            int(os.getenv("COLONY_DASHBOARD_HEARTBEAT_SECONDS", "10")),
        )
        self._dashboard_heartbeat_stop = threading.Event()
        self._dashboard_heartbeat_thread: threading.Thread | None = None

        self._scheduler_log_path = logs_root / "colony" / "scheduler.jsonl"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def status(self) -> ColonyStatus:
        return self._status

    @property
    def last_tick_completed_at(self) -> datetime:
        return self._last_tick_completed_at

    def seconds_since_last_tick(self) -> float:
        return (datetime.now(tz=timezone.utc) - self._last_tick_completed_at).total_seconds()

    def start(self) -> None:
        """Blocking tick loop. Exits only on Colony kill or operator interrupt."""
        logger.info("Scheduler starting. tick_interval=%ds", self._tick_interval)
        self._log_event(AuditEventType.COLONY_HALTED, source="scheduler", payload={"action": "start"})
        self._start_watchdog()
        self._start_dashboard_heartbeat()

        try:
            while self._status == ColonyStatus.RUNNING:
                try:
                    self.tick()
                except Exception as exc:
                    logger.exception("Scheduler tick fout — tick-cyclus wordt automatisch hervat.")
                    self._log_scheduler_error("scheduler_tick_error", exc)
                time.sleep(self._tick_interval)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by KeyboardInterrupt.")
        finally:
            self._stop_dashboard_heartbeat()
            self._stop_watchdog()
            logger.info("Scheduler exited. Final status: %s", self._status)

    def tick(self) -> None:
        """
        Single scheduler tick.

        Order of operations (fail-closed):
          1. Check heartbeats — stale agents are aborted first
          2. Enforce TTLs
          3. Dispatch pending missions
          4. Log tick summary
        """
        if self._status != ColonyStatus.RUNNING:
            logger.debug("Tick skipped — colony status is %s", self._status)
            return

        if not self._tick_lock.acquire(blocking=False):
            logger.warning("Tick skipped — vorige scheduler tick loopt nog.")
            return

        try:
            self._tick_sequence += 1
            tick_start = datetime.now(tz=timezone.utc)
            self._last_tick_started_at = tick_start

            stale = self._check_heartbeats()
            expired = self._enforce_ttls()
            dispatched = self._dispatch_pending_missions()

            self._last_tick_completed_at = datetime.now(tz=timezone.utc)
            self._log_tick_summary(tick_start, stale=stale, expired=expired, dispatched=dispatched)
        finally:
            self._tick_lock.release()

    def register_agent(self, record: AgentRecord) -> None:
        """Register a newly started agent with the scheduler."""
        self._agents[record.ant_id] = record
        logger.debug("Agent registered: %s (mission=%s)", record.ant_id, record.mission_id)

    def record_heartbeat(self, heartbeat: Heartbeat) -> None:
        """Update the last-seen timestamp for a running agent."""
        record = self._agents.get(heartbeat.ant_id)
        if record is None:
            logger.warning("Heartbeat from unknown agent: %s — ignoring", heartbeat.ant_id)
            return
        record.last_heartbeat = heartbeat.timestamp
        record.status = AntStatus(heartbeat.status.value.replace("running", "running").replace("paused", "paused"))

    def enqueue_mission(self, mission: Mission) -> None:
        """Add a mission to the dispatch queue."""
        self._pending_missions.append(mission)
        logger.debug("Mission enqueued: %s (ant_type=%s)", mission.mission_id, mission.ant_type)

    def kill_switch(self, level: KillLevel, scope: str | None = None) -> None:
        """
        Activate the kill-switch at the specified level.

        Args:
            level:  KillLevel.AGENT | KillLevel.NODE | KillLevel.COLONY
            scope:  ant_id (Level 1) or node_id (Level 2); ignored for Level 3
        """
        if level == KillLevel.AGENT:
            self._kill_agent(scope, reason="kill_switch_level_1")
        elif level == KillLevel.NODE:
            self._kill_node(scope)
        elif level == KillLevel.COLONY:
            self._kill_colony()

    # ------------------------------------------------------------------
    # Internal — heartbeat monitoring
    # ------------------------------------------------------------------

    def _check_heartbeats(self) -> list[str]:
        """
        Abort agents with stale heartbeats.

        Returns list of aborted ant_ids.
        """
        stale: list[str] = []
        for ant_id, record in list(self._agents.items()):
            if record.status != AntStatus.RUNNING:
                continue
            if record.is_heartbeat_stale():
                logger.warning(
                    "Stale heartbeat: %s (last seen %.1fs ago, interval=%ds)",
                    ant_id,
                    record.seconds_since_heartbeat(),
                    record.heartbeat_interval,
                )
                self._abort_agent(ant_id, reason="stale_heartbeat")
                self._log_event(
                    AuditEventType.NODE_HEARTBEAT_STALE,
                    source="scheduler",
                    payload={
                        "ant_id": ant_id,
                        "seconds_since_heartbeat": record.seconds_since_heartbeat(),
                    },
                )
                stale.append(ant_id)
        return stale

    # ------------------------------------------------------------------
    # Internal — TTL enforcement
    # ------------------------------------------------------------------

    def _enforce_ttls(self) -> list[str]:
        """
        Abort agents that have exceeded their TTL.

        Returns list of aborted ant_ids.
        """
        expired: list[str] = []
        for ant_id, record in list(self._agents.items()):
            if record.status != AntStatus.RUNNING:
                continue
            if record.is_ttl_expired():
                logger.warning(
                    "TTL expired: %s (age=%.1fs, ttl=%ds)",
                    ant_id,
                    record.age_seconds(),
                    record.ttl,
                )
                self._abort_agent(ant_id, reason="ttl_expired")
                expired.append(ant_id)
        return expired

    # ------------------------------------------------------------------
    # Internal — mission dispatch
    # ------------------------------------------------------------------

    def _dispatch_pending_missions(self) -> list[str]:
        """
        Dispatch queued missions.

        Skeleton: dispatch logic implemented in Fase 2+.
        Returns list of dispatched mission_ids.
        """
        dispatched: list[str] = []
        # TODO (Fase 2): instantiate correct Ant type per mission.ant_type,
        # bind to allowed_node, register AgentRecord, call agent.start().
        if self._pending_missions:
            logger.debug("%d mission(s) pending — dispatch not yet implemented", len(self._pending_missions))
        return dispatched

    # ------------------------------------------------------------------
    # Internal — kill-switch helpers
    # ------------------------------------------------------------------

    def _kill_agent(self, ant_id: str | None, reason: str) -> None:
        if ant_id is None:
            logger.error("kill_agent called with no ant_id")
            return
        self._abort_agent(ant_id, reason=reason)
        self._log_event(
            AuditEventType.KILL_SWITCH_ACTIVATED,
            source="scheduler",
            payload={"level": KillLevel.AGENT, "ant_id": ant_id, "reason": reason},
        )

    def _kill_node(self, node_id: str | None) -> None:
        if node_id is None:
            logger.error("kill_node called with no node_id")
            return
        targets = [r for r in self._agents.values() if r.node_id == node_id]
        for record in targets:
            self._abort_agent(record.ant_id, reason="kill_switch_level_2")
        self._log_event(
            AuditEventType.KILL_SWITCH_ACTIVATED,
            source="scheduler",
            payload={"level": KillLevel.NODE, "node_id": node_id, "agents_stopped": len(targets)},
        )

    def _kill_colony(self) -> None:
        """
        Level-3 kill: halt the entire colony.

        Sets status to HALTED. The scheduler will not process further ticks.
        Manual operator restart required — no auto-restart.
        """
        logger.critical("COLONY KILL-SWITCH ACTIVATED — halting all agents")
        for ant_id in list(self._agents.keys()):
            self._abort_agent(ant_id, reason="kill_switch_level_3")
        self._status = ColonyStatus.HALTED
        self._log_event(
            AuditEventType.COLONY_HALTED,
            source="scheduler",
            payload={"reason": "kill_switch_level_3", "agents_stopped": len(self._agents)},
        )

    # ------------------------------------------------------------------
    # Internal — agent abort
    # ------------------------------------------------------------------

    def _abort_agent(self, ant_id: str, reason: str) -> None:
        record = self._agents.get(ant_id)
        if record is None:
            return

        if record.status in (AntStatus.COMPLETED, AntStatus.ABORTED):
            return

        record.status = AntStatus.ABORTED
        logger.info("Agent aborted: %s (reason=%s)", ant_id, reason)

        if self._agent_stopper is not None:
            try:
                self._agent_stopper(ant_id, reason)
            except Exception:
                logger.exception("agent_stopper raised for ant_id=%s", ant_id)

    # ------------------------------------------------------------------
    # Internal — logging
    # ------------------------------------------------------------------

    def _log_event(
        self,
        event_type: AuditEventType,
        source: str,
        payload: dict | None = None,
    ) -> None:
        event = AuditEvent(
            event_type=event_type,
            source=source,
            payload=payload or {},
            sequence=self._tick_sequence,
        )
        self._append_to_log(self._scheduler_log_path, event.model_dump(mode="json"))

    def _log_tick_summary(
        self,
        tick_start: datetime,
        stale: list[str],
        expired: list[str],
        dispatched: list[str],
    ) -> None:
        active = sum(1 for r in self._agents.values() if r.status == AntStatus.RUNNING)
        duration_seconds = (self._last_tick_completed_at - tick_start).total_seconds()
        if duration_seconds > self._slow_tick_threshold_seconds:
            logger.error(
                "Scheduler tick traag | duration=%.1fs threshold=%ds active_agents=%d",
                duration_seconds,
                self._slow_tick_threshold_seconds,
                active,
            )
        self._append_to_log(
            self._scheduler_log_path,
            {
                "event_type": "tick",
                "sequence": self._tick_sequence,
                "timestamp": tick_start.isoformat(),
                "duration_seconds": round(duration_seconds, 3),
                "active_agents": active,
                "stale_aborted": stale,
                "ttl_expired": expired,
                "dispatched": dispatched,
            },
        )

    def _append_to_log(self, path: Path, record: dict) -> None:
        """Append a single JSON record to a log file. Creates parent dirs if needed."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write to log: %s", path)

    # ------------------------------------------------------------------
    # Internal — dashboard heartbeat
    # ------------------------------------------------------------------

    def _start_dashboard_heartbeat(self) -> None:
        if self._dashboard_heartbeat_seconds <= 0:
            return
        if (
            self._dashboard_heartbeat_thread is not None
            and self._dashboard_heartbeat_thread.is_alive()
        ):
            return
        self._dashboard_heartbeat_stop.clear()
        self._dashboard_heartbeat_thread = threading.Thread(
            target=self._dashboard_heartbeat_loop,
            name="colony-dashboard-heartbeat",
            daemon=True,
        )
        self._dashboard_heartbeat_thread.start()

    def _stop_dashboard_heartbeat(self) -> None:
        self._dashboard_heartbeat_stop.set()
        if (
            self._dashboard_heartbeat_thread is not None
            and self._dashboard_heartbeat_thread.is_alive()
        ):
            self._dashboard_heartbeat_thread.join(timeout=1.0)

    def _dashboard_heartbeat_loop(self) -> None:
        self._write_dashboard_heartbeat()
        while not self._dashboard_heartbeat_stop.wait(self._dashboard_heartbeat_seconds):
            self._write_dashboard_heartbeat()

    def _write_dashboard_heartbeat(self) -> None:
        """Schrijf dashboard-liveness los van scheduler ticks en agent-logica."""
        now = datetime.now(tz=timezone.utc)
        try:
            seconds_since_last_tick = self.seconds_since_last_tick()
        except Exception:
            seconds_since_last_tick = None
        self._append_to_log(
            self._scheduler_log_path,
            {
                "event_type": "dashboard_heartbeat",
                "sequence": self._tick_sequence,
                "timestamp": now.isoformat(),
                "scheduler_status": self._status.value,
                "last_tick_completed_at": self._last_tick_completed_at.isoformat(),
                "seconds_since_last_tick": round(seconds_since_last_tick, 3)
                if seconds_since_last_tick is not None
                else None,
            },
        )

    # ------------------------------------------------------------------
    # Internal — scheduler liveness watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        if self._watchdog_threshold_seconds <= 0:
            return
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="colony-scheduler-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=1.0)

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_check_seconds):
            self._watchdog_recover_if_stale()

    def _watchdog_recover_if_stale(self) -> bool:
        if self._status != ColonyStatus.RUNNING or self._watchdog_threshold_seconds <= 0:
            return False

        seconds = self.seconds_since_last_tick()
        if seconds <= self._watchdog_threshold_seconds:
            return False

        now = datetime.now(tz=timezone.utc)
        if (
            self._last_watchdog_recovery_at is not None
            and (now - self._last_watchdog_recovery_at).total_seconds() < self._watchdog_threshold_seconds
        ):
            return False
        self._last_watchdog_recovery_at = now

        logger.error(
            "Scheduler watchdog: Colony tick %.1fs stale (> %ds) — herstart tick-cyclus.",
            seconds,
            self._watchdog_threshold_seconds,
        )
        self._append_to_log(
            self._scheduler_log_path,
            {
                "event_type": "scheduler_watchdog_stale",
                "sequence": self._tick_sequence,
                "timestamp": now.isoformat(),
                "seconds_since_last_tick": seconds,
                "threshold_seconds": self._watchdog_threshold_seconds,
                "last_tick_started_at": self._last_tick_started_at.isoformat() if self._last_tick_started_at else None,
                "last_tick_completed_at": self._last_tick_completed_at.isoformat(),
            },
        )

        if not self._tick_lock.acquire(blocking=False):
            logger.error("Scheduler watchdog: vorige tick loopt nog — geen concurrente tick gestart.")
            return False
        self._tick_lock.release()

        try:
            self.tick()
            return True
        except Exception as exc:
            logger.exception("Scheduler watchdog recovery tick faalde.")
            self._log_scheduler_error("scheduler_watchdog_recovery_error", exc)
            return False

    def _log_scheduler_error(self, event_type: str, exc: BaseException) -> None:
        self._append_to_log(
            self._scheduler_log_path,
            {
                "event_type": event_type,
                "sequence": self._tick_sequence,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "error": repr(exc),
            },
        )
