"""
ant_colony/ants/time_filter_ant.py

TimeFilterAnt — ICT Kill Zone filter voor handelssignalen.

Kill Zones (alle tijden in UTC):
  London Kill Zone:  02:00 – 05:00  → TRADE
  NY AM Kill Zone:   13:30 – 15:00  → TRADE
  NY PM:             15:00 – 16:00  → AVOID  (aansluitend op NY AM)
  Asian Session:     19:00 – 24:00  → DO NOT TRADE
  Neutral:           overig         → AVOID

Schrijft elke tick een TimeSignal naar ANT_LOGS/time_filter/{ant_id}.jsonl:
  { "action": "time_signal", "session": "...", "trade_allowed": bool, "reason": "..." }

Andere ants lezen de laatste TimeSignal via read_latest_time_signal().
Als geen signaal gevonden (bijv. TimeFilterAnt niet actief) → standaard TRADE.

Regels:
  - Geen orders, geen kapitaal (P1)
  - Fail-closed (P2)
  - Geen code bij import (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Session classificatie — pure functie (eenvoudig te testen)
# ---------------------------------------------------------------------------

_SESSIONS = (
    # (start_min, end_min, name, trade_allowed, reason)
    (120,  300,  "london_kill_zone", True,  "London Kill Zone — institutioneel volume hoog"),
    (810,  900,  "ny_am_kill_zone",  True,  "NY AM Kill Zone — institutioneel volume hoog"),
    (900,  960,  "ny_pm",            False, "NY PM — institutioneel volume afnemend"),
    (1140, 1440, "asian",            False, "Asian Session — institutioneel volume laag"),
)


def _classify_session(hour: int, minute: int) -> tuple[str, bool, str]:
    """
    Classificeer de huidige UTC tijd als een Kill Zone sessie.

    Returns:
        (session_name, trade_allowed, reason)
    """
    t = hour * 60 + minute
    for start, end, name, allowed, reason in _SESSIONS:
        if start <= t < end:
            return name, allowed, reason
    return "neutral", False, "Buiten kill zones — geen institutioneel momentum"


# ---------------------------------------------------------------------------
# Reader — te importeren door ScoutAnt en PaperAnt
# ---------------------------------------------------------------------------

def read_latest_time_signal(logs_root: Path) -> dict | None:
    """
    Lees de meest recente TimeSignal uit ANT_LOGS/time_filter/*.jsonl.

    Retourneert de payload dict of None als geen signal gevonden.
    Als None: behandel als trade_allowed=True (fail-open — filter niet actief).
    """
    time_filter_dir = logs_root / "time_filter"
    if not time_filter_dir.exists():
        return None

    latest_payload = None
    latest_ts_str: str = ""

    for path in time_filter_dir.glob("*.jsonl"):
        try:
            last_line: str | None = None
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
            if last_line is None:
                continue
            rec = json.loads(last_line)
            payload = rec.get("payload") or {}
            if payload.get("action") != "time_signal":
                continue
            ts = rec.get("timestamp", "")
            if ts > latest_ts_str:
                latest_ts_str = ts
                latest_payload = payload
        except (OSError, json.JSONDecodeError):
            pass

    return latest_payload


# ---------------------------------------------------------------------------
# TimeFilterAnt
# ---------------------------------------------------------------------------

class TimeFilterAnt:
    """
    Schrijft elke tick een TimeSignal naar disk op basis van de UTC tijd.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission (TTL + heartbeat_interval).
        scheduler: ColonyScheduler voor heartbeat.
        logs_root: Root van ANT_LOGS. None = geen disk-I/O.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root

        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._log_seq: int = 0

        self._log = logging.getLogger(f"ant.time_filter.{ant_id[:8]}")

        if self.logs_root is not None:
            (self.logs_root / "time_filter").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "TimeFilterAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id, self.mission.ttl,
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
            self._log.info("TimeFilterAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("TimeFilterAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Bepaal huidige sessie en schrijf TimeSignal naar disk."""
        try:
            now = datetime.now(tz=timezone.utc)
            session, trade_allowed, reason = _classify_session(now.hour, now.minute)
            self._write_signal(session, trade_allowed, reason, now)
            self._last_action = f"time_signal:{session}"
            self._log.debug(
                "TimeSignal | session=%s trade_allowed=%s", session, trade_allowed
            )
        except Exception:
            self._log.exception("Onverwachte fout in TimeFilterAnt._tick()")

    # ------------------------------------------------------------------
    # Schrijf naar disk
    # ------------------------------------------------------------------

    def _write_signal(
        self,
        session: str,
        trade_allowed: bool,
        reason: str,
        now: datetime,
    ) -> None:
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":        "time_signal",
                "session":       session,
                "trade_allowed": trade_allowed,
                "reason":        reason,
                "utc_time":      now.strftime("%H:%M:%S"),
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "time_filter" / f"{self.ant_id}.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon TimeSignal niet schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
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
                budget_used=0.0,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
