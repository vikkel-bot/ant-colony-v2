"""
ant_colony/queen/queen.py

Queen — soevereine centrale autoriteit van de ANT COLONY.

Verantwoordelijkheden:
  1. Missions uitgeven met kapitaalvalidatie en audit trail
  2. Kolonie-niveau kapitaalhiërarchie handhaven
  3. Kill-switch activeren op alle drie niveaus (via ColonyScheduler)
  4. Missions intrekken en kapitaal vrijgeven
  5. StrategyCandidate promoveren of afwijzen (enige autoriteit)
  6. AllocationPlan toepassen en allocatiestaat inzichtelijk maken

Kapitaalhiërarchie:
  capital_total     = vast bij instantiatie (door Operator)
  capital_allocated = som(mission.capital_limit) over actieve missions
  capital_available = max(0, capital_total - capital_allocated)

Promotieketen (COLONY_GOVERNANCE.md §4):
  RESEARCH → PAPER    (backtest criteria)
  PAPER    → APPROVED (paper criteria, approved_by="queen" gezet)
  APPROVED → LIVE     (approved_by="queen" vereist)
  Elk stadium → REJECTED (Queen kan altijd afwijzen)

Regels:
  - Queen is de enige autoriteit die missions mag uitgeven (P1)
  - Queen is de enige die StrategyCandidate mag promoveren (P1)
  - Promotie gaat alleen vooruit — nooit terugzetten zonder Queen actie
  - Kill-switch delegeert naar ColonyScheduler, gevolgd door audit log
  - Alle acties worden append-only gelogd naar ANT_LOGS
  - Geen code wordt uitgevoerd bij import (P7)

Audit logs:
  - Mission issued/rejected/aborted  → ANT_LOGS/missions/{mission_id}.jsonl
  - Kill-switch activated            → ANT_LOGS/colony/kill_switch.jsonl
  - Candidate promoted/rejected      → ANT_LOGS/strategy/{candidate_id}.jsonl
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ant_colony.colony.node_registry import NodeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, KillLevel
from ant_colony.lab.promotion_criteria import AssessmentResult, PromotionCriteria
from ant_colony.queen.allocator import (
    AllocationPlan,
    AllocationResult,
    AllocationSnapshot,
    BiomeAllocationState,
)
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.node import Node
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)

# Geldige voorwaartse promotie-stappen (COLONY_GOVERNANCE.md §4)
_VALID_PROMOTIONS: dict[CandidateStatus, CandidateStatus] = {
    CandidateStatus.RESEARCH: CandidateStatus.PAPER,
    CandidateStatus.PAPER:    CandidateStatus.APPROVED,
    CandidateStatus.APPROVED: CandidateStatus.LIVE,
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mission issue resultaat
# ---------------------------------------------------------------------------

class MissionRejectionReason(str, Enum):
    CAPITAL_EXCEEDED = "capital_exceeded"
    DUPLICATE_MISSION_ID = "duplicate_mission_id"
    BIOME_CAPITAL_EXCEEDED = "biome_capital_exceeded"
    NODE_NOT_TRUSTED = "node_not_trusted"
    ANT_TYPE_NOT_ALLOWED = "ant_type_not_allowed"
    BIOME_NOT_ALLOWED_ON_NODE = "biome_not_allowed_on_node"


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
# Promotie resultaat
# ---------------------------------------------------------------------------

@dataclass
class PromotionResult:
    """
    Resultaat van een promote_candidate()-aanroep.

    accepted:          True als de promotie is doorgevoerd.
    candidate:         Nieuw StrategyCandidate met bijgewerkte status en provenance.
                       None bij afwijzing.
    rejection_reason:  Mensleesbare reden bij afwijzing.
    """
    accepted: bool
    candidate: StrategyCandidate | None = None
    rejection_reason: str = ""


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
        self._biome_limits: dict[str, float] = {}
        self._node_registry: NodeRegistry = NodeRegistry()
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
    # Per-biome kapitaal
    # ------------------------------------------------------------------

    def set_biome_capital(self, biome_id: str, capital_limit: float) -> None:
        """
        Stel een kapitaallimiet in voor één biome.

        Validaties:
          - capital_limit >= 0
          - capital_limit <= colony capital_total (biome is een subset van de kolonie)

        Args:
            biome_id:      Biome-identifier (bijv. "crypto", "equities").
            capital_limit: Maximaal te alloceren kapitaal binnen dit biome.
        """
        if capital_limit < 0:
            raise ValueError(
                f"capital_limit must be >= 0, got {capital_limit}"
            )
        if capital_limit > self._capital_total:
            raise ValueError(
                f"capital_limit={capital_limit:.2f} exceeds colony capital_total={self._capital_total:.2f}"
            )
        self._biome_limits[biome_id] = capital_limit
        logger.debug(
            "Biome capital set: biome_id='%s' limit=%.2f", biome_id, capital_limit
        )

    def biome_capital_limit(self, biome_id: str) -> float | None:
        """
        Geef de ingestelde limiet voor biome_id, of None als er geen limiet is.
        """
        return self._biome_limits.get(biome_id)

    def biome_capital_allocated(self, biome_id: str) -> float:
        """
        Som van capital_limit over alle actieve missions in het opgegeven biome.
        """
        return sum(
            m.capital_limit
            for m in self._active_missions.values()
            if m.market_scope.biome == biome_id
        )

    def biome_capital_available(self, biome_id: str) -> float | None:
        """
        Beschikbaar kapitaal voor biome_id.

        Returns:
            None  — geen biome-limiet ingesteld (onbeperkt op biome-niveau).
            float — max(0, limit - allocated).
        """
        limit = self._biome_limits.get(biome_id)
        if limit is None:
            return None
        return max(0.0, limit - self.biome_capital_allocated(biome_id))

    # ------------------------------------------------------------------
    # Node-governance (P1: Queen is enige die nodes vertrouwt)
    # ------------------------------------------------------------------

    def register_node(self, node: Node) -> None:
        """
        Registreer een node als vertrouwd.

        Delegeert naar NodeRegistry. Hot-swap is toegestaan — een bestaande
        node met hetzelfde node_id wordt overschreven met een waarschuwing.

        Args:
            node: Volledig geconfigureerde Node met node_id, allowed_biomes,
                  allowed_ant_types en heartbeat_interval.
        """
        self._node_registry.register(node)
        logger.info(
            "Node registered: node_id='%s' biomes=%s ant_types=%s",
            node.node_id, node.allowed_biomes, node.allowed_ant_types,
        )

    def unregister_node(self, node_id: str) -> None:
        """
        Verwijder een node uit het vertrouwde register.

        Idempotent: onbekend node_id wordt genegeerd.
        """
        self._node_registry.unregister(node_id)
        logger.info("Node unregistered: node_id='%s'", node_id)

    def trusted_nodes(self) -> list[str]:
        """Gesorteerde lijst van actieve (vertrouwde) node_ids."""
        return self._node_registry.list_trusted()

    def apply_allocation_plan(self, plan: AllocationPlan) -> AllocationResult:
        """
        Pas een AllocationPlan toe: vertaal fracties naar absolute biome-limieten.

        Voor elke biome_id in het plan wordt fraction × capital_total berekend
        en via set_biome_capital() ingesteld. Bestaande limieten voor biomes
        die niet in het plan staan worden niet geraakt.

        Fail-closed: als één biome-limiet de validatie van set_biome_capital()
        niet doorstaat (bijv. negatief of > capital_total), wordt het plan
        volledig afgewezen en wordt geen enkele limiet gewijzigd (atomair).

        Args:
            plan: AllocationPlan met fracties per biome.

        Returns:
            AllocationResult — nooit een exception voor zakelijke afwijzingen.
        """
        # Bereken absolute limieten vooraf — valideer alles vóór toepassen
        computed: dict[str, float] = {}
        for biome_id, fraction in plan.allocations.items():
            limit = fraction * self._capital_total
            if limit < 0 or limit > self._capital_total:
                # Dit kan alleen bij float edge cases; defensieve check
                reason = (
                    f"computed limit={limit:.2f} for biome '{biome_id}' "
                    f"out of range [0, {self._capital_total:.2f}]"
                )
                logger.warning("AllocationPlan rejected: %s", reason)
                return AllocationResult.rejected(reason)
            computed[biome_id] = limit

        # Alles valide — pas toe
        for biome_id, limit in computed.items():
            self._biome_limits[biome_id] = limit
            logger.debug(
                "AllocationPlan: set biome_id='%s' limit=%.2f (fraction=%.4f)",
                biome_id, limit, plan.allocations[biome_id],
            )

        logger.info(
            "AllocationPlan applied: %d biomes, total_fraction=%.4f, unallocated=%.4f",
            len(computed),
            plan.total_fraction,
            plan.unallocated_fraction,
        )
        return AllocationResult(applied=True, biome_limits=dict(computed))

    def allocation_snapshot(self) -> AllocationSnapshot:
        """
        Geef een point-in-time weergave van de volledige kapitaalallocatie.

        Bevat colony-totalen en de staat van alle biomes waarvoor een limiet
        is ingesteld. Wordt altijd vers berekend — nooit gecached (P4).

        Returns:
            AllocationSnapshot met colony-totalen en per-biome staat.
        """
        biome_states: list[BiomeAllocationState] = []
        for biome_id, limit in sorted(self._biome_limits.items()):
            allocated = self.biome_capital_allocated(biome_id)
            available = max(0.0, limit - allocated)
            utilization_pct = (allocated / limit * 100.0) if limit > 0 else 0.0
            biome_states.append(BiomeAllocationState(
                biome_id=biome_id,
                limit=limit,
                allocated=allocated,
                available=available,
                utilization_pct=utilization_pct,
            ))

        return AllocationSnapshot(
            colony_total=self._capital_total,
            colony_allocated=self.capital_allocated,
            colony_available=self.capital_available,
            biomes=biome_states,
        )

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

        # --- validatie: node vertrouwd ---
        node_id = mission.allowed_node
        if not self._node_registry.is_trusted(node_id):
            detail = f"node='{node_id}' is not registered or not ACTIVE"
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.NODE_NOT_TRUSTED,
                mission_id=mission.mission_id,
                detail=detail,
            )
            self._log_mission_event(
                AuditEventType.MISSION_REJECTED, mission,
                extra={"rejection_reason": result.rejection_reason},
            )
            return result

        # --- validatie: ant_type toegestaan op node ---
        if not self._node_registry.can_run_ant(node_id, mission.ant_type):
            detail = (
                f"ant_type='{mission.ant_type}' not in allowed_ant_types "
                f"for node='{node_id}'"
            )
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.ANT_TYPE_NOT_ALLOWED,
                mission_id=mission.mission_id,
                detail=detail,
            )
            self._log_mission_event(
                AuditEventType.MISSION_REJECTED, mission,
                extra={"rejection_reason": result.rejection_reason},
            )
            return result

        # --- validatie: biome toegestaan op node ---
        biome_id = mission.market_scope.biome
        if not self._node_registry.can_run_biome(node_id, biome_id):
            detail = (
                f"biome='{biome_id}' not in allowed_biomes "
                f"for node='{node_id}'"
            )
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.BIOME_NOT_ALLOWED_ON_NODE,
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

        # --- validatie: biome-kapitaal ---
        biome_avail = self.biome_capital_available(biome_id)
        if biome_avail is not None and mission.capital_limit > biome_avail:
            detail = (
                f"capital_limit={mission.capital_limit:.2f} "
                f"> biome_capital_available[{biome_id}]={biome_avail:.2f}"
            )
            logger.warning("Mission rejected: %s", detail)
            result = MissionIssueResult.rejected(
                MissionRejectionReason.BIOME_CAPITAL_EXCEEDED,
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
    # StrategyCandidate promotie
    # ------------------------------------------------------------------

    def promote_candidate(
        self,
        candidate: StrategyCandidate,
        to_status: CandidateStatus,
        criteria: PromotionCriteria | None = None,
    ) -> PromotionResult:
        """
        Promoveer een StrategyCandidate naar de volgende status in de keten.

        Valideert:
          1. Huidige status is niet terminaal (REJECTED of LIVE)
          2. Transitie is geldig en voorwaarts (RESEARCH→PAPER, PAPER→APPROVED, APPROVED→LIVE)
          3. Criteria.assess() slaagt als criteria zijn meegegeven

        Bij acceptatie:
          - Nieuwe ProvenanceEntry toegevoegd (actor="queen")
          - approved_by="queen" gezet bij APPROVED of LIVE
          - Audit event gelogd naar ANT_LOGS/strategy/{candidate_id}.jsonl

        Args:
            candidate:  Te promoveren StrategyCandidate (origineel ongewijzigd).
            to_status:  Doelstatus.
            criteria:   Optionele PromotionCriteria — als None: alleen transitiecheck.

        Returns:
            PromotionResult — nooit een exception voor zakelijke afwijzingen.
        """
        # --- terminale status ---
        if candidate.status in (CandidateStatus.REJECTED, CandidateStatus.LIVE):
            reason = f"cannot promote from terminal status '{candidate.status.value}'"
            logger.warning("Promotion rejected: %s (%s)", candidate.candidate_id, reason)
            return PromotionResult(accepted=False, rejection_reason=reason)

        # --- geldige transitie ---
        expected = _VALID_PROMOTIONS.get(candidate.status)
        if expected != to_status:
            reason = (
                f"invalid promotion {candidate.status.value} → {to_status.value} "
                f"(expected → {expected.value if expected else 'none'})"
            )
            logger.warning("Promotion rejected: %s (%s)", candidate.candidate_id, reason)
            return PromotionResult(accepted=False, rejection_reason=reason)

        # --- criteria ---
        if criteria is not None:
            assessment = criteria.assess(candidate)
            if not assessment.passed:
                reason = f"criteria not met: {assessment.reason}"
                logger.info("Promotion rejected by criteria: %s (%s)", candidate.candidate_id, reason)
                return PromotionResult(accepted=False, rejection_reason=reason)

        # --- bouw nieuwe candidate ---
        new_entry = ProvenanceEntry(
            actor="queen",
            action=f"promoted_to_{to_status.value}",
            details={
                "from_status": candidate.status.value,
                "criteria_applied": criteria is not None,
            },
        )
        updates: dict = {
            "status": to_status,
            "provenance": list(candidate.provenance) + [new_entry],
        }
        if to_status in (CandidateStatus.APPROVED, CandidateStatus.LIVE):
            updates["approved_by"] = "queen"

        promoted = candidate.model_copy(update=updates)
        logger.info(
            "Candidate promoted: %s %s → %s",
            promoted.candidate_id, candidate.status.value, to_status.value,
        )
        self._log_candidate_event(
            AuditEventType.STRATEGY_CANDIDATE_PROMOTED, promoted,
            extra={"from_status": candidate.status.value, "to_status": to_status.value},
        )
        return PromotionResult(accepted=True, candidate=promoted)

    def reject_candidate(
        self,
        candidate: StrategyCandidate,
        reason: str = "",
    ) -> StrategyCandidate:
        """
        Wijs een StrategyCandidate af op elk moment in de keten.

        Idempotent: als de candidate al REJECTED is, wordt hij ongewijzigd teruggegeven.
        ProvenanceEntry met reden wordt toegevoegd (append-only).

        Args:
            candidate:  Te verwerpen StrategyCandidate (origineel ongewijzigd).
            reason:     Mensleesbare reden van afwijzing.

        Returns:
            Nieuw StrategyCandidate met status=REJECTED en bijgewerkte provenance.
        """
        if candidate.status == CandidateStatus.REJECTED:
            logger.warning(
                "reject_candidate: %s already rejected — ignoring", candidate.candidate_id
            )
            return candidate

        new_entry = ProvenanceEntry(
            actor="queen",
            action="rejected",
            details={"reason": reason, "from_status": candidate.status.value},
        )
        rejected = candidate.model_copy(update={
            "status": CandidateStatus.REJECTED,
            "provenance": list(candidate.provenance) + [new_entry],
        })
        logger.info(
            "Candidate rejected: %s (from=%s reason=%s)",
            rejected.candidate_id, candidate.status.value, reason,
        )
        self._log_candidate_event(
            AuditEventType.STRATEGY_CANDIDATE_REJECTED, rejected,
            extra={"reason": reason, "from_status": candidate.status.value},
        )
        return rejected

    # ------------------------------------------------------------------
    # Advisor integratie
    # ------------------------------------------------------------------

    def apply_advisor_decision(self, decision) -> None:
        """
        Verwerk een QueenDecision van QueenAdvisor.

        Queen heeft altijd veto: acties worden gelogd; allocatie-aanpassingen
        worden doorgevoerd als het plan geldig is. kapitaal_verhogen/verlagen
        worden als aanbevelingen gelogd (mission capital is immutable).

        Args:
            decision: QueenDecision van QueenAdvisor.advise().
        """
        from ant_colony.queen.allocator import AllocationPlan

        if decision.is_empty():
            logger.debug("apply_advisor_decision: lege beslissing — niets te doen")
            return

        # Allocatie aanpassen als het plan adviezen bevat
        alloc_result = None
        if decision.allocatie_aanpassingen:
            try:
                plan = AllocationPlan(allocations=decision.allocatie_aanpassingen)
                alloc_result = self.apply_allocation_plan(plan)
                logger.info(
                    "Advisor allocatie toegepast: applied=%s biomes=%s",
                    alloc_result.applied,
                    list(decision.allocatie_aanpassingen.keys()),
                )
            except Exception:
                logger.exception("Advisor allocatie kon niet worden toegepast")

        # Log de volledige beslissing
        self._log_advisor_decision(decision, alloc_result)

    def _log_advisor_decision(self, decision, alloc_result=None) -> None:
        if self._logs_root is None:
            return
        record = {
            "timestamp":               datetime.now(tz=timezone.utc).isoformat(),
            "allocatie_aanpassingen":  decision.allocatie_aanpassingen,
            "prioriteit_kandidaten":   decision.prioriteit_kandidaten,
            "deprioriteer_kandidaten": decision.deprioriteer_kandidaten,
            "kapitaal_verhogen":       decision.kapitaal_verhogen,
            "kapitaal_verlagen":       decision.kapitaal_verlagen,
            "adviezen_gevolgd":        decision.adviezen_gevolgd,
            "adviezen_genegeerd":      decision.adviezen_genegeerd,
            "allocatie_toegepast":     alloc_result.applied if alloc_result else None,
        }
        log_path = self._logs_root / "queen" / "decisions.jsonl"
        self._append_to_log(log_path, record)
        logger.info(
            "Advisor beslissing gelogd | gevolgd=%d genegeerd=%d verhogen=%d verlagen=%d",
            len(decision.adviezen_gevolgd),
            len(decision.adviezen_genegeerd),
            len(decision.kapitaal_verhogen),
            len(decision.kapitaal_verlagen),
        )

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

    def _log_candidate_event(
        self,
        event_type: AuditEventType,
        candidate: StrategyCandidate,
        extra: dict | None = None,
    ) -> None:
        if self._logs_root is None:
            return
        payload = {
            "candidate_id": candidate.candidate_id,
            "name": candidate.name,
            "status": candidate.status.value,
            "biome": candidate.biome,
            **(extra or {}),
        }
        event = AuditEvent(
            event_type=event_type,
            source="queen",
            payload=payload,
            sequence=self._next_sequence(),
        )
        log_path = self._logs_root / "strategy" / f"{candidate.candidate_id}.jsonl"
        self._append_to_log(log_path, event.model_dump(mode="json"))

    def _append_to_log(self, path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write audit log: %s", path)
