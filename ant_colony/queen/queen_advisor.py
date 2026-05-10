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
  1. Paper win_rate > 0.55 over 10+ trades → kapitaal_verhogen
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
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_log = logging.getLogger(__name__)

# Amsterdam timezone voor markturen
_AMS_TZ = ZoneInfo("Europe/Amsterdam")

# Equities markturen (Amsterdam): ma-vr 15:30-22:00
_MARKET_OPEN_HOUR    = 15
_MARKET_OPEN_MINUTE  = 30
_MARKET_CLOSE_HOUR   = 22
_MARKET_CLOSE_MINUTE = 0

# Briefing-venster: 15:15-15:30 Amsterdam (15 min voor market open)
_BRIEFING_HOUR   = 15
_BRIEFING_MINUTE = 15

# Weekend log interval
_WEEKEND_LOG_INTERVAL = timedelta(hours=1)

# Drempelwaarden beslissingslogica
_WIN_HIGH         = 0.60   # Claude-paper bevestiging blijft strenger
_PAPER_WIN_HIGH   = 0.55   # paper win_rate boven dit → verhoog kapitaal
_WIN_LOW          = 0.30   # win_rate onder dit → verlaag kapitaal
_MIN_TRADES_HIGH  = 20     # minimale trades voor Claude-paper bevestiging
_PAPER_MIN_TRADES_HIGH = 10 # minimale trades voor paper verhogen

# Mapping van backtester best_regime naar PaperAnt-regime
_REGIME_MAP: dict[str, str] = {
    "bull":     "TRENDING",
    "sideways": "SIDEWAYS",
    "bear":     "VOLATILE",
}

_RS_REGIME_TO_QUEEN_REGIME: dict[str, str] = {
    "RISK_ON":  "RISK_ON",
    "NEUTRAL":  "SIDEWAYS",
    "RISK_OFF": "VOLATILE",
    "CRISIS":   "VOLATILE",
    "TRENDING": "TRENDING",
    "SIDEWAYS": "SIDEWAYS",
    "VOLATILE": "VOLATILE",
}

# Market signal tabel: (regime, news_sentiment) → (combined_signal, position_size_mult, sl_mult)
_MARKET_SIGNAL_TABLE: dict[tuple[str, str], tuple[str, float, float]] = {
    ("SIDEWAYS", "bearish"):  ("cautious",           0.5,  1.25),
    ("SIDEWAYS", "bullish"):  ("normal",              1.0,  1.0),
    ("SIDEWAYS", "neutral"):  ("normal",              1.0,  1.0),
    ("TRENDING", "bullish"):  ("optimistic",          1.25, 1.0),
    ("TRENDING", "bearish"):  ("cautious_trending",   0.75, 1.0),
    ("TRENDING", "neutral"):  ("normal",              1.0,  1.0),
    ("VOLATILE", "bullish"):  ("restrictive",         0.5,  1.0),
    ("VOLATILE", "bearish"):  ("restrictive",         0.5,  1.0),
    ("VOLATILE", "neutral"):  ("restrictive",         0.5,  1.0),
}
_MARKET_SIGNAL_DEFAULT: tuple[str, float, float] = ("normal", 1.0, 1.0)


def _compute_market_signal(
    regime: str | None,
    news_sentiment: str | None,
) -> tuple[str, float, float]:
    """
    Bereken gecombineerd signaal op basis van regime en nieuwssentiment.

    Returns:
        (combined_signal, position_size_mult, sl_mult)
        Valt terug op ("normal", 1.0, 1.0) als regime of sentiment ontbreekt.
    """
    if regime is None or news_sentiment is None:
        return _MARKET_SIGNAL_DEFAULT
    key = (regime.upper(), (news_sentiment or "neutral").lower())
    return _MARKET_SIGNAL_TABLE.get(key, _MARKET_SIGNAL_DEFAULT)


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _parse_context_ts(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _watchtower_context_is_fresh(wt: dict, max_age: timedelta = timedelta(minutes=30)) -> bool:
    ts = _parse_context_ts(wt.get("last_signal_ts"))
    if ts is None:
        return False
    age = datetime.now(tz=timezone.utc) - ts
    return timedelta(0) <= age <= max_age


def _candidate_matches_watchtower(candidate: dict, wt: dict, regime: str | None) -> bool:
    symbol = str(candidate.get("symbol") or candidate.get("asset") or "").upper().strip()
    wt_asset = str(wt.get("last_asset") or "").upper().strip()
    if symbol and wt_asset and symbol == wt_asset:
        return True

    wt_asset_class = str(wt.get("last_asset_class") or "").lower().strip()
    biome = str(candidate.get("biome") or "").lower().strip()
    if wt_asset_class and biome and wt_asset_class in {biome, f"{biome}s"}:
        return True
    if wt_asset_class == "equity" and biome == "equities":
        return True

    wt_regime = str(wt.get("last_regime") or "").upper().strip()
    if not wt_regime:
        return False
    candidate_regime = str(candidate.get("best_regime") or "").lower().strip()
    mapped_candidate_regime = _REGIME_MAP.get(candidate_regime, candidate_regime.upper())
    return wt_regime in {mapped_candidate_regime, str(regime or "").upper().strip()}
_MIN_TRADES_LOW   = 10     # minimale trades voor verlagen
_CLAUDE_MIN_CONF  = 6      # minimale confidence voor Claude advies

# Paper stats tijdvenster — historische trades (pre-reset, bevroren prijzen) buiten
# dit venster worden genegeerd bij win_rate berekening.
_STATS_WINDOW_DAYS = 7
_STAGNANT_TOP3_AGE = timedelta(days=14)


# ---------------------------------------------------------------------------
# Diversiteitselectie — top-N met unieke symbolen én strategy_types
# ---------------------------------------------------------------------------

def select_diverse_top_n(candidates: list[dict], n: int = 3) -> list[dict]:
    """
    Selecteer top-N kandidaten op sharpe met diversiteitsconstraint.

    Regels (in volgorde):
      1. Sorteer op sharpe (hoogste eerst)
      2. Maximaal 1 kandidaat per symbool
      3. Maximaal 1 kandidaat per strategy_type

    Args:
        candidates: Lijst van candidate-dicts met "sharpe", "symbol", "strategy_type".
        n:          Maximum aantal te retourneren kandidaten.

    Returns:
        Gefilterde lijst, maximaal n entries, gesorteerd op sharpe.
    """
    sorted_cands = sorted(
        candidates,
        key=lambda c: float(c.get("sharpe_ratio") or c.get("sharpe") or 0),
        reverse=True,
    )

    seen_symbols: set[str] = set()
    seen_types:   set[str] = set()
    result: list[dict] = []

    for c in sorted_cands:
        if len(result) >= n:
            break
        symbol        = _candidate_symbol_key(c)
        strategy_type = _candidate_strategy_type_key(c)

        if symbol and symbol in seen_symbols:
            continue
        if strategy_type and strategy_type in seen_types:
            continue

        if symbol:
            seen_symbols.add(symbol)
        if strategy_type:
            seen_types.add(strategy_type)
        result.append(c)

    _log.info(
        "Queen diversiteit | top3=%s typen=%s",
        [c.get("symbol") or c.get("asset") or "unknown" for c in result],
        [c.get("strategy_type") or c.get("strategy") or "unknown" for c in result],
    )
    return result


def _candidate_symbol_key(candidate: dict) -> str:
    return str(candidate.get("symbol") or candidate.get("asset") or "").upper().strip()


def _candidate_strategy_type_key(candidate: dict) -> str:
    return str(candidate.get("strategy_type") or candidate.get("strategy") or "unknown").lower().strip()


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
    watchtower_context:     dict               = field(default_factory=dict)
    regime:                 str | None         = None

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
            "watchtower_context":     self.watchtower_context,
            "regime":                 self.regime,
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
        stats_window_days: int = _STATS_WINDOW_DAYS,
    ) -> None:
        self._queen             = queen
        self._logs_root         = logs_root
        self._advice_ttl        = timedelta(minutes=advice_ttl_minutes)
        self._stats_window      = timedelta(days=stats_window_days)
        self._log               = logging.getLogger(f"{__name__}.advisor")
        self._stats_window_logged = False   # one-time startup log guard
        self._cache_ttl = timedelta(seconds=max(1, _env_int("QUEEN_ADVISOR_CACHE_SECONDS", 60)))
        self._io_cache_lock = threading.RLock()
        self._io_cache: dict[str, tuple[datetime, tuple[int, int, int], object]] = {}
        self._stagnant_cache_lock = threading.RLock()
        self._stagnant_cache: tuple[datetime, tuple, list[dict]] | None = None

        # Weekend protocol state
        self._last_weekend_log: datetime | None = None
        self._briefing_written_date: object = None   # date of last briefing
        self._market_was_open: bool | None = None    # None = unknown (first cycle)

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
            # --- Weekend / market-hours protocol ---
            is_open, opens_in = self._is_market_open()

            if not is_open:
                now_utc = datetime.now(timezone.utc)
                if (self._last_weekend_log is None
                        or (now_utc - self._last_weekend_log) >= _WEEKEND_LOG_INTERVAL):
                    opens_in_h = opens_in // 60
                    opens_in_m = opens_in % 60
                    self._log.info(
                        "Weekend modus — markt geopend over %dh%02dm | crypto gaat door",
                        opens_in_h, opens_in_m,
                    )
                    self._last_weekend_log = now_utc

            if is_open and self._market_was_open is False:
                self._log.info("Markt geopend — equities beslissingen hervat")
            self._market_was_open = is_open

            # Schrijf briefing in het venster 15:15-15:30 Amsterdam (één keer per dag)
            if self._is_in_briefing_window():
                today = self._now_amsterdam().date()
                if self._briefing_written_date != today:
                    self._compose_and_write_briefing()

            # --- Reguliere advies-cyclus (crypto altijd actief) ---
            candidates     = self._read_research_candidates()
            paper_stats    = self._read_paper_stats()
            claude_advices = self._read_claude_advices()

            self._apply_paper_logic(decision, candidates, paper_stats)
            self._apply_claude_logic(decision, candidates, paper_stats, claude_advices)

            rs_regime = self.sync_rs_regime_once()
            regime = rs_regime or self._determine_regime(candidates)
            if regime:
                if rs_regime is None:
                    self._write_regime_signal(regime)
                news = self._read_latest_news_snapshot()
                news_sentiment = (
                    (news.get("market_sentiment") or "neutral") if news else "neutral"
                )
                combined, pos_mult, sl_mult = _compute_market_signal(regime, news_sentiment)
                self._write_market_signal(regime, news_sentiment, combined, pos_mult, sl_mult)

            decision.regime = regime
            self._apply_watchtower_context(decision, candidates, regime)

            self._log.info(
                "Advies-cyclus klaar | open=%s regime=%s prioriteit=%d deprioriteer=%d "
                "verhogen=%d verlagen=%d gevolgd=%d genegeerd=%d",
                is_open,
                regime or "—",
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

    def _apply_watchtower_context(
        self,
        decision: QueenDecision,
        candidates: list[dict],
        regime: str | None,
    ) -> None:
        """Gebruik Watchtower als zachte prioriteitsinput, nooit als harde blokkade."""
        try:
            get_context = getattr(self._queen, "get_watchtower_context", None)
            wt = get_context() if callable(get_context) else {}
            wt = wt if isinstance(wt, dict) else {}
            decision.watchtower_context = wt
        except Exception:
            self._log.exception("Watchtower-context kon niet gelezen worden")
            decision.watchtower_context = {}
            return

        wt = decision.watchtower_context
        wt_regime = str(wt.get("last_regime") or "unknown").upper()
        queen_regime = str(regime or "unknown").upper()
        if wt_regime == "SIDEWAYS" and queen_regime == "RISK_ON":
            self._log.warning(
                "Watchtower/Queen regime conflict | watchtower=%s queen=%s",
                wt_regime,
                queen_regime,
            )

        risk_flags = [str(flag).lower() for flag in (wt.get("last_risk_flags") or [])]
        if "high_risk" in risk_flags:
            for candidate in candidates:
                cid = str(candidate.get("candidate_id") or "").strip()
                if cid and cid not in decision.deprioriteer_kandidaten:
                    decision.deprioriteer_kandidaten.append(cid)
            if candidates:
                decision.adviezen_genegeerd.append(
                    f"watchtower: high_risk → {len(candidates)} kandidaat/kandidaten lager"
                )

        score = _safe_float(wt.get("last_score"))
        if score <= 0.65 or not _watchtower_context_is_fresh(wt):
            return

        matches = 0
        for candidate in candidates:
            if not _candidate_matches_watchtower(candidate, wt, regime):
                continue
            cid = str(candidate.get("candidate_id") or "").strip()
            if cid and cid not in decision.prioriteit_kandidaten:
                decision.prioriteit_kandidaten.append(cid)
                matches += 1

        if matches:
            decision.adviezen_gevolgd.append(
                f"watchtower: score={score:.2f} → {matches} kandidaat/kandidaten hoger"
            )

    # ------------------------------------------------------------------
    # Bronnen lezen
    # ------------------------------------------------------------------

    def _paths_fingerprint(self, paths: list[Path]) -> tuple[int, int, int]:
        """
        Compacte fingerprint voor logbestanden.

        De cache blijft maximaal 60s geldig, maar wordt direct ongeldig zodra
        het aantal bestanden, de nieuwste mtime of de totale grootte wijzigt.
        """
        count = 0
        newest_mtime_ns = 0
        total_size = 0
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            count += 1
            newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
            total_size += stat.st_size
        return count, newest_mtime_ns, total_size

    def _cached_log_read(self, key: str, paths: list[Path], loader):
        fingerprint = self._paths_fingerprint(paths)
        now = datetime.now(tz=timezone.utc)
        with self._io_cache_lock:
            cached = self._io_cache.get(key)
            if cached is not None:
                cached_at, cached_fingerprint, cached_value = cached
                if cached_fingerprint == fingerprint and now - cached_at <= self._cache_ttl:
                    return cached_value

        value = loader(paths)
        with self._io_cache_lock:
            self._io_cache[key] = (now, fingerprint, value)
        return value

    def _read_research_candidates(self) -> list[dict]:
        """Lees geaccepteerde kandidaten uit ANT_LOGS/research/*.jsonl."""
        if self._logs_root is None:
            return []
        research_dir = self._logs_root / "research"
        if not research_dir.exists():
            return []

        paths = list(research_dir.glob("*.jsonl"))
        return self._cached_log_read(
            "research_candidates",
            paths,
            self._load_research_candidates,
        )

    def _load_research_candidates(self, paths: list[Path]) -> list[dict]:
        candidates: list[dict] = []
        for path in paths:
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
                        enriched = dict(payload)
                        if "_accepted_at" not in enriched:
                            enriched["_accepted_at"] = (
                                rec.get("timestamp")
                                or rec.get("created_at")
                                or rec.get("generated_at")
                            )
                        candidates.append(enriched)
            except OSError:
                pass
        return candidates

    def _read_paper_stats(self) -> dict[str, dict]:
        """
        Bereken paper-trade statistieken per symbool.

        Alleen trades van de afgelopen _stats_window_days dagen worden meegenomen
        (op basis van het 'closed_at' veld). Trades zonder geldige closed_at worden
        altijd meegenomen (fail-open). Historische trades worden genegeerd zodat
        bevroren entry-prijzen uit voor een reset de win_rate niet vertekenen.

        Eenmalig bij de eerste aanroep wordt een startup-log geschreven met het
        aantal recente vs. historische trades per symbool.

        Retourneert: symbol → {"total_trades": int, "win_count": int,
                                "win_rate": float, "total_pnl": float}
        """
        if self._logs_root is None:
            return {}
        paper_dir = self._logs_root / "paper"
        if not paper_dir.exists():
            return {}

        paths = list(paper_dir.glob("*_trades.jsonl"))
        return self._cached_log_read(
            "paper_stats",
            paths,
            self._load_paper_stats,
        )

    def _load_paper_stats(self, paths: list[Path]) -> dict[str, dict]:
        """
        Werkelijke paper-statistiek loader achter de I/O-cache.
        """
        cutoff = datetime.now(tz=timezone.utc) - self._stats_window
        stats: dict[str, dict] = {}
        skipped_per_symbol: dict[str, int] = {}

        for path in paths:
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

                    # Tijdfilter op closed_at — trades buiten het venster overslaan.
                    closed_at_str = trade.get("closed_at")
                    if closed_at_str:
                        try:
                            closed_at = datetime.fromisoformat(
                                str(closed_at_str).replace("Z", "+00:00")
                            )
                            if closed_at < cutoff:
                                skipped_per_symbol[symbol] = (
                                    skipped_per_symbol.get(symbol, 0) + 1
                                )
                                continue
                        except (ValueError, TypeError):
                            pass  # ongeldig formaat → fail-open, trade meenemen

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

        if not self._stats_window_logged:
            self._stats_window_logged = True
            total_recent   = sum(s["total_trades"] for s in stats.values())
            total_skipped  = sum(skipped_per_symbol.values())
            self._log.info(
                "Queen stats: %d trades van laatste %dd, %d historisch genegeerd%s",
                total_recent,
                self._stats_window.days,
                total_skipped,
                (
                    " — " + ", ".join(
                        f"{sym}: {n} overgeslagen"
                        for sym, n in sorted(skipped_per_symbol.items())
                    )
                    if skipped_per_symbol else ""
                ),
            )

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

        paths = list(advice_dir.glob("*.json"))
        return self._cached_log_read(
            "claude_advices",
            paths,
            self._load_claude_advices,
        )

    def _load_claude_advices(self, paths: list[Path]) -> list[dict]:
        now   = datetime.now(tz=timezone.utc)
        valid: list[dict] = []

        for path in paths:
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

    def _determine_regime(self, candidates: list[dict]) -> str | None:
        """
        Bepaal het dominante marktregime op basis van best_regime in candidates.

        Mapping (backtester → PaperAnt):
          bull     → TRENDING
          sideways → SIDEWAYS
          bear     → VOLATILE

        Retourneert None als geen candidates of geen bekende waarden.
        """
        best_regimes = [c.get("best_regime") for c in candidates if c.get("best_regime")]
        if not best_regimes:
            return None
        dominant = Counter(best_regimes).most_common(1)[0][0]
        return _REGIME_MAP.get(dominant)

    def _write_regime_signal(self, regime: str) -> None:
        """Schrijf regime-signaal append-only naar ANT_LOGS/queen/regime.jsonl."""
        if self._logs_root is None:
            return
        queen_dir = self._logs_root / "queen"
        try:
            queen_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "action": "regime_signal",
                    "regime": regime,
                },
            }
            with (queen_dir / "regime.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self._log.debug("Regime-signaal geschreven: %s", regime)
        except OSError:
            self._log.exception("Kan regime-signaal niet schrijven naar %s", queen_dir)

    def _write_market_signal(
        self,
        regime: str,
        news_sentiment: str,
        combined_signal: str,
        position_size_mult: float,
        sl_mult: float,
    ) -> None:
        """Schrijf gecombineerd markt-signaal append-only naar ANT_LOGS/queen/market_signal.jsonl."""
        if self._logs_root is None:
            return
        queen_dir = self._logs_root / "queen"
        try:
            queen_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "action":             "market_signal",
                    "regime":             regime,
                    "news_sentiment":     news_sentiment,
                    "combined_signal":    combined_signal,
                    "position_size_mult": position_size_mult,
                    "sl_mult":            sl_mult,
                },
            }
            with (queen_dir / "market_signal.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            self._log.debug(
                "Markt-signaal geschreven: %s (pos_mult=%.2f sl_mult=%.2f)",
                combined_signal, position_size_mult, sl_mult,
            )
        except OSError:
            self._log.exception("Kan markt-signaal niet schrijven naar %s", queen_dir)

    def sync_rs_regime_once(self) -> str | None:
        """
        Sync het meest recente RSRegimeAnt signaal direct naar Queen regime.

        Dit draait ook buiten de normale 5-minuten adviescyclus, zodat PaperAnt
        het actuele equity-regime ziet zodra RSRegimeAnt heeft getickt.
        """
        rs_regime = self._read_rs_regime()
        if not rs_regime:
            return None
        regime = _RS_REGIME_TO_QUEEN_REGIME.get(str(rs_regime).upper())
        if not regime:
            return None
        old_regime = self._read_latest_queen_regime()
        if old_regime != regime:
            self._write_regime_signal(regime)
            self._log.info(
                "Queen regime bijgewerkt | oud=%s nieuw=%s bron=rs_regime_ant",
                old_regime or "UNKNOWN",
                regime,
            )
        return regime

    def _read_latest_queen_regime(self) -> str | None:
        if self._logs_root is None:
            return None
        try:
            from ant_colony.ants._queen_regime import read_latest_queen_regime
            return read_latest_queen_regime(self._logs_root)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Weekend / markturen protocol
    # ------------------------------------------------------------------

    def _now_amsterdam(self) -> datetime:
        """Huidige tijd in Amsterdam timezone (uitwisselbaar in tests)."""
        return datetime.now(tz=_AMS_TZ)

    def _is_market_open(self) -> tuple[bool, int]:
        """
        Retourneert (is_open, opens_in_minutes).

        Markturen: ma-vr 15:30-22:00 Amsterdam.
        opens_in_minutes is 0 als de markt open is.
        """
        now = self._now_amsterdam()
        weekday = now.weekday()   # 0=ma, 6=zo

        open_time  = now.replace(hour=_MARKET_OPEN_HOUR,  minute=_MARKET_OPEN_MINUTE,  second=0, microsecond=0)
        close_time = now.replace(hour=_MARKET_CLOSE_HOUR, minute=_MARKET_CLOSE_MINUTE, second=0, microsecond=0)

        if weekday < 5 and open_time <= now < close_time:
            return True, 0

        # Bepaal wanneer de markt volgende keer opent
        if weekday < 5 and now < open_time:
            # Vandaag voor openingstijd
            return False, max(0, int((open_time - now).total_seconds() / 60))

        # Na sluitingstijd of weekend → volgende handelsdag 15:30
        if weekday == 4:    # vrijdag na sluit → maandag
            days_ahead = 3
        elif weekday == 5:  # zaterdag → maandag
            days_ahead = 2
        elif weekday == 6:  # zondag → maandag
            days_ahead = 1
        else:               # ma-do na sluit → volgende dag
            days_ahead = 1

        next_open = open_time + timedelta(days=days_ahead)
        return False, max(0, int((next_open - now).total_seconds() / 60))

    def _is_in_briefing_window(self) -> bool:
        """True als de huidige tijd in het 15:15-15:30 briefing-venster valt (ma-vr)."""
        now = self._now_amsterdam()
        if now.weekday() >= 5:
            return False
        briefing_start = now.replace(hour=_BRIEFING_HOUR,      minute=_BRIEFING_MINUTE,     second=0, microsecond=0)
        market_open    = now.replace(hour=_MARKET_OPEN_HOUR,   minute=_MARKET_OPEN_MINUTE,  second=0, microsecond=0)
        return briefing_start <= now < market_open

    def _read_latest_news_snapshot(self) -> dict | None:
        """Lees de meest recente snapshot uit ANT_LOGS/news/*.json."""
        if self._logs_root is None:
            return None
        news_dir = self._logs_root / "news"
        if not news_dir.exists():
            return None
        snapshots = list(news_dir.glob("*.json"))
        return self._cached_log_read(
            "latest_news_snapshot",
            snapshots,
            self._load_latest_news_snapshot,
        )

    def _load_latest_news_snapshot(self, snapshots: list[Path]) -> dict | None:
        try:
            snapshots = sorted(snapshots, key=lambda p: p.stat().st_mtime)
        except OSError:
            snapshots = []
        if not snapshots:
            return None
        try:
            return json.loads(snapshots[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read_top_sector_signals(self, n: int = 3) -> list[str]:
        """
        Lees top-N unieke sector symbolen uit ANT_LOGS/scouts/*.jsonl.

        Sorteert op momentum_rank (lager = beter).
        """
        if self._logs_root is None:
            return []
        scouts_dir = self._logs_root / "scouts"
        if not scouts_dir.exists():
            return []

        paths = list(scouts_dir.glob("*.jsonl"))
        return self._cached_log_read(
            f"top_sector_signals:{n}",
            paths,
            lambda cached_paths: self._load_top_sector_signals(cached_paths, n=n),
        )

    def _load_top_sector_signals(self, paths: list[Path], n: int = 3) -> list[str]:
        signals: list[dict] = []
        for path in paths:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") or {}
                    if (payload.get("action") == "opportunity_detected"
                            and payload.get("signal_type") == "sector_rotation"):
                        signals.append(payload)
            except OSError:
                pass

        signals.sort(key=lambda s: s.get("momentum_rank", 99))
        seen: set[str] = set()
        result: list[str] = []
        for sig in signals:
            sym = sig.get("symbol", "")
            if sym and sym not in seen:
                seen.add(sym)
                result.append(sym)
                if len(result) >= n:
                    break
        return result

    def _read_rs_regime(self) -> str | None:
        """Lees het RS-regime uit ANT_LOGS/rs_regime/*.jsonl via RSRegimeAnt reader."""
        if self._logs_root is None:
            return None
        rs_dir = self._logs_root / "rs_regime"
        paths = list(rs_dir.glob("*.jsonl")) if rs_dir.exists() else []
        return self._cached_log_read(
            "rs_regime",
            paths,
            lambda _paths: self._load_rs_regime(),
        )

    def _load_rs_regime(self) -> str | None:
        try:
            from ant_colony.ants.equities.rs_regime_ant import read_latest_rs_regime
            payload = read_latest_rs_regime(self._logs_root)
            return payload.get("regime") if payload else None
        except Exception:
            return None

    def _write_briefing(
        self,
        market_sentiment: str,
        top_sectors: list[str],
        rs_regime: str | None,
        headlines: list[str],
        recommendation: str,
    ) -> None:
        """Schrijf market-opening briefing append-only naar ANT_LOGS/queen/briefing.jsonl."""
        if self._logs_root is None:
            return
        queen_dir = self._logs_root / "queen"
        try:
            queen_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp":        datetime.now(timezone.utc).isoformat(),
                "action":           "market_opening_briefing",
                "market_sentiment": market_sentiment,
                "top_sectors":      top_sectors,
                "rs_regime":        rs_regime or "UNKNOWN",
                "headlines":        headlines[:3],
                "recommendation":   recommendation,
            }
            with (queen_dir / "briefing.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._log.info(
                "Markt-opening briefing geschreven | sentiment=%s rs_regime=%s sectors=%s",
                market_sentiment, rs_regime, top_sectors,
            )
        except OSError:
            self._log.exception("Kan briefing niet schrijven naar %s", queen_dir)

    def _compose_and_write_briefing(self) -> None:
        """Verzamel nieuws, sector-signalen en RS-regime en schrijf de dagelijkse briefing."""
        news        = self._read_latest_news_snapshot()
        top_sectors = self._read_top_sector_signals(n=3)
        rs_regime   = self._read_rs_regime()

        if news:
            market_sentiment = news.get("market_sentiment", "neutral")
            headlines = [h for h in (news.get("top_headlines") or []) if h][:3]
        else:
            market_sentiment = "neutral"
            headlines = []

        parts = [f"Markt opent {market_sentiment}."]
        if top_sectors:
            parts.append(f"Focus op {' en '.join(top_sectors)}.")
        if rs_regime:
            parts.append(f"{rs_regime} regime actief.")
        recommendation = " ".join(parts)

        self._write_briefing(market_sentiment, top_sectors, rs_regime, headlines, recommendation)
        self._briefing_written_date = self._now_amsterdam().date()

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

        1. win_rate > 0.55 over 10+ trades → kandidaat in kapitaal_verhogen
        2. win_rate < 0.30 over 10+ trades → kandidaat in kapitaal_verlagen + deprioriteer
        3. strategy_type 2+ weken in top-3 zonder paper trade → stagnant + deprioriteer
        """
        self._apply_stagnant_top3_logic(decision, candidates, paper_stats)

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

            if total >= _PAPER_MIN_TRADES_HIGH and wr > _PAPER_WIN_HIGH:
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

    def _apply_stagnant_top3_logic(
        self,
        decision: QueenDecision,
        candidates: list[dict],
        paper_stats: dict[str, dict],
    ) -> None:
        """
        Verlaag prioriteit voor strategy_types die lang zichtbaar zijn maar nooit paper-traden.

        Conservatief: alleen de huidige diverse top-3 wordt beoordeeld. Een type
        is stagnant als er een top-3 kandidaat is, dezelfde strategy_type al
        minstens 14 dagen in research logs voorkomt en geen enkel symbool van
        dat type een paper trade heeft.
        """
        if not candidates:
            return

        adjustments = self._get_stagnant_top3_adjustments(candidates, paper_stats)
        for adjustment in adjustments:
            for cid in adjustment.get("candidate_ids", []):
                if cid and cid not in decision.deprioriteer_kandidaten:
                    decision.deprioriteer_kandidaten.append(cid)
            strategy_type = adjustment.get("strategy_type") or "unknown"
            decision.adviezen_gevolgd.append(
                f"stagnant:{strategy_type} al >=14d in top-3 zonder paper trade → prioriteit verlaagd"
            )

    def _get_stagnant_top3_adjustments(
        self,
        candidates: list[dict],
        paper_stats: dict[str, dict],
    ) -> list[dict]:
        paper_totals = tuple(
            sorted(
                (str(symbol), int((stats or {}).get("total_trades", 0)))
                for symbol, stats in paper_stats.items()
            )
        )
        cache_key = (id(candidates), len(candidates), id(paper_stats), paper_totals)
        now = datetime.now(tz=timezone.utc)
        with self._stagnant_cache_lock:
            cached = self._stagnant_cache
            if cached is not None:
                cached_at, cached_key, cached_value = cached
                if cached_key == cache_key and now - cached_at <= self._cache_ttl:
                    return list(cached_value)

        adjustments = self._compute_stagnant_top3_adjustments(candidates, paper_stats)
        with self._stagnant_cache_lock:
            self._stagnant_cache = (now, cache_key, list(adjustments))
        return adjustments

    def _compute_stagnant_top3_adjustments(
        self,
        candidates: list[dict],
        paper_stats: dict[str, dict],
    ) -> list[dict]:
        top3 = select_diverse_top_n(candidates, n=3)
        if not top3:
            return []

        now = datetime.now(tz=timezone.utc)
        adjustments: list[dict] = []
        by_type: dict[str, list[dict]] = {}
        for candidate in candidates:
            strategy_type = _candidate_strategy_type_key(candidate)
            if strategy_type:
                by_type.setdefault(strategy_type, []).append(candidate)

        for top_candidate in top3:
            strategy_type = _candidate_strategy_type_key(top_candidate)
            if not strategy_type:
                continue
            same_type = by_type.get(strategy_type, [])
            first_seen: datetime | None = None
            symbols: set[str] = set()
            cids: list[str] = []

            for candidate in same_type:
                symbol = _candidate_symbol_key(candidate)
                if symbol:
                    symbols.add(symbol)
                cid = str(candidate.get("candidate_id") or "").strip()
                if cid:
                    cids.append(cid)
                ts = _parse_context_ts(candidate.get("_accepted_at") or candidate.get("timestamp"))
                if ts is not None and (first_seen is None or ts < first_seen):
                    first_seen = ts

            if first_seen is None or now - first_seen < _STAGNANT_TOP3_AGE:
                continue

            has_paper_trade = any((paper_stats.get(symbol) or {}).get("total_trades", 0) > 0 for symbol in symbols)
            if has_paper_trade:
                continue

            adjustments.append({
                "strategy_type": strategy_type,
                "candidate_ids": list(dict.fromkeys(cids)),
            })
        return adjustments

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
