"""
Optional VectorBT validator for ResearchAnt candidates.

The public class name remains ``LeanValidator`` for compatibility with the
existing ResearchAnt pipeline. Internally this module no longer shells out to
QuantConnect Lean/Docker. VectorBT is a soft dependency: if it is unavailable,
the colony keeps running and records an ``unavailable`` validation result.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

LeanResult = dict[str, Any]


class LeanValidator:
    """Validate a StrategyCandidate dict with a local VectorBT backtest."""

    def __init__(
        self,
        logs_root: Path | str | None = None,
        *,
        timeout_seconds: int = 120,
        **_: Any,
    ) -> None:
        self.logs_root = Path(logs_root) if logs_root is not None else Path(
            os.getenv("ANT_LOGS", "logs")
        )
        self.timeout_seconds = timeout_seconds

    def validate(self, candidate: dict[str, Any]) -> LeanResult:
        """
        Validate a StrategyCandidate with VectorBT.

        The output schema intentionally keeps the historical ``lean_*`` keys so
        ResearchAnt and dashboards do not need to change.
        """
        candidate_id = str(candidate.get("candidate_id") or "unknown-candidate")
        try:
            vbt, reason = self._load_vectorbt()
            if vbt is None:
                result = self._base_result("unavailable", reason)
                self._write_result(candidate_id, result)
                return result

            data = self._fetch_ohlcv(candidate)
            if data is None or data.empty:
                result = self._base_result("failed", "Geen OHLCV-data beschikbaar")
                self._write_result(candidate_id, result)
                return result

            close = _series(data, "Close")
            if close.empty or close.dropna().shape[0] < 35:
                result = self._base_result("failed", "Te weinig candles voor VectorBT validatie")
                self._write_result(candidate_id, result)
                return result

            signals = self._build_signals(candidate, close)
            metrics = self._run_portfolio(vbt, candidate, close, signals)
            result = {
                **metrics,
                "lean_status": "passed",
                "lean_reason": "VectorBT backtest voltooid",
            }
            self._write_result(candidate_id, result)
            return result
        except Exception as exc:  # pragma: no cover - final safety net
            _log.exception("VectorBT validatie onverwacht mislukt voor %s", candidate_id)
            result = self._base_result("failed", f"VectorBT validator fout: {exc}")
            self._write_result(candidate_id, result)
            return result

    def _load_vectorbt(self) -> tuple[Any | None, str]:
        try:
            import vectorbt as vbt  # type: ignore
        except Exception as exc:
            return None, f"vectorbt niet beschikbaar: {exc}"
        return vbt, ""

    def _fetch_ohlcv(self, candidate: dict[str, Any]) -> pd.DataFrame | None:
        symbol = _candidate_symbol(candidate)
        biome = str(candidate.get("biome") or "").lower()
        if _looks_like_crypto(symbol, biome):
            ccxt_data = self._fetch_ccxt_ohlcv(symbol)
            if ccxt_data is not None and not ccxt_data.empty:
                return ccxt_data
        return self._fetch_yfinance_ohlcv(symbol)

    def _fetch_ccxt_ohlcv(self, symbol: str) -> pd.DataFrame | None:
        try:
            import ccxt  # type: ignore
        except Exception:
            return None
        try:
            exchange = ccxt.binance(
                {"enableRateLimit": True, "timeout": max(1, self.timeout_seconds) * 1000}
            )
            market = _ccxt_market(symbol)
            since = int((datetime.now(timezone.utc).timestamp() - 365 * 24 * 3600) * 1000)
            rows = exchange.fetch_ohlcv(market, timeframe="1d", since=since, limit=1000)
        except Exception as exc:
            _log.warning("ccxt OHLCV ophalen mislukt voor %s: %s", symbol, exc)
            return None
        if not rows:
            return None
        frame = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
        frame["Date"] = pd.to_datetime(frame["Date"], unit="ms", utc=True)
        frame = frame.set_index("Date")
        return frame

    def _fetch_yfinance_ohlcv(self, symbol: str) -> pd.DataFrame | None:
        try:
            import yfinance as yf  # type: ignore
        except Exception as exc:
            _log.warning("yfinance niet beschikbaar voor VectorBT validatie: %s", exc)
            return None
        ticker = _yfinance_symbol(symbol)
        try:
            data = yf.download(
                ticker,
                period="365d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
                timeout=max(1, self.timeout_seconds),
            )
        except Exception as exc:
            _log.warning("yfinance OHLCV ophalen mislukt voor %s: %s", ticker, exc)
            return None
        if data is None or data.empty:
            return None
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        return data.rename(columns={str(c): str(c).title() for c in data.columns})

    def _build_signals(self, candidate: dict[str, Any], close: pd.Series) -> dict[str, pd.Series | None]:
        strategy_type = str(candidate.get("strategy_type") or "").lower()
        direction = _candidate_direction(candidate)
        if strategy_type in {"rsi_based", "rsi", "rsi_overbought", "rsi_oversold"}:
            return self._rsi_signals(candidate, close, direction)
        if strategy_type in {"bollinger_bands", "bb_lower_approach", "bb_upper_touch"}:
            return self._bollinger_signals(candidate, close, direction)
        return self._sma_signals(candidate, close, direction)

    def _sma_signals(
        self,
        candidate: dict[str, Any],
        close: pd.Series,
        direction: str,
    ) -> dict[str, pd.Series | None]:
        short_period = int(_candidate_param(candidate, ("short_period", "short_sma", "fast_period"), 10))
        long_period = int(_candidate_param(candidate, ("long_period", "long_sma", "slow_period"), 30))
        fast = close.rolling(short_period).mean()
        slow = close.rolling(long_period).mean()
        above = (fast > slow).fillna(False)
        below = (fast < slow).fillna(False)
        if direction == "short":
            return {
                "entries": None,
                "exits": None,
                "short_entries": (below & ~below.shift(1, fill_value=False)).fillna(False),
                "short_exits": (above & ~above.shift(1, fill_value=False)).fillna(False),
            }
        return {
            "entries": (above & ~above.shift(1, fill_value=False)).fillna(False),
            "exits": (below & ~below.shift(1, fill_value=False)).fillna(False),
            "short_entries": None,
            "short_exits": None,
        }

    def _rsi_signals(
        self,
        candidate: dict[str, Any],
        close: pd.Series,
        direction: str,
    ) -> dict[str, pd.Series | None]:
        window = int(_candidate_param(candidate, ("rsi_period", "window"), 14))
        oversold = float(_candidate_param(candidate, ("rsi_oversold", "oversold"), 35))
        overbought = float(_candidate_param(candidate, ("rsi_overbought", "overbought"), 65))
        rsi = _rsi(close, window)
        if direction == "short":
            overbought_now = (rsi > overbought).fillna(False)
            below_mid = (rsi < 50).fillna(False)
            short_entries = overbought_now & ~overbought_now.shift(1, fill_value=False)
            short_exits = below_mid & ~below_mid.shift(1, fill_value=False)
            return {"entries": None, "exits": None, "short_entries": short_entries, "short_exits": short_exits}
        oversold_now = (rsi < oversold).fillna(False)
        above_mid = (rsi > 50).fillna(False)
        entries = oversold_now & ~oversold_now.shift(1, fill_value=False)
        exits = above_mid & ~above_mid.shift(1, fill_value=False)
        return {"entries": entries, "exits": exits, "short_entries": None, "short_exits": None}

    def _bollinger_signals(
        self,
        candidate: dict[str, Any],
        close: pd.Series,
        direction: str,
    ) -> dict[str, pd.Series | None]:
        window = int(_candidate_param(candidate, ("bb_period", "rolling_window", "window"), 20))
        std_mult = float(_candidate_param(candidate, ("bb_std", "std_mult"), 2.0))
        mid = close.rolling(window).mean()
        std = close.rolling(window).std()
        upper = mid + std_mult * std
        lower = mid - std_mult * std
        if direction == "short":
            return {
                "entries": None,
                "exits": None,
                "short_entries": (close >= upper).fillna(False),
                "short_exits": (close <= mid).fillna(False),
            }
        return {
            "entries": (close <= lower).fillna(False),
            "exits": (close >= mid).fillna(False),
            "short_entries": None,
            "short_exits": None,
        }

    def _run_portfolio(
        self,
        vbt: Any,
        candidate: dict[str, Any],
        close: pd.Series,
        signals: dict[str, pd.Series | None],
    ) -> LeanResult:
        pf = vbt.Portfolio.from_signals(
            close,
            entries=signals.get("entries"),
            exits=signals.get("exits"),
            short_entries=signals.get("short_entries"),
            short_exits=signals.get("short_exits"),
            init_cash=float(_candidate_param(candidate, ("cash", "starting_cash"), 10000.0)),
            fees=float(_candidate_param(candidate, ("fee_pct", "fees"), 0.001)),
            slippage=float(_candidate_param(candidate, ("slippage_pct", "slippage"), 0.001)),
            freq="1D",
        )
        return self._metrics_from_portfolio(pf)

    def _metrics_from_portfolio(self, pf: Any) -> LeanResult:
        sharpe = _call_metric(pf, "sharpe_ratio")
        max_dd = _call_metric(pf, "max_drawdown")
        trades_obj = getattr(pf, "trades", None)
        trades = _call_metric(trades_obj, "count")
        win_rate = _call_metric(trades_obj, "win_rate")
        if win_rate is not None and win_rate > 1:
            win_rate = win_rate / 100.0
        return {
            "lean_sharpe": _clean_float(sharpe),
            "lean_max_drawdown": abs(_clean_float(max_dd) or 0.0),
            "lean_win_rate": _clean_float(win_rate),
            "lean_trades": int(trades or 0),
        }

    @staticmethod
    def _base_result(status: str, reason: str) -> LeanResult:
        return {
            "lean_sharpe": None,
            "lean_max_drawdown": None,
            "lean_win_rate": None,
            "lean_trades": None,
            "lean_status": status,
            "lean_reason": reason,
        }

    def _write_result(self, candidate_id: str, result: LeanResult) -> None:
        path = self.logs_root / "lean" / f"{_safe_id(candidate_id)}.json"
        payload = {
            "candidate_id": candidate_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **result,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except OSError:
            _log.exception("Kon VectorBT resultaat niet schrijven: %s", path)


def _candidate_symbol(candidate: dict[str, Any]) -> str:
    market_scope = candidate.get("market_scope") or {}
    params = candidate.get("parameters") or {}
    return str(
        market_scope.get("symbol")
        or candidate.get("symbol")
        or candidate.get("asset")
        or params.get("symbol")
        or "BTC-EUR"
    ).upper()


def _candidate_direction(candidate: dict[str, Any]) -> str:
    entry = candidate.get("entry_conditions") or {}
    direction = str(candidate.get("direction") or entry.get("direction") or "").lower()
    strategy_type = str(candidate.get("strategy_type") or "").lower()
    if direction in {"long", "short"}:
        return direction
    if "short" in strategy_type or "overbought" in strategy_type or "upper" in strategy_type:
        return "short"
    return "long"


def _candidate_param(candidate: dict[str, Any], keys: tuple[str, ...], default: Any) -> Any:
    sources = [
        candidate.get("parameters") or {},
        candidate.get("strategy_parameters") or {},
        candidate.get("entry_conditions") or {},
        candidate.get("exit_conditions") or {},
        candidate,
    ]
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return default


def _looks_like_crypto(symbol: str, biome: str) -> bool:
    return biome == "crypto" or "-" in symbol or symbol.endswith(("USDT", "USD", "EUR"))


def _ccxt_market(symbol: str) -> str:
    base = symbol.upper().split("-")[0].replace("USDT", "").replace("USD", "")
    return f"{base}/USDT"


def _yfinance_symbol(symbol: str) -> str:
    upper = symbol.upper().strip()
    crypto_map = {
        "BTC-EUR": "BTC-USD",
        "ETH-EUR": "ETH-USD",
        "SOL-EUR": "SOL-USD",
        "XRP-EUR": "XRP-USD",
        "ADA-EUR": "ADA-USD",
        "LTC-EUR": "LTC-USD",
        "LINK-EUR": "LINK-USD",
        "DOT-EUR": "DOT-USD",
        "BTCUSDT": "BTC-USD",
        "ETHUSDT": "ETH-USD",
    }
    return crypto_map.get(upper, upper)


def _series(data: pd.DataFrame, column: str) -> pd.Series:
    if column in data.columns:
        return data[column].dropna().astype(float)
    lower_lookup = {str(c).lower(): c for c in data.columns}
    found = lower_lookup.get(column.lower())
    if found is None:
        return pd.Series(dtype=float)
    return data[found].dropna().astype(float)


def _rsi(close: pd.Series, window: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def _call_metric(obj: Any, name: str) -> float | int | None:
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _clean_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:120] or "candidate"
