"""
ant_colony/ants/scout_ant.py

ScoutAnt — observeert markten en rapporteert kansen aan het systeem.

Verantwoordelijkheden:
  - Marktdata ophalen via BiomeAdapter voor toegewezen symbolen
  - Prijsbewegingen > 1% en volume-spikes > 2x gemiddelde detecteren
  - Gedetecteerde kansen loggen als OpportunitySignal naar ANT_LOGS/scouts/
  - Heartbeat rapporteren aan de scheduler na elke tick
  - Zichzelf netjes beëindigen bij TTL expiry

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.ants.time_filter_ant import read_latest_time_signal
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.opportunity_signal import OpportunitySignal, SignalType

_PRICE_MOVE_THRESHOLD = 0.01   # 1%
_VOLUME_SPIKE_FACTOR  = 2.0    # 2× gemiddeld volume
_HISTORY_SIZE         = 10     # candles voor volume-baseline
_MOMENTUM_LOOKBACK    = 20     # vorige 20 candles voor breakout
_MEAN_REVERSION_WINDOW = 20    # laatste 20 closes voor Z-score


class ScoutAnt:
    """
    Observeert markten op kansen; rapporteert via OpportunitySignal.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission met scope, TTL en heartbeat-interval.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry voor adapter-lookup.
        logs_root:       Pad naar ANT_LOGS; None schakelt disk-logging uit.
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

        self._status: AntStatus = AntStatus.IDLE
        self._budget_used: float = 0.0
        self._last_action: str = "init"
        self._log_seq: int = 0

        # Rollenend volume-venster per symbool (maxlen=10)
        self._vol_history: dict[str, deque[float]] = {
            symbol: deque(maxlen=_HISTORY_SIZE)
            for symbol in mission.market_scope.symbols
        }
        self._candle_history: dict[str, deque[MarketData]] = {
            symbol: deque(maxlen=_MOMENTUM_LOOKBACK + 1)
            for symbol in mission.market_scope.symbols
        }

        self._log = logging.getLogger(f"ant.scout.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Blokkerende tick-loop. Retourneert AntStatus bij afsluiting.

        Elke tick:
          1. TTL controleren
          2. Marktdata ophalen en kansen detecteren
          3. Heartbeat sturen
          4. Wachten tot volgende tick
        """
        self._status = AntStatus.RUNNING
        self._log.info(
            "ScoutAnt gestart | mission=%s ttl=%ds symbols=%s",
            self.mission.mission_id,
            self.mission.ttl,
            self.mission.market_scope.symbols,
        )

        started_at    = datetime.now(tz=timezone.utc)
        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info("TTL verlopen (%.1fs / %ds) — afsluiten", elapsed, self.mission.ttl)
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("ScoutAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("ScoutAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één marktcyclus: data ophalen voor alle symbolen en kansen detecteren."""
        if self.logs_root is not None:
            sig = read_latest_time_signal(self.logs_root)
            if sig is not None and not sig.get("trade_allowed", True):
                self._log.debug(
                    "Signalen overgeslagen — buiten kill zone: session=%s",
                    sig.get("session", "unknown"),
                )
                self._last_action = f"skip:buiten_kill_zone:{sig.get('session', 'unknown')}"
                return

        biome_id = self.mission.market_scope.biome
        timeframe = self._pick_timeframe()

        for symbol in self.mission.market_scope.symbols:
            candle = self._fetch_candle(symbol, timeframe, biome_id)
            if candle is None:
                continue

            self._vol_history[symbol].append(candle.volume)
            self._candle_history[symbol].append(candle)

            signals = self._detect(symbol, candle)
            for signal in signals:
                self._emit(signal)

        self._last_action = "tick"

    def _pick_timeframe(self) -> str:
        """Gebruik de eerste timeframe uit de mission-scope, anders '1h'."""
        tf = self.mission.market_scope.timeframes
        return tf[0] if tf else "1h"

    # ------------------------------------------------------------------
    # Data ophalen
    # ------------------------------------------------------------------

    def _fetch_candle(self, symbol: str, timeframe: str, biome_id: str) -> MarketData | None:
        """Haal één candle op. Retourneert None bij elke fout (fail-closed P2)."""
        try:
            adapter = self.biome_registry.get(biome_id)
            if adapter is None:
                self._log.warning("Geen adapter voor biome='%s'", biome_id)
                return None

            if not adapter.is_available():
                self._log.warning("Adapter niet beschikbaar: biome='%s'", biome_id)
                return None

            candle = adapter.get_market_data(symbol, timeframe)
            if candle is None:
                self._log.debug("Geen data: symbol=%s timeframe=%s", symbol, timeframe)
                return None

            if candle.is_stale():
                self._log.warning("Stale data genegeerd: symbol=%s", symbol)
                return None

            if not candle.is_valid_price:
                self._log.warning("Ongeldige prijs genegeerd: symbol=%s close=%.4f", symbol, candle.close)
                return None

            return candle

        except Exception:
            self._log.exception("Fout bij ophalen marktdata: symbol=%s", symbol)
            return None

    # ------------------------------------------------------------------
    # Detectie
    # ------------------------------------------------------------------

    def _detect(self, symbol: str, candle: MarketData) -> list[OpportunitySignal]:
        """Detecteer kansen in de candle. Retourneert 0, 1 of 2 signals."""
        signals: list[OpportunitySignal] = []

        price_signal = self._check_price_move(symbol, candle)
        if price_signal:
            signals.append(price_signal)

        volume_signal = self._check_volume_spike(symbol, candle)
        if volume_signal:
            signals.append(volume_signal)

        momentum_signal = self._check_momentum_breakout(symbol, candle)
        if momentum_signal:
            signals.append(momentum_signal)

        mean_reversion_signal = self._check_mean_reversion_oversold(symbol, candle)
        if mean_reversion_signal:
            signals.append(mean_reversion_signal)

        return signals

    def _check_price_move(self, symbol: str, candle: MarketData) -> OpportunitySignal | None:
        """Prijsbeweging > 1% in de candle (open→close)."""
        if candle.open <= 0:
            return None

        change_pct = (candle.close - candle.open) / candle.open

        if abs(change_pct) <= _PRICE_MOVE_THRESHOLD:
            return None

        # Confidence schaalt van 1% (0.33) naar 3%+ (1.0)
        confidence = min(abs(change_pct) / 0.03, 1.0)

        self._log.info(
            "PRICE_MOVE | %s %.2f%% @ %.4f (confidence=%.2f)",
            symbol, change_pct * 100, candle.close, confidence,
        )
        return OpportunitySignal(
            symbol=symbol,
            signal_type=SignalType.PRICE_MOVE,
            current_price=candle.close,
            change_pct=round(change_pct, 6),
            confidence=round(confidence, 4),
            biome=self.mission.market_scope.biome,
            mission_id=self.mission.mission_id,
        )

    def _check_volume_spike(self, symbol: str, candle: MarketData) -> OpportunitySignal | None:
        """Volume-spike > 2× het gemiddelde van de laatste 10 candles."""
        history = self._vol_history[symbol]

        # Wacht tot er minstens 2 vorige candles zijn voor een betrouwbare baseline.
        # De huidige candle is al toegevoegd vóór _detect(); vergelijk dus met history[:-1].
        if len(history) < 3:
            return None

        # Baseline = gemiddelde zonder de zojuist toegevoegde candle
        baseline_volumes = list(history)[:-1]
        avg_volume = mean(baseline_volumes)

        if avg_volume <= 0 or candle.volume <= 0:
            return None

        ratio = candle.volume / avg_volume

        if ratio <= _VOLUME_SPIKE_FACTOR:
            return None

        # Confidence schaalt van 2× (0.30) naar 5×+ (1.0)
        confidence = min((ratio - 2.0) / 3.0 + 0.30, 1.0)

        self._log.info(
            "VOLUME_SPIKE | %s ratio=%.2fx @ %.4f (confidence=%.2f)",
            symbol, ratio, candle.close, confidence,
        )
        return OpportunitySignal(
            symbol=symbol,
            signal_type=SignalType.VOLUME_SPIKE,
            current_price=candle.close,
            change_pct=0.0,
            confidence=round(confidence, 4),
            biome=self.mission.market_scope.biome,
            mission_id=self.mission.mission_id,
        )

    def _check_momentum_breakout(
        self, symbol: str, candle: MarketData
    ) -> OpportunitySignal | None:
        """Laatste close breekt boven de hoogste high van de vorige 20 candles."""
        history = list(self._candle_history[symbol])
        if len(history) < _MOMENTUM_LOOKBACK + 1:
            return None

        previous_highs = [bar.high for bar in history[-(_MOMENTUM_LOOKBACK + 1):-1]]
        if len(previous_highs) < _MOMENTUM_LOOKBACK:
            return None

        breakout_level = max(previous_highs)
        if breakout_level <= 0 or candle.close <= breakout_level:
            return None

        breakout_pct = (candle.close - breakout_level) / breakout_level
        confidence = min(0.35 + breakout_pct / 0.04, 1.0)

        self._log.info(
            "MOMENTUM_BREAKOUT | %s close=%.4f high20=%.4f (confidence=%.2f)",
            symbol, candle.close, breakout_level, confidence,
        )
        return OpportunitySignal(
            symbol=symbol,
            signal_type=SignalType.MOMENTUM_BREAKOUT,
            current_price=candle.close,
            change_pct=round(breakout_pct, 6),
            confidence=round(confidence, 4),
            biome=self.mission.market_scope.biome,
            mission_id=self.mission.mission_id,
        )

    def _check_mean_reversion_oversold(
        self, symbol: str, candle: MarketData
    ) -> OpportunitySignal | None:
        """Laatste close heeft een Z-score lager dan -2.0 over 20 closes."""
        history = list(self._candle_history[symbol])
        if len(history) < _MEAN_REVERSION_WINDOW:
            return None

        closes = [bar.close for bar in history[-_MEAN_REVERSION_WINDOW:]]
        avg_close = mean(closes)
        std_close = pstdev(closes)
        if avg_close <= 0 or std_close <= 0:
            return None

        z_score = (candle.close - avg_close) / std_close
        if z_score >= -2.0:
            return None

        deviation_pct = (candle.close - avg_close) / avg_close
        confidence = min(0.35 + (abs(z_score) - 2.0) / 2.0, 1.0)

        self._log.info(
            "MEAN_REVERSION_OVERSOLD | %s z=%.2f close=%.4f mean20=%.4f (confidence=%.2f)",
            symbol, z_score, candle.close, avg_close, confidence,
        )
        return OpportunitySignal(
            symbol=symbol,
            signal_type=SignalType.MEAN_REVERSION_OVERSOLD,
            current_price=candle.close,
            change_pct=round(deviation_pct, 6),
            confidence=round(confidence, 4),
            biome=self.mission.market_scope.biome,
            mission_id=self.mission.mission_id,
        )

    # ------------------------------------------------------------------
    # Signaal emissie
    # ------------------------------------------------------------------

    def _emit(self, signal: OpportunitySignal) -> None:
        """Log OpportunitySignal naar disk en stuur last_action bij."""
        self._last_action = f"signal:{signal.signal_type.value}:{signal.symbol}"
        self._write_signal_log(signal)

    def _write_signal_log(self, signal: OpportunitySignal) -> None:
        """Schrijf het signaal als AuditEvent naar ANT_LOGS/scouts/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action": "opportunity_detected",
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "signal_type": signal.signal_type.value,
                "current_price": signal.current_price,
                "change_pct": signal.change_pct,
                "confidence": signal.confidence,
                "biome": signal.biome,
                "detected_at": signal.detected_at.isoformat(),
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "scouts" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon signaal niet naar disk schrijven: %s", log_path)

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
                ant_id=self.ant_id,
                mission_id=self.mission.mission_id,
                node_id=self.mission.allowed_node,
                status=hb_status,
                budget_used=self._budget_used,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
            self._log.debug("Heartbeat gestuurd | action=%s", self._last_action)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
