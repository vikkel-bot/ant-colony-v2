"""
ant_colony/ants/equities/rotation_ant.py

RotationAnt — leest MomentumRankAnt output en emitteert rotatie-signalen.

Strategie: Sector Rotatie (Setup 1 uit CLAUDE.md)
  - Leest meest recente momentum_ranking uit ANT_LOGS/momentum_rank/*.jsonl
  - Vergelijkt top-3 en bottom-3 met de vorige rotatie
  - Emitteert rotation_signal (BUY top 3, SELL bottom 3) alleen als de
    ranking significant veranderd is: ≥ _MIN_SHIFT positiewisselingen
    in de top-3 of bottom-3 (standaard: >2, dus minstens 3 veranderingen)
  - Schrijft signalen naar ANT_LOGS/rotation/{ant_id}.jsonl

Output payload (action = "rotation_signal"):
  signal_id       "rotation-{date}"
  date            ISO-datum
  buys            Top-3 symbolen (lijst, sterkste eerst)
  sells           Bottom-3 symbolen (lijst, zwakste eerst)
  previous_top3   Top-3 van de vorige rotatie ([] bij eerste keer)
  previous_bot3   Bottom-3 van de vorige rotatie
  shift_count     Aantal positiewijzigingen t.o.v. vorige rotatie
  trigger         "initial" | "ranking_shift"

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed als geen rankingdata beschikbaar is (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Minimaal aantal positiewisselingen in top-3 of bottom-3 om een rotatie
# te triggeren. >2 betekent minstens 3 nieuw binnengekomen symbolen.
_MIN_SHIFT = 2

# Hoeveel sectoren BUY / SELL krijgen
_TOP_N    = 3
_BOTTOM_N = 3


class RotationAnt:
    """
    Leest MomentumRankAnt rankings en emitteert rotatie-signalen wanneer
    de top-3 of bottom-3 significant veranderd is.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry (niet actief gebruikt, conformiteit).
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
        min_shift:       Drempel voor positiewisselingen (standaard 2).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        min_shift: int = _MIN_SHIFT,
        **kwargs,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root
        self._min_shift     = min_shift

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"

        # Bijgehouden state: top-3 en bottom-3 van de laatste emissie
        self._last_top3: list[str] = []
        self._last_bot3: list[str] = []
        self._emitted_signal_dates: set[str] = set()

        self._log = logging.getLogger(f"ant.rotation.{ant_id[:8]}")

        # Herstel state uit bestaande rotation-logs bij herstart
        self._restore_last_rotation()

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "RotationAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id, self.mission.ttl,
        )

        started_at = datetime.now(tz=timezone.utc)

        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info("TTL verlopen — afsluiten")
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("RotationAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("RotationAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> dict | None:
        """
        Laad de meest recente ranking, vergelijk met de vorige rotatie
        en emiteer een rotation_signal als de drempel overschreden is.

        Retourneert het signaal-dict of None (nuttig voor tests).
        """
        today = date.today().isoformat()
        if today in self._emitted_signal_dates:
            self._last_action = "tick:dedup"
            return None

        ranking = self._load_latest_ranking()
        if not ranking:
            self._log.debug("Geen ranking beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_ranking"
            return None

        new_top3 = [r["symbol"] for r in ranking[:_TOP_N]]
        new_bot3 = [r["symbol"] for r in ranking[-_BOTTOM_N:]]

        is_initial = not self._last_top3
        shift = self._count_shift(self._last_top3, new_top3, self._last_bot3, new_bot3)

        if not is_initial and shift <= self._min_shift:
            self._log.debug(
                "Rotatie niet nodig | shift=%d ≤ drempel=%d", shift, self._min_shift
            )
            self._last_action = f"tick:no_shift({shift})"
            return None

        trigger = "initial" if is_initial else "ranking_shift"
        signal  = self._build_signal(new_top3, new_bot3, shift, trigger, today)

        self._last_top3 = new_top3
        self._last_bot3 = new_bot3
        self._emitted_signal_dates.add(today)

        self._write_signal(signal)
        self._last_action = f"tick:rotation({trigger})"

        self._log.info(
            "ROTATIE SIGNAAL | trigger=%s shift=%d buy=%s sell=%s",
            trigger, shift, new_top3, new_bot3,
        )
        return signal

    # ------------------------------------------------------------------
    # Ranking laden
    # ------------------------------------------------------------------

    def _load_latest_ranking(self) -> list[dict]:
        """
        Laad de meest recente momentum_ranking uit ANT_LOGS/momentum_rank/*.jsonl.
        Retourneert de ranking-lijst (gesorteerd op rank) of [] bij geen data.
        """
        if self.logs_root is None:
            return []

        rank_dir = self.logs_root / "momentum_rank"
        if not rank_dir.exists():
            return []

        latest_event: dict | None = None
        latest_date:  str        = ""

        for path in rank_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("action") != "momentum_ranking":
                        continue
                    ranking_date = str(payload.get("ranking_date") or "")
                    if ranking_date > latest_date:
                        latest_date  = ranking_date
                        latest_event = payload
            except OSError:
                pass

        if latest_event is None:
            return []

        ranking = latest_event.get("ranking") or []
        return sorted(ranking, key=lambda r: r.get("rank", 999))

    # ------------------------------------------------------------------
    # State herstel
    # ------------------------------------------------------------------

    def _restore_last_rotation(self) -> None:
        """
        Herstel _last_top3 / _last_bot3 / _emitted_signal_dates uit
        bestaande rotation-logs zodat een herstart niet opnieuw emitteert.
        """
        if self.logs_root is None:
            return

        rot_dir = self.logs_root / "rotation"
        if not rot_dir.exists():
            return

        latest_date  = ""
        latest_signal: dict | None = None

        for path in rot_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("action") != "rotation_signal":
                        continue
                    sig_date = str(payload.get("date") or "")
                    self._emitted_signal_dates.add(sig_date)
                    if sig_date > latest_date:
                        latest_date   = sig_date
                        latest_signal = payload
            except OSError:
                pass

        if latest_signal is not None:
            self._last_top3 = list(latest_signal.get("buys")  or [])
            self._last_bot3 = list(latest_signal.get("sells") or [])
            self._log.debug(
                "Vorige rotatie hersteld | top3=%s bot3=%s",
                self._last_top3, self._last_bot3,
            )

    # ------------------------------------------------------------------
    # Shift berekening
    # ------------------------------------------------------------------

    @staticmethod
    def _count_shift(
        old_top3: list[str], new_top3: list[str],
        old_bot3: list[str], new_bot3: list[str],
    ) -> int:
        """
        Tel het aantal symbolen dat nieuw binnengekomen is in top-3 of bottom-3.

        Een symbool telt als "verschoven" als het in de nieuwe top-3/bottom-3
        staat maar niet in de overeenkomstige oude set.
        """
        top_shift = len(set(new_top3) - set(old_top3))
        bot_shift = len(set(new_bot3) - set(old_bot3))
        return top_shift + bot_shift

    # ------------------------------------------------------------------
    # Signaal bouwen
    # ------------------------------------------------------------------

    @staticmethod
    def _build_signal(
        buys: list[str],
        sells: list[str],
        shift: int,
        trigger: str,
        today: str,
    ) -> dict:
        return {
            "action":        "rotation_signal",
            "signal_id":     f"rotation-{today}",
            "date":          today,
            "buys":          buys,
            "sells":         sells,
            "shift_count":   shift,
            "trigger":       trigger,
        }

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_signal(self, signal: dict) -> None:
        """Schrijf rotation_signal event naar ANT_LOGS/rotation/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload=signal,
        )
        self._log_seq += 1

        log_path = self.logs_root / "rotation" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon rotatie-signaal niet naar disk schrijven: %s", log_path)

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
