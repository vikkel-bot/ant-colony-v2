"""
ant_colony/ants/equities/fundamental_ant.py

FundamentalAnt — screent de top 50 S&P500-aandelen op Piotroski F-Score
en 12-maands momentum. Kandidaten (score >= 7 + positief momentum) worden
naar ANT_LOGS geschreven als research-input voor de Queen.

Strategie: Fundamenteel + Technisch Hybride (Setup 2)
  - Watchlist: top 50 S&P500 op marktkapitalisatie
  - Piotroski F-Score (9 criteria) — zie _piotroski_score()
  - Score >= 7 + 12-maands momentum > 0 → kandidaat
  - Tick-interval: 1x per dag (heartbeat_interval bepaalt cadans)

Piotroski criteria (vereenvoudigd op snapshot-data van yfinance):
  F1  ROA > 0
  F2  Operating Cash Flow > 0
  F3  OCF > Net Income (accruals kwaliteitssignaal)
  F4  Debt-to-equity < 1.0
  F5  Current ratio > 1.0
  F6  Gross margin > 25%
  F7  ROE > 10%
  F8  PE ratio < 30 (geen negatieve P/E)
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Top 50 S&P500-aandelen bij benadering op marktkapitalisatie
_SP500_TOP50: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK-B", "TSLA",
    "AVGO", "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD", "PG",
    "JNJ", "ABBV", "BAC", "MRK", "CRM", "NFLX", "CVX", "AMD", "PEP",
    "TMO", "WMT", "ORCL", "KO", "ADBE", "CSCO", "MCD", "ACN", "IBM",
    "GE", "QCOM", "ABT", "TXN", "ISRG", "PM", "INTU", "SPGI", "GS",
    "DHR", "AXP", "NOW", "BKNG", "RTX",
]

_MIN_PIOTROSKI_SCORE = 7
_API_DELAY_SECS      = 2.0  # vertraging tussen API-calls om rate limits te vermijden


class FundamentalAnt:
    """
    Screent S&P500-aandelen op Piotroski F-Score en momentum.

    Args:
        ant_id:    Unieke identifier (UUID-string).
        mission:   Toegewezen Mission.
        scheduler: ColonyScheduler voor heartbeat-registratie.
        adapter:   YahooFinanceAdapter instantie (injectable voor tests).
        logs_root: Pad naar ANT_LOGS. None = geen disk-logging.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        adapter: YahooFinanceAdapter | None = None,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.adapter   = adapter or YahooFinanceAdapter()
        self.logs_root = logs_root

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._log = logging.getLogger(f"ant.fundamental.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "equities" / "fundamental"
            log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "FundamentalAnt gestart | mission=%s ttl=%ds watchlist=%d",
            self.mission.mission_id,
            self.mission.ttl,
            len(_SP500_TOP50),
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
        Screent alle aandelen op de watchlist.
        Retourneert lijst van geaccepteerde kandidaten (ook gebruikt door tests).
        """
        candidates = []

        for symbol in _SP500_TOP50:
            try:
                result = self._screen_symbol(symbol)
                if result is not None:
                    candidates.append(result)
                    self._write_candidate(result)
                    self._log.info(
                        "KANDIDAAT | %s  score=%d  momentum=%.2f%%",
                        symbol,
                        result["piotroski_score"],
                        result["momentum_12m"] * 100,
                    )
                time.sleep(_API_DELAY_SECS)
            except Exception:
                self._log.exception("Screening mislukt voor %s — overgeslagen", symbol)

        self._last_action = f"tick:{len(candidates)} kandidaten"
        return candidates

    def _screen_symbol(self, symbol: str) -> dict | None:
        """
        Screen één aandeel. Retourneert kandidaat-dict of None.

        Criteria:
          1. Piotroski F-Score >= _MIN_PIOTROSKI_SCORE
          2. 12-maands momentum positief
        """
        fundamentals = self.adapter.get_fundamentals(symbol)
        score        = self._piotroski_score(fundamentals)

        if score < _MIN_PIOTROSKI_SCORE:
            self._log.debug("%s: score %d < %d — overgeslagen", symbol, score, _MIN_PIOTROSKI_SCORE)
            return None

        momentum = self._get_12mo_momentum(symbol)
        if momentum is None or momentum <= 0:
            self._log.debug("%s: momentum %.4f <= 0 — overgeslagen", symbol, momentum or 0)
            return None

        return {
            "symbol":          symbol,
            "piotroski_score": score,
            "momentum_12m":    round(momentum, 6),
            "fundamentals":    fundamentals,
            "screened_at":     datetime.now(tz=timezone.utc).isoformat(),
        }

    def _piotroski_score(self, fundamentals: dict) -> int:
        """
        Bereken vereenvoudigde Piotroski F-Score (0–9) op basis van snapshot-data.

        F1  ROA > 0
        F2  Operating Cash Flow > 0
        F3  OCF > Net Income (accruals kwaliteit)
        F4  Debt-to-equity < 1.0
        F5  Current ratio > 1.0
        F6  Gross margin > 25%
        F7  ROE > 10%
        F8  PE ratio 0 < PE < 30
        F9  ROA > 5%
        """
        score = 0
        roa    = fundamentals.get("roa", 0.0)
        ocf    = fundamentals.get("operating_cashflow", 0.0)
        ni     = fundamentals.get("net_income", 0.0)
        dte    = fundamentals.get("debt_to_equity", 999.0)
        cr     = fundamentals.get("current_ratio", 0.0)
        gm     = fundamentals.get("gross_margin", 0.0)
        roe    = fundamentals.get("roe", 0.0)
        pe     = fundamentals.get("pe_ratio", 0.0)

        if roa > 0:            score += 1  # F1
        if ocf > 0:            score += 1  # F2
        if ocf > ni:           score += 1  # F3 accruals
        if 0 <= dte < 1.0:     score += 1  # F4
        if cr > 1.0:           score += 1  # F5
        if gm > 0.25:          score += 1  # F6
        if roe > 0.10:         score += 1  # F7
        if 0 < pe < 30:        score += 1  # F8
        if roa > 0.05:         score += 1  # F9

        return score

    def _get_12mo_momentum(self, symbol: str) -> float | None:
        """Bereken 12-maands prijsreturn. Retourneert None bij onvoldoende data."""
        candles = self.adapter.get_candles(symbol, period="1y", interval="1mo")
        if len(candles) < 2:
            return None
        first_close = candles[0].close
        last_close  = candles[-1].close
        if first_close <= 0:
            return None
        return (last_close - first_close) / first_close

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(self, candidate: dict) -> None:
        """Schrijf kandidaat naar ANT_LOGS/equities/fundamental/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":    "fundamental_candidate",
                "symbol":    candidate["symbol"],
                "score":     candidate["piotroski_score"],
                "momentum":  candidate["momentum_12m"],
                "screened_at": candidate["screened_at"],
                "fundamentals": candidate["fundamentals"],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "fundamental" / f"{self.ant_id}.jsonl"
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
