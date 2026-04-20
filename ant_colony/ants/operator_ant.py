"""
ant_colony/ants/operator_ant.py

OperatorAnt — verwerkt menselijke input (URL, tekst, code) naar ingestion-kandidaten.

Verantwoordelijkheden:
  1. Leest invoer uit ANT_LOGS/operator/input/*.json
     Elk bestand: {timestamp}_{type}.json (types: url, text, code)
  2. Per input:
     - Bepaal type (URL/tekst/code)
     - Dedup check tegen bestaande ingestion logs (URL of keyword overlap)
     - Als nieuw: normaliseer naar StrategyCandidate (status=INGESTED),
       schrijf naar ANT_LOGS/ingestion/{ant_id}.jsonl
     - Altijd: schrijf bevestiging naar ANT_LOGS/operator/processed/
  3. Logt eigen activiteit naar ANT_LOGS/operator/{ant_id}.jsonl
  4. Heartbeat + TTL zoals andere ants

Regels:
  - Geen orders — alleen ingestion (P1)
  - Fail-closed — negeert alle exceptions (P2)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)

_ENTRY_KEYWORDS: frozenset[str] = frozenset({
    "entry", "buy signal", "long", "crossover", "breakout",
    "momentum", "mean reversion", "rsi", "macd", "ema", "sma",
    "bollinger", "indicator", "oversold", "overbought", "signal",
    "moving average", "trend", "volume",
})
_EXIT_KEYWORDS: frozenset[str] = frozenset({
    "exit", "sell", "stop loss", "stop_loss", "take profit", "take_profit",
    "trailing stop", "trailing_stop", "close position",
})
_ALL_KEYWORDS = _ENTRY_KEYWORDS | _EXIT_KEYWORDS

_KEYWORD_OVERLAP_THRESHOLD = 3


class OperatorAnt:
    """
    Verwerkt menselijke input naar StrategyCandidate ingestion-events.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission.
        scheduler: ColonyScheduler voor heartbeat.
        logs_root: Pad naar ANT_LOGS. None = geen disk-I/O.
        node_id:   Node identifier voor audit events.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
        node_id: str = "pc2",
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root
        self._node_id  = node_id

        self._seen_input_files: set[str] = set()
        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"

        self._log = logging.getLogger(f"ant.operator.{ant_id[:8]}")

        if self.logs_root is not None:
            for subdir in ("operator/input", "operator/processed", "operator", "ingestion"):
                (self.logs_root / subdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._log.info("OperatorAnt run() gestart")
        self._status = AntStatus.RUNNING
        self._log.info(
            "OperatorAnt gestart | mission=%s ttl=%ds",
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
            self._log.info("OperatorAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("OperatorAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één verwerkingscyclus: lees input bestanden, verwerk elk."""
        try:
            self._process_input_files()
        except Exception:
            self._log.exception("Onverwachte fout in OperatorAnt._tick()")
        self._last_action = "tick"

    def _process_input_files(self) -> None:
        if self.logs_root is None:
            return

        input_dir = self.logs_root / "operator" / "input"
        if not input_dir.exists():
            return

        for path in sorted(input_dir.glob("*.json")):
            fname = path.name
            if fname in self._seen_input_files:
                continue
            self._seen_input_files.add(fname)
            try:
                self._process_input_file(path)
            except Exception:
                self._log.exception("Fout bij verwerken input bestand: %s", fname)

    def _process_input_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._log.warning("Kan input bestand niet lezen: %s", path)
            return

        input_type = (raw.get("type") or _type_from_filename(path.name)).strip().lower()

        if input_type == "image":
            self._process_image_input(path, raw)
            return

        content    = (raw.get("content") or "").strip()
        if not content:
            self._log.debug("Leeg input bestand — overgeslagen: %s", path.name)
            return

        self._log.info(
            "Operator input | bestand=%s type=%s content_len=%d",
            path.name, input_type, len(content),
        )

        url      = _extract_url(content) if input_type == "url" else ""
        keywords = _extract_keywords(content)

        dup_info = self._find_duplicate(url, keywords)
        if dup_info:
            msg = (
                f"al bekend | candidate_id={dup_info['candidate_id']} "
                f"| status={dup_info.get('status', '?')} "
                f"| sharpe={dup_info.get('sharpe', '?')}"
            )
            self._write_processed(path.stem, {
                "result":     "duplicate",
                "message":    msg,
                "input_file": path.name,
                "input_type": input_type,
            })
            self._write_operator_log("operator_input_duplicate", {
                "input_file":   path.name,
                "input_type":   input_type,
                "candidate_id": dup_info["candidate_id"],
                "message":      msg,
            })
            self._log.info(
                "Operator input duplicaat | bestand=%s candidate_id=%s",
                path.name, dup_info["candidate_id"],
            )
            return

        candidate = self._build_candidate(input_type, content, url, keywords, path.name)
        if candidate is None:
            self._write_processed(path.stem, {
                "result":     "error",
                "message":    "Kon geen kandidaat bouwen uit input",
                "input_file": path.name,
                "input_type": input_type,
            })
            return

        self._write_to_ingestion(candidate)
        self._write_processed(path.stem, {
            "result":       "accepted",
            "message":      f"kandidaat ingested | candidate_id={candidate.candidate_id}",
            "candidate_id": candidate.candidate_id,
            "input_file":   path.name,
            "input_type":   input_type,
        })
        self._write_operator_log("operator_input_processed", {
            "input_file":   path.name,
            "input_type":   input_type,
            "candidate_id": candidate.candidate_id,
            "keywords":     keywords[:6],
        })
        self._last_action = f"ingested:{candidate.candidate_id[:8]}"
        self._log.info(
            "Operator kandidaat ingested | candidate_id=%s type=%s keywords=%s",
            candidate.candidate_id[:8], input_type, keywords[:4],
        )

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    def _find_duplicate(self, url: str, keywords: list[str]) -> dict | None:
        """Check bestaande ingestion logs op URL of keyword overlap."""
        if self.logs_root is None:
            return None

        ingestion_dir = self.logs_root / "ingestion"
        if not ingestion_dir.exists():
            return None

        kw_set = set(keywords)

        for path in ingestion_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") or {}
                    if payload.get("action") != "candidate_ingested":
                        continue

                    if url and payload.get("source_url") == url:
                        return {
                            "candidate_id": payload.get("candidate_id", ""),
                            "status":       payload.get("status", ""),
                            "sharpe":       None,
                        }

                    existing_kws = set(payload.get("entry_keywords") or [])
                    if kw_set and len(kw_set & existing_kws) >= _KEYWORD_OVERLAP_THRESHOLD:
                        return {
                            "candidate_id": payload.get("candidate_id", ""),
                            "status":       payload.get("status", ""),
                            "sharpe":       None,
                        }
            except OSError:
                pass

        return None

    # ------------------------------------------------------------------
    # Kandidaat bouwen
    # ------------------------------------------------------------------

    def _build_candidate(
        self,
        input_type: str,
        content: str,
        url: str,
        keywords: list[str],
        source_file: str,
    ) -> StrategyCandidate | None:
        try:
            uid_seed = url if url else content[:200]
            cid = str(uuid.uuid5(uuid.NAMESPACE_URL, uid_seed))

            entry_kws = [kw for kw in _ENTRY_KEYWORDS if kw in content.lower()]
            exit_kws  = [kw for kw in _EXIT_KEYWORDS  if kw in content.lower()]

            return StrategyCandidate(
                candidate_id=cid,
                name=f"operator:{source_file[:32]}",
                source="operator",
                source_url=url or "",
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=(content[:500] if input_type != "url" else f"Operator URL: {url}"),
                parameters={"input_type": input_type},
                entry_conditions={"keywords": entry_kws or keywords},
                exit_conditions={"keywords": exit_kws},
                status=CandidateStatus.INGESTED,
                provenance=[
                    ProvenanceEntry(
                        actor=self.ant_id,
                        action="operator_input",
                        details={
                            "source":     "operator",
                            "input_type": input_type,
                            "file":       source_file,
                            "found_at":   datetime.now(tz=timezone.utc).isoformat(),
                        },
                    )
                ],
            )
        except Exception:
            self._log.exception("Kan StrategyCandidate niet bouwen")
            return None

    # ------------------------------------------------------------------
    # Image analyse
    # ------------------------------------------------------------------

    def _process_image_input(self, path: Path, raw: dict) -> None:
        """Verwerk een chart screenshot: Claude vision → StrategyCandidate."""
        image_data = (raw.get("image_data") or "").strip()
        media_type = (raw.get("media_type") or "image/jpeg").strip()

        if not image_data:
            self._log.warning("Image input zonder image_data — overgeslagen: %s", path.name)
            self._write_processed(path.stem, {
                "result": "error", "message": "Geen image_data in bestand", "input_file": path.name,
            })
            return

        self._log.info("Image input | bestand=%s media_type=%s", path.name, media_type)

        analysis = self._analyze_image_with_claude(image_data, media_type)
        if not analysis:
            self._write_processed(path.stem, {
                "result": "error", "message": "Claude image analyse mislukt", "input_file": path.name,
            })
            return

        summary        = analysis.get("summary") or analysis.get("entry_suggestion") or "chart analyse"
        strategy_type  = analysis.get("strategy_type") or "chart_pattern"
        entry_sug      = analysis.get("entry_suggestion") or ""
        exit_sug       = analysis.get("exit_suggestion") or ""
        patterns       = analysis.get("patterns") or []

        cid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"image:{path.stem}:{summary[:80]}"))

        try:
            candidate = StrategyCandidate(
                candidate_id=cid,
                name=f"chart:{strategy_type}:{path.stem[:20]}",
                source="operator_image",
                source_url="",
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=summary[:500],
                parameters={"input_type": "image", "strategy_type": strategy_type, "patterns": patterns},
                entry_conditions={"suggestion": entry_sug, "keywords": [kw for kw in _ENTRY_KEYWORDS if kw in (entry_sug + summary).lower()]},
                exit_conditions={"suggestion": exit_sug, "keywords": [kw for kw in _EXIT_KEYWORDS if kw in (exit_sug + summary).lower()]},
                status=CandidateStatus.INGESTED,
                provenance=[
                    ProvenanceEntry(
                        actor=self.ant_id,
                        action="operator_image",
                        details={
                            "source":        "operator_image",
                            "input_type":    "image",
                            "file":          path.name,
                            "strategy_type": strategy_type,
                            "confidence":    analysis.get("confidence", "unknown"),
                            "found_at":      datetime.now(tz=timezone.utc).isoformat(),
                        },
                    )
                ],
            )
        except Exception:
            self._log.exception("Kan StrategyCandidate niet bouwen voor image input")
            self._write_processed(path.stem, {
                "result": "error", "message": "StrategyCandidate bouwen mislukt", "input_file": path.name,
            })
            return

        self._write_to_ingestion(candidate)
        self._write_processed(path.stem, {
            "result":       "accepted",
            "message":      f"chart kandidaat ingested | candidate_id={candidate.candidate_id}",
            "candidate_id": candidate.candidate_id,
            "input_file":   path.name,
            "input_type":   "image",
            "strategy_type": strategy_type,
        })
        self._write_operator_log("operator_input_processed", {
            "input_file":    path.name,
            "input_type":    "image",
            "candidate_id":  candidate.candidate_id,
            "strategy_type": strategy_type,
        })
        self._last_action = f"image_ingested:{candidate.candidate_id[:8]}"
        self._log.info(
            "Chart kandidaat ingested | candidate_id=%s strategy_type=%s",
            candidate.candidate_id[:8], strategy_type,
        )

    def _analyze_image_with_claude(self, image_b64: str, media_type: str) -> dict:
        """Stuur chart naar Claude Vision. Retourneert gestructureerde analyse of {}."""
        try:
            import anthropic
        except ImportError:
            self._log.warning("anthropic pakket niet beschikbaar — image analyse overgeslagen")
            return {}

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            self._log.warning("ANTHROPIC_API_KEY niet ingesteld — image analyse overgeslagen")
            return {}

        prompt = (
            "Analyseer dit chart patroon. Welke technische patronen zie je? "
            "Vergelijk met: head and shoulders, double top/bottom, triangle, flag, wedge, "
            "support/resistance levels. Geef strategy_type en entry/exit suggestie.\n\n"
            "Antwoord ALLEEN als JSON (geen uitleg erbuiten):\n"
            '{"strategy_type":"...","patterns":["..."],'
            '"entry_suggestion":"...","exit_suggestion":"...",'
            '"summary":"...","confidence":"low|medium|high"}'
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            text = msg.content[0].text.strip()
            # strip markdown code fences
            if text.startswith("```"):
                parts = text.split("```")
                text = parts[1] if len(parts) > 1 else parts[0]
                if text.startswith("json"):
                    text = text[4:].strip()
            return json.loads(text)
        except json.JSONDecodeError:
            self._log.warning("Claude image analyse: ongeldige JSON ontvangen")
            return {}
        except Exception:
            self._log.exception("Claude image analyse API-call mislukt")
            return {}

    # ------------------------------------------------------------------
    # Schrijf naar disk
    # ------------------------------------------------------------------

    def _write_to_ingestion(self, candidate: StrategyCandidate) -> None:
        """Schrijf een AuditEvent naar ANT_LOGS/ingestion/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":         "candidate_ingested",
                "candidate_id":   candidate.candidate_id,
                "name":           candidate.name,
                "source_url":     candidate.source_url,
                "status":         candidate.status.value,
                "stars":          None,
                "entry_keywords": candidate.entry_conditions.get("keywords"),
                "exit_keywords":  candidate.exit_conditions.get("keywords"),
                "logic_summary":  candidate.logic_summary,
                "provenance":     [p.model_dump(mode="json") for p in candidate.provenance],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "ingestion" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon ingestion event niet schrijven: %s", log_path)

    def _write_operator_log(self, action: str, extra: dict) -> None:
        """Schrijf een AuditEvent naar ANT_LOGS/operator/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={"action": action, **extra},
        )
        self._log_seq += 1

        log_path = self.logs_root / "operator" / f"{self.ant_id}.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon operator log niet schrijven: %s", log_path)

    def _write_processed(self, stem: str, data: dict) -> None:
        """Schrijf verwerkingsbevestiging naar ANT_LOGS/operator/processed/."""
        if self.logs_root is None:
            return

        processed_dir = self.logs_root / "operator" / "processed"
        try:
            processed_dir.mkdir(parents=True, exist_ok=True)
            out = {"timestamp": datetime.now(tz=timezone.utc).isoformat(), **data}
            out_path = processed_dir / f"{stem}_result.json"
            with out_path.open("w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2, default=str)
        except OSError:
            self._log.exception("Kon processed bestand niet schrijven")

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
            self._log.debug("Heartbeat gestuurd | action=%s", self._last_action)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://\S+')


def _extract_url(content: str) -> str:
    """Haal eerste URL uit content, of geef content terug als het al een URL is."""
    stripped = content.strip()
    m = _URL_RE.search(stripped)
    return m.group(0).rstrip('.,;)') if m else stripped


def _type_from_filename(name: str) -> str:
    """Leid type af uit bestandsnaam: {timestamp}_{type}.json"""
    stem = Path(name).stem
    parts = stem.split("_", 1)
    if len(parts) > 1 and parts[-1] in ("url", "text", "code", "image"):
        return parts[-1]
    return "text"


def _extract_keywords(content: str) -> list[str]:
    """Zoek bekende strategie-keywords in de content."""
    text = content.lower()
    return [kw for kw in _ALL_KEYWORDS if kw in text]
