"""
ant_colony/ants/equities/fundamental_ant.py

FundamentalAnt — screent aandelen op Piotroski F-Score, 12-maands momentum
en 52-weeks high breakout. Kandidaten worden naar ANT_LOGS/research/ geschreven
zodat PaperAnt ze automatisch oppikt.

Strategie: Fundamenteel + Technisch Hybride (Setup 2 uit CLAUDE.md)
  - Piotroski F-Score (9 criteria op snapshot-ratio's)  ≥ 7
  - 12-maands prijsmomentum positief
  - Prijs binnen 5% van 52-weeks high (breakout zone)
  - Screent de symbolen uit mission.market_scope.symbols
    (standaard: _SP500_TOP50 als mission.market_scope.symbols leeg is)

Output: ANT_LOGS/research/{ant_id}.jsonl — action=candidate_accepted
  → PaperAnt pikt deze op en opent paper posities met tp_pct=0.20 / sl_pct=0.08

Piotroski F-Score criteria (vereenvoudigd op snapshot-data van yfinance.info):
  F1  ROA > 0
  F2  Operating Cash Flow > 0
  F3  OCF > Net Income (accruals kwaliteitssignaal)
  F4  Debt-to-equity < 1.0
  F5  Current ratio > 1.0
  F6  Gross margin > 25%
  F7  ROE > 10%
  F8  0 < PE ratio < 30
  F9  ROA > 5%

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Standaard watchlist als de missie geen symbolen opgeeft
_SP500_TOP50: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "TSLA",
    "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG",
    "JNJ", "ABBV", "BAC", "MRK", "CRM", "NFLX", "CVX", "AMD", "PEP",
    "TMO", "WMT", "ORCL", "KO", "ADBE", "CSCO", "MCD", "ACN", "IBM",
    "GE", "QCOM", "ABT", "TXN", "ISRG", "PM", "INTU", "SPGI", "GS",
    "DHR", "AXP", "NOW", "BKNG", "RTX",
]

_MIN_PIOTROSKI_SCORE = 7
_BREAKOUT_MARGIN     = 0.05   # prijs binnen 5% van 52-weeks high
_TP_PCT              = 0.20   # 20% take-profit doel (CLAUDE.md Setup 2)
_SL_PCT              = 0.08   # 8% stop-loss (CLAUDE.md Setup 2)


class FundamentalAnt:
    """
    Screent aandelen op Piotroski F-Score ≥ 7, positief 12M-momentum
    en 52-weeks high breakout. Kandidaten worden als candidate_accepted
    gelogd naar ANT_LOGS/research/{ant_id}.jsonl.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
        min_f_score:     Minimum Piotroski F-Score (standaard 7).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        min_f_score: int = _MIN_PIOTROSKI_SCORE,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root
        self._min_f_score   = min_f_score

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        # Dedup: één candidate_id per (symbool, dag)
        self._emitted_candidates: set[str] = set()

        self._log = logging.getLogger(f"ant.fundamental.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        watchlist = list(self.mission.market_scope.symbols) or _SP500_TOP50
        self._status = AntStatus.RUNNING
        self._log.info(
            "FundamentalAnt gestart | mission=%s ttl=%ds watchlist=%d",
            self.mission.mission_id, self.mission.ttl, len(watchlist),
        )

        started_at     = datetime.now(tz=timezone.utc)
        last_heartbeat = started_at

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info("TTL verlopen — afsluiten")
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("FundamentalAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("FundamentalAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Screent alle symbolen uit de watchlist.
        Retourneert lijst van geaccepteerde kandidaten (nuttig voor tests).
        """
        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        if adapter is None or not adapter.is_available():
            self._log.warning("Geen equities adapter beschikbaar")
            self._last_action = "tick:no_adapter"
            return []

        get_fundamentals_fn = getattr(adapter, "get_fundamentals", None)
        get_candles_fn      = getattr(adapter, "get_candles", None)
        if get_fundamentals_fn is None or get_candles_fn is None:
            self._log.warning(
                "Adapter mist get_fundamentals() of get_candles() — FundamentalAnt vereist YahooFinanceAdapter"
            )
            self._last_action = "tick:missing_methods"
            return []

        watchlist  = list(self.mission.market_scope.symbols) or _SP500_TOP50
        candidates = []

        for symbol in watchlist:
            try:
                result = self._screen_symbol(symbol, get_fundamentals_fn, get_candles_fn)
                if result is not None:
                    candidates.append(result)
                    self._write_candidate(result)
                    self._log.info(
                        "KANDIDAAT | %s  f_score=%d  momentum=%.1f%%",
                        symbol, result["f_score"], result["momentum_12m"] * 100,
                    )
            except Exception:
                self._log.exception("Screening mislukt voor %s — overgeslagen", symbol)

        self._last_action = f"tick:{len(candidates)} kandidaten"
        return candidates

    def _screen_symbol(
        self, symbol: str, get_fundamentals_fn, get_candles_fn
    ) -> dict | None:
        """
        Screen één aandeel op alle drie criteria.

        Retourneert een kandidaat-dict of None als het symbool niet voldoet.
        """
        candidate_id = f"fundamental-{symbol.lower()}-{date.today().isoformat()}"
        if candidate_id in self._emitted_candidates:
            return None

        fundamentals = get_fundamentals_fn(symbol)
        f_score      = self.piotroski_score(fundamentals)

        if f_score < self._min_f_score:
            self._log.debug("%s: f_score=%d < %d — overgeslagen", symbol, f_score, self._min_f_score)
            return None

        momentum = self._get_12mo_momentum(symbol, get_candles_fn)
        if momentum is None or momentum <= 0:
            self._log.debug("%s: momentum=%.4f ≤ 0 — overgeslagen", symbol, momentum or 0)
            return None

        at_breakout = self._at_52w_high_breakout(symbol, get_candles_fn)
        if not at_breakout:
            self._log.debug("%s: niet in 52-weeks high breakout zone — overgeslagen", symbol)
            return None

        self._emitted_candidates.add(candidate_id)
        return {
            "candidate_id":  candidate_id,
            "symbol":        symbol,
            "f_score":       f_score,
            "momentum_12m":  round(momentum, 6),
            "fundamentals":  fundamentals,
            "screened_at":   datetime.now(tz=timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Piotroski F-Score (9 criteria)
    # ------------------------------------------------------------------

    @staticmethod
    def piotroski_score(fundamentals: dict) -> int:
        """
        Bereken vereenvoudigde Piotroski F-Score (0–9) op basis van snapshot-data.

        Criteria:
          F1  ROA > 0
          F2  Operating Cash Flow > 0
          F3  OCF > Net Income (accruals kwaliteitssignaal)
          F4  Debt-to-equity < 1.0
          F5  Current ratio > 1.0
          F6  Gross margin > 25%
          F7  ROE > 10%
          F8  0 < PE ratio < 30
          F9  ROA > 5%
        """
        score = 0
        roa = fundamentals.get("roa", 0.0) or 0.0
        ocf = fundamentals.get("operating_cashflow", 0.0) or 0.0
        ni  = fundamentals.get("net_income", 0.0) or 0.0
        dte = fundamentals.get("debt_to_equity", 999.0) or 999.0
        cr  = fundamentals.get("current_ratio", 0.0) or 0.0
        gm  = fundamentals.get("gross_margin", 0.0) or 0.0
        roe = fundamentals.get("roe", 0.0) or 0.0
        pe  = fundamentals.get("pe_ratio", 0.0) or 0.0

        if roa > 0:         score += 1  # F1
        if ocf > 0:         score += 1  # F2
        if ocf > ni:        score += 1  # F3 accruals
        if 0 <= dte < 1.0:  score += 1  # F4
        if cr > 1.0:        score += 1  # F5
        if gm > 0.25:       score += 1  # F6
        if roe > 0.10:      score += 1  # F7
        if 0 < pe < 30:     score += 1  # F8
        if roa > 0.05:      score += 1  # F9

        return score

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_12mo_momentum(symbol: str, get_candles_fn) -> float | None:
        """12-maands prijsreturn. Retourneert None bij onvoldoende data."""
        try:
            candles = get_candles_fn(symbol, period="1y", interval="1mo")
            if len(candles) < 2:
                return None
            first_close = candles[0].close
            last_close  = candles[-1].close
            if first_close <= 0:
                return None
            return (last_close - first_close) / first_close
        except Exception:
            return None

    @staticmethod
    def _at_52w_high_breakout(symbol: str, get_candles_fn) -> bool:
        """True als huidige prijs binnen _BREAKOUT_MARGIN van 52-weeks high ligt."""
        try:
            candles = get_candles_fn(symbol, period="1y", interval="1d")
            if len(candles) < 2:
                return False
            current_price = candles[-1].close
            high_52w      = max(c.high for c in candles)
            if high_52w <= 0:
                return False
            return (high_52w - current_price) / high_52w <= _BREAKOUT_MARGIN
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(self, candidate: dict) -> None:
        """
        Schrijf geaccepteerde kandidaat als candidate_accepted event naar
        ANT_LOGS/research/{ant_id}.jsonl — PaperAnt leest dit format.
        """
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":        "candidate_accepted",
                "candidate_id":  candidate["candidate_id"],
                "symbol":        candidate["symbol"],
                "strategy_type": "piotroski_breakout",
                "direction":     "long",
                "tp_pct":        _TP_PCT,
                "sl_pct":        _SL_PCT,
                "sharpe":        None,
                "biome":         self.mission.market_scope.biome,
                "f_score":       candidate["f_score"],
                "momentum_12m":  candidate["momentum_12m"],
                "grade":         "A" if candidate["f_score"] >= 8 else "B",
                "screened_at":   candidate["screened_at"],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "research" / f"{self.ant_id}.jsonl"
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
