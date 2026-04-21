"""
ant_colony/ants/equities/momentum_rank_ant.py

MomentumRankAnt — berekent gewogen multi-timeframe momentum per sector-ETF
en schrijft een volledige ranking naar ANT_LOGS/momentum_rank/.

Strategie: Sector Rotatie (Setup 1 uit CLAUDE.md)
  - Sectoruniversum: unieke symbolen uit ANT_LOGS/scouts/*.jsonl
    (fallback: alle 11 SPDR sector ETFs)
  - Gewogen score:  40% 1-maands + 40% 3-maands + 20% 6-maands return
  - Één ranking per dag (dedup op ranking_date)
  - Ranking geschreven als momentum_ranking event naar
    ANT_LOGS/momentum_rank/{ant_id}.jsonl — RotationAnt leest dit

Output payload (action = "momentum_ranking"):
  ranking_date  ISO-datum van de ranking
  ranking       Gesorteerde lijst (hoogste score eerst):
                  rank, symbol, sector, score, r1m, r3m, r6m

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed per symbool bij API-fouten (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Fallback sector-ETF universe als scouts/ geen data bevat
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
    "XLC":  "Communication Services",
}

# Gewichten voor de drie perioden (som = 1.0)
_W1M = 0.40   # 1-maands gewicht
_W3M = 0.40   # 3-maands gewicht
_W6M = 0.20   # 6-maands gewicht


class MomentumRankAnt:
    """
    Rankt sector-ETFs op gewogen 1m/3m/6m-return en schrijft een
    dagelijkse ranking naar ANT_LOGS/momentum_rank/{ant_id}.jsonl.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    # Ranking verandert niet snel; 5 minuten is voldoende.
    _TICK_INTERVAL: int = 300

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        **kwargs,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._last_tick_at: float = 0.0
        self._emitted_ranking_dates: set[str] = set()

        self._log = logging.getLogger(f"ant.momentum_rank.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "MomentumRankAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id, self.mission.ttl,
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
            self._log.info("MomentumRankAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("MomentumRankAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Berekent gewogen momentum voor alle sectoren en schrijft ranking.
        Retourneert de ranking als lijst van dicts (nuttig voor tests).
        """
        today = date.today().isoformat()
        if today in self._emitted_ranking_dates:
            self._last_action = "tick:dedup"
            return []

        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        if adapter is None or not adapter.is_available():
            self._log.warning("Geen equities adapter beschikbaar")
            self._last_action = "tick:no_adapter"
            return []

        get_candles_fn = getattr(adapter, "get_candles", None)
        if get_candles_fn is None:
            self._log.warning("Adapter mist get_candles() — MomentumRankAnt vereist YahooFinanceAdapter")
            self._last_action = "tick:missing_method"
            return []

        sectors = self._load_sector_universe()
        scores: list[dict] = []

        for symbol in sectors:
            try:
                row = self._score_symbol(symbol, get_candles_fn)
                if row is not None:
                    scores.append(row)
            except Exception:
                self._log.exception("Scoring mislukt voor %s — overgeslagen", symbol)

        if not scores:
            self._log.warning("Geen scores berekend — ranking overgeslagen")
            self._last_action = "tick:no_scores"
            return []

        ranking = sorted(scores, key=lambda x: x["score"], reverse=True)
        for i, row in enumerate(ranking):
            row["rank"] = i + 1

        self._emitted_ranking_dates.add(today)
        self._write_ranking(ranking, today)
        self._last_action = f"tick:ranked={len(ranking)}"

        self._log.info(
            "Momentum ranking | top3=%s",
            [r["symbol"] for r in ranking[:3]],
        )
        return ranking

    def _score_symbol(self, symbol: str, get_candles_fn) -> dict | None:
        """
        Bereken gewogen momentum-score voor één symbool.
        Retourneert None als onvoldoende data beschikbaar is.
        """
        r1m = self._period_return(symbol, "1mo", get_candles_fn)
        r3m = self._period_return(symbol, "3mo", get_candles_fn)
        r6m = self._period_return(symbol, "6mo", get_candles_fn)

        if r1m is None and r3m is None and r6m is None:
            self._log.debug("%s: geen data voor alle perioden — overgeslagen", symbol)
            return None

        # Ontbrekende perioden worden als 0.0 behandeld; gebruik alleen
        # beschikbare perioden met genormaliseerde gewichten.
        w1 = _W1M if r1m is not None else 0.0
        w3 = _W3M if r3m is not None else 0.0
        w6 = _W6M if r6m is not None else 0.0
        total_w = w1 + w3 + w6

        score = (
            (r1m or 0.0) * w1 +
            (r3m or 0.0) * w3 +
            (r6m or 0.0) * w6
        ) / total_w

        return {
            "symbol":  symbol,
            "sector":  _SPDR_ETFS.get(symbol, symbol),
            "score":   round(score, 6),
            "r1m":     round(r1m, 6) if r1m is not None else None,
            "r3m":     round(r3m, 6) if r3m is not None else None,
            "r6m":     round(r6m, 6) if r6m is not None else None,
            "rank":    0,  # ingevuld na sortering
        }

    # ------------------------------------------------------------------
    # Sectoruniversum
    # ------------------------------------------------------------------

    def _load_sector_universe(self) -> list[str]:
        """
        Bouw sectoruniversum op uit ANT_LOGS/scouts/*.jsonl.
        Extraheer unieke symbolen uit opportunity_detected events.
        Fallback: alle 11 SPDR ETFs als scouts/ leeg of afwezig is.
        """
        if self.logs_root is None:
            return list(_SPDR_ETFS.keys())

        scouts_dir = self.logs_root / "scouts"
        if not scouts_dir.exists():
            return list(_SPDR_ETFS.keys())

        symbols: set[str] = set()
        for path in scouts_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "opportunity_detected":
                        continue
                    sym = str(payload.get("symbol") or "").strip().upper()
                    if sym:
                        symbols.add(sym)
            except OSError:
                pass

        # Zorg dat symbolen die in scouts/ staan maar niet in _SPDR_ETFS
        # ook worden meegenomen; voeg ook alle SPDR ETFs toe voor volledigheid
        return list(_SPDR_ETFS.keys()) if not symbols else list(symbols | set(_SPDR_ETFS.keys()))

    # ------------------------------------------------------------------
    # Return berekening
    # ------------------------------------------------------------------

    @staticmethod
    def _period_return(symbol: str, period: str, get_candles_fn) -> float | None:
        """
        Bereken prijsreturn over de opgegeven periode.
        Retourneert None bij onvoldoende data of API-fout.
        """
        try:
            candles = get_candles_fn(symbol, period=period, interval="1d")
            if len(candles) < 2:
                return None
            first_close = candles[0].close
            last_close  = candles[-1].close
            if first_close <= 0:
                return None
            return (last_close - first_close) / first_close
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_ranking(self, ranking: list[dict], ranking_date: str) -> None:
        """Schrijf de volledige ranking als momentum_ranking event naar disk."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":        "momentum_ranking",
                "ranking_date":  ranking_date,
                "biome":         self.mission.market_scope.biome,
                "weights":       {"1m": _W1M, "3m": _W3M, "6m": _W6M},
                "ranking":       ranking,
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "momentum_rank" / f"{self.ant_id}.jsonl"
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
