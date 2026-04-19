"""
ant_colony/ants/claude_ant.py

ClaudeAnt — analyseert top-kandidaten via de Anthropic API en genereert verbeterde varianten.

Verantwoordelijkheden:
  1. Leest top-5 kandidaten uit ANT_LOGS/research/ (hoogste sharpe)
  2. Stuurt ze naar claude-sonnet-4-6 via de Anthropic API
  3. Parseert de JSON-response en schrijft verbeterde varianten naar ANT_LOGS/ingestion/
     zodat ze door de normale pipeline gaan (ResearchAnt → Promoter → PaperAnt)
  4. Schrijft analyse naar ANT_LOGS/claude/{ant_id}.jsonl
  5. Rate limit: maximaal 1 API call per 5 minuten
  6. Logt geschatte kosten per call

Regels:
  - Geen orders — alleen analyse en ingestion-voeding (P1)
  - Fail-closed — negeert alle exceptions (P2)
  - Vereist ANTHROPIC_API_KEY environment variabele
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import json
import logging
import os
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

_MODEL             = "claude-sonnet-4-6"
_MAX_TOKENS        = 2000
_TOP_N_CANDIDATES  = 5
_RATE_LIMIT_SECS   = 300.0   # 1 call per 5 minuten

# Ruwe kostenschatting: Sonnet $3/M input + $15/M output tokens
_COST_PER_M_INPUT  = 3.0
_COST_PER_M_OUTPUT = 15.0


_PROMPT_TEMPLATE = """\
Je bent een quant analist. Analyseer deze trading strategie kandidaten:
{candidates_json}

Voor elke kandidaat:
- Waarom werkt deze strategie theoretisch?
- Wanneer faalt hij waarschijnlijk?
- Geef een verbeterd variant als JSON met aangepaste parameters
- Geef een confidence score 0-10

Antwoord alleen in JSON als een array met dit formaat:
[
  {{
    "candidate_id": "...",
    "rationale": "...",
    "failure_modes": "...",
    "confidence": 7,
    "improved_variant": {{
      "entry_keywords": ["momentum", "rsi"],
      "take_profit_pct": 0.08,
      "stop_loss_pct": 0.04,
      "logic_summary": "verbeterde versie van ..."
    }}
  }}
]"""


class ClaudeAnt:
    """
    Analyseert top research-kandidaten via de Anthropic API.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission.
        scheduler: ColonyScheduler voor heartbeat.
        logs_root: Pad naar ANT_LOGS. None = geen disk-I/O.
        api_key:   Anthropic API key (default: ANTHROPIC_API_KEY env var).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
        api_key: str | None = None,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root

        self._api_key        = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._log_seq: int   = 0
        self._status         = AntStatus.IDLE
        self._last_action    = "init"
        self._last_api_call  = 0.0   # monotonic timestamp van laatste API call
        self._total_cost_usd = 0.0

        self._log = logging.getLogger(f"ant.claude.{ant_id[:8]}")

        if self.logs_root is not None:
            for subdir in ("claude", "ingestion"):
                (self.logs_root / subdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "ClaudeAnt gestart | mission=%s ttl=%ds model=%s",
            self.mission.mission_id,
            self.mission.ttl,
            _MODEL,
        )

        if not self._api_key:
            self._log.error(
                "ANTHROPIC_API_KEY niet gezet — ClaudeAnt kan niet starten"
            )
            self._status = AntStatus.ABORTED
            return self._status

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
            self._log.info("ClaudeAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info(
                "ClaudeAnt gestopt | status=%s total_cost=$%.4f",
                self._status.value, self._total_cost_usd,
            )

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één analyse-cyclus: lees top-kandidaten en analyseer via Claude."""
        try:
            elapsed_since_call = time.monotonic() - self._last_api_call
            if elapsed_since_call < _RATE_LIMIT_SECS:
                remaining = _RATE_LIMIT_SECS - elapsed_since_call
                self._log.debug(
                    "Rate limit — nog %.0fs wachten voor volgende API call", remaining
                )
                return

            candidates = self._read_top_candidates()
            if not candidates:
                self._log.debug("Geen research-kandidaten beschikbaar — tick overgeslagen")
                return

            self._analyse_with_claude(candidates)

        except Exception:
            self._log.exception("Onverwachte fout in ClaudeAnt._tick()")
        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Kandidaten lezen
    # ------------------------------------------------------------------

    def _read_top_candidates(self) -> list[dict]:
        """Lees top-N kandidaten uit ANT_LOGS/research/ gesorteerd op sharpe."""
        if self.logs_root is None:
            return []

        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return []

        all_candidates: list[dict] = []

        for path in research_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") or {}
                    if payload.get("action") != "candidate_accepted":
                        continue
                    sharpe = payload.get("sharpe")
                    if sharpe is not None:
                        all_candidates.append(payload)
            except OSError:
                pass

        all_candidates.sort(key=lambda c: c.get("sharpe") or 0.0, reverse=True)
        return all_candidates[:_TOP_N_CANDIDATES]

    # ------------------------------------------------------------------
    # Claude API
    # ------------------------------------------------------------------

    def _analyse_with_claude(self, candidates: list[dict]) -> None:
        """Stuur kandidaten naar Claude Sonnet en verwerk de response."""
        try:
            import anthropic
        except ImportError:
            self._log.error("anthropic package niet geïnstalleerd — pip install anthropic")
            return

        candidates_json = json.dumps(candidates, indent=2, default=str)
        prompt = _PROMPT_TEMPLATE.format(candidates_json=candidates_json)

        self._log.info(
            "Claude API call | model=%s candidates=%d",
            _MODEL, len(candidates),
        )
        self._last_api_call = time.monotonic()

        try:
            client  = anthropic.Anthropic(api_key=self._api_key)
            message = client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            self._log.exception("Anthropic API call mislukt")
            return

        response_text   = message.content[0].text if message.content else ""
        input_tokens    = message.usage.input_tokens  if message.usage else 0
        output_tokens   = message.usage.output_tokens if message.usage else 0
        cost_usd        = (
            (input_tokens  / 1_000_000) * _COST_PER_M_INPUT
            + (output_tokens / 1_000_000) * _COST_PER_M_OUTPUT
        )
        self._total_cost_usd += cost_usd

        self._log.info(
            "Claude response ontvangen | input_tokens=%d output_tokens=%d cost=$%.4f",
            input_tokens, output_tokens, cost_usd,
        )

        analyses = self._parse_response(response_text)
        variants_written = 0

        for analysis in analyses:
            variant = analysis.get("improved_variant") or {}
            if variant:
                written = self._write_variant_to_ingestion(analysis, variant)
                if written:
                    variants_written += 1

        self._write_claude_log("claude_analysis_complete", {
            "candidates_analysed": len(candidates),
            "variants_written":    variants_written,
            "input_tokens":        input_tokens,
            "output_tokens":       output_tokens,
            "cost_usd":            round(cost_usd, 6),
            "total_cost_usd":      round(self._total_cost_usd, 6),
            "analyses":            analyses,
        })

        self._last_action = f"claude:{len(candidates)}cands:{variants_written}variants"
        self._log.info(
            "Claude analyse klaar | kandidaten=%d varianten_geschreven=%d cost=$%.4f",
            len(candidates), variants_written, cost_usd,
        )

    def _parse_response(self, text: str) -> list[dict]:
        """Parseer JSON-array uit Claude response. Fail-closed: retourneert [] bij fouten."""
        if not text.strip():
            return []
        # Claude kan markdown code blocks gebruiken — strip ze
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(
                l for l in lines
                if not l.strip().startswith("```")
            ).strip()
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError:
            self._log.warning("Kon Claude response niet als JSON parsen — overgeslagen")
        return []

    # ------------------------------------------------------------------
    # Verbeterde varianten naar ingestion schrijven
    # ------------------------------------------------------------------

    def _write_variant_to_ingestion(self, analysis: dict, variant: dict) -> bool:
        """
        Schrijft een verbeterde variant als AuditEvent naar ANT_LOGS/ingestion/.

        Retourneert True als succesvol geschreven.
        """
        if self.logs_root is None:
            return False

        entry_kws = variant.get("entry_keywords") or ["claude", "improved"]
        logic     = variant.get("logic_summary") or (
            f"Claude-verbeterd variant van {analysis.get('candidate_id', 'onbekend')[:8]}"
        )

        try:
            candidate = StrategyCandidate(
                candidate_id=str(uuid.uuid4()),
                name=f"claude_variant:{analysis.get('candidate_id', '')[:8]}",
                source="claude",
                source_url="",
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=logic[:500],
                parameters={
                    "take_profit_pct":  variant.get("take_profit_pct"),
                    "stop_loss_pct":    variant.get("stop_loss_pct"),
                    "confidence":       analysis.get("confidence"),
                    "source_candidate": analysis.get("candidate_id"),
                },
                entry_conditions={"keywords": entry_kws},
                exit_conditions={
                    "take_profit_pct": variant.get("take_profit_pct", 0.06),
                    "stop_loss_pct":   variant.get("stop_loss_pct", 0.03),
                },
                status=CandidateStatus.INGESTED,
                provenance=[
                    ProvenanceEntry(
                        actor=self.ant_id,
                        action="claude_improved",
                        details={
                            "model":            _MODEL,
                            "confidence":       analysis.get("confidence"),
                            "rationale":        (analysis.get("rationale") or "")[:200],
                            "source_candidate": analysis.get("candidate_id"),
                        },
                    )
                ],
            )
        except Exception:
            self._log.exception("Kon variant StrategyCandidate niet bouwen")
            return False

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
                "source_url":     "",
                "status":         candidate.status.value,
                "stars":          None,
                "entry_keywords": entry_kws,
                "exit_keywords":  [],
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
            return True
        except OSError:
            self._log.exception("Kon variant niet naar ingestion schrijven: %s", log_path)
            return False

    # ------------------------------------------------------------------
    # Claude log
    # ------------------------------------------------------------------

    def _write_claude_log(self, action: str, extra: dict) -> None:
        """Schrijf een AuditEvent naar ANT_LOGS/claude/{ant_id}.jsonl."""
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

        log_path = self.logs_root / "claude" / f"{self.ant_id}.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon claude log niet schrijven: %s", log_path)

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
