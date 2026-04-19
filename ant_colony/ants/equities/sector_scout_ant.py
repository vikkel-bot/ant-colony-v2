"""
ant_colony/ants/equities/sector_scout_ant.py

SectorScoutAnt — rankt de 11 SPDR sector ETFs op 3-maands relatief momentum
en geeft LONG/SHORT-signalen aan de Queen.

Strategie: Sector Rotatie (Setup 1)
  - Monitor 11 SPDR sector ETFs
  - Relatief momentum over 3 maanden
  - Top 3 sectoren: signaal LONG
  - Bottom 3 sectoren: signaal SHORT
  - Tick-interval: 1x per dag (heartbeat_interval bepaalt cadans)

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten — skip symbool, log, doorgaan (P2)
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

_SPDR_ETFS: dict[str, str] = {
    "XLK":  "Technology",
    "XLE":  "Energy",
    "XLV":  "Health Care",
    "XLF":  "Financials",
    "XLI":  "Industrials",
    "XLB":  "Materials",
    "XLP":  "Consumer Staples",
    "XLY":  "Consumer Discretionary",
    "XLU":  "Utilities",
    "XLRE": "Real Estate",
    "XLC":  "Communication",
}

_TOP_N    = 3
_BOTTOM_N = 3


class SectorScoutAnt:
    """
    Rankt SPDR sector ETFs op 3-maands return en emiteert LONG/SHORT-signalen.

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
        self._log = logging.getLogger(f"ant.sector_scout.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "equities" / "sector_scout"
            log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "SectorScoutAnt gestart | mission=%s ttl=%ds",
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
                    self._log.info("TTL verlopen — afsluiten")
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("SectorScoutAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("SectorScoutAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Haal 3-maands returns op voor alle ETFs, rank, en emiteer signalen.
        Retourneert de ranking (ook gebruikt door tests).
        """
        returns: dict[str, float] = {}

        for symbol in _SPDR_ETFS:
            ret = self._get_3mo_return(symbol)
            if ret is not None:
                returns[symbol] = ret
            else:
                self._log.warning("3-maands return niet beschikbaar voor %s — overgeslagen", symbol)

        if not returns:
            self._log.warning("Geen ETF-data beschikbaar — tick overgeslagen")
            self._last_action = "tick_no_data"
            return []

        ranking = sorted(returns.items(), key=lambda x: x[1], reverse=True)
        signals = self._build_signals(ranking)
        self._write_ranking(ranking, signals)
        self._last_action = f"tick:top={ranking[0][0] if ranking else 'none'}"

        self._log.info(
            "Sector ranking | top3=%s  bottom3=%s",
            [s for s, _ in ranking[:_TOP_N]],
            [s for s, _ in ranking[-_BOTTOM_N:]],
        )
        return signals

    def _get_3mo_return(self, symbol: str) -> float | None:
        """Bereken 3-maands prijsreturn. Retourneert None bij onvoldoende data."""
        candles = self.adapter.get_candles(symbol, period="3mo", interval="1d")
        if len(candles) < 2:
            return None
        first_close = candles[0].close
        last_close  = candles[-1].close
        if first_close <= 0:
            return None
        return (last_close - first_close) / first_close

    def _build_signals(self, ranking: list[tuple[str, float]]) -> list[dict]:
        """Bouw LONG/SHORT-signalen op basis van de ranking."""
        signals = []
        for i, (symbol, ret) in enumerate(ranking):
            if i < _TOP_N:
                direction = "LONG"
            elif i >= len(ranking) - _BOTTOM_N:
                direction = "SHORT"
            else:
                direction = "NEUTRAL"
            signals.append({
                "symbol":    symbol,
                "sector":    _SPDR_ETFS.get(symbol, ""),
                "return_3mo": round(ret, 6),
                "rank":      i + 1,
                "signal":    direction,
            })
        return signals

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_ranking(self, ranking: list[tuple[str, float]], signals: list[dict]) -> None:
        """Schrijf ranking + signalen naar ANT_LOGS/equities/sector_scout/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":    "sector_ranking",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "ranking":   [{"symbol": s, "return_3mo": round(r, 6)} for s, r in ranking],
                "signals":   signals,
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "sector_scout" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon ranking niet naar disk schrijven: %s", log_path)

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
