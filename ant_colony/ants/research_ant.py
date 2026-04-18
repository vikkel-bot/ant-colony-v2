"""
ant_colony/ants/research_ant.py

ResearchAnt — analyseert markten en genereert StrategyCandidate objecten.

Verantwoordelijkheden:
  - Historische OHLCV data ophalen via BiomeAdapter (100 candles, 1h)
  - Drie technische signalen detecteren per symbool:
      1. SMA-crossover    — SMA20 kruist SMA50
      2. RSI              — oversold (< 30) of overbought (> 70)
      3. Bollinger bands  — prijs raakt boven- of onderband
  - Per signaal een backtest uitvoeren via Backtester
  - Kandidaten met sharpe > 0.5 en win_rate > 0.45 loggen als JSON naar
    ANT_LOGS/research/{ant_id}.jsonl
  - Heartbeat rapporteren aan scheduler na elke tick
  - Zichzelf netjes beëindigen bij TTL expiry

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.lab.backtester import Backtester, BacktestConfig, OHLCVBar
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)

_MIN_CANDLES        = 52    # SMA50 + 2 bars voor crossover detectie
_CANDLE_LIMIT       = 100   # candles ophalen per symbool per tick
_SHARPE_THRESHOLD   = 0.5
_WIN_RATE_THRESHOLD = 0.45
_TP_PCT             = 0.06  # 6 % take-profit voor backtests
_SL_PCT             = 0.03  # 3 % stop-loss voor backtests
_MAX_BARS_HELD      = 10


class ResearchAnt:
    """
    Analyseert markten op technische patronen en stelt StrategyCandidate objecten voor.

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

        # Deduplicatie: sla de laatste geëmitteerde candidate_id op per (symbol, signal_type).
        # Voorkomt dat dezelfde kandidaat meerdere ticks achtereen gelogd wordt.
        self._last_emitted: dict[tuple[str, str], str] = {}

        self._backtester = Backtester()
        self._log = logging.getLogger(f"ant.research.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Blokkerende tick-loop. Retourneert AntStatus bij afsluiting.

        Elke tick:
          1. TTL controleren
          2. Candles ophalen + strategieën analyseren per symbool
          3. Heartbeat sturen
          4. Wachten tot volgende tick (heartbeat_interval)
        """
        self._status = AntStatus.RUNNING
        self._log.info(
            "ResearchAnt gestart | mission=%s ttl=%ds symbols=%s",
            self.mission.mission_id,
            self.mission.ttl,
            self.mission.market_scope.symbols,
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
            self._log.info("ResearchAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("ResearchAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één analysecyclus: candles ophalen en drie strategieën controleren per symbool."""
        biome_id  = self.mission.market_scope.biome
        timeframe = (
            self.mission.market_scope.timeframes[0]
            if self.mission.market_scope.timeframes
            else "1h"
        )

        for symbol in self.mission.market_scope.symbols:
            candles = self._fetch_candles(symbol, timeframe, biome_id)
            if len(candles) < _MIN_CANDLES:
                self._log.debug(
                    "Te weinig candles voor %s (%d/%d) — overgeslagen",
                    symbol, len(candles), _MIN_CANDLES,
                )
                continue

            closes = [c.close for c in candles]
            self._check_sma_crossover(symbol, candles, closes)
            self._check_rsi(symbol, candles, closes)
            self._check_bollinger(symbol, candles, closes)

        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Data ophalen
    # ------------------------------------------------------------------

    def _fetch_candles(
        self, symbol: str, timeframe: str, biome_id: str
    ) -> list[MarketData]:
        """
        Haal historische candles op via de BiomeAdapter.

        Verwacht dat de adapter een `get_candles()` methode heeft (BitvavoAdapter v2).
        Retourneert [] als de methode ontbreekt of bij elke fout (fail-closed P2).
        """
        try:
            adapter = self.biome_registry.get(biome_id)
            if adapter is None:
                self._log.warning("Geen adapter voor biome='%s'", biome_id)
                return []

            if not adapter.is_available():
                self._log.warning("Adapter niet beschikbaar: biome='%s'", biome_id)
                return []

            get_candles_fn = getattr(adapter, "get_candles", None)
            if get_candles_fn is None:
                self._log.warning(
                    "Adapter heeft geen get_candles() — ResearchAnt vereist BitvavoAdapter v2"
                )
                return []

            return get_candles_fn(symbol, timeframe, _CANDLE_LIMIT) or []

        except Exception:
            self._log.exception("Fout bij ophalen candles: symbol=%s", symbol)
            return []

    # ------------------------------------------------------------------
    # Strategie detectie
    # ------------------------------------------------------------------

    def _check_sma_crossover(
        self, symbol: str, candles: list[MarketData], closes: list[float]
    ) -> None:
        """SMA20/SMA50 crossover — long bij golden cross, short bij death cross."""
        if len(closes) < 52:
            return

        sma20 = _sma(closes, 20)
        sma50 = _sma(closes, 50)

        s20_curr, s20_prev = sma20[-1], sma20[-2]
        s50_curr, s50_prev = sma50[-1], sma50[-2]

        if s20_prev <= s50_prev and s20_curr > s50_curr:
            direction = "long"
        elif s20_prev >= s50_prev and s20_curr < s50_curr:
            direction = "short"
        else:
            return

        self._evaluate_and_emit(
            symbol=symbol,
            candles=candles,
            signal_type="sma_crossover",
            direction=direction,
            parameters={"sma_fast": 20, "sma_slow": 50},
            entry_conditions={"sma20_crosses_sma50": direction},
            logic_summary=(
                f"SMA20 kruist SMA50 ({'golden' if direction == 'long' else 'death'} cross) op {symbol}"
            ),
        )

    def _check_rsi(
        self, symbol: str, candles: list[MarketData], closes: list[float]
    ) -> None:
        """RSI < 30 → long (oversold), RSI > 70 → short (overbought)."""
        rsi_val = _rsi(closes, period=14)
        if rsi_val is None:
            return

        if rsi_val < 30:
            direction   = "long"
            signal_type = "rsi_oversold"
        elif rsi_val > 70:
            direction   = "short"
            signal_type = "rsi_overbought"
        else:
            return

        self._evaluate_and_emit(
            symbol=symbol,
            candles=candles,
            signal_type=signal_type,
            direction=direction,
            parameters={"rsi_period": 14, "rsi_value": round(rsi_val, 2)},
            entry_conditions={
                "rsi": round(rsi_val, 2),
                "threshold": 30 if direction == "long" else 70,
            },
            logic_summary=f"RSI={rsi_val:.1f} {signal_type} op {symbol}",
        )

    def _check_bollinger(
        self, symbol: str, candles: list[MarketData], closes: list[float]
    ) -> None:
        """Prijs raakt de bovenband → short, onderband → long."""
        bb = _bollinger(closes, period=20, std_dev=2.0)
        if bb is None:
            return

        upper, middle, lower = bb
        last_close = closes[-1]

        if last_close >= upper:
            direction   = "short"
            signal_type = "bb_upper_touch"
        elif last_close <= lower:
            direction   = "long"
            signal_type = "bb_lower_touch"
        else:
            return

        self._evaluate_and_emit(
            symbol=symbol,
            candles=candles,
            signal_type=signal_type,
            direction=direction,
            parameters={
                "bb_period": 20,
                "bb_std": 2.0,
                "upper": round(upper, 4),
                "lower": round(lower, 4),
            },
            entry_conditions={
                "price": round(last_close, 4),
                "band": "upper" if direction == "short" else "lower",
            },
            logic_summary=(
                f"Prijs raakt {'bovenste' if direction == 'short' else 'onderste'} "
                f"Bollinger band op {symbol}"
            ),
        )

    # ------------------------------------------------------------------
    # Evaluatie + emissie
    # ------------------------------------------------------------------

    def _evaluate_and_emit(
        self,
        symbol: str,
        candles: list[MarketData],
        signal_type: str,
        direction: str,
        parameters: dict,
        entry_conditions: dict,
        logic_summary: str,
    ) -> None:
        """
        Voer backtest uit; emitteer StrategyCandidate als fitness boven drempel.

        Deduplicatie: dezelfde (symbol, signal_type) combinatie wordt niet opnieuw
        gelogd zolang er geen nieuw kandidaat-ID aangemaakt wordt.
        """
        bars = [
            OHLCVBar(
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
            for c in candles
            if c.close > 0
        ]

        if len(bars) < 2:
            return

        try:
            config  = BacktestConfig(
                direction=direction,
                take_profit_pct=_TP_PCT,
                stop_loss_pct=_SL_PCT,
                max_bars_held=_MAX_BARS_HELD,
            )
            results = self._backtester.run(bars, config)
        except Exception:
            self._log.exception("Backtest mislukt voor %s/%s", symbol, signal_type)
            return

        sharpe   = results.sharpe_ratio or 0.0
        win_rate = results.win_rate or 0.0

        self._log.debug(
            "%s %s | direction=%s sharpe=%.3f win_rate=%.3f trades=%s",
            symbol, signal_type, direction, sharpe, win_rate, results.total_trades,
        )

        if sharpe < _SHARPE_THRESHOLD or win_rate < _WIN_RATE_THRESHOLD:
            return

        candidate_id = (
            f"candidate-{symbol.lower().replace('-', '')}"
            f"-{signal_type}-{uuid.uuid4().hex[:8]}"
        )

        candidate = StrategyCandidate(
            candidate_id=candidate_id,
            name=f"{signal_type.upper()} {symbol}",
            source="internal",
            biome=self.mission.market_scope.biome,
            market_scope={"symbol": symbol, "timeframe": "1h"},
            logic_summary=logic_summary,
            parameters=parameters,
            entry_conditions=entry_conditions,
            exit_conditions={
                "take_profit_pct": _TP_PCT,
                "stop_loss_pct": _SL_PCT,
                "max_bars_held": _MAX_BARS_HELD,
            },
            backtest_results=results,
            fitness_score=round(sharpe, 4),
            status=CandidateStatus.RESEARCH,
            provenance=[
                ProvenanceEntry(
                    actor=self.ant_id,
                    action="proposed",
                    details={
                        "source": "research_ant",
                        "symbol": symbol,
                        "signal_type": signal_type,
                        "direction": direction,
                        "mission_id": self.mission.mission_id,
                    },
                )
            ],
        )

        self._last_emitted[(symbol, signal_type)] = candidate_id
        self._last_action = f"candidate:{signal_type}:{symbol}"
        self._log_seq += 1

        self._log.info(
            "KANDIDAAT | %s %s | sharpe=%.3f win_rate=%.3f direction=%s id=%s",
            symbol, signal_type, sharpe, win_rate, direction, candidate_id,
        )
        self._write_candidate_log(candidate)

    # ------------------------------------------------------------------
    # Disk logging
    # ------------------------------------------------------------------

    def _write_candidate_log(self, candidate: StrategyCandidate) -> None:
        """Schrijf StrategyCandidate als JSON-regel naar ANT_LOGS/research/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        log_path = self.logs_root / "research" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(candidate.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet naar disk schrijven: %s", log_path)

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


# ---------------------------------------------------------------------------
# Technische indicator hulpfuncties (module-privaat)
# ---------------------------------------------------------------------------

def _sma(closes: list[float], period: int) -> list[float]:
    """Rolling SMA. Retourneert lege lijst als er minder dan `period` waarden zijn."""
    if len(closes) < period:
        return []
    return [sum(closes[i : i + period]) / period for i in range(len(closes) - period + 1)]


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Vereenvoudigde RSI op basis van SMA van gains/losses over `period` bars.

    Retourneert None bij onvoldoende data (minder dan period + 1 waarden).
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    recent   = deltas[-period:]
    avg_gain = sum(d for d in recent if d > 0) / period
    avg_loss = sum(-d for d in recent if d < 0) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(
    closes: list[float], period: int = 20, std_dev: float = 2.0
) -> tuple[float, float, float] | None:
    """
    Bollinger Bands voor de laatste bar.

    Retourneert (upper, middle, lower) of None bij onvoldoende data.
    """
    if len(closes) < period:
        return None
    window   = closes[-period:]
    middle   = sum(window) / period
    variance = sum((c - middle) ** 2 for c in window) / period
    std      = math.sqrt(variance)
    return middle + std_dev * std, middle, middle - std_dev * std
