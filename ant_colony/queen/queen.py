"""
ant_colony/queen/queen.py

Queen — soevereine centrale autoriteit van de ANT COLONY.

Verantwoordelijkheden:
  1. Missions uitgeven met kapitaalvalidatie en audit trail
  2. Kolonie-niveau kapitaalhiërarchie handhaven
  3. Kill-switch activeren op alle drie niveaus (via ColonyScheduler)
  4. Missions intrekken en kapitaal vrijgeven

Kapitaalhiërarchie:
  capital_total     = vast bij instantiatie (door Operator)
  capital_allocated = som(mission.capital_limit) over actieve missions
  capital_available = max(0, capital_total - capital_allocated)

Regels:
  - Queen is de enige autoriteit die missions mag uitgeven (P1)
  - Mission met capital_limit > capital_available wordt geweigerd
  - Kill-switch delegeert naar ColonyScheduler, gevolgd door audit log
  - Alle acties worden append-only gelogd naar ANT_LOGS
  - Geen code wordt uitgevoerd bij import (P7)

Audit logs:
  - Mission issued/rejected/aborted → ANT_LOGS/missions/{mission_id}.jsonl
  - Kill-switch activated           → ANT_LOGS/colony/kill_switch.jsonl
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, KillLevel
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.mission import Mission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mission issue resultaat
# ---------------------------------------------------------------------------

class MissionRejectionReason(str, Enum):
    CAPITAL_EXCEEDED = "capital_exceeded"
    DUPLICATE_MISSION_ID = "duplicate_mission_id"


@dataclass
class MissionIssueResult:
    """
    Resultaat van een issue_mission()-aanroep.

    accepted:          True als de mission is uitgegeven.
    mission_id:        mission_id van de mission (ook bij afwijzing beschikbaar).
    rejection_reason:  Reden van afwijzing, of None bij acceptatie.
    rejection_detail:  Mensleesbare toelichting.
    """
    accepted: bool
    mission_id: str | None = None
    rejection_reason: MissionRejectionReason | None = None
    rejection_detail: str = ""

    @classmethod
    def rejected(
        cls,
        reason: MissionRejectionReason,
        mission_id: str | None = None,
        detail: str = "",
    ) -> MissionIssueResult:
        return cls(
            accepted=False,
            mission_id=mission_id,
            rejection_reason=reason,
            rejection_detail=detail,
        )


# ---------------------------------------------------------------------------
# Queen
# ---------------------------------------------------------------------------

class Queen:
    """
    Soevereine centrale autoriteit van de ANT COLONY.

    Args:
        capital_total:  Totaal kolonie-kapitaal (door Operator vastgesteld).
        scheduler:      ColonyScheduler voor mission dispatch en kill-switch.
        logs_root:      Root van ANT_LOGS. None = geen disk-logging (tests).

    Usage::

        queen = Queen(capital_total=50_000.0, scheduler=scheduler, logs_root=logs_root)

        result = queen.issue_mission(mission)
        if result.accepted:
            print("Mission issued:", result.mission_id)

        queen.kill_switch(KillLevel.COLONY)
    """

    def __init__(
        self,
        capital_total: float,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
    ) -> None:
        if capital_total < 0:
            raise ValueError(f"capital_total must be >= 0, got {capital_total}")
        self._capital_total = capital_total
        self._scheduler = scheduler
        self._logs_root = logs_root
        self._active_missions: dict[str, Mission] = {}
        self._log_sequence: int = 0

    # ------------------------------------------------------------------
    # Kapitaal
    # ------------------------------------------------------------------

    @property
    def capital_total(self) -> float:
        """Totaal kolonie-kapitaal — vastgelegd bij instantiatie."""
        return self._capital_total

    @property
    def capital_allocated(self) -> float:
        """Som van capital_limit over alle actieve missions."""
        return sum(m.capital_limit for m in self._active_missions.values())

    @property
    def capital_available(self) -> float:
        """Vrij te alloceren kapitaal."""
        return max(0.0, self._capital_total - self.capital_allocated)

    # ------------------------------------------------------------------
    # Missions
    # ------------------------------------------------------------------

    @property
    def active_missions(self) -> dict[str, Mission]:
        """Snapshot van actieve missions (kopie)."""
        return dict(self._active_missions)

    def issue_mission(self, mission: Mission) -> MissionIssueResult:
        """
        Geef een mission uit.

        Controleert duplicate mission_id en kapitaalbeschikbaarheid.
        Bij acceptatie: registreert de mission, enqueut haar bij de scheduler
        en schrijft een audit event.

        Args:
            mission:  Volledig gevalideerde Mission. Het schema dwingt
                      issued_by="queen" af — een mission van een andere bron
                      wordt nooit geaccepteerd.

        Returns:
            MissionIssueResult — nooit een exception voor zakelijke afwijzingen.
        """
        # --- validatie: duplicate ---
        if mission.mission_id in self._active_missions:
            detail = f"mission_id={mission.mission_id} already active"
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.DUPLICATE_MISSION_ID,
                mission_id=mission.mission_id,
                detail=detail,
            )
            self._log_mission_event(
                AuditEventType.MISSION_REJECTED, mission,
                extra={"rejection_reason": result.rejection_reason},
            )
            return result

        # --- validatie: kapitaal ---
        if mission.capital_limit > self.capital_available:
            detail = (
                f"capital_limit={mission.capital_limit:.2f} "
                f"> capital_available={self.capital_available:.2f}"
            )
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.CAPITAL_EXCEEDED,
                mission_id=mission.mission_id,
                detail=detail,
            )
            self._log_mission_event(
                AuditEventType.MISSION_REJECTED, mission,
                extra={"rejection_reason": result.rejection_reason},
            )
            return result

        # --- acceptatie ---
        self._active_missions[mission.mission_id] = mission
        self._scheduler.enqueue_mission(mission)
        logger.info(
            "Mission issued: %s (ant_type=%s capital=%.2f node=%s)",
            mission.mission_id,
            mission.ant_type,
            mission.capital_limit,
            mission.allowed_node,
        )
        self._log_mission_event(AuditEventType.MISSION_ISSUED, mission)
        return MissionIssueResult(accepted=True, mission_id=mission.mission_id)

    def revoke_mission(self, mission_id: str) -> None:
        """
        Trek een actieve mission in en geef het kapitaal vrij.

        Verwijdert de mission uit de actieve set en schrijft een audit event.
        Negeert onbekende mission_ids (idempotent).
        """
        mission = self._active_missions.pop(mission_id, None)
        if mission is None:
            logger.warning(
                "revoke_mission: mission_id=%s not in active missions — ignoring",
                mission_id,
            )
            return
        logger.info("Mission revoked: %s (capital freed: %.2f)", mission_id, mission.capital_limit)
        self._log_mission_event(AuditEventType.MISSION_ABORTED, mission)

    # ------------------------------------------------------------------
    # Kill-switch
    # ------------------------------------------------------------------

    def kill_switch(self, level: KillLevel, scope: str | None = None) -> None:
        """
        Activeer de kill-switch.

        Delegeert naar ColonyScheduler en schrijft een audit event naar
        ANT_LOGS/colony/kill_switch.jsonl.

        Args:
            level:  KillLevel.AGENT (1) — één agent.
                    KillLevel.NODE  (2) — alle agents op één node.
                    KillLevel.COLONY(3) — hele kolonie; vereist handmatige herstart.
            scope:  ant_id bij Level 1, node_id bij Level 2; genegeerd bij Level 3.
        """
        logger.warning(
            "Kill-switch activated: level=%s scope=%s", level.name, scope
        )
        self._scheduler.kill_switch(level, scope)
        self._log_kill_event(level, scope)

    # ------------------------------------------------------------------
    # Intern — logging
    # ------------------------------------------------------------------

    def _next_sequence(self) -> int:
        self._log_sequence += 1
        return self._log_sequence

    def _log_mission_event(
        self,
        event_type: AuditEventType,
        mission: Mission,
        extra: dict | None = None,
    ) -> None:
        if self._logs_root is None:
            return
        payload = {
            "ant_type": mission.ant_type,
            "allowed_node": mission.allowed_node,
            "capital_limit": mission.capital_limit,
            **(extra or {}),
        }
        event = AuditEvent(
            event_type=event_type,
            source="queen",
            mission_id=mission.mission_id,
            payload=payload,
            sequence=self._next_sequence(),
        )
        log_path = self._logs_root / "missions" / f"{mission.mission_id}.jsonl"
        self._append_to_log(log_path, event.model_dump(mode="json"))

    def _log_kill_event(self, level: KillLevel, scope: str | None) -> None:
        if self._logs_root is None:
            return
        event = AuditEvent(
            event_type=AuditEventType.KILL_SWITCH_ACTIVATED,
            source="queen",
            payload={"level": level.value, "scope": scope},
            sequence=self._next_sequence(),
        )
        log_path = self._logs_root / "colony" / "kill_switch.jsonl"
        self._append_to_log(log_path, event.model_dump(mode="json"))

    def _append_to_log(self, path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write audit log: %s", path)
