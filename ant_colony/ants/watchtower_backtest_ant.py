"""
ant_colony/ants/watchtower_backtest_ant.py

WatchtowerBacktestAnt — replays exported Watchtower signals inside Colony.

This ant does not call Watchtower live endpoints. It reads
ANT_LOGS/watchtower/signals.jsonl and historical candles from the configured
biome adapters, then writes a backtest report to ANT_LOGS/backtests/.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ant_colony.lab.backtester import OHLCVBar
from ant_colony.lab.watchtower_backtester import (
    WatchtowerBacktestAssumptions,
    WatchtowerSignalBacktester,
    load_watchtower_signals,
)
from ant_colony.schemas.mission import Mission


class WatchtowerBacktestAnt:
    """
    One-shot Watchtower signal replay ant.

    Args:
        ant_id: Unique ant identifier.
        mission: Mission with market_scope symbols to include. Empty/["GLOBAL"]
                 means use all symbols present in Watchtower logs.
        scheduler: Optional scheduler reference, currently only retained for
                   interface consistency.
        logs_root: ANT_LOGS root.
        biome_registry: Registry containing adapters with get_candles().
        assumptions: Realistic replay assumptions.
        timeframe: Candle timeframe passed to adapters.
        candle_limit: Maximum candles requested from adapters that support it.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: Any,
        logs_root: Path | None,
        biome_registry: Any,
        assumptions: WatchtowerBacktestAssumptions | None = None,
        timeframe: str = "1h",
        candle_limit: int = 1000,
        signal_source: str = "live",
        min_market_quality: str | None = None,
        include_neutral: bool = False,
    ) -> None:
        self.ant_id = ant_id
        self.mission = mission
        self.scheduler = scheduler
        self.logs_root = Path(logs_root) if logs_root is not None else None
        self.biome_registry = biome_registry
        self.assumptions = assumptions or WatchtowerBacktestAssumptions()
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        self.signal_source = signal_source
        self.min_market_quality = min_market_quality
        self.include_neutral = include_neutral
        self._backtester = WatchtowerSignalBacktester()
        self._log = logging.getLogger(f"ant.watchtower_backtest.{ant_id[:8]}")
        self.last_report_path: Path | None = None

    def run_once(self) -> dict[str, Any]:
        """
        Run one replay pass and write the report.

        Returns:
            Report dict. Empty report with zero trades if logs_root is None or
            no signals/candles are available.
        """
        if self.logs_root is None:
            return self._empty_report("logs_root_none")

        signals = load_watchtower_signals(
            self.logs_root,
            include_neutral_as_long=self.include_neutral,
        )
        signals = self._filter_market_quality(signals)
        allowed = self._allowed_symbols()
        if allowed is not None:
            signals = [s for s in signals if s.symbol in allowed]

        bars_by_symbol = self._load_bars_by_symbol(sorted({s.symbol for s in signals}))
        report = self._backtester.run(signals, bars_by_symbol, self.assumptions)
        payload = report.to_dict()
        payload["signal_source"] = self.signal_source
        payload["min_market_quality"] = self.min_market_quality
        payload["include_neutral"] = self.include_neutral
        report_path = self._write_report(payload)
        if report_path is not None:
            payload["report_path"] = str(report_path)
        return payload

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _allowed_symbols(self) -> set[str] | None:
        symbols = list(getattr(self.mission.market_scope, "symbols", []) or [])
        if not symbols or symbols == ["GLOBAL"]:
            return None
        return set(symbols)

    def _load_bars_by_symbol(self, symbols: list[str]) -> dict[str, list[OHLCVBar]]:
        result: dict[str, list[OHLCVBar]] = {}
        for symbol in symbols:
            bars = self._load_symbol_bars(symbol)
            if bars:
                result[symbol] = bars
        return result

    def _filter_market_quality(self, signals):
        if self.min_market_quality != "historical":
            return signals
        return [
            signal
            for signal in signals
            if str(signal.raw.get("seed_market_quality") or "").lower() == "historical"
        ]

    def _load_symbol_bars(self, symbol: str) -> list[OHLCVBar]:
        if symbol.upper() == "ETH-BTC":
            return self._load_ratio_bars("ETH-EUR", "BTC-EUR")

        adapter = self._adapter_for_symbol(symbol)
        if adapter is None or not hasattr(adapter, "get_candles"):
            return []
        candles = self._get_candles(adapter, symbol)
        bars = [self._to_bar(c) for c in candles]
        return [b for b in bars if b is not None]

    def _load_ratio_bars(self, base_symbol: str, quote_symbol: str) -> list[OHLCVBar]:
        base_bars = self._load_symbol_bars(base_symbol)
        quote_bars = self._load_symbol_bars(quote_symbol)
        base_by_ts = {bar.timestamp: bar for bar in base_bars}
        quote_by_ts = {bar.timestamp: bar for bar in quote_bars}

        ratio_bars: list[OHLCVBar] = []
        for ts in sorted(base_by_ts.keys() & quote_by_ts.keys()):
            base = base_by_ts[ts]
            quote = quote_by_ts[ts]
            if min(quote.open, quote.high, quote.low, quote.close) <= 0:
                continue
            ratio_bars.append(
                OHLCVBar(
                    timestamp=ts,
                    open=base.open / quote.open,
                    high=base.high / quote.low,
                    low=base.low / quote.high,
                    close=base.close / quote.close,
                    volume=base.volume,
                )
            )
        return ratio_bars

    def _adapter_for_symbol(self, symbol: str):
        biome = "crypto" if "-" in symbol else "equities"
        try:
            return self.biome_registry.get(biome)
        except Exception:
            return None

    def _get_candles(self, adapter, symbol: str):
        try:
            # BitvavoAdapter signature: get_candles(symbol, timeframe, limit)
            if "-" in symbol:
                return adapter.get_candles(symbol, self.timeframe, self.candle_limit)
            # Yahoo/IBKR style signature: get_candles(symbol, period, interval)
            period = "1y" if self.timeframe in ("1d", "1wk", "1mo") else "3mo"
            return adapter.get_candles(symbol, period=period, interval=self.timeframe)
        except TypeError:
            try:
                return adapter.get_candles(symbol, self.timeframe)
            except Exception:
                self._log.exception("get_candles failed for %s", symbol)
                return []
        except Exception:
            self._log.exception("get_candles failed for %s", symbol)
            return []

    @staticmethod
    def _to_bar(candle: Any) -> OHLCVBar | None:
        try:
            return OHLCVBar(
                timestamp=candle.timestamp,
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=float(getattr(candle, "volume", 0.0) or 0.0),
            )
        except Exception:
            return None

    def _write_report(self, payload: dict[str, Any]) -> Path | None:
        if self.logs_root is None:
            return None
        out_dir = self.logs_root / "backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        out_path = out_dir / f"watchtower_backtest_{ts}.json"
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.last_report_path = out_path
        return out_path

    def _empty_report(self, reason: str) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "reason": reason,
            "total_signals": 0,
            "total_trades": 0,
            "skipped_signals": 0,
            "trades": [],
        }
