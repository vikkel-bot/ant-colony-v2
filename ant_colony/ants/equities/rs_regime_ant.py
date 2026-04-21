"""
ant_colony/ants/equities/rs_regime_ant.py

RSRegimeAnt — detecteert marktregime op basis van Relative Strength (RS)
tussen de Nasdaq-100 (QQQ) en een defensieve ETF-basket.

Doel: de Queen informeren wanneer de markt kantelt van risk-on naar risk-off
zodat de kapitaalallocatie automatisch kan worden verschoven.

Berekeningen per tick:
  - 50-day Simple Moving Average van QQQ
  - QQQ-prijs vs 50d SMA ratio  (≥1 = boven SMA = risk-on conditie)
  - 20-day Relative Strength vs QQQ per defensive ETF:
      RS = (price_now / price_20d_ago) / (qqq_now / qqq_20d_ago)
      RS > 1 = defensive ETF outperformed QQQ (risk-off signaal)

Regime classificatie (prioriteit: CRISIS > RISK_OFF > NEUTRAL > RISK_ON):
  RISK_ON:  QQQ ≥ SMA50 EN avg_defensive_rs < 1.0
  NEUTRAL:  QQQ ≥ SMA50 EN 1.0 ≤ avg_defensive_rs ≤ 1.1
  RISK_OFF: QQQ < SMA50 OF avg_defensive_rs > 1.1
  CRISIS:   QQQ < SMA50 EN avg_defensive_rs > 1.2

Output: ANT_LOGS/rs_regime/{ant_id}.jsonl
  { "action": "regime_signal", "regime": "...", "qqq_vs_50d": float,
    "avg_defensive_rs": float, "components": {...} }

Reader: read_latest_rs_regime(logs_root) → dict | None
  Retourneert de meest recente payload, of None als geen signaal beschikbaar.
  None → geen wijziging in allocatie (fail-open).

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Fail-closed bij API-fouten — skip tick, log, doorgaan (P2)
  - Alle state in het object (P7)
  - Geen code bij import (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

_QQQ = "QQQ"   # Nasdaq-100 proxy — risk-on indicator

_DEFENSIVE_BASKET: tuple[str, ...] = ("XLV", "XLP", "XLU", "XLE", "GLD")

_MIN_BARS_SMA  = 50    # minimaal 50 dagbars voor betrouwbare SMA-50
_MIN_BARS_RS   = 21    # minimaal 21 bars voor 20-daagse RS (sluit[−1] / sluit[−21])
_MIN_BARS      = max(_MIN_BARS_SMA, _MIN_BARS_RS)

_RS_NEUTRAL_LOW  = 1.0   # ondergrens NEUTRAL
_RS_NEUTRAL_HIGH = 1.1   # bovengrens NEUTRAL / ondergrens RISK_OFF
_RS_CRISIS       = 1.2   # defensieve RS boven dit niveau = CRISIS kandidaat

_CANDLE_PERIOD   = "3mo"  # yfinance period — geeft ≈ 63 handelsdagen
_CANDLE_INTERVAL = "1d"


# ---------------------------------------------------------------------------
# Regime classificatie — pure functie (eenvoudig te testen)
# ---------------------------------------------------------------------------

def classify_regime(qqq_vs_50d: float, avg_defensive_rs: float) -> str:
    """
    Classificeer het marktregime op basis van QQQ-SMA-ratio en gemiddelde RS.

    Args:
        qqq_vs_50d:       QQQ-sluitprijs / SMA-50. ≥ 1.0 = boven SMA.
        avg_defensive_rs: Gemiddeld 20-day RS defensive basket vs QQQ.

    Returns:
        "RISK_ON" | "NEUTRAL" | "RISK_OFF" | "CRISIS"
    """
    qqq_above = qqq_vs_50d >= 1.0

    if not qqq_above and avg_defensive_rs > _RS_CRISIS:
        return "CRISIS"
    if not qqq_above or avg_defensive_rs > _RS_NEUTRAL_HIGH:
        return "RISK_OFF"
    if qqq_above and avg_defensive_rs >= _RS_NEUTRAL_LOW:
        return "NEUTRAL"
    return "RISK_ON"


# ---------------------------------------------------------------------------
# Reader — te importeren door Queen / allocator
# ---------------------------------------------------------------------------

def read_latest_rs_regime(logs_root: Path) -> dict | None:
    """
    Lees de meest recente RegimeSignal uit ANT_LOGS/rs_regime/*.jsonl.

    Retourneert de payload dict of None als geen signaal gevonden.
    None = geen RSRegimeAnt actief → geen wijziging in allocatie (fail-open).
    """
    rs_dir = logs_root / "rs_regime"
    if not rs_dir.exists():
        return None

    latest_payload = None
    latest_ts_str: str = ""

    for path in rs_dir.glob("*.jsonl"):
        try:
            last_line: str | None = None
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
            if last_line is None:
                continue
            rec = json.loads(last_line)
            payload = rec.get("payload") or {}
            if payload.get("action") != "regime_signal":
                continue
            ts = rec.get("timestamp", "")
            if ts > latest_ts_str:
                latest_ts_str = ts
                latest_payload = payload
        except (OSError, json.JSONDecodeError):
            pass

    return latest_payload


# ---------------------------------------------------------------------------
# RSRegimeAnt
# ---------------------------------------------------------------------------

class RSRegimeAnt:
    """
    Detecteert marktregime via Relative Strength analyse en emitteert
    RegimeSignals naar ANT_LOGS/rs_regime/.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry met YahooFinanceAdapter geregistreerd
                         onder "equities".
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    # Tick elke 60s — regime-data (20d RS, SMA-50) verandert niet per seconde.
    _TICK_INTERVAL: int = 60
    # Log op INFO bij regime-change OF als qqq_vs_50d meer dan 2% verschilt.
    _QQQ_LOG_THRESHOLD: float = 0.02

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

        self._log_seq: int     = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str  = "init"

        self._last_logged_regime: str | None   = None
        self._last_logged_qqq:    float | None = None
        self._last_tick_at:       float        = 0.0

        self._log = logging.getLogger(f"ant.rs_regime.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "RSRegimeAnt gestart | mission=%s ttl=%ds",
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
            self._log.info("RSRegimeAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("RSRegimeAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> dict | None:
        """
        Haal OHLCV data op, bereken SMA + RS, classificeer regime.

        Retourneert de signal-payload (ook nuttig voor tests), of None bij
        onvoldoende data.
        """
        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        if adapter is None or not adapter.is_available():
            self._log.warning("Geen equities adapter beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_adapter"
            return None

        get_candles_fn = getattr(adapter, "get_candles", None)
        if get_candles_fn is None:
            self._log.warning("Adapter heeft geen get_candles() — RSRegimeAnt vereist YahooFinanceAdapter")
            self._last_action = "tick:no_get_candles"
            return None

        # Haal QQQ-data op
        qqq_closes = self._fetch_closes(_QQQ, get_candles_fn)
        if qqq_closes is None or len(qqq_closes) < _MIN_BARS:
            self._log.warning(
                "Onvoldoende QQQ-data (%d bars, minimaal %d vereist) — tick overgeslagen",
                len(qqq_closes) if qqq_closes else 0, _MIN_BARS,
            )
            self._last_action = "tick:insufficient_qqq"
            return None

        qqq_now     = qqq_closes[-1]
        qqq_20d_ago = qqq_closes[-_MIN_BARS_RS]   # close van ≈ 20 handelsdagen geleden
        qqq_sma50   = mean(qqq_closes[-_MIN_BARS_SMA:])
        qqq_vs_50d  = qqq_now / qqq_sma50 if qqq_sma50 > 0 else 1.0

        if qqq_20d_ago <= 0:
            self._log.warning("QQQ-prijs 20 dagen geleden is nul — tick overgeslagen")
            self._last_action = "tick:invalid_qqq_20d"
            return None

        # Bereken RS per defensive ETF
        components: dict[str, dict] = {
            _QQQ: {
                "price":   round(qqq_now, 4),
                "sma50":   round(qqq_sma50, 4),
                "vs_sma":  round(qqq_vs_50d, 6),
            }
        }
        rs_values: list[float] = []

        for symbol in _DEFENSIVE_BASKET:
            closes = self._fetch_closes(symbol, get_candles_fn)
            if closes is None or len(closes) < _MIN_BARS:
                self._log.warning("Onvoldoende data voor %s — overgeslagen", symbol)
                continue

            price_now    = closes[-1]
            price_20d_ago = closes[-_MIN_BARS_RS]

            if price_20d_ago <= 0 or qqq_20d_ago <= 0:
                continue

            rs_20d = (price_now / price_20d_ago) / (qqq_now / qqq_20d_ago)
            rs_values.append(rs_20d)
            components[symbol] = {
                "price":   round(price_now, 4),
                "rs_20d":  round(rs_20d, 6),
            }

        if not rs_values:
            self._log.warning("Geen defensive ETF data beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_defensive_data"
            return None

        avg_defensive_rs = mean(rs_values)
        regime = classify_regime(qqq_vs_50d, avg_defensive_rs)

        payload = {
            "action":           "regime_signal",
            "regime":           regime,
            "qqq_vs_50d":       round(qqq_vs_50d, 6),
            "avg_defensive_rs": round(avg_defensive_rs, 6),
            "components":       components,
        }

        self._emit_signal(payload)
        self._last_action = f"tick:regime={regime}"

        regime_changed = regime != self._last_logged_regime
        qqq_moved = (
            self._last_logged_qqq is None
            or abs(qqq_vs_50d - self._last_logged_qqq) >= self._QQQ_LOG_THRESHOLD
        )
        if regime_changed or qqq_moved:
            self._log.info(
                "RS Regime | regime=%s  qqq_vs_50d=%.3f  avg_def_rs=%.3f",
                regime, qqq_vs_50d, avg_defensive_rs,
            )
            self._last_logged_regime = regime
            self._last_logged_qqq    = qqq_vs_50d
        else:
            self._log.debug(
                "RS Regime (onveranderd) | regime=%s  qqq_vs_50d=%.3f  avg_def_rs=%.3f",
                regime, qqq_vs_50d, avg_defensive_rs,
            )
        return payload

    def _fetch_closes(self, symbol: str, get_candles_fn) -> list[float] | None:
        """
        Haal sluitprijzen op voor `symbol`. Retourneert lijst of None bij fout.
        """
        try:
            candles = get_candles_fn(symbol, period=_CANDLE_PERIOD, interval=_CANDLE_INTERVAL)
            if not candles:
                return None
            return [c.close for c in candles if c.close > 0]
        except Exception:
            self._log.exception("Fout bij ophalen candles voor %s", symbol)
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_signal(self, payload: dict) -> None:
        """Schrijf RegimeSignal naar ANT_LOGS/rs_regime/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload=payload,
        )
        self._log_seq += 1

        log_path = self.logs_root / "rs_regime" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon regime-signaal niet naar disk schrijven")

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
