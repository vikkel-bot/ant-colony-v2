"""
ant_colony/ants/killzone_ant.py

KillZoneAnt — genereert hoge-confidence LONG kandidaten uitsluitend tijdens
ICT Kill Zones en schrijft ze naar ANT_LOGS/approved/ voor Paper Ant.

Kill Zones (UTC):
  London Kill Zone:  02:00 – 05:00
  NY AM Kill Zone:   13:30 – 15:00

Gedrag:
  - Buiten kill zones: slaapt (geen analyse, geen disk-I/O)
  - Binnen kill zone: analyseert alle missie-symbolen elke tick op:
      1. RSI momentum richting — stijgend RSI = bullish
      2. Volume boven 20-candle gemiddelde
      3. Prijs richting — higher high = bullish
  - Alle drie positief → emitteert LONG kandidaat naar ANT_LOGS/approved/
  - Deduplicatie per (symbool, sessie): één kandidaat per sessievenster

Paper Ant leest ANT_LOGS/approved/*.jsonl via _process_approved_candidates()
en opent posities op basis van de fitness_score en stale-timestamp check.

Regels:
  - Geen orders, geen kapitaal (P1)
  - Fail-closed (P2)
  - Geen code bij import (P7)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Kill zone vensters (UTC minuten van de dag)
# ---------------------------------------------------------------------------

_KILL_ZONES: list[tuple[int, int, str]] = [
    (120, 300, "london_kill_zone"),   # 02:00 – 05:00 UTC
    (810, 900, "ny_am_kill_zone"),    # 13:30 – 15:00 UTC
]

_MIN_CANDLES   = 22    # 20 voor volume SMA + 2 voor richting
_TICK_INTERVAL = 60    # analyseer elke 60 seconden binnen kill zone
_FITNESS_BASE  = 0.80  # basisfitness — kill zone signalen hebben altijd prioriteit
_RSI_PERIOD    = 14


def _current_kill_zone(hour: int, minute: int) -> str | None:
    """Retourneer sessienaam als de huidige UTC tijd in een kill zone valt, anders None."""
    t = hour * 60 + minute
    for start, end, name in _KILL_ZONES:
        if start <= t < end:
            return name
    return None


class KillZoneAnt:
    """
    Genereert hoge-confidence signalen uitsluitend tijdens ICT Kill Zones.

    Args:
        ant_id:          Unieke identifier.
        mission:         Toegewezen Mission (symbolen, TTL, heartbeat-interval).
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry voor candle-data.
        logs_root:       Root van ANT_LOGS. None = geen disk-I/O.
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

        self._status:      AntStatus = AntStatus.IDLE
        self._last_action: str       = "init"
        self._log_seq:     int       = 0

        # (symbol, session) → candidate_id — gereset bij sessiewisseling
        self._emitted:          dict[tuple[str, str], str] = {}
        self._current_session:  str | None                  = None

        self._log = logging.getLogger(f"ant.killzone.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "KillZoneAnt gestart | mission=%s ttl=%ds symbols=%s",
            self.mission.mission_id,
            self.mission.ttl,
            self.mission.market_scope.symbols,
        )

        started_at = datetime.now(tz=timezone.utc)

        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

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

                session = _current_kill_zone(now.hour, now.minute)

                if session != self._current_session:
                    if self._current_session is not None:
                        self._log.info(
                            "Kill zone sessie beëindigd: %s → emitted=%d — cache gereset",
                            self._current_session, len(self._emitted),
                        )
                    self._emitted.clear()
                    self._current_session = session

                if session is not None:
                    self._tick(session)
                else:
                    self._log.debug("Buiten kill zone — wachten")
                    self._last_action = "idle"

                time.sleep(_TICK_INTERVAL)

        except KeyboardInterrupt:
            self._log.info("KillZoneAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("KillZoneAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick — alleen binnen kill zone
    # ------------------------------------------------------------------

    def _tick(self, session: str) -> None:
        """Analyseer alle missie-symbolen en emitteer kandidaten bij positief signaal."""
        biome_id  = self.mission.market_scope.biome
        timeframe = (
            self.mission.market_scope.timeframes[0]
            if self.mission.market_scope.timeframes
            else "1h"
        )

        emitted = 0
        symbols = list(self.mission.market_scope.symbols)
        for symbol in symbols:
            if (symbol, session) in self._emitted:
                continue
            if self._analyze_and_emit(symbol, session, biome_id, timeframe):
                emitted += 1

        self._log.info(
            "killzone tick | session=%s symbols=%d emitted=%d",
            session, len(symbols), emitted,
        )
        self._last_action = f"tick:{session}"

    # ------------------------------------------------------------------
    # Signaalanalyse
    # ------------------------------------------------------------------

    def _analyze_and_emit(
        self, symbol: str, session: str, biome_id: str, timeframe: str
    ) -> bool:
        """
        Analyseer één symbool op drie criteria. Emitteer LONG kandidaat als
        alle drie positief zijn.

        Returns True als een kandidaat geëmitteerd is.
        """
        candles = self._fetch_candles(symbol, timeframe, biome_id)
        if len(candles) < _MIN_CANDLES:
            self._log.debug(
                "Te weinig candles voor %s (%d/%d) — overgeslagen",
                symbol, len(candles), _MIN_CANDLES,
            )
            return False

        closes  = [c.close  for c in candles]
        highs   = [c.high   for c in candles]
        lows    = [c.low    for c in candles]
        volumes = [c.volume for c in candles]

        rsi_dir      = _rsi_direction(closes)
        vol_above    = _volume_above_avg(volumes)
        price_dir    = _price_direction(highs, lows)

        self._log.debug(
            "%s | session=%s rsi=%s vol_above=%s price=%s",
            symbol, session, rsi_dir, vol_above, price_dir,
        )

        if rsi_dir == "long" and vol_above and price_dir == "long":
            fitness = _fitness_score(closes, volumes)
            self._emit_candidate(symbol, session, fitness)
            return True

        return False

    # ------------------------------------------------------------------
    # Kandidaat emitteren
    # ------------------------------------------------------------------

    def _emit_candidate(self, symbol: str, session: str, fitness: float) -> None:
        """Schrijf een LONG kandidaat naar ANT_LOGS/approved/{ant_id}.jsonl."""
        candidate_id = (
            f"kz-{symbol.lower().replace('-', '')}"
            f"-{session[:2]}-{uuid.uuid4().hex[:8]}"
        )

        record = {
            "candidate_id":     candidate_id,
            "timestamp":        datetime.now(tz=timezone.utc).isoformat(),
            "market_scope":     {"symbol": symbol},
            "biome":            self.mission.market_scope.biome,
            "entry_conditions": {"direction": "long"},
            "parameters": {
                "source":  "killzone_ant",
                "session": session,
                "ant_id":  self.ant_id,
            },
            "fitness_score": fitness,
            "priority":      "high",
        }

        self._emitted[(symbol, session)] = candidate_id
        self._last_action = f"candidate:{symbol}:{session}"
        self._log.info(
            "KANDIDAAT | %s LONG | session=%s fitness=%.3f id=%s",
            symbol, session, fitness, candidate_id,
        )
        self._write_approved(record)

    def _write_approved(self, record: dict) -> None:
        """Append record naar ANT_LOGS/approved/{ant_id}.jsonl."""
        if self.logs_root is None:
            return
        log_path = self.logs_root / "approved" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Candles ophalen
    # ------------------------------------------------------------------

    def _fetch_candles(self, symbol: str, timeframe: str, biome_id: str) -> list:
        """Haal candles op via de BiomeAdapter (fail-closed P2)."""
        try:
            adapter = self.biome_registry.get(biome_id)
            if adapter is None or not adapter.is_available():
                return []
            get_candles_fn = getattr(adapter, "get_candles", None)
            if get_candles_fn is None:
                return []
            return get_candles_fn(symbol, timeframe, 50) or []
        except Exception:
            self._log.exception("Fout bij ophalen candles: %s", symbol)
            return []

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


# ---------------------------------------------------------------------------
# Module-privaat — indicator hulpfuncties
# ---------------------------------------------------------------------------

def _rsi_direction(closes: list[float], period: int = _RSI_PERIOD) -> str | None:
    """
    'long' als RSI stijgt (huidige RSI > vorige), 'short' als dalend.
    Vereist minimaal period + 2 sluitprijzen.
    """
    if len(closes) < period + 2:
        return None

    def _compute_rsi(series: list[float]) -> float | None:
        if len(series) < period + 1:
            return None
        deltas   = [series[i + 1] - series[i] for i in range(len(series) - 1)]
        recent   = deltas[-period:]
        avg_gain = sum(d for d in recent if d > 0) / period
        avg_loss = sum(-d for d in recent if d < 0) / period
        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

    rsi_now  = _compute_rsi(closes)
    rsi_prev = _compute_rsi(closes[:-1])
    if rsi_now is None or rsi_prev is None:
        return None
    if rsi_now > rsi_prev:
        return "long"
    if rsi_now < rsi_prev:
        return "short"
    return None


def _volume_above_avg(volumes: list[float], period: int = 20) -> bool:
    """True als het laatste volume boven de SMA(period) van de vorige candles ligt."""
    if len(volumes) < period + 1:
        return False
    avg = sum(volumes[-(period + 1):-1]) / period
    return volumes[-1] > avg if avg > 0 else False


def _price_direction(highs: list[float], lows: list[float]) -> str | None:
    """
    'long' bij higher high (laatste high > vorige high),
    'short' bij lower low (laatste low < vorige low).
    Higher high heeft voorrang.
    """
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2]:
        return "long"
    if lows[-1] < lows[-2]:
        return "short"
    return None


def _fitness_score(closes: list[float], volumes: list[float]) -> float:
    """
    Berekent fitness [0.80 – 1.00]:
      +0.10 als volume > 1.5× gemiddelde (sterke volume-bevestiging)
      +0.05 als RSI in gezonde range 40–65 (niet overbought)
    """
    score = _FITNESS_BASE

    if len(volumes) >= 21:
        avg = sum(volumes[-21:-1]) / 20
        if avg > 0 and volumes[-1] > 1.5 * avg:
            score += 0.10

    if len(closes) >= _RSI_PERIOD + 2:
        deltas   = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
        recent   = deltas[-_RSI_PERIOD:]
        avg_gain = sum(d for d in recent if d > 0) / _RSI_PERIOD
        avg_loss = sum(-d for d in recent if d < 0) / _RSI_PERIOD
        if avg_loss > 0:
            rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
            if 40.0 <= rsi <= 65.0:
                score += 0.05

    return round(min(score, 1.0), 3)
