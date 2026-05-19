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
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.lab.backtester import Backtester, BacktestConfig, OHLCVBar
from ant_colony.research.lean_validator import LeanValidator
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)
from ant_colony.strategies import DISABLED_STRATEGY_TYPES

_MIN_CANDLES        = 200   # backtester vereist ≥200 bars voor betrouwbare walk-forward resultaten
_CANDLE_LIMIT       = 500   # candles ophalen per symbool per tick (verhoogd voor betrouwbaardere backtests)
_SHARPE_THRESHOLD   = 0.15
_WIN_RATE_THRESHOLD = 0.45
_TP_PCT             = 0.06  # 6 % take-profit voor backtests
_SL_PCT             = 0.03  # 3 % stop-loss voor backtests
_MAX_BARS_HELD      = 10
_RESEARCH_WATCHDOG_SECONDS = float(os.getenv("RESEARCH_ANT_WATCHDOG_SECONDS", "600"))
_REJECT_LOG_COOLDOWN_SECONDS = float(os.getenv("RESEARCH_REJECT_LOG_COOLDOWN_SECONDS", "1800"))

# Commodity symbool → yfinance ticker mapping
_COMMODITY_YFINANCE_TICKERS: dict[str, str] = {
    "NATGAS": "NG=F",
    "COPPER": "HG=F",
    "SILVER": "SI=F",
}

# BacktestConfig profielen per keyword-categorie (tp_pct, sl_pct, max_bars_held)
_KEYWORD_PROFILES: dict[str, tuple[float, float, int]] = {
    "momentum":       (0.08, 0.04, 15),   # langere trend, ruimere TP
    "macd":           (0.07, 0.035, 12),  # momentum-achtig
    "breakout":       (0.07, 0.035, 12),  # trend-volgend
    "sma":            (0.06, 0.03, 20),   # crossover — langzamer signaal
    "crossover":      (0.06, 0.03, 20),
    "ema":            (0.06, 0.03, 15),
    "rsi":            (0.04, 0.02, 7),    # mean-reversion, sneller
    "bollinger":      (0.03, 0.015, 5),   # tight mean-reversion
    "mean reversion": (0.03, 0.015, 5),
}


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
        self._last_tick_started_monotonic: float | None = None
        self._last_tick_completed_monotonic: float = monotonic()
        self._watchdog_restarts: int = 0

        # Deduplicatie: sla de laatste geëmitteerde candidate_id op per (symbol, signal_type).
        # Voorkomt dat dezelfde kandidaat meerdere ticks achtereen gelogd wordt.
        self._last_emitted: dict[tuple[str, str], str] = {}
        self._last_rejected: dict[tuple[str, str, str, str], float] = {}
        self._reject_tick_new: int = 0
        self._reject_tick_suppressed: int = 0
        self._reject_tick_reasons: dict[str, int] = {}
        self._seen_ingestion_ids: set[str] = self._preload_seen_ingestion_ids(logs_root)

        self._backtester = Backtester()
        self._lean_validator = LeanValidator(logs_root=logs_root) if logs_root is not None else None
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

                self._run_tick_with_watchdog()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("ResearchAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("ResearchAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _run_tick_with_watchdog(self) -> None:
        """
        Voer één research tick uit met een harde liveness watchdog.

        De meest waarschijnlijke stall zit in externe resources binnen _tick()
        (adapter.get_candles, filesystem scans of backtest calls). Python kan
        zo'n geblokkeerde call niet veilig killen, dus bij timeout laten we de
        worker als daemon achter en starten we de volgende research-cyclus.
        """
        timeout = max(0.0, float(_RESEARCH_WATCHDOG_SECONDS))
        if timeout <= 0:
            self._tick()
            self._last_tick_completed_monotonic = monotonic()
            return

        completed = threading.Event()
        errors: list[BaseException] = []
        started = monotonic()
        self._last_tick_started_monotonic = started

        def worker() -> None:
            try:
                self._tick()
            except BaseException as exc:  # pragma: no cover - opnieuw gegooid in hoofdthread
                errors.append(exc)
            finally:
                completed.set()

        tick_thread = threading.Thread(
            target=worker,
            name=f"research-cycle-{self.ant_id[:8]}",
            daemon=True,
        )
        tick_thread.start()

        if not completed.wait(timeout=timeout):
            age = monotonic() - started
            self._watchdog_restarts += 1
            self._last_action = "tick_watchdog_restart"
            self._log.error(
                "ResearchAnt watchdog: geen tick voltooid in %.1fs (> %.1fs) — "
                "research cyclus wordt opnieuw gestart | restarts=%d",
                age,
                timeout,
                self._watchdog_restarts,
            )
            self._write_activity_log(
                {
                    "action": "research_watchdog_restart",
                    "duration_seconds": round(age, 3),
                    "threshold_seconds": timeout,
                    "watchdog_restarts": self._watchdog_restarts,
                }
            )
            self._send_heartbeat()
            return

        if errors:
            raise errors[0]

        self._last_tick_completed_monotonic = monotonic()

    def _tick(self) -> None:
        """Één analysecyclus: candles ophalen en drie strategieën controleren per symbool."""
        tick_started = monotonic()
        biome_id  = self.mission.market_scope.biome
        timeframe = (
            self.mission.market_scope.timeframes[0]
            if self.mission.market_scope.timeframes
            else "1h"
        )
        symbols_scanned = 0
        self._reject_tick_new = 0
        self._reject_tick_suppressed = 0
        self._reject_tick_reasons = {}

        for symbol in self.mission.market_scope.symbols:
            symbols_scanned += 1
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

        self._process_ingestion_candidates()
        self._process_commodity_watchtower_signals()
        self._check_strategy_diversity()
        self._last_action = "tick"
        self._write_activity_log(
            {
                "action": "research_tick",
                "biome": biome_id,
                "timeframe": timeframe,
                "symbols_scanned": symbols_scanned,
                "duration_seconds": round(monotonic() - tick_started, 3),
                "watchdog_restarts": self._watchdog_restarts,
                "rejected_new": self._reject_tick_new,
                "rejected_suppressed": self._reject_tick_suppressed,
                "rejected_reasons_count": dict(self._reject_tick_reasons),
            }
        )

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
        """RSI < 40 → long (oversold), RSI > 60 → short (overbought) — ruimer voor SIDEWAYS regime."""
        rsi_val = _rsi(closes, period=14)
        if rsi_val is None:
            return

        if rsi_val < 40:
            direction   = "long"
            signal_type = "rsi_oversold"
        elif rsi_val > 60:
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
                "threshold": 40 if direction == "long" else 60,
            },
            logic_summary=f"RSI={rsi_val:.1f} {signal_type} op {symbol}",
        )

    def _check_bollinger(
        self, symbol: str, candles: list[MarketData], closes: list[float]
    ) -> None:
        """Prijs raakt of nadert de bands (binnen 10% van bandbreedte) → long/short."""
        bb = _bollinger(closes, period=20, std_dev=2.0)
        if bb is None:
            return

        upper, middle, lower = bb
        last_close = closes[-1]
        proximity = 0.10 * (upper - lower)

        if last_close >= upper:
            direction   = "short"
            signal_type = "bb_upper_touch"
        elif last_close >= upper - proximity:
            direction   = "short"
            signal_type = "bb_upper_approach"
        elif last_close <= lower:
            direction   = "long"
            signal_type = "bb_lower_touch"
        elif last_close <= lower + proximity:
            direction   = "long"
            signal_type = "bb_lower_approach"
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
                "proximity_pct": 10,
            },
            entry_conditions={
                "price": round(last_close, 4),
                "band": "upper" if direction == "short" else "lower",
            },
            logic_summary=(
                f"Prijs {'op' if 'touch' in signal_type else 'nabij'} "
                f"{'bovenste' if direction == 'short' else 'onderste'} "
                f"Bollinger band op {symbol}"
            ),
        )

    # ------------------------------------------------------------------
    # Ingestion-gekoppelde kandidaten
    # ------------------------------------------------------------------

    @staticmethod
    def _preload_seen_ingestion_ids(logs_root: Path | None) -> set[str]:
        """
        Scan ANT_LOGS/ingestion/*.jsonl bij startup en retourneer alle candidate_ids.

        Voorkomt dat historische ingested kandidaten na herstart opnieuw gebacktest
        en geëmitteerd worden.
        """
        seen: set[str] = set()
        if logs_root is None:
            return seen
        ingestion_dir = logs_root / "ingestion"
        if not ingestion_dir.exists():
            return seen
        for path in sorted(ingestion_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "candidate_ingested":
                        continue
                    cid = str(payload.get("candidate_id") or "")
                    if cid:
                        seen.add(cid)
            except OSError:
                pass
        return seen

    def _process_ingestion_candidates(self) -> None:
        """Lees ANT_LOGS/ingestion/*.jsonl en backtest nieuwe ingested candidates."""
        if self.logs_root is None:
            return
        ingestion_dir = self.logs_root / "ingestion"
        if not ingestion_dir.exists():
            self._log.debug("Ingestion map niet gevonden: %s", ingestion_dir)
            return

        biome_id  = self.mission.market_scope.biome
        timeframe = (
            self.mission.market_scope.timeframes[0]
            if self.mission.market_scope.timeframes
            else "1h"
        )

        all_files = sorted(ingestion_dir.glob("*.jsonl"))

        new_count = 0
        seen_count = 0
        for path in all_files:
            try:
                lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
                self._log.debug("research — ingestion | bestand=%s regels=%d", path.name, len(lines))
                candidates_in_file = 0
                for line in lines:
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    # Dubbele filter: event_type én payload.action
                    if record.get("event_type") != "action_executed":
                        continue
                    if payload.get("action") != "candidate_ingested":
                        continue

                    candidates_in_file += 1
                    candidate_id = str(payload.get("candidate_id") or "")
                    if not candidate_id:
                        self._log.debug(
                            "Ingestion record zonder candidate_id overgeslagen in %s", path.name
                        )
                        continue

                    if candidate_id in self._seen_ingestion_ids:
                        seen_count += 1
                        self._log.debug(
                            "Ingestion kandidaat al verwerkt — overgeslagen | id=%s",
                            candidate_id[:16],
                        )
                        continue

                    entry_keywords = payload.get("entry_keywords") or []
                    self._log.info(
                        "Nieuwe ingestion kandidaat gevonden | id=%s keywords=%s",
                        candidate_id[:16], entry_keywords[:5],
                    )
                    self._seen_ingestion_ids.add(candidate_id)
                    new_count += 1
                    self._backtest_ingested_candidate(candidate_id, payload, biome_id, timeframe)

                self._log.debug(
                    "research — ingestion | bestand=%s kandidaten=%d",
                    path.name, candidates_in_file,
                )
            except OSError:
                self._log.warning("Kan ingestion-log niet lezen: %s", path)

        self._log.info(
            "research tick | files=%d nieuw=%d verwerkt=%d",
            len(all_files), new_count, seen_count,
        )

    def _backtest_ingested_candidate(
        self,
        candidate_id: str,
        payload: dict,
        biome_id: str,
        timeframe: str,
    ) -> None:
        """
        Backtest een ingested candidate op alle missie-symbolen.

        IngestionAnt-payloads bevatten geen specifiek symbool (GitHub repos zijn
        niet symbool-specifiek). We testen de strategie op alle symbolen uit de
        missie. Richting leiden we af uit entry_keywords (default: long).
        """
        # --- symbolen bepalen ---
        # Payload heeft geen market_scope veld; gebruik missie-symbolen.
        market_scope = payload.get("market_scope") or {}
        symbol_field = market_scope.get("symbol") or market_scope.get("symbols")
        if isinstance(symbol_field, list):
            symbols = [str(s) for s in symbol_field if s]
        elif symbol_field:
            symbols = [str(symbol_field)]
        else:
            symbols = list(self.mission.market_scope.symbols)

        if not symbols:
            self._log.warning(
                "Ingested candidate %s: geen symbolen gevonden — overgeslagen", candidate_id
            )
            return

        # --- richting afleiden uit entry_keywords ---
        entry_keywords = payload.get("entry_keywords") or []
        if "short" in entry_keywords and "long" not in entry_keywords:
            direction = "short"
        else:
            direction = "long"

        logic_summary = (
            payload.get("logic_summary")
            or f"Ingested candidate {candidate_id[:8]} gevalideerd door ResearchAnt"
        )

        bt_config = _config_from_keywords(entry_keywords, direction)
        self._log.info(
            "Ingested backtest | id=%s direction=%s symbolen=%s keywords=%s"
            " → tp=%.3f sl=%.3f bars=%d",
            candidate_id[:12], direction, symbols, entry_keywords[:5],
            bt_config.take_profit_pct, bt_config.stop_loss_pct, bt_config.max_bars_held,
        )

        for symbol in symbols:
            candles = self._fetch_candles(symbol, timeframe, biome_id)
            if len(candles) < _MIN_CANDLES:
                self._log.debug(
                    "Te weinig candles voor ingested %s/%s (%d/%d)",
                    candidate_id[:8], symbol, len(candles), _MIN_CANDLES,
                )
                continue

            self._evaluate_and_emit(
                symbol=symbol,
                candles=candles,
                signal_type=f"ingested_{candidate_id[:8]}",
                direction=direction,
                parameters={
                    "source_candidate_id": candidate_id,
                    "entry_keywords": entry_keywords,
                    "tp_pct": bt_config.take_profit_pct,
                    "sl_pct": bt_config.stop_loss_pct,
                    "max_bars_held": bt_config.max_bars_held,
                },
                entry_conditions={"direction": direction, "keywords": entry_keywords},
                logic_summary=f"{logic_summary} [{symbol}]",
                backtest_config=bt_config,
            )

    # ------------------------------------------------------------------
    # Commodity watchtower-signalen (NATGAS / COPPER / SILVER via yfinance)
    # ------------------------------------------------------------------

    def _process_commodity_watchtower_signals(self) -> None:
        """Genereer watchtower_signal kandidaten voor commodity symbolen via yfinance.

        Alleen actief als de missie-biome "commodity" is. Per symbool in
        _COMMODITY_YFINANCE_TICKERS dat ook in de mission.market_scope.symbols staat
        worden candles opgehaald en een kandidaat geëmitteerd via het bestaande
        _evaluate_and_emit pad (inclusief backtest en deduplicatie).
        """
        if self.mission.market_scope.biome != "commodity":
            return
        mission_symbols = set(self.mission.market_scope.symbols or [])
        for symbol, yf_ticker in _COMMODITY_YFINANCE_TICKERS.items():
            if mission_symbols and symbol not in mission_symbols:
                continue
            candles = self._fetch_commodity_candles(yf_ticker, symbol)
            if len(candles) < _MIN_CANDLES:
                self._log.debug(
                    "Te weinig commodity candles voor %s/%s (%d/%d) — overgeslagen",
                    symbol, yf_ticker, len(candles), _MIN_CANDLES,
                )
                continue
            closes = [c.close for c in candles]
            self._evaluate_and_emit(
                symbol=symbol,
                candles=candles,
                signal_type="watchtower_signal",
                direction="long",
                parameters={"yf_ticker": yf_ticker, "asset_type": "commodity"},
                entry_conditions={"asset_type": "commodity", "yf_ticker": yf_ticker},
                logic_summary=f"Commodity watchtower signal voor {symbol} via {yf_ticker}",
            )

    def _fetch_commodity_candles(self, yf_ticker: str, symbol: str) -> list[MarketData]:
        """Haal dagelijkse OHLCV candles voor een commodity op via yfinance (lazy import).

        Gebruikt period="2y" en interval="1d" zodat ≥200 bars beschikbaar zijn
        voor de backtester. Retourneert [] bij importfout of elke andere fout (P2).
        """
        try:
            import yfinance as yf  # lazy import — niet verplicht geïnstalleerd
            df = yf.Ticker(yf_ticker).history(period="2y", interval="1d", auto_adjust=True)
            if df is None or df.empty:
                self._log.warning("Geen yfinance data voor commodity %s (%s)", symbol, yf_ticker)
                return []
            result: list[MarketData] = []
            for ts, row in df.iterrows():
                try:
                    close = float(row.get("Close") or 0.0)
                    if close <= 0:
                        continue
                    if hasattr(ts, "to_pydatetime"):
                        dt = ts.to_pydatetime()
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = datetime.now(tz=timezone.utc)
                    result.append(MarketData(
                        symbol=symbol,
                        timeframe="1d",
                        timestamp=dt,
                        open=float(row.get("Open") or 0.0),
                        high=float(row.get("High") or 0.0),
                        low=float(row.get("Low") or 0.0),
                        close=close,
                        volume=float(row.get("Volume") or 0.0),
                        biome_id="commodity",
                    ))
                except Exception:
                    continue
            return result
        except ImportError:
            self._log.warning("yfinance niet geïnstalleerd — commodity candles niet beschikbaar")
            return []
        except Exception:
            self._log.exception("Fout bij ophalen commodity candles voor %s (%s)", symbol, yf_ticker)
            return []

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
        backtest_config: BacktestConfig | None = None,
    ) -> None:
        """
        Voer backtest uit; emitteer StrategyCandidate als fitness boven drempel.

        backtest_config: optionele override — als None worden module-defaults gebruikt.
        Deduplicatie: dezelfde (symbol, signal_type) combinatie wordt niet opnieuw
        gelogd zolang er geen nieuw kandidaat-ID aangemaakt wordt.
        """
        if (symbol, signal_type) in self._last_emitted:
            return

        if backtest_config is not None:
            config = backtest_config
        else:
            _st = _strategy_type_from_signal_type(signal_type)
            config = BacktestConfig(
                direction=direction,
                take_profit_pct=_TP_PCT,
                stop_loss_pct=_SL_PCT,
                max_bars_held=_MAX_BARS_HELD,
                strategy_type=_st,
            )

        disabled_type = _disabled_strategy_type(config.strategy_type)
        if disabled_type is not None:
            self._last_action = f"strategy_disabled:{disabled_type}:{symbol}"
            self._log.info(
                "Kandidaat onderdrukt | %s %s | strategy_type=%s uitgeschakeld",
                symbol,
                signal_type,
                disabled_type,
            )
            return

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
            results = self._backtester.run(bars, config)
        except Exception:
            self._log.exception("Backtest mislukt voor %s/%s", symbol, signal_type)
            return

        sharpe   = results.sharpe_ratio or 0.0
        win_rate = results.win_rate or 0.0

        if sharpe < _SHARPE_THRESHOLD or win_rate < _WIN_RATE_THRESHOLD:
            self._record_rejected_candidate(
                symbol=symbol,
                signal_type=signal_type,
                direction=direction,
                parameters=parameters,
                sharpe=sharpe,
                win_rate=win_rate,
                total_trades=results.total_trades,
            )
            return

        self._log.info(
            "Kandidaat ACCEPTED | %s %s | direction=%s sharpe=%.3f win_rate=%.3f trades=%s",
            symbol, signal_type, direction, sharpe, win_rate, results.total_trades,
        )

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
                "take_profit_pct": config.take_profit_pct,
                "stop_loss_pct": config.stop_loss_pct,
                "max_bars_held": config.max_bars_held,
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
        self._validate_with_lean(candidate, internal_sharpe=sharpe)
        self._write_candidate_log(candidate, direction=direction)

    def _record_rejected_candidate(
        self,
        *,
        symbol: str,
        signal_type: str,
        direction: str,
        parameters: dict,
        sharpe: float,
        win_rate: float,
        total_trades: int,
    ) -> None:
        """Log nieuwe rejects, maar onderdruk herhalingen binnen de cooldown."""
        reason = "sharpe" if sharpe < _SHARPE_THRESHOLD else "win_rate"
        self._reject_tick_reasons[reason] = self._reject_tick_reasons.get(reason, 0) + 1
        params_hash = json.dumps(parameters, sort_keys=True, default=str)
        key = (symbol, signal_type, direction, params_hash)
        now = monotonic()
        last = self._last_rejected.get(key)
        if last is not None and now - last < _REJECT_LOG_COOLDOWN_SECONDS:
            self._reject_tick_suppressed += 1
            return

        self._last_rejected[key] = now
        self._reject_tick_new += 1
        self._log.info(
            "Kandidaat REJECTED | %s %s | direction=%s sharpe=%.3f (min %.2f)"
            " win_rate=%.3f (min %.2f) trades=%s reason=%s",
            symbol, signal_type, direction,
            sharpe, _SHARPE_THRESHOLD,
            win_rate, _WIN_RATE_THRESHOLD,
            total_trades,
            reason,
        )

    # ------------------------------------------------------------------
    # Optional Lean validation
    # ------------------------------------------------------------------

    def _validate_with_lean(self, candidate: StrategyCandidate, *, internal_sharpe: float) -> None:
        """Run optional Lean validation and attach the result to the candidate."""
        if self._lean_validator is None:
            return
        try:
            lean_result = self._lean_validator.validate(candidate.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - defensive: Lean may never block research
            self._log.warning(
                "Lean validatie overgeslagen | candidate_id=%s reden=%s",
                candidate.candidate_id,
                exc,
            )
            lean_result = {
                "lean_sharpe": None,
                "lean_max_drawdown": None,
                "lean_win_rate": None,
                "lean_trades": None,
                "lean_status": "unavailable",
                "lean_reason": str(exc),
            }
        lean_status = str(lean_result.get("lean_status") or "unavailable")
        lean_sharpe = lean_result.get("lean_sharpe")

        lean_validated: bool | None = None
        if lean_status == "passed":
            try:
                lean_validated = bool(float(lean_sharpe) > (internal_sharpe * 0.8))
            except (TypeError, ValueError):
                lean_validated = False
        elif lean_status == "failed":
            lean_validated = False

        candidate.parameters["lean_validation"] = lean_result
        if lean_validated is not None:
            candidate.parameters["lean_validated"] = lean_validated
        if candidate.backtest_results is not None:
            candidate.backtest_results.extra["lean_validation"] = lean_result
            if lean_validated is not None:
                candidate.backtest_results.extra["lean_validated"] = lean_validated

        self._log.info(
            "Lean validatie | candidate_id=%s status=%s lean_sharpe=%s",
            candidate.candidate_id,
            lean_status,
            lean_sharpe,
        )

    # ------------------------------------------------------------------
    # Disk logging
    # ------------------------------------------------------------------

    def _write_candidate_log(self, candidate: StrategyCandidate, direction: str = "") -> None:
        """Schrijf een AuditEvent naar ANT_LOGS/research/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        bt        = candidate.backtest_results
        sharpe    = bt.sharpe_ratio  if bt else None
        win_rate  = bt.win_rate      if bt else None
        avg_win   = bt.avg_win       if bt else None
        avg_loss  = bt.avg_loss      if bt else None

        exp_ret: float | None = None
        if win_rate is not None and avg_win is not None and avg_loss is not None:
            exp_ret = round((avg_win * win_rate) - (avg_loss * (1.0 - win_rate)), 4)

        entry_kws     = (candidate.entry_conditions or {}).get("keywords") or []
        tp_pct        = (candidate.exit_conditions or {}).get("take_profit_pct")
        strategy_type = _strategy_type_from_signal(candidate.name or "", entry_kws, tp_pct=tp_pct)
        grade         = _grade_from_sharpe(sharpe)
        lean_validation = (candidate.parameters or {}).get("lean_validation") or {}
        lean_validated = (candidate.parameters or {}).get("lean_validated")

        self._log.debug(
            "Classificatie | signal=%s keywords=%s tp_pct=%s → strategy_type=%s",
            candidate.name, entry_kws, tp_pct, strategy_type,
        )

        disabled_type = _disabled_strategy_type(strategy_type)
        if disabled_type is not None:
            self._last_action = f"strategy_disabled:{disabled_type}:{candidate.candidate_id}"
            self._log.debug(
                "Kandidaat afgewezen — strategy_type=%s is uitgeschakeld | %s",
                disabled_type,
                candidate.candidate_id,
            )
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":                  "candidate_accepted",
                "candidate_id":            candidate.candidate_id,
                "symbol":                  (candidate.market_scope or {}).get("symbol", ""),
                "sharpe":                  round(sharpe, 3) if sharpe is not None else None,
                "win_rate":                round(win_rate, 3) if win_rate is not None else None,
                "direction":               direction or (candidate.entry_conditions or {}).get("direction", ""),
                "signal_type":             candidate.name,
                "biome":                   candidate.biome,
                "strategy_type":           strategy_type,
                "grade":                   grade,
                "expected_return":         exp_ret,
                "best_streak":             bt.best_streak if bt else None,
                "worst_drawdown":          round(bt.max_drawdown_pct, 4) if bt and bt.max_drawdown_pct is not None else None,
                "best_regime":             bt.best_regime if bt else None,
                "regime_stats":            bt.regime_stats if bt else None,
                "tp_pct":                  (candidate.exit_conditions or {}).get("take_profit_pct"),
                "sl_pct":                  (candidate.exit_conditions or {}).get("stop_loss_pct"),
                "lean_validated":          lean_validated,
                "lean_status":             lean_validation.get("lean_status"),
                "lean_sharpe":             lean_validation.get("lean_sharpe"),
                "lean_max_drawdown":       lean_validation.get("lean_max_drawdown"),
                "lean_win_rate":           lean_validation.get("lean_win_rate"),
                "lean_trades":             lean_validation.get("lean_trades"),
                "lean_reason":             lean_validation.get("lean_reason"),
            },
        )

        log_path = self.logs_root / "research" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet naar disk schrijven: %s", log_path)

    def _write_activity_log(self, payload: dict) -> None:
        """Schrijf een compact research lifecycle-event zodat inactiviteit zichtbaar wordt."""
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
        log_path = self.logs_root / "research" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon research activiteit niet naar disk schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Strategy diversiteit controle
    # ------------------------------------------------------------------

    def _check_strategy_diversity(self) -> None:
        """
        Log een WARNING als de lokale research-top 3 hetzelfde strategy_type heeft.

        Leest ANT_LOGS/research/*.jsonl, sorteert op sharpe, bekijkt top 3.
        """
        if self.logs_root is None:
            return

        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return

        candidates: list[dict] = []
        for path in research_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                        payload = rec.get("payload") or {}
                        if (payload.get("action") == "candidate_accepted"
                                and payload.get("sharpe") is not None
                                and payload.get("strategy_type")):
                            candidates.append(payload)
                    except json.JSONDecodeError:
                        pass
            except OSError:
                pass

        if len(candidates) < 3:
            return

        top3 = sorted(candidates, key=lambda c: float(c.get("sharpe") or 0), reverse=True)[:3]
        counts: dict[str, int] = {}
        for candidate in candidates:
            st = str(candidate.get("strategy_type") or "unknown")
            counts[st] = counts.get(st, 0) + 1
        types = {c.get("strategy_type") for c in top3}

        if len(types) == 1:
            sole_type = next(iter(types))
            self._log.warning(
                "research_diversity_low | layer=research diversiteit=laag type=%s top3_sharpes=%s counts=%s",
                sole_type,
                [round(float(c.get("sharpe") or 0), 3) for c in top3],
                counts,
            )
        else:
            self._log.info(
                "research_diversity | layer=research diversiteit=ok top3_types=%s counts=%s",
                sorted(str(t) for t in types),
                counts,
            )

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
# Keyword → BacktestConfig vertaling
# ---------------------------------------------------------------------------

def _disabled_strategy_type(strategy_type: str | None) -> str | None:
    normalized = str(strategy_type or "unknown").strip().lower()
    return normalized if normalized in DISABLED_STRATEGY_TYPES else None


def _strategy_type_from_signal(
    signal_type: str,
    keywords: list[str],
    tp_pct: float | None = None,
) -> str:
    """
    Leid strategy_type af uit signal_type, entry_keywords en optionele tp_pct.

    Directe ResearchAnt signalen (sma_crossover, rsi_*, bb_*) worden exact
    gematcht op signal_type. Voor ingested candidates wordt keyword-analyse
    gebruikt; combinaties van twee of meer typen → "hybrid".

    Claude Ant varianten (keywords bevatten 'claude' of 'improved') worden
    geclassificeerd op basis van tp_pct:
      - tp_pct > 0.07 → momentum
      - tp_pct <= 0.04 → mean_reversion
      - anders         → hybrid
    """
    st  = (signal_type or "").lower()
    kw  = {k.lower() for k in (keywords or [])}
    kws = " ".join(sorted(kw))  # voor multi-word substring check

    # --- Commodity watchtower signalen ---
    if "watchtower" in st:
        return "watchtower_signal"

    # --- Claude Ant varianten: expliciete tp_pct classificatie ---
    if "claude" in kw or "improved" in kw:
        if tp_pct is not None:
            if tp_pct > 0.07:
                return "momentum"
            if tp_pct <= 0.04:
                return "mean_reversion"
        return "hybrid"

    # --- Directe signalen van ResearchAnt ---
    if "sma_crossover" in st or ("sma" in st and "cross" in st):
        return "sma_crossover"
    if "momentum_breakout" in st:
        return "momentum"
    if "mean_reversion_oversold" in st:
        return "mean_reversion"
    if "rsi" in st and "ingested" not in st:
        return "rsi_based"
    if "bb_upper" in st or "bb_lower" in st or ("bollinger" in st and "ingested" not in st):
        return "bollinger_bands"

    # --- Keyword-gebaseerde classificatie (ingested candidates + fallback) ---
    has_sma      = bool(kw & {"sma", "crossover", "ema", "moving average"})
    has_rsi      = "rsi" in kw
    has_bollinger = "bollinger" in kw
    has_macd     = "macd" in kw
    has_momentum = "momentum" in kw
    has_breakout = "breakout" in kw
    has_mean_rev = "mean reversion" in kws or "mean_reversion" in kw

    active_types = sum([
        has_sma, has_rsi, has_bollinger, has_macd,
        has_momentum, has_breakout, has_mean_rev,
    ])

    if active_types >= 2:
        return "hybrid"
    if has_macd:
        return "macd_based"
    if has_momentum:
        return "momentum"
    if has_breakout:
        return "breakout"
    if has_sma:
        return "sma_crossover"
    if has_rsi:
        return "rsi_based"
    if has_bollinger:
        return "bollinger_bands"
    if has_mean_rev:
        return "mean_reversion"
    return "unknown"


def _grade_from_sharpe(sharpe: float | None) -> str:
    """A = sharpe > 0.5 · B = 0.3–0.5 · C = < 0.3"""
    if sharpe is None:
        return "C"
    if sharpe > 0.5:
        return "A"
    if sharpe >= 0.3:
        return "B"
    return "C"


def _strategy_type_from_signal_type(signal_type: str) -> str | None:
    """
    Leid strategy_type af uit signal_type voor backtester entry-logica.

    Directe ResearchAnt signalen worden exact gematcht.
    Ingested/onbekende signalen geven None terug (= elke bar).
    """
    st = signal_type.lower()
    if st.startswith("sma") or "crossover" in st:
        return "sma_crossover"
    if st.startswith("rsi"):
        return "rsi_based"
    if st.startswith("bb") or "bollinger" in st:
        return "bollinger_bands"
    if "momentum" in st:
        return "momentum"
    if "mean_rev" in st or "reversion" in st:
        return "mean_reversion"
    if "watchtower" in st:
        return "watchtower_signal"
    if "momentum_breakout" in st:
        return "momentum"
    if "mean_reversion_oversold" in st:
        return "mean_reversion"
    return None   # ingested of onbekend → elke bar (veilig fallback)


def _config_from_keywords(keywords: list[str], direction: str) -> BacktestConfig:
    """
    Vertaal entry_keywords naar een BacktestConfig inclusief strategy_type.

    Doorloopt _KEYWORD_PROFILES in prioriteitsvolgorde (eerst gedefinieerde
    trefwoord wint). Geeft standaard _TP_PCT/_SL_PCT/_MAX_BARS_HELD terug
    als geen enkel keyword matcht.
    """
    kw_lower = {k.lower() for k in keywords}
    st = _strategy_type_from_signal(
        signal_type="",
        keywords=list(kw_lower),
        tp_pct=None,
    )
    strategy_type = st if st not in ("unknown", None) else None
    for keyword, (tp, sl, bars) in _KEYWORD_PROFILES.items():
        if keyword in kw_lower:
            return BacktestConfig(
                direction=direction,
                take_profit_pct=tp,
                stop_loss_pct=sl,
                max_bars_held=bars,
                strategy_type=strategy_type,
            )
    return BacktestConfig(
        direction=direction,
        take_profit_pct=_TP_PCT,
        stop_loss_pct=_SL_PCT,
        max_bars_held=_MAX_BARS_HELD,
        strategy_type=strategy_type,
    )


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
