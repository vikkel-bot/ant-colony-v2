"""
ant_colony/ants/claude_ant.py

ClaudeAnt — analyseert top-kandidaten via de Anthropic API en genereert verbeterde varianten.

Verantwoordelijkheden:
  1. Leest top-5 kandidaten uit ANT_LOGS/research/ (hoogste sharpe)
  2. Stuurt ze naar claude-sonnet-4-6 via de Anthropic API
  3. Parseert de JSON-response en schrijft verbeterde varianten naar ANT_LOGS/ingestion/
     zodat ze door de normale pipeline gaan (ResearchAnt → Promoter → PaperAnt)
  4. Schrijft analyse naar ANT_LOGS/claude/{ant_id}.jsonl
  5. Rate limit: 1 call per 5 minuten (escalatie naar 1/uur bij 80% budget)
  6. Maandelijks budget: CLAUDE_ANT_MONTHLY_BUDGET_EUR (default €10)
  7. Logt geschatte kosten per call in ANT_LOGS/claude/costs.jsonl

Regels:
  - Geen orders — alleen analyse en ingestion-voeding (P1)
  - Fail-closed — negeert alle exceptions (P2)
  - Vereist ANTHROPIC_API_KEY environment variabele
  - CLAUDE_ANT_ENABLED=true vereist om te starten (opt-in)
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
_MAX_TOKENS        = 500
_TOP_N_CANDIDATES  = 3
_MAX_WORDS_PER_CANDIDATE = 200
_RATE_LIMIT_FAST   = 1800.0   # normaal: 1 call per 30 minuten
_RATE_LIMIT_SLOW   = 3600.0   # bij 80% budget: 1 call per uur
_BUDGET_WARN_PCT   = 0.80     # drempel voor rate limit escalatie

# Pipeline-gating: minimale pipeline-activiteit voordat Claude API wordt aangeroepen
_PIPELINE_MIN_RESEARCH      = 3    # minimaal N accepted candidates in laatste 24u
_PIPELINE_MIN_CLOSED_TRADES = 0    # gesloten paper trades vereist (0 = geen eis)
_PIPELINE_WINDOW_SECS       = 86_400  # 24 uur

# Kostenschatting in EUR: Sonnet €3/M input + €15/M output tokens
_COST_PER_M_INPUT  = 3.0
_COST_PER_M_OUTPUT = 15.0


def _trim_candidate(c: dict) -> dict:
    """Beperk elke tekstveld tot _MAX_WORDS_PER_CANDIDATE woorden voor een kleinere prompt."""
    result = {}
    for key, val in c.items():
        if isinstance(val, str):
            words = val.split()
            result[key] = " ".join(words[:_MAX_WORDS_PER_CANDIDATE]) if len(words) > _MAX_WORDS_PER_CANDIDATE else val
        else:
            result[key] = val
    return result


_PROMPT_TEMPLATE = """\
Analyseer deze kandidaten en geef JSON terug.
Gebruik alleen ASCII tekens. Geen apostrofs in strings.
Kies voor elke variant een strategy_type: sma_crossover, rsi_momentum, mean_reversion, breakout of bollinger.
Geef type-specifieke parameters en passende keywords.
{candidates_json}
Format: [{{"candidate_id":"...","strategy_type":"sma_crossover","improved_tp_pct":0.08,"improved_sl_pct":0.04,"confidence":7,"keywords":["sma","crossover","momentum"]}}]"""


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

        self._api_key       = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._log_seq: int  = 0
        self._status        = AntStatus.IDLE
        self._last_action   = "init"
        self._last_api_call = 0.0   # monotonic timestamp van laatste API call

        self._budget_eur = float(os.getenv("CLAUDE_ANT_MONTHLY_BUDGET_EUR", "10.0"))
        self._month_cost_eur = self._load_month_costs()
        self._total_cost_eur = self._load_total_costs()

        self._log = logging.getLogger(f"ant.claude.{ant_id[:8]}")

        if self.logs_root is not None:
            for subdir in ("claude", "ingestion"):
                (self.logs_root / subdir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    @property
    def _rate_limit(self) -> float:
        """Effectieve rate limit: langzamer bij ≥80% budget."""
        if self._budget_eur > 0 and self._month_cost_eur >= _BUDGET_WARN_PCT * self._budget_eur:
            return _RATE_LIMIT_SLOW
        return _RATE_LIMIT_FAST

    def _load_month_costs(self) -> float:
        """Som van kosten in ANT_LOGS/claude/costs.jsonl voor de huidige maand."""
        if self.logs_root is None:
            return 0.0
        costs_path = self.logs_root / "claude" / "costs.jsonl"
        if not costs_path.exists():
            return 0.0
        now = datetime.now(tz=timezone.utc)
        total = 0.0
        try:
            for line in costs_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.year == now.year and ts.month == now.month:
                        total += rec.get("cost_eur", 0.0)
                except (ValueError, TypeError):
                    pass
        except OSError:
            pass
        return total

    def _load_total_costs(self) -> float:
        """Laad cumulatief totaalverbruik uit ANT_LOGS/claude/total_costs.json."""
        if self.logs_root is None:
            return self._month_cost_eur
        total_path = self.logs_root / "claude" / "total_costs.json"
        if not total_path.exists():
            return self._month_cost_eur
        try:
            data = json.loads(total_path.read_text(encoding="utf-8"))
            saved = float(data.get("total_cost_eur", 0.0))
            # Neem het maximum van opgeslagen totaal en huidig maandtotaal
            # (beschermt bij corrupt/leeg bestand)
            return max(saved, self._month_cost_eur)
        except (OSError, json.JSONDecodeError, ValueError):
            return self._month_cost_eur

    def _save_total_costs(self) -> None:
        """Sla cumulatief totaalverbruik op in ANT_LOGS/claude/total_costs.json."""
        if self.logs_root is None:
            return
        total_path = self.logs_root / "claude" / "total_costs.json"
        record = {
            "total_cost_eur": round(self._total_cost_eur, 6),
            "month_cost_eur": round(self._month_cost_eur, 6),
            "updated_at":     datetime.now(tz=timezone.utc).isoformat(),
            "ant_id":         self.ant_id,
        }
        try:
            total_path.parent.mkdir(parents=True, exist_ok=True)
            total_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError:
            self._log.exception("Kon total_costs.json niet schrijven: %s", total_path)

    def _append_cost_record(self, cost_eur: float) -> None:
        """Voeg kostenregel toe aan ANT_LOGS/claude/costs.jsonl."""
        if self.logs_root is None:
            return
        costs_path = self.logs_root / "claude" / "costs.jsonl"
        record = {
            "timestamp":    datetime.now(tz=timezone.utc).isoformat(),
            "ant_id":       self.ant_id,
            "cost_eur":     round(cost_eur, 6),
            "month_total":  round(self._month_cost_eur, 6),
            "budget_eur":   self._budget_eur,
        }
        try:
            costs_path.parent.mkdir(parents=True, exist_ok=True)
            with costs_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            self._log.exception("Kon cost record niet schrijven: %s", costs_path)
        self._save_total_costs()

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "ClaudeAnt gestart | mission=%s ttl=%ds model=%s budget=€%.2f",
            self.mission.mission_id,
            self.mission.ttl,
            _MODEL,
            self._budget_eur,
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

                if self._status != AntStatus.RUNNING:
                    break

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
                "ClaudeAnt gestopt | status=%s month_cost=€%.4f budget=€%.2f",
                self._status.value, self._month_cost_eur, self._budget_eur,
            )

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één analyse-cyclus: lees top-kandidaten en analyseer via Claude."""
        try:
            # Budget volledig verbruikt → stop zichzelf
            if self._budget_eur > 0 and self._month_cost_eur >= self._budget_eur:
                self._log.error(
                    "Maandbudget volledig verbruikt (€%.4f / €%.2f) — ClaudeAnt stopt.",
                    self._month_cost_eur, self._budget_eur,
                )
                self._write_claude_log("BUDGET_EXCEEDED", {
                    "month_cost_eur": round(self._month_cost_eur, 6),
                    "budget_eur":     self._budget_eur,
                })
                self._status = AntStatus.ABORTED
                return

            ready, reason = self._pipeline_ready()
            if not ready:
                self._log.info("Claude Ant wacht op pipeline data — %s", reason)
                self._last_action = f"wacht:{reason}"
                return

            elapsed_since_call = time.monotonic() - self._last_api_call
            effective_limit    = self._rate_limit
            if elapsed_since_call < effective_limit:
                remaining = effective_limit - elapsed_since_call
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
    # Pipeline gating
    # ------------------------------------------------------------------

    def _pipeline_ready(self) -> tuple[bool, str]:
        """
        Controleer of de pipeline genoeg data heeft voor zinvolle Claude-analyse.

        Returns (True, "") als pipeline groen is.
        Returns (False, reden) als één of meer voorwaarden niet voldaan zijn.
        """
        # Voorwaarde 1: budget niet in waarschuwingsstatus
        if self._budget_eur > 0 and self._month_cost_eur >= _BUDGET_WARN_PCT * self._budget_eur:
            return False, "BUDGET_WARNING actief"

        if self.logs_root is None:
            return False, "geen logs_root geconfigureerd"

        # Voorwaarde 2: minimaal N research-kandidaten in laatste 24u
        research_count = self._count_recent_research_candidates()
        if research_count < _PIPELINE_MIN_RESEARCH:
            return False, (
                f"onvoldoende research-kandidaten: {research_count}/{_PIPELINE_MIN_RESEARCH} "
                f"in laatste 24u"
            )

        # Voorwaarde 3: minimaal N gesloten paper trades
        closed_trades = self._count_closed_paper_trades()
        if closed_trades < _PIPELINE_MIN_CLOSED_TRADES:
            return False, (
                f"onvoldoende gesloten paper trades: {closed_trades}/{_PIPELINE_MIN_CLOSED_TRADES}"
            )

        return True, ""

    def _count_recent_research_candidates(self) -> int:
        """Tel accepted candidates in ANT_LOGS/research/ die in de laatste 24u zijn gelogd."""
        if self.logs_root is None:
            return 0

        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return 0

        cutoff = datetime.now(tz=timezone.utc).timestamp() - _PIPELINE_WINDOW_SECS
        count = 0

        for path in research_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        payload = rec.get("payload") or {}
                        if payload.get("action") != "candidate_accepted":
                            continue
                        ts_str = rec.get("timestamp", "")
                        if not ts_str:
                            count += 1  # geen timestamp → tel mee (fail-open voor oude logs)
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str)
                            if ts.timestamp() >= cutoff:
                                count += 1
                        except (ValueError, TypeError):
                            count += 1
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass

        return count

    def _count_closed_paper_trades(self) -> int:
        """Tel gesloten paper trades in ANT_LOGS/paper/ (entries met closed_at niet-null)."""
        if self.logs_root is None:
            return 0

        paper_dir = self.logs_root / "paper"
        if not paper_dir.exists():
            return 0

        count = 0
        for path in paper_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        payload = rec.get("payload") or {}
                        if payload.get("closed_at") is not None or payload.get("action") == "trade_closed":
                            count += 1
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass

        return count

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

        candidates_json = json.dumps(
            [_trim_candidate(c) for c in candidates], indent=2, default=str
        )
        prompt = _PROMPT_TEMPLATE.format(candidates_json=candidates_json)

        self._log.info(
            "Claude API call | model=%s candidates=%d month_cost=€%.4f/€%.2f",
            _MODEL, len(candidates), self._month_cost_eur, self._budget_eur,
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
        cost_eur        = (
            (input_tokens  / 1_000_000) * _COST_PER_M_INPUT
            + (output_tokens / 1_000_000) * _COST_PER_M_OUTPUT
        )
        self._month_cost_eur += cost_eur
        self._total_cost_eur += cost_eur
        self._append_cost_record(cost_eur)

        # Waarschuwing bij 80% budget
        if self._budget_eur > 0:
            usage_pct = self._month_cost_eur / self._budget_eur
            if usage_pct >= _BUDGET_WARN_PCT:
                self._log.warning(
                    "Budget waarschuwing: %.0f%% van €%.2f verbruikt (€%.4f) — "
                    "rate limit verlaagd naar 1 call/uur",
                    usage_pct * 100, self._budget_eur, self._month_cost_eur,
                )

        self._log.info(
            "Claude response ontvangen | input_tokens=%d output_tokens=%d cost=€%.4f",
            input_tokens, output_tokens, cost_eur,
        )

        analyses = self._parse_response(response_text)
        variants_written = 0

        for analysis in analyses:
            # Ondersteunt zowel nieuw flat formaat als oud genest formaat
            variant = analysis.get("improved_variant") or {
                "take_profit_pct": analysis.get("improved_tp_pct"),
                "stop_loss_pct":   analysis.get("improved_sl_pct"),
            }
            if variant.get("take_profit_pct") or variant.get("stop_loss_pct"):
                written = self._write_variant_to_ingestion(analysis, variant)
                if written:
                    variants_written += 1

        self._write_claude_log("claude_analysis_complete", {
            "candidates_analysed": len(candidates),
            "variants_written":    variants_written,
            "input_tokens":        input_tokens,
            "output_tokens":       output_tokens,
            "cost_usd":            round(cost_eur, 6),   # naam behouden voor backwards compat tests
            "cost_eur":            round(cost_eur, 6),
            "total_cost_usd":      round(self._total_cost_eur, 6),
            "total_cost_eur":      round(self._total_cost_eur, 6),
            "month_cost_eur":      round(self._month_cost_eur, 6),
            "budget_eur":          self._budget_eur,
            "analyses":            analyses,
        })

        self._last_action = f"claude:{len(candidates)}cands:{variants_written}variants"
        self._log.info(
            "Claude analyse klaar | kandidaten=%d varianten_geschreven=%d cost=€%.4f",
            len(candidates), variants_written, cost_eur,
        )

    def _parse_response(self, text: str) -> list[dict]:
        """Parseer JSON-array uit Claude response. Fail-closed: retourneert [] bij fouten."""
        if not text.strip():
            return []

        # Strip markdown code blocks (```json ... ``` of ``` ... ```)
        clean = text.replace("```json", "").replace("```", "").strip()

        # Fallback: knip alles buiten de eerste [ ... ] (array) of { ... } (object)
        start = clean.find("[")
        end   = clean.rfind("]")
        if start == -1 or end == -1 or end < start:
            # Probeer object fallback
            start = clean.find("{")
            end   = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            clean = clean[start : end + 1]

        try:
            parsed = json.loads(clean)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except json.JSONDecodeError as exc:
            self._log.warning(
                "Kon Claude response niet als JSON parsen: %s — raw[:200]=%r",
                exc, text[:200],
            )
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

        strategy_type = analysis.get("strategy_type") or "unknown"
        entry_kws = (
            analysis.get("keywords")
            or variant.get("entry_keywords")
            or ["claude", "improved"]
        )
        logic     = variant.get("logic_summary") or (
            f"Claude-verbeterd variant van {analysis.get('candidate_id', 'onbekend')[:8]} ({strategy_type})"
        )

        try:
            candidate = StrategyCandidate(
                candidate_id=str(uuid.uuid4()),
                name=f"claude_variant:{strategy_type}:{analysis.get('candidate_id', '')[:8]}",
                source="claude",
                source_url="",
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=logic[:500],
                parameters={
                    "strategy_type":    strategy_type,
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
