"""
ant_colony/ants/strategy_ant.py

StrategyAnt — genereert nieuwe strategie-varianten door bestaande kandidaten
te combineren, muteren en uit te breiden. Valideert elke variant via
walk-forward backtesting.

Verantwoordelijkheden:
  1. Broncandidaten lezen:
       - ANT_LOGS/research/*.jsonl  → status RESEARCH
       - ANT_LOGS/ingestion/*.jsonl → status INGESTED (candidate_ingested payload)
  2. Drie mutatietypen genereren per tick:
       - mutation:          3 parametervarianten per kandidaat (TP/SL/bars)
       - combination:       twee kandidaten combineren tot één variant
       - feature_expansion: uitbreiding met een extra indicator (tightere/ruimere exits)
  3. Elke variant backtesten via walk-forward:
       - Candles ophalen via BiomeAdapter.get_candles()
       - Train: eerste 70% — wordt niet gebruikt voor evaluatie
       - Test:  laatste 30% — sharpe > 0.5 en win_rate > 0.45 vereist
  4. Geaccepteerde varianten loggen naar ANT_LOGS/strategy/{ant_id}.jsonl
  5. Heartbeat rapporteren aan scheduler na elke tick
  6. TTL expiry stopt de ant netjes

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.lab.backtester import Backtester, BacktestConfig, BacktestResults, OHLCVBar
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.strategy_candidate import (
    BacktestResults as CandidateBacktestResults,
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)

# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

_MIN_CANDLES        = 50    # minimum totaal candles voor walk-forward split
_CANDLE_LIMIT       = 150   # candles ophalen per tick per symbool
_TRAIN_SPLIT        = 0.70  # 70% train, 30% test
_SHARPE_THRESHOLD   = 0.5
_WIN_RATE_THRESHOLD = 0.45

_MAX_MUTATIONS_PER_TICK    = 5
_MAX_COMBINATIONS_PER_TICK = 3
_MAX_EXPANSIONS_PER_TICK   = 5

# Drie parametersets voor mutaties
_MUTATION_CONFIGS = [
    {"tp_pct": 0.04, "sl_pct": 0.01, "max_bars_held": 5,  "label": "tight"},
    {"tp_pct": 0.06, "sl_pct": 0.02, "max_bars_held": 10, "label": "standard"},
    {"tp_pct": 0.08, "sl_pct": 0.03, "max_bars_held": 15, "label": "wide"},
]

# Twee expansie-profielen (RSI-filter = tighter stop; BB-filter = ruimere TP)
_EXPANSION_CONFIGS = [
    {"tp_delta": 0.00, "sl_delta": -0.005, "bars_delta": -3, "feature": "rsi_filter"},
    {"tp_delta": 0.01, "sl_delta": 0.000,  "bars_delta":  0, "feature": "bb_filter"},
]

_DEFAULT_TP  = 0.06
_DEFAULT_SL  = 0.03
_DEFAULT_BARS = 10


# ---------------------------------------------------------------------------
# Intern type voor broncandidaten
# ---------------------------------------------------------------------------

@dataclass
class _SourceCandidate:
    """Genormaliseerde broncandidaat voor intern gebruik."""
    candidate_id:     str
    name:             str
    symbol:           str
    direction:        str        # "long" | "short"
    tp_pct:           float
    sl_pct:           float
    max_bars_held:    int
    entry_conditions: dict
    exit_conditions:  dict
    logic_summary:    str
    source_type:      str        # "research" | "ingested"
    fitness_score:    float = 0.0


# ---------------------------------------------------------------------------
# StrategyAnt
# ---------------------------------------------------------------------------

class StrategyAnt:
    """
    Genereert en valideert strategie-varianten op basis van bestaande kandidaten.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry voor candle-data via get_candles().
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root

        self._backtester      = Backtester()
        self._seen_variants:  set[str] = set()
        self._log_seq:        int = 0
        self._status:         AntStatus = AntStatus.IDLE
        self._last_action:    str = "init"

        self._log = logging.getLogger(f"ant.strategy.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "strategy"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log.info("Logs map: %s", log_dir)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "StrategyAnt gestart | mission=%s ttl=%ds",
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
            self._log.info("StrategyAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("StrategyAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één generatiecyclus: lezen, muteren, testen, loggen."""
        emitted = 0
        sources: list[_SourceCandidate] = []

        try:
            research_files = self._count_research_files()
            self._log.info(
                "strategy tick start | research_files=%d", research_files
            )

            sources = self._read_source_candidates()
            self._log.info(
                "strategy tick | research_candidates=%d seen_variants=%d",
                len(sources), len(self._seen_variants),
            )

            if not sources:
                self._last_action = "tick_no_sources"
                return

            # --- Mutaties ---
            self._log.info(
                "mutaties genereren voor %d kandidaat(en)", len(sources[:_MAX_MUTATIONS_PER_TICK])
            )
            for c in sources[:_MAX_MUTATIONS_PER_TICK]:
                for spec in self._generate_mutations(c):
                    if self._try_backtest_and_emit(c.symbol, spec):
                        emitted += 1

            # --- Combinaties (paren van kandidaten per symbool) ---
            pairs = [
                (a, b)
                for i, a in enumerate(sources)
                for b in sources[i + 1:]
                if a.symbol == b.symbol
            ]
            if pairs:
                self._log.info("combinaties genereren: %d paar(en)", len(pairs[:_MAX_COMBINATIONS_PER_TICK]))
            for a, b in pairs[:_MAX_COMBINATIONS_PER_TICK]:
                spec = self._generate_combination(a, b)
                if self._try_backtest_and_emit(a.symbol, spec):
                    emitted += 1

            # --- Feature expansies ---
            for c in sources[:_MAX_EXPANSIONS_PER_TICK]:
                for spec in self._generate_expansions(c):
                    if self._try_backtest_and_emit(c.symbol, spec):
                        emitted += 1

        except Exception:
            self._log.exception(
                "Fout in strategy tick (sources=%d emitted=%d) — tick afgebroken",
                len(sources), emitted,
            )

        self._log.info(
            "strategy tick klaar | research_candidates=%d seen=%d new=%d",
            len(sources), len(self._seen_variants), emitted,
        )
        self._last_action = f"tick_emitted:{emitted}" if sources else "tick_no_sources"

    # ------------------------------------------------------------------
    # Broncandidaten lezen
    # ------------------------------------------------------------------

    def _count_research_files(self) -> int:
        """Geeft het aantal research JSONL-bestanden terug (voor logging)."""
        if self.logs_root is None:
            return 0
        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return 0
        return sum(1 for _ in research_dir.glob("*.jsonl"))

    def _read_source_candidates(self) -> list[_SourceCandidate]:
        """Lees research + ingestion logs en retourneer genormaliseerde kandidaten."""
        if self.logs_root is None:
            return []

        results: list[_SourceCandidate] = []
        default_symbol = (
            self.mission.market_scope.symbols[0]
            if self.mission.market_scope.symbols
            else ""
        )

        # Research kandidaten
        research_dir = self.logs_root / "research"
        if research_dir.exists():
            for path in sorted(research_dir.glob("*.jsonl")):
                results.extend(
                    self._parse_research_file(path, default_symbol)
                )

        # Ingestion kandidaten
        ingestion_dir = self.logs_root / "ingestion"
        if ingestion_dir.exists():
            for path in sorted(ingestion_dir.glob("*.jsonl")):
                results.extend(
                    self._parse_ingestion_file(path, default_symbol)
                )

        return results

    def _parse_research_file(
        self, path: Path, default_symbol: str
    ) -> list[_SourceCandidate]:
        """
        Parseer een research JSONL-bestand.

        Research logs zijn AuditEvents:
          {"event_type": "action_executed", "payload": {"action": "candidate_accepted", ...}}
        Filter: event_type == "action_executed" AND payload.action == "candidate_accepted".
        """
        candidates: list[_SourceCandidate] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Uitpakken AuditEvent envelope
                if "event_type" in record:
                    payload = record.get("payload") or {}
                    if payload.get("action") != "candidate_accepted":
                        continue
                    data = payload
                else:
                    # Directe StrategyCandidate JSON (legacy/tests)
                    data = record

                cid = data.get("candidate_id", "")
                if not cid:
                    continue

                # Symbol: payload.symbol heeft voorrang op market_scope.symbol
                ms     = data.get("market_scope") or {}
                symbol = data.get("symbol") or ms.get("symbol") or default_symbol
                if not symbol:
                    continue

                exit_c  = data.get("exit_conditions") or {}
                entry_c = data.get("entry_conditions") or {}

                direction = "long"
                for v in entry_c.values():
                    if v in ("long", "short"):
                        direction = v
                        break

                candidates.append(_SourceCandidate(
                    candidate_id  = cid,
                    name          = data.get("name", cid),
                    symbol        = symbol,
                    direction     = direction,
                    tp_pct        = float(exit_c.get("take_profit_pct") or _DEFAULT_TP),
                    sl_pct        = float(exit_c.get("stop_loss_pct")   or _DEFAULT_SL),
                    max_bars_held = int(exit_c.get("max_bars_held")     or _DEFAULT_BARS),
                    entry_conditions = entry_c,
                    exit_conditions  = exit_c,
                    logic_summary    = data.get("logic_summary", ""),
                    source_type      = "research",
                    fitness_score    = float(
                        data.get("fitness_score") or data.get("sharpe") or 0.0
                    ),
                ))

        except OSError:
            self._log.warning("Kan research-log niet lezen: %s", path)
        return candidates

    def _parse_ingestion_file(
        self, path: Path, default_symbol: str
    ) -> list[_SourceCandidate]:
        """Parseer een ingestion JSONL-bestand (AuditEvents met candidate_ingested payload)."""
        candidates: list[_SourceCandidate] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record  = json.loads(line)
                    payload = record.get("payload") or {}
                except json.JSONDecodeError:
                    continue

                if payload.get("action") != "candidate_ingested":
                    continue

                cid = payload.get("candidate_id", "")
                if not cid:
                    continue

                entry_kw = payload.get("entry_keywords") or []
                exit_kw  = payload.get("exit_keywords")  or []

                if not entry_kw or not exit_kw:
                    continue

                candidates.append(_SourceCandidate(
                    candidate_id  = cid,
                    name          = payload.get("name", cid),
                    symbol        = default_symbol,
                    direction     = "long",
                    tp_pct        = _DEFAULT_TP,
                    sl_pct        = _DEFAULT_SL,
                    max_bars_held = _DEFAULT_BARS,
                    entry_conditions = {"keywords": entry_kw},
                    exit_conditions  = {
                        "keywords":       exit_kw,
                        "take_profit_pct": _DEFAULT_TP,
                        "stop_loss_pct":   _DEFAULT_SL,
                    },
                    logic_summary = payload.get("logic_summary", ""),
                    source_type   = "ingested",
                    fitness_score = 0.0,
                ))

        except OSError:
            self._log.warning("Kan ingestion-log niet lezen: %s", path)
        return candidates

    # ------------------------------------------------------------------
    # Variant-generatoren
    # ------------------------------------------------------------------

    def _generate_mutations(self, c: _SourceCandidate) -> list[dict]:
        """Genereer maximaal 3 parameter-mutaties van een broncandidaat."""
        variants: list[dict] = []
        for cfg in _MUTATION_CONFIGS:
            tp   = cfg["tp_pct"]
            sl   = cfg["sl_pct"]
            bars = cfg["max_bars_held"]
            if tp == c.tp_pct and sl == c.sl_pct and bars == c.max_bars_held:
                continue  # identiek aan parent
            variants.append({
                "mutation_type":    "mutation",
                "parent_ids":       [c.candidate_id],
                "direction":        c.direction,
                "tp_pct":           tp,
                "sl_pct":           sl,
                "max_bars_held":    bars,
                "entry_conditions": c.entry_conditions,
                "exit_conditions":  {
                    "take_profit_pct": tp,
                    "stop_loss_pct":   sl,
                    "max_bars_held":   bars,
                },
                "name":         f"MUT {c.name} [{cfg['label']}]",
                "logic_summary": (
                    f"Mutatie van '{c.name}': "
                    f"tp={tp:.0%} sl={sl:.0%} bars={bars}"
                ),
            })
        return variants

    def _generate_combination(
        self, a: _SourceCandidate, b: _SourceCandidate
    ) -> dict:
        """Combineer twee kandidaten tot één variant met gedeelde entry-condities."""
        tp   = round((a.tp_pct + b.tp_pct) / 2, 4)
        sl   = round((a.sl_pct + b.sl_pct) / 2, 4)
        bars = max(a.max_bars_held, b.max_bars_held)

        merged_entry = {**a.entry_conditions, **b.entry_conditions}
        merged_entry["combination_parents"] = [a.name, b.name]

        return {
            "mutation_type":    "combination",
            "parent_ids":       [a.candidate_id, b.candidate_id],
            "direction":        a.direction,
            "tp_pct":           tp,
            "sl_pct":           sl,
            "max_bars_held":    bars,
            "entry_conditions": merged_entry,
            "exit_conditions":  {
                "take_profit_pct": tp,
                "stop_loss_pct":   sl,
                "max_bars_held":   bars,
            },
            "name":         f"COMBO {a.name} + {b.name}",
            "logic_summary": (
                f"Combinatie van '{a.name}' en '{b.name}': "
                f"gedeelde entry-condities, gemiddelde TP/SL"
            ),
        }

    def _generate_expansions(self, c: _SourceCandidate) -> list[dict]:
        """Genereer twee feature-expansie varianten van een broncandidaat."""
        variants: list[dict] = []
        for cfg in _EXPANSION_CONFIGS:
            tp   = max(0.01, round(c.tp_pct + cfg["tp_delta"], 4))
            sl   = max(0.005, round(c.sl_pct + cfg["sl_delta"], 4))
            bars = max(1, c.max_bars_held + cfg["bars_delta"])
            feature = cfg["feature"]
            variants.append({
                "mutation_type":    "feature_expansion",
                "parent_ids":       [c.candidate_id],
                "direction":        c.direction,
                "tp_pct":           tp,
                "sl_pct":           sl,
                "max_bars_held":    bars,
                "entry_conditions": {**c.entry_conditions, "extra_filter": feature},
                "exit_conditions":  {
                    "take_profit_pct": tp,
                    "stop_loss_pct":   sl,
                    "max_bars_held":   bars,
                },
                "name":         f"EXP {c.name} +{feature}",
                "logic_summary": (
                    f"Feature-expansie van '{c.name}': "
                    f"{feature} toegevoegd als extra filter"
                ),
            })
        return variants

    # ------------------------------------------------------------------
    # Walk-forward backtest + emissie
    # ------------------------------------------------------------------

    def _try_backtest_and_emit(self, symbol: str, spec: dict) -> bool:
        """
        Voer walk-forward backtest uit voor spec. Emitteer als boven drempel.
        Retourneert True als een nieuwe variant geëmitteerd is.
        """
        key = self._variant_key(spec)
        if key in self._seen_variants:
            return False
        self._seen_variants.add(key)

        if symbol not in self.mission.market_scope.symbols:
            return False

        candles = self._fetch_candles(symbol)
        if len(candles) < _MIN_CANDLES:
            self._log.debug(
                "Te weinig candles voor %s (%d/%d)", symbol, len(candles), _MIN_CANDLES
            )
            return False

        # Walk-forward split: gebruik alleen test-set voor evaluatie
        split     = int(len(candles) * _TRAIN_SPLIT)
        test_bars = candles[split:]
        if len(test_bars) < 2:
            return False

        try:
            config  = BacktestConfig(
                direction       = spec["direction"],
                take_profit_pct = spec["tp_pct"],
                stop_loss_pct   = spec["sl_pct"],
                max_bars_held   = spec["max_bars_held"],
            )
            results = self._backtester.run(test_bars, config)
        except Exception:
            self._log.exception("Backtest mislukt voor %s / %s", symbol, spec["mutation_type"])
            return False

        sharpe   = results.sharpe_ratio   or 0.0
        win_rate = results.win_rate or 0.0

        self._log.debug(
            "%s %s | sharpe=%.3f win_rate=%.3f trades=%s",
            symbol, spec["mutation_type"], sharpe, win_rate, results.total_trades,
        )

        if sharpe < _SHARPE_THRESHOLD or win_rate < _WIN_RATE_THRESHOLD:
            return False

        candidate = self._build_candidate(symbol, spec, results)
        self._write_candidate(candidate)
        self._last_action = f"emitted:{spec['mutation_type']}:{symbol}"
        self._log.info(
            "VARIANT GEËMITTEERD | %s %s | sharpe=%.3f win_rate=%.3f parents=%s",
            symbol, spec["mutation_type"], sharpe, win_rate, spec["parent_ids"],
        )
        return True

    def _build_candidate(
        self, symbol: str, spec: dict, results: BacktestResults
    ) -> StrategyCandidate:
        """Bouw een StrategyCandidate van een geslaagde variant."""
        candidate_id = (
            f"strategy-{symbol.lower().replace('-', '')}"
            f"-{spec['mutation_type']}-{uuid.uuid4().hex[:8]}"
        )
        return StrategyCandidate(
            candidate_id     = candidate_id,
            name             = spec["name"],
            source           = "internal",
            biome            = self.mission.market_scope.biome,
            market_scope     = {
                "symbol":    symbol,
                "biome":     self.mission.market_scope.biome,
                "timeframe": (
                    self.mission.market_scope.timeframes[0]
                    if self.mission.market_scope.timeframes else "1h"
                ),
            },
            logic_summary    = spec["logic_summary"],
            parameters       = {
                "tp_pct":        spec["tp_pct"],
                "sl_pct":        spec["sl_pct"],
                "max_bars_held": spec["max_bars_held"],
                "direction":     spec["direction"],
            },
            entry_conditions = spec["entry_conditions"],
            exit_conditions  = spec["exit_conditions"],
            backtest_results = CandidateBacktestResults(
                sharpe_ratio     = results.sharpe_ratio,
                max_drawdown_pct = results.max_drawdown_pct,
                total_trades     = results.total_trades,
                win_rate         = results.win_rate,
            ),
            fitness_score    = round(results.sharpe_ratio or 0.0, 4),
            status           = CandidateStatus.RESEARCH,
            provenance       = [
                ProvenanceEntry(
                    actor  = self.ant_id,
                    action = "generated",
                    details = {
                        "mutation_type": spec["mutation_type"],
                        "parent_ids":    spec["parent_ids"],
                        "mission_id":    self.mission.mission_id,
                        "symbol":        symbol,
                        "walk_forward_split": _TRAIN_SPLIT,
                    },
                )
            ],
        )

    # ------------------------------------------------------------------
    # Candle ophalen
    # ------------------------------------------------------------------

    def _fetch_candles(self, symbol: str) -> list[OHLCVBar]:
        """Haal candles op en converteer naar OHLCVBar. Fail-closed: [] bij elke fout."""
        biome_id = self.mission.market_scope.biome
        timeframe = (
            self.mission.market_scope.timeframes[0]
            if self.mission.market_scope.timeframes else "1h"
        )
        try:
            adapter = self.biome_registry.get(biome_id)
            if adapter is None or not adapter.is_available():
                return []

            get_candles_fn = getattr(adapter, "get_candles", None)
            if get_candles_fn is None:
                return []

            raw = get_candles_fn(symbol, timeframe, _CANDLE_LIMIT) or []
            return [
                OHLCVBar(
                    timestamp = c.timestamp,
                    open      = c.open,
                    high      = c.high,
                    low       = c.low,
                    close     = c.close,
                    volume    = c.volume,
                )
                for c in raw if c.close > 0
            ]
        except Exception:
            self._log.exception("Fout bij ophalen candles voor %s", symbol)
            return []

    # ------------------------------------------------------------------
    # Hulpfuncties
    # ------------------------------------------------------------------

    def _variant_key(self, spec: dict) -> str:
        """Deterministisch deduplicatie-sleutel voor een variant."""
        raw = (
            f"{spec['mutation_type']}:"
            f"{spec['direction']}:"
            f"{spec['tp_pct']:.4f}:"
            f"{spec['sl_pct']:.4f}:"
            f"{spec['max_bars_held']}:"
            f"{sorted(spec['parent_ids'])}"
        )
        return hashlib.md5(raw.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(self, candidate: StrategyCandidate) -> None:
        """Schrijf kandidaat als JSON-regel naar ANT_LOGS/strategy/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type = AuditEventType.ACTION_EXECUTED,
            source     = self.ant_id,
            mission_id = self.mission.mission_id,
            node_id    = self.mission.allowed_node,
            sequence   = self._log_seq,
            payload    = {
                "action":           "variant_emitted",
                "candidate_id":     candidate.candidate_id,
                "name":             candidate.name,
                "mutation_type":    candidate.provenance[0].details.get("mutation_type"),
                "parent_ids":       candidate.provenance[0].details.get("parent_ids"),
                "symbol":           candidate.provenance[0].details.get("symbol"),
                "sharpe_ratio":     candidate.backtest_results.sharpe_ratio if candidate.backtest_results else None,
                "win_rate":         candidate.backtest_results.win_rate if candidate.backtest_results else None,
                "total_trades":     candidate.backtest_results.total_trades if candidate.backtest_results else None,
                "status":           candidate.status.value,
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "strategy" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon variant niet naar disk schrijven: %s", log_path)

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
                ant_id    = self.ant_id,
                mission_id = self.mission.mission_id,
                node_id   = self.mission.allowed_node,
                status    = hb_status,
                budget_used = 0.0,
                last_action = self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
