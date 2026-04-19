"""
ant_colony/queen/queen_advisor.py

QueenAdvisor — verbindt alle mieren-output met de Queen.

Architectuur:
  Claude adviseert, Queen beslist, nooit conflict.
  De Advisor is read-only ten aanzien van andere ants: hij leest logs
  maar schrijft nooit naar hun directories. Beslissingen worden alleen
  via Queen.apply_advisor_decision() doorgevoerd.

Verantwoordelijkheden:
  1. Leest research-kandidaten (sharpe, grade, regime)
  2. Berekent paper-trade prestaties per kandidaat/symbool
  3. Leest Claude adviezen uit ANT_LOGS/claude/advice/ (met TTL)
  4. Past beslissingslogica toe
  5. Retourneert een QueenDecision (Queen heeft altijd veto via apply)

Beslissingslogica:
  1. Paper win_rate > 0.60 over 20+ trades → kapitaal_verhogen
  2. Paper win_rate < 0.30 over 10+ trades → kapitaal_verlagen, deprioriteer
  3. Claude advies EN paper bevestigt → prioriteit_kandidaten
  4. Claude advies MAAR paper tegenspreekt → adviezen_genegeerd
  5. Geen paper data → neutral, wacht op bewijs

Regels:
  - Fail-closed: alle exceptions resulteren in een lege QueenDecision
  - Adviezen verlopen na advice_ttl_minutes (default 60)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# Drempelwaarden beslissingslogica
_WIN_HIGH         = 0.60   # win_rate boven dit → verhoog kapitaal
_WIN_LOW          = 0.30   # win_rate onder dit → verlaag kapitaal
_MIN_TRADES_HIGH  = 20     # minimale trades voor verhogen
_MIN_TRADES_LOW   = 10     # minimale trades voor verlagen
_CLAUDE_MIN_CONF  = 6      # minimale confidence voor Claude advies


# ---------------------------------------------------------------------------
# QueenDecision
# ---------------------------------------------------------------------------

@dataclass
class QueenDecision:
    """
    Advies van QueenAdvisor aan Queen.

    Queen heeft altijd veto: apply_advisor_decision() logt en voert uit
    wat de Queen accepteert.

    allocatie_aanpassingen: biome → nieuwe fractie (0, 1]
    prioriteit_kandidaten:  candidate_ids die als eerste paper-getest worden
    deprioriteer_kandidaten: candidate_ids met slechte resultaten
    kapitaal_verhogen:      mission_ids waarvoor meer budget is aanbevolen
    kapitaal_verlagen:      mission_ids waarvoor budget terugschroeven is aanbevolen
    adviezen_gevolgd:       leesbare omschrijving van gevolgde adviezen
    adviezen_genegeerd:     leesbare omschrijving van genegeerde adviezen + reden
    """
    allocatie_aanpassingen: dict[str, float]  = field(default_factory=dict)
    prioriteit_kandidaten:  list[str]          = field(default_factory=list)
    deprioriteer_kandidaten: list[str]         = field(default_factory=list)
    kapitaal_verhogen:      list[str]          = field(default_factory=list)
    kapitaal_verlagen:      list[str]          = field(default_factory=list)
    adviezen_gevolgd:       list[str]          = field(default_factory=list)
    adviezen_genegeerd:     list[str]          = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.allocatie_aanpassingen,
            self.prioriteit_kandidaten,
            self.deprioriteer_kandidaten,
            self.kapitaal_verhogen,
            self.kapitaal_verlagen,
            self.adviezen_gevolgd,
            self.adviezen_genegeerd,
        ])

    def to_dict(self) -> dict:
        return {
            "allocatie_aanpassingen": self.allocatie_aanpassingen,
            "prioriteit_kandidaten":  self.prioriteit_kandidaten,
            "deprioriteer_kandidaten": self.deprioriteer_kandidaten,
            "kapitaal_verhogen":      self.kapitaal_verhogen,
            "kapitaal_verlagen":      self.kapitaal_verlagen,
            "adviezen_gevolgd":       self.adviezen_gevolgd,
            "adviezen_genegeerd":     self.adviezen_genegeerd,
        }


# ---------------------------------------------------------------------------
# QueenAdvisor
# ---------------------------------------------------------------------------

class QueenAdvisor:
    """
    Verbindt alle mieren-output met de Queen via gestructureerde adviescycli.

    Args:
        queen:               Queen instantie (voor allocation_snapshot).
        logs_root:           Root van ANT_LOGS. None = geen disk-I/O.
        advice_ttl_minutes:  Vervaltijd voor Claude adviezen in minuten (default 60).
    """

    def __init__(
        self,
        queen,
        logs_root: Path | None,
        advice_ttl_minutes: int = 60,
    ) -> None:
        self._queen       = queen
        self._logs_root   = logs_root
        self._advice_ttl  = timedelta(minutes=advice_ttl_minutes)
        self._log         = logging.getLogger(f"{__name__}.advisor")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def advise(self, tick_context: dict | None = None) -> QueenDecision:
        """
        Voer één advies-cyclus uit.

        Leest alle beschikbare bronnen, past beslissingslogica toe en
        retourneert een QueenDecision. Fail-closed: exceptions resulteren
        in een lege beslissing.

        Args:
            tick_context: Optionele extra context (bijv. huidige tick, tijd).

        Returns:
            QueenDecision — altijd een geldig object, nooit None.
        """
        decision = QueenDecision()
        try:
            candidates   = self._read_research_candidates()
            paper_stats  = self._read_paper_stats()
            claude_advices = self._read_claude_advices()

            self._apply_paper_logic(decision, candidates, paper_stats)
            self._apply_claude_logic(decision, candidates, paper_stats, claude_advices)

            self._log.info(
                "Advies-cyclus klaar | prioriteit=%d deprioriteer=%d verhogen=%d verlagen=%d "
                "gevolgd=%d genegeerd=%d",
                len(decision.prioriteit_kandidaten),
                len(decision.deprioriteer_kandidaten),
                len(decision.kapitaal_verhogen),
                len(decision.kapitaal_verlagen),
                len(decision.adviezen_gevolgd),
                len(decision.adviezen_genegeerd),
            )
        except Exception:
            self._log.exception("Fout in QueenAdvisor.advise() — lege beslissing geretourneerd")
        return decision

    # ------------------------------------------------------------------
    # Bronnen lezen
    # ------------------------------------------------------------------

    def _read_research_candidates(self) -> list[dict]:
        """Lees geaccepteerde kandidaten uit ANT_LOGS/research/*.jsonl."""
        if self._logs_root is None:
            return []
        research_dir = self._logs_root / "research"
        if not research_dir.exists():
            return []

        candidates: list[dict] = []
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
                    if payload.get("action") == "candidate_accepted":
                        candidates.append(payload)
            except OSError:
                pass
        return candidates

    def _read_paper_stats(self) -> dict[str, dict]:
        """
        Bereken paper-trade statistieken per symbool.

        Retourneert: symbol → {"total_trades": int, "win_count": int,
                                "win_rate": float, "total_pnl": float}
        """
        if self._logs_root is None:
            return {}
        paper_dir = self._logs_root / "paper"
        if not paper_dir.exists():
            return {}

        stats: dict[str, dict] = {}

        for path in paper_dir.glob("*_trades.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        trade = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    symbol = trade.get("symbol")
                    pnl    = trade.get("realized_pnl", 0.0) or 0.0
                    if not symbol:
                        continue
                    if symbol not in stats:
                        stats[symbol] = {"total_trades": 0, "win_count": 0, "total_pnl": 0.0}
                    stats[symbol]["total_trades"] += 1
                    stats[symbol]["total_pnl"]    += pnl
                    if pnl > 0:
                        stats[symbol]["win_count"] += 1
            except OSError:
                pass

        for s in stats.values():
            t = s["total_trades"]
            s["win_rate"] = s["win_count"] / t if t > 0 else 0.0

        return stats

    def _read_claude_advices(self) -> list[dict]:
        """
        Lees Claude adviezen uit ANT_LOGS/claude/advice/*.json.

        Filtert adviezen ouder dan advice_ttl op basis van het "timestamp" veld.
        """
        if self._logs_root is None:
            return []
        advice_dir = self._logs_root / "claude" / "advice"
        if not advice_dir.exists():
            return []

        now   = datetime.now(tz=timezone.utc)
        valid: list[dict] = []

        for path in advice_dir.glob("*.json"):
            try:
                advice = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            ts_str = advice.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (now - ts) > self._advice_ttl:
                    continue   # verlopen
            except (ValueError, TypeError):
                continue       # ongeldig timestamp → negeer
            valid.append(advice)

        return valid

    # ------------------------------------------------------------------
    # Beslissingslogica
    # ------------------------------------------------------------------

    def _apply_paper_logic(
        self,
        decision: QueenDecision,
        candidates: list[dict],
        paper_stats: dict[str, dict],
    ) -> None:
        """
        Regels 1 en 2: paper-prestaties bepalen kapitaal- en prioriteitsaanpassingen.

        1. win_rate > 0.60 over 20+ trades → kandidaat in kapitaal_verhogen
        2. win_rate < 0.30 over 10+ trades → kandidaat in kapitaal_verlagen + deprioriteer
        """
        if not paper_stats:
            return

        # Bouw symbol → [candidate_id] mapping voor kandidaten
        symbol_to_cands: dict[str, list[str]] = {}
        for c in candidates:
            sym = c.get("symbol", "")
            cid = c.get("candidate_id", "")
            if sym and cid:
                symbol_to_cands.setdefault(sym, []).append(cid)

        # Per symbool: zoek actieve paper-missies die dat symbool bevatten
        active_missions = self._queen.active_missions
        symbol_to_missions: dict[str, list[str]] = {}
        for mid, mission in active_missions.items():
            if mission.ant_type != "paper_ant":
                continue
            for sym in (mission.market_scope.symbols or []):
                symbol_to_missions.setdefault(sym, []).append(mid)

        for symbol, st in paper_stats.items():
            total  = st["total_trades"]
            wr     = st["win_rate"]
            cids   = symbol_to_cands.get(symbol, [])
            mids   = symbol_to_missions.get(symbol, [])

            if total >= _MIN_TRADES_HIGH and wr > _WIN_HIGH:
                decision.kapitaal_verhogen.extend(mids)
                decision.adviezen_gevolgd.append(
                    f"paper:{symbol} win_rate={wr:.2%} ({total} trades) → kapitaal verhogen"
                )

            elif total >= _MIN_TRADES_LOW and wr < _WIN_LOW:
                decision.kapitaal_verlagen.extend(mids)
                decision.deprioriteer_kandidaten.extend(cids)
                decision.adviezen_gevolgd.append(
                    f"paper:{symbol} win_rate={wr:.2%} ({total} trades) → verlagen + deprioriteer"
                )

    def _apply_claude_logic(
        self,
        decision: QueenDecision,
        candidates: list[dict],
        paper_stats: dict[str, dict],
        claude_advices: list[dict],
    ) -> None:
        """
        Regels 3 en 4: Claude adviezen getoetst aan paper-bewijs.

        3. Claude adviseert EN paper bevestigt (wr > 0.60, 20+ trades) → prioriteit
        4. Claude adviseert MAAR paper tegenspreekt (wr < 0.30, 10+ trades) → genegeerd
        5. Geen paper data → neutral, wacht op bewijs (advies genegeerd)
        """
        if not claude_advices:
            return

        # Bouw candidate_id → symbol mapping
        cid_to_symbol: dict[str, str] = {
            c.get("candidate_id", ""): c.get("symbol", "")
            for c in candidates
            if c.get("candidate_id")
        }

        for advice in claude_advices:
            advice_id  = advice.get("advice_id", "?")
            cid        = advice.get("candidate_id", "")
            confidence = advice.get("confidence", 0)
            action     = advice.get("action", "")

            if confidence < _CLAUDE_MIN_CONF:
                decision.adviezen_genegeerd.append(
                    f"claude:{advice_id} cand={cid[:8]} confidence={confidence} < {_CLAUDE_MIN_CONF} (te laag)"
                )
                continue

            if action not in ("promote", "prioritize"):
                continue   # alleen positieve adviezen beoordelen

            symbol = cid_to_symbol.get(cid)
            if symbol is None:
                decision.adviezen_genegeerd.append(
                    f"claude:{advice_id} cand={cid[:8]} geen research-match gevonden"
                )
                continue

            st = paper_stats.get(symbol)
            if st is None:
                decision.adviezen_genegeerd.append(
                    f"claude:{advice_id} cand={cid[:8]} sym={symbol} geen paper-data, wacht op bewijs"
                )
                continue

            total = st["total_trades"]
            wr    = st["win_rate"]

            if total >= _MIN_TRADES_HIGH and wr > _WIN_HIGH:
                # Paper bevestigt → verhoog prioriteit
                if cid not in decision.prioriteit_kandidaten:
                    decision.prioriteit_kandidaten.append(cid)
                decision.adviezen_gevolgd.append(
                    f"claude:{advice_id} cand={cid[:8]} sym={symbol} bevestigd door paper "
                    f"(wr={wr:.2%}/{total}t) → prioriteit"
                )

            elif total >= _MIN_TRADES_LOW and wr < _WIN_LOW:
                # Paper tegenspreekt → negeer Claude advies
                decision.adviezen_genegeerd.append(
                    f"claude:{advice_id} cand={cid[:8]} sym={symbol} paper tegenspreekt "
                    f"(wr={wr:.2%}/{total}t)"
                )

            else:
                # Onvoldoende bewijs
                decision.adviezen_genegeerd.append(
                    f"claude:{advice_id} cand={cid[:8]} sym={symbol} onvoldoende trades "
                    f"({total}t) voor beslissing"
                )
