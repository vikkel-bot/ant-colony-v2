"""
ant_colony/ants/equities/sector_scout_ant.py

SectorScoutAnt — rankt de 11 SPDR sector ETFs op 3-maands relatief momentum
en emitteert OpportunitySignals voor de top-3 sectoren.

Strategie: Sector Rotatie (Setup 1 uit CLAUDE.md)
  - Monitor 11 SPDR sector ETFs
  - Relatief momentum = (huidige koers / koers 63 handelsdagen geleden) − 1
  - Top 3 sectoren → LONG OpportunitySignal naar ANT_LOGS/scouts/{ant_id}.jsonl
  - PaperAnt leest deze signalen automatisch op (bestaand patroon)

Deduplicatie: één signal_id per (symbool, dag) zodat PaperAnt elk signaal
slechts eenmaal verwerkt, ook bij meerdere ticks per dag.

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten — skip symbool, log, doorgaan (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
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
    "XLC":  "Communication Services",
}

_MOMENTUM_PERIOD = "3mo"   # yfinance period voor 3-maands data
_MIN_BARS        = 2       # minimum candles voor betrouwbare return
_TOP_N           = 3       # top 3 sectoren krijgen een LONG-signaal
_CONFIDENCE_TOP  = 0.90    # confidence voor de sterkste sector
_CONFIDENCE_BASE = 0.75    # confidence voor de zwakste top-3 sector
_MAX_CANDLE_WORKERS = 5


class SectorScoutAnt:
    """
    Rankt SPDR sector ETFs op 3-maands return en emitteert LONG-signalen
    voor de top-3 sectoren naar ANT_LOGS/scouts/{ant_id}.jsonl.

    De missie-symbolen worden genegeerd: de ant monitort altijd de volledige
    lijst van 11 SPDR ETFs. De biome uit de missie bepaalt welke adapter
    uit het BiomeRegistry gebruikt wordt (typisch "equities").

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    # Sector-ranking verandert niet elke seconde; 5 minuten is voldoende.
    _TICK_INTERVAL: int = 300
    # Log op INFO bij >5% momentum-verschil in rank-1 ten opzichte van vorige log.
    _MOMENTUM_LOG_THRESHOLD: float = 0.05

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
        self._emitted_signal_ids: set[str] = set()

        self._last_tick_at:       float            = 0.0
        self._last_top3:          list[str] | None = None
        self._last_top1_momentum: float | None     = None

        self._log = logging.getLogger(f"ant.sector_scout.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "SectorScoutAnt gestart | mission=%s ttl=%ds",
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
            self._log.info("SectorScoutAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("SectorScoutAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Bereken momentum voor alle sector-ETFs, rank, en emiteer signalen.

        Retourneert de volledige ranking als lijst van dicts (ook nuttig voor tests).
        """
        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        if adapter is None or not adapter.is_available():
            self._log.warning("Geen equities adapter beschikbaar")
            self._last_action = "tick:no_adapter"
            return []

        get_candles_fn = getattr(adapter, "get_candles", None)
        if get_candles_fn is None:
            self._log.warning("Adapter heeft geen get_candles() — SectorScoutAnt vereist YahooFinanceAdapter")
            self._last_action = "tick:no_get_candles"
            return []

        returns = self._fetch_3mo_returns_parallel(get_candles_fn)

        if not returns:
            self._log.warning("Geen ETF-data beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_data"
            return []

        ranking = sorted(returns.items(), key=lambda x: x[1], reverse=True)
        signals = self._build_signals(ranking)
        self._emit_top_signals(ranking, adapter)
        self._last_action = f"tick:top={ranking[0][0] if ranking else 'none'}"

        current_top3 = [s for s, _ in ranking[:_TOP_N]]
        top1_momentum = ranking[0][1] if ranking else 0.0

        top3_changed = current_top3 != self._last_top3
        momentum_moved = (
            self._last_top1_momentum is None
            or abs(top1_momentum - self._last_top1_momentum) >= self._MOMENTUM_LOG_THRESHOLD
        )
        if top3_changed or momentum_moved:
            self._log.info(
                "Sector ranking | top3=%s  rank1_momentum=%.2f%%",
                current_top3, top1_momentum * 100,
            )
            self._last_top3          = current_top3
            self._last_top1_momentum = top1_momentum
        else:
            self._log.debug(
                "Sector ranking (onveranderd) | top3=%s  rank1_momentum=%.2f%%",
                current_top3, top1_momentum * 100,
            )
        return signals

    def _get_3mo_return(self, symbol: str, get_candles_fn) -> float | None:
        """Bereken 3-maands prijsreturn. Retourneert None bij onvoldoende data."""
        try:
            candles = get_candles_fn(symbol, period=_MOMENTUM_PERIOD, interval="1d")
            return self._return_from_candles(candles)
        except Exception:
            self._log.exception("Fout bij berekenen return voor %s", symbol)
            return None

    def _fetch_3mo_returns_parallel(
        self,
        get_candles_fn,
        symbols: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, float]:
        """Fetch candles parallel en bereken 3-maands returns per symbool."""
        target_symbols = list(symbols or _SPDR_ETFS.keys())
        t0 = time.monotonic()
        returns: dict[str, float] = {}
        if not target_symbols:
            return returns

        workers = min(_MAX_CANDLE_WORKERS, len(target_symbols))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sector-candles") as executor:
            futures = {
                executor.submit(self._fetch_symbol_candles, symbol, get_candles_fn): symbol
                for symbol in target_symbols
            }
            for future in as_completed(futures):
                symbol, candles = future.result()
                ret = self._return_from_candles(candles)
                if ret is not None:
                    returns[symbol] = ret
                else:
                    self._log.warning("3-maands return niet beschikbaar voor %s — overgeslagen", symbol)

        elapsed = time.monotonic() - t0
        self._log.info("Candle fetch klaar | symbols=%d tijd=%.1fs", len(target_symbols), elapsed)
        return returns

    def _fetch_symbol_candles(self, symbol: str, get_candles_fn) -> tuple[str, list]:
        try:
            candles = get_candles_fn(symbol, period=_MOMENTUM_PERIOD, interval="1d")
            return symbol, list(candles or [])
        except Exception as exc:
            self._log.warning("Candle fetch mislukt voor %s: %s", symbol, exc)
            return symbol, []

    @staticmethod
    def _return_from_candles(candles: list) -> float | None:
        if len(candles) < _MIN_BARS:
            return None
        first_close = candles[0].close
        last_close = candles[-1].close
        if first_close <= 0:
            return None
        return (last_close - first_close) / first_close

    def _build_signals(self, ranking: list[tuple[str, float]]) -> list[dict]:
        """Bouw signaal-dicts op basis van de ranking (voor logging en tests)."""
        signals = []
        for i, (symbol, ret) in enumerate(ranking):
            direction = "LONG" if i < _TOP_N else "NEUTRAL"
            signals.append({
                "symbol":     symbol,
                "sector":     _SPDR_ETFS.get(symbol, ""),
                "return_3mo": round(ret, 6),
                "rank":       i + 1,
                "signal":     direction,
            })
        return signals

    def _emit_top_signals(
        self, ranking: list[tuple[str, float]], adapter
    ) -> None:
        """Emitteer OpportunitySignals voor de top-_TOP_N sectoren."""
        conf_step = (_CONFIDENCE_TOP - _CONFIDENCE_BASE) / max(_TOP_N - 1, 1)

        for rank, (symbol, momentum) in enumerate(ranking[:_TOP_N]):
            today     = date.today().isoformat()
            signal_id = f"sector-{symbol.lower()}-{today}"

            if signal_id in self._emitted_signal_ids:
                continue

            current_price = self._get_current_price(symbol, adapter)
            if current_price is None:
                continue

            confidence = round(_CONFIDENCE_TOP - rank * conf_step, 3)
            self._emitted_signal_ids.add(signal_id)

            payload = {
                "action":        "opportunity_detected",
                "signal_id":     signal_id,
                "symbol":        symbol,
                "current_price": current_price,
                "confidence":    confidence,
                "change_pct":    round(momentum, 6),
                "signal_type":   "sector_rotation",
                "biome":         self.mission.market_scope.biome,
                "sector_name":   _SPDR_ETFS.get(symbol, symbol),
                "momentum_rank": rank + 1,
            }
            self._write_signal(payload)
            self._log.info(
                "SECTOR SIGNAAL | %s (%s) rank=%d momentum=%.1f%% confidence=%.2f",
                symbol, _SPDR_ETFS.get(symbol, "?"),
                rank + 1, momentum * 100, confidence,
            )

    def _get_current_price(self, symbol: str, adapter) -> float | None:
        """Haal actuele prijs op via adapter. Retourneert None bij fout."""
        try:
            md = adapter.get_market_data(symbol, "1d")
            if md is None or not md.is_valid_price:
                return None
            return md.close
        except Exception:
            self._log.exception("Fout bij ophalen prijs voor %s", symbol)
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_signal(self, payload: dict) -> None:
        """Schrijf OpportunitySignal naar ANT_LOGS/scouts/{ant_id}.jsonl."""
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

        log_path = self.logs_root / "scouts" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon signaal niet naar disk schrijven")

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
