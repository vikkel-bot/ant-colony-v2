"""
ant_colony/ants/audit_ant.py

AuditAnt — bewaakt de integriteit van de colony en rapporteert anomalieën.

Verantwoordelijkheden:
  1. Scheduler heartbeat — detecteert stilstaande scheduler
     (laatste tick ouder dan 2× tick_interval)
  2. Audit trail integriteit — detecteert sequence-gaps in alle JSONL logs
  3. Positie-leeftijd — waarschuwt bij BUY-artifacts ouder dan max_position_age
  4. Colony v1 heartbeat — waarschuwt als v1 heartbeat.json ouder is dan 10 min
  5. Alle bevindingen loggen naar ANT_LOGS/audit/{ant_id}.jsonl
  6. Heartbeat rapporteren aan scheduler na elke tick
  7. Zichzelf netjes beëindigen bij TTL expiry

Severity niveaus:
  INFO    — normale bevinding, alles in orde
  WARNING — lichte afwijking, geen actie vereist maar let op
  ANOMALY — ernstige afwijking, operator-aandacht gewenst

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_V1_HEARTBEAT_MAX_AGE_SEC: int = 600     # 10 minuten
_SCHEDULER_STALE_FACTOR: int   = 2       # 2× tick_interval


@dataclass
class AuditFinding:
    """Één bevinding uit een audit check."""
    severity:   str    # "INFO" | "WARNING" | "ANOMALY"
    component:  str    # bijv. "scheduler", "audit_trail", "positions", "v1_heartbeat"
    check_name: str
    detail:     str


class AuditAnt:
    """
    Bewaakt de integriteit van de colony; logt bevindingen als JSON.

    Args:
        ant_id:                  Unieke identifier (UUID-string).
        mission:                 Toegewezen Mission met scope, TTL en heartbeat-interval.
        scheduler:               ColonyScheduler voor heartbeat-registratie.
        biome_registry:          BiomeRegistry (voor toekomstige uitbreiding).
        logs_root:               Pad naar ANT_LOGS; None schakelt disk-logging uit.
        live_root:               Pad naar ANT_LIVE voor broker artifacts en v1 heartbeat.
                                 Standaard: C:\\Trading\\ANT_LIVE.
        scheduler_tick_interval: Verwacht tick-interval van de scheduler in seconden.
                                 Wordt gebruikt om staleness te berekenen.
        max_position_age_days:   Maximale leeftijd van een open BUY-artifact in dagen.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        live_root: Path | None = None,
        scheduler_tick_interval: int = 5,
        max_position_age_days: int = 7,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root
        self.live_root      = live_root or Path(r"C:\Trading\ANT_LIVE")

        self._scheduler_tick_interval = scheduler_tick_interval
        self._max_position_age        = timedelta(days=max_position_age_days)

        self._status: AntStatus = AntStatus.IDLE
        self._budget_used: float = 0.0
        self._last_action: str = "init"
        self._log_seq: int = 0

        self._log = logging.getLogger(f"ant.audit.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Blokkerende tick-loop. Retourneert AntStatus bij afsluiting.

        Elke tick:
          1. TTL controleren
          2. Alle vier audit checks uitvoeren
          3. Bevindingen loggen
          4. Heartbeat sturen
          5. Wachten tot volgende tick (heartbeat_interval)
        """
        self._status = AntStatus.RUNNING
        self._log.info(
            "AuditAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id,
            self.mission.ttl,
        )

        started_at     = datetime.now(tz=timezone.utc)
        last_heartbeat = started_at

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info(
                        "TTL verlopen (%.1fs / %ds) — afsluiten", elapsed, self.mission.ttl
                    )
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("AuditAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("AuditAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Voer alle vier audit checks uit en log de bevindingen."""
        findings: list[AuditFinding] = []

        findings.extend(self._check_scheduler_heartbeat())
        findings.extend(self._check_audit_trail_integrity())
        findings.extend(self._check_open_position_age())
        findings.extend(self._check_v1_heartbeat())

        for finding in findings:
            self._emit(finding)

        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Check 1: Scheduler heartbeat
    # ------------------------------------------------------------------

    def _check_scheduler_heartbeat(self) -> list[AuditFinding]:
        """
        Leest de laatste regel van scheduler.jsonl en controleert de timestamp.

        Retourneert een ANOMALY finding als de laatste tick ouder is dan
        2× scheduler_tick_interval. Retourneert INFO als alles in orde is.
        Retourneert [] als het logbestand ontbreekt of niet leesbaar is.
        """
        if self.logs_root is None:
            return []

        scheduler_log = self.logs_root / "colony" / "scheduler.jsonl"
        if not scheduler_log.exists():
            return [AuditFinding(
                severity="WARNING",
                component="scheduler",
                check_name="scheduler_heartbeat",
                detail=f"Scheduler log niet gevonden: {scheduler_log}",
            )]

        try:
            last_line = _read_last_nonempty_line(scheduler_log)
            if last_line is None:
                return [AuditFinding(
                    severity="WARNING",
                    component="scheduler",
                    check_name="scheduler_heartbeat",
                    detail="Scheduler log is leeg",
                )]

            record = json.loads(last_line)
            ts_str = record.get("timestamp")
            if not ts_str:
                return []

            last_tick = datetime.fromisoformat(ts_str)
            if last_tick.tzinfo is None:
                last_tick = last_tick.replace(tzinfo=timezone.utc)

            age_sec = (datetime.now(tz=timezone.utc) - last_tick).total_seconds()
            threshold = self._scheduler_tick_interval * _SCHEDULER_STALE_FACTOR

            if age_sec > threshold:
                return [AuditFinding(
                    severity="ANOMALY",
                    component="scheduler",
                    check_name="scheduler_heartbeat",
                    detail=(
                        f"Laatste tick {age_sec:.0f}s geleden "
                        f"(drempel {threshold}s = {_SCHEDULER_STALE_FACTOR}× interval)"
                    ),
                )]

            return [AuditFinding(
                severity="INFO",
                component="scheduler",
                check_name="scheduler_heartbeat",
                detail=f"Scheduler actief — laatste tick {age_sec:.1f}s geleden",
            )]

        except Exception:
            self._log.exception("Fout bij scheduler heartbeat check")
            return []

    # ------------------------------------------------------------------
    # Check 2: Audit trail integriteit (sequence gaps)
    # ------------------------------------------------------------------

    def _check_audit_trail_integrity(self) -> list[AuditFinding]:
        """
        Scant alle JSONL-bestanden onder logs_root op ontbrekende sequence-nummers.

        Per bestand: als er minstens twee regels met een `sequence` veld zijn,
        wordt gecontroleerd of de reeks aaneengesloten is. Bij een gap wordt
        een ANOMALY gelogd.

        Het eigen audit-logbestand wordt overgeslagen (voorkomt circulaire detectie).
        """
        if self.logs_root is None:
            return []

        findings: list[AuditFinding] = []
        own_log = self.logs_root / "audit" / f"{self.ant_id}.jsonl"

        try:
            for jsonl_path in sorted(self.logs_root.rglob("*.jsonl")):
                if jsonl_path == own_log:
                    continue

                gaps = _find_sequence_gaps(jsonl_path)
                for gap_start, gap_end in gaps:
                    findings.append(AuditFinding(
                        severity="ANOMALY",
                        component="audit_trail",
                        check_name="sequence_gap",
                        detail=(
                            f"{jsonl_path.relative_to(self.logs_root)} — "
                            f"gap tussen sequence {gap_start} en {gap_end}"
                        ),
                    ))

        except Exception:
            self._log.exception("Fout bij audit trail integriteitscheck")

        return findings

    # ------------------------------------------------------------------
    # Check 3: Positie-leeftijd
    # ------------------------------------------------------------------

    def _check_open_position_age(self) -> list[AuditFinding]:
        """
        Leest broker execution artifacts en controleert de leeftijd van BUY-fills.

        Elke unieke BUY-order ouder dan max_position_age_days krijgt een WARNING.
        Artifacten zonder `ts_utc` veld worden overgeslagen.
        """
        broker_dir = self.live_root / "live_test" / "broker"
        if not broker_dir.exists():
            return []

        findings: list[AuditFinding] = []
        now = datetime.now(tz=timezone.utc)
        seen_order_ids: set[str] = set()

        try:
            for path in sorted(broker_dir.glob("LIVE-*.json")):
                try:
                    artifact = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

                data_block = artifact.get("data") or {}
                raw_block  = data_block.get("raw") or {}

                if raw_block.get("status") != "filled":
                    continue
                if data_block.get("side", "").lower() != "buy":
                    continue

                order_id = str(raw_block.get("orderId") or raw_block.get("order_id") or "")
                if order_id and order_id in seen_order_ids:
                    continue
                if order_id:
                    seen_order_ids.add(order_id)

                ts_str = artifact.get("ts_utc")
                if not ts_str:
                    continue

                try:
                    filled_at = datetime.fromisoformat(str(ts_str))
                    if filled_at.tzinfo is None:
                        filled_at = filled_at.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue

                age = now - filled_at
                if age > self._max_position_age:
                    market = data_block.get("market", "?")
                    findings.append(AuditFinding(
                        severity="WARNING",
                        component="positions",
                        check_name="position_age",
                        detail=(
                            f"Open BUY {market} (order {order_id or path.name}) "
                            f"is {age.days} dagen oud "
                            f"(max {self._max_position_age.days} dagen)"
                        ),
                    ))

        except Exception:
            self._log.exception("Fout bij positie-leeftijdscheck")

        return findings

    # ------------------------------------------------------------------
    # Check 4: Colony v1 heartbeat
    # ------------------------------------------------------------------

    def _check_v1_heartbeat(self) -> list[AuditFinding]:
        """
        Leest ANT_LIVE/heartbeat.json en controleert de leeftijd.

        WARNING als het bestand ouder is dan _V1_HEARTBEAT_MAX_AGE_SEC (10 min).
        Retourneert [] als het bestand niet bestaat (v1 mogelijk niet actief).
        """
        hb_path = self.live_root / "heartbeat.json"
        if not hb_path.exists():
            return []

        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            ts_str = hb.get("ts_utc") or hb.get("timestamp")
            if not ts_str:
                return [AuditFinding(
                    severity="WARNING",
                    component="v1_heartbeat",
                    check_name="v1_heartbeat_age",
                    detail="heartbeat.json heeft geen timestamp veld",
                )]

            hb_time = datetime.fromisoformat(str(ts_str))
            if hb_time.tzinfo is None:
                hb_time = hb_time.replace(tzinfo=timezone.utc)

            age_sec = (datetime.now(tz=timezone.utc) - hb_time).total_seconds()

            if age_sec > _V1_HEARTBEAT_MAX_AGE_SEC:
                return [AuditFinding(
                    severity="WARNING",
                    component="v1_heartbeat",
                    check_name="v1_heartbeat_age",
                    detail=(
                        f"Colony v1 heartbeat is {age_sec:.0f}s oud "
                        f"(max {_V1_HEARTBEAT_MAX_AGE_SEC}s)"
                    ),
                )]

            return [AuditFinding(
                severity="INFO",
                component="v1_heartbeat",
                check_name="v1_heartbeat_age",
                detail=f"Colony v1 actief — heartbeat {age_sec:.1f}s geleden",
            )]

        except Exception:
            self._log.exception("Fout bij v1 heartbeat check")
            return []

    # ------------------------------------------------------------------
    # Emissie en logging
    # ------------------------------------------------------------------

    def _emit(self, finding: AuditFinding) -> None:
        """Log een AuditFinding naar disk en update last_action."""
        self._last_action = f"audit:{finding.severity.lower()}:{finding.check_name}"

        log_level = logging.WARNING if finding.severity != "INFO" else logging.DEBUG
        self._log.log(
            log_level,
            "[%s] %s/%s — %s",
            finding.severity, finding.component, finding.check_name, finding.detail,
        )
        self._write_finding_log(finding)

    def _write_finding_log(self, finding: AuditFinding) -> None:
        """Schrijf bevinding als AuditEvent-JSON naar ANT_LOGS/audit/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action": "audit_finding",
                "severity": finding.severity,
                "component": finding.component,
                "check_name": finding.check_name,
                "detail": finding.detail,
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "audit" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon bevinding niet naar disk schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
        """Stuur heartbeat naar de scheduler (fail-closed: negeert fouten)."""
        try:
            hb_status = (
                HeartbeatStatus.RUNNING
                if self._status == AntStatus.RUNNING
                else HeartbeatStatus.PAUSED
            )
            hb = Heartbeat(
                ant_id=self.ant_id,
                mission_id=self.mission.mission_id,
                node_id=self.mission.allowed_node,
                status=hb_status,
                budget_used=self._budget_used,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
            self._log.debug("Heartbeat gestuurd | action=%s", self._last_action)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")


# ---------------------------------------------------------------------------
# Module-private hulpfuncties
# ---------------------------------------------------------------------------

def _read_last_nonempty_line(path: Path) -> str | None:
    """Lees de laatste niet-lege regel van een tekstbestand. None als leeg."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else None


def _find_sequence_gaps(path: Path) -> list[tuple[int, int]]:
    """
    Zoek sequence-gaps in een JSONL-bestand.

    Leest alle regels, pakt die met een integer `sequence` veld, sorteert ze
    en rapporteert elk niet-aaneengesloten paar als (prev_seq, next_seq).

    Bestanden met minder dan 2 regels met sequence worden overgeslagen (geen gap
    mogelijk). Onleesbare bestanden of regels worden stilzwijgend overgeslagen.
    """
    sequences: list[int] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                seq = record.get("sequence")
                if isinstance(seq, int):
                    sequences.append(seq)
            except (json.JSONDecodeError, AttributeError):
                pass
    except OSError:
        return []

    if len(sequences) < 2:
        return []

    sequences.sort()
    gaps: list[tuple[int, int]] = []
    for i in range(len(sequences) - 1):
        if sequences[i + 1] != sequences[i] + 1:
            gaps.append((sequences[i], sequences[i + 1]))
    return gaps
