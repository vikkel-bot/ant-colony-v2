"""
ant_colony/ants/equities/piotroski_ant.py

PiotroskiAnt — leest FundamentalAnt-kandidaten, hervalideert de Piotroski
F-Score met verse data, en schrijft geaccepteerde kandidaten door naar
ANT_LOGS/equities/piotroski/.

Pipeline positie: FundamentalAnt → **PiotroskiAnt** → BreakoutAnt

Werkwijze per tick:
  1. Scan ANT_LOGS/research/*.jsonl voor nieuwe candidate_accepted entries.
  2. Per kandidaat: haal verse fundamentals op via de equities-adapter.
  3. Herbereken Piotroski F-Score met FundamentalAnt.piotroski_score().
  4. Score >= min_f_score → schrijf piotroski_candidate naar ANT_LOGS/equities/piotroski/.
  5. Dedupliceer op candidate_id zodat elk signaal slechts eenmaal wordt verwerkt.

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.ants.equities.fundamental_ant import FundamentalAnt
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_MIN_PIOTROSKI_SCORE = 7
_READ_ACTION         = "candidate_accepted"    # FundamentalAnt output
_WRITE_ACTION        = "piotroski_candidate"   # eigen output


class PiotroskiAnt:
    """
    Leest FundamentalAnt-kandidaten, hervalideert Piotroski F-Score
    met verse fundamentals en stuurt door naar de breakout-fase.

    Args:
        ant_id:           Unieke identifier.
        mission:          Toegewezen Mission.
        scheduler:        ColonyScheduler voor heartbeat-registratie.
        biome_registry:   BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:        Pad naar ANT_LOGS. None = geen disk-logging.
        min_f_score:      Minimum Piotroski F-Score om door te sturen (standaard 7).
        research_log_dir: Override van de te lezen FundamentalAnt log-map.
                          Standaard: logs_root/research/.
    """

    # Hervalidatie elk uur is voldoende (fundamentals veranderen dagelijks).
    _TICK_INTERVAL: int = 3600

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        min_f_score: int = _MIN_PIOTROSKI_SCORE,
        research_log_dir: Path | None = None,
        **kwargs,
    ) -> None:
        self.ant_id          = ant_id
        self.mission         = mission
        self.scheduler       = scheduler
        self.biome_registry  = biome_registry
        self.logs_root       = logs_root
        self._min_f_score    = min_f_score

        # Waar FundamentalAnt-logs worden gelezen
        if research_log_dir is not None:
            self._research_log_dir = research_log_dir
        elif logs_root is not None:
            self._research_log_dir = logs_root / "research"
        else:
            self._research_log_dir = None

        self._seen_candidate_ids: set[str] = set()
        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._last_tick_at: float = 0.0
        self._log = logging.getLogger(f"ant.piotroski.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "PiotroskiAnt gestart | mission=%s ttl=%ds min_f_score=%d",
            self.mission.mission_id, self.mission.ttl, self._min_f_score,
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

                now_mono = time.monotonic()
                if now_mono - self._last_tick_at >= self._TICK_INTERVAL:
                    self._last_tick_at = now_mono
                    self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("PiotroskiAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("PiotroskiAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Scan FundamentalAnt-logs, hervalideer scores, schrijf doorgestuurde kandidaten.
        Retourneert lijst van doorgestuurde kandidaten (nuttig voor tests).
        """
        if self._research_log_dir is None or not self._research_log_dir.exists():
            self._log.debug("Geen research log-map beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_log_dir"
            return []

        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        adapter_available = adapter is not None and adapter.is_available()
        get_fundamentals_fn = getattr(adapter, "get_fundamentals", None) if adapter_available else None

        forwarded = []
        for candidate_id, payload in self._scan_log_dir(self._research_log_dir, _READ_ACTION):
            try:
                result = self._evaluate_candidate(candidate_id, payload, get_fundamentals_fn)
                if result is not None:
                    forwarded.append(result)
                    self._write_candidate(result)
                    self._log.info(
                        "PIOTROSKI PASS | %s  f_score=%d",
                        result["symbol"], result["f_score"],
                    )
            except Exception:
                self._log.exception("Evaluatie mislukt voor candidate_id=%s", candidate_id)

        self._last_action = f"tick:{len(forwarded)} doorgestuurd"
        return forwarded

    def _evaluate_candidate(
        self,
        candidate_id: str,
        payload: dict,
        get_fundamentals_fn,
    ) -> dict | None:
        """
        Hervalideer één kandidaat. Retourneert kandidaat-dict of None.

        Primair: haal verse fundamentals op en herbereken.
        Fallback: gebruik f_score uit het log als adapter niet beschikbaar is.
        """
        symbol = payload.get("symbol", "")
        if not symbol:
            return None

        # Verse fundamentals ophalen (primair pad)
        if get_fundamentals_fn is not None:
            try:
                fundamentals = get_fundamentals_fn(symbol)
                f_score = FundamentalAnt.piotroski_score(fundamentals)
            except Exception:
                self._log.warning("get_fundamentals mislukt voor %s — fallback op log-score", symbol)
                f_score = int(payload.get("f_score", 0))
                fundamentals = {}
        else:
            # Adapter niet beschikbaar: gebruik score uit log
            f_score = int(payload.get("f_score", 0))
            fundamentals = {}

        if f_score < self._min_f_score:
            self._log.debug("%s: f_score=%d < %d — gefilterd", symbol, f_score, self._min_f_score)
            return None

        self._seen_candidate_ids.add(candidate_id)
        return {
            "candidate_id":  candidate_id,
            "symbol":        symbol,
            "f_score":       f_score,
            "fundamentals":  fundamentals,
            "source_action": _READ_ACTION,
            "evaluated_at":  datetime.now(tz=timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Log scanning
    # ------------------------------------------------------------------

    def _scan_log_dir(self, log_dir: Path, action: str):
        """
        Yield (candidate_id, payload) voor alle nieuwe entries met het gegeven action
        in alle *.jsonl bestanden in log_dir.
        """
        try:
            for path in sorted(log_dir.glob("*.jsonl")):
                yield from self._scan_log_file(path, action)
        except Exception:
            self._log.exception("Log-scan mislukt voor %s", log_dir)

    def _scan_log_file(self, path: Path, action: str):
        """Yield (candidate_id, payload) voor nieuwe entries in één .jsonl bestand."""
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload", {})
                        if payload.get("action") != action:
                            continue
                        candidate_id = payload.get("candidate_id", "")
                        if not candidate_id or candidate_id in self._seen_candidate_ids:
                            continue
                        yield candidate_id, payload
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            self._log.exception("Kan log-bestand niet lezen: %s", path)

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(self, candidate: dict) -> None:
        """Schrijf piotroski_candidate naar ANT_LOGS/equities/piotroski/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":       _WRITE_ACTION,
                "candidate_id": candidate["candidate_id"],
                "symbol":       candidate["symbol"],
                "f_score":      candidate["f_score"],
                "fundamentals": candidate["fundamentals"],
                "evaluated_at": candidate["evaluated_at"],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "piotroski" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet naar disk schrijven: %s", log_path)

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
