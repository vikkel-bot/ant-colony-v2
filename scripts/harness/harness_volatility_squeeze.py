"""
scripts/harness/harness_volatility_squeeze.py

Standalone backtest harness — Bollinger Bands Squeeze.

Strategie:
  - Squeeze als Bollinger Bands binnen Keltner Channel liggen.
  - LONG als squeeze eindigt en close > EMA20.
  - SHORT als squeeze eindigt en close < EMA20.
  - Exit: SL 3%, TP 9%, of max 48 bars.

Gebruik:
  python scripts/harness/harness_volatility_squeeze.py

Vereisten:
  - python-bitvavo-api geïnstalleerd (PC2)
  - BITVAVO_API_KEY / BITVAVO_API_SECRET (optioneel, candles zijn publiek)
  - ANT_LOGS env var (standaard: C:\\Trading\\ANT_LOGS)
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ant_colony.biome.crypto_adapter import CryptoAdapter

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

SYMBOLS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"]
TIMEFRAME = "1h"
LOOKBACK_DAYS = 90
BB_WINDOW = 20
BB_STD_MULT = 2.0
EMA_WINDOW = 20
ATR_WINDOW = 14
KC_ATR_MULT = 1.5
SL_PCT = 0.03
TP_PCT = 0.09
MAX_BARS = 48
BARS_PER_YEAR = 365 * 24

DEFAULT_LOGS_ROOT = Path(os.environ.get("ANT_LOGS", r"C:\Trading\ANT_LOGS"))


# ---------------------------------------------------------------------------
# Data ophalen
# ---------------------------------------------------------------------------

def _fetch_candles(adapter: CryptoAdapter, symbol: str) -> list:
    """
    Haal ~90 dagen uurlijkse candles op voor symbol.

    Bitvavo accepteert maximaal 1440 candles per request; daarom halen we
    desnoods een tweede batch op vóór de oudste candle uit batch 1.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    batch1 = adapter.get_candles(symbol, TIMEFRAME, limit=1440)
    if not batch1:
        return []

    if batch1[0].timestamp <= cutoff:
        return [c for c in batch1 if c.timestamp >= cutoff]

    oldest_ms = int(batch1[0].timestamp.timestamp() * 1000)
    batch2 = adapter.get_candles(symbol, TIMEFRAME, limit=1440, end_ms=oldest_ms - 1)

    seen: set = set()
    merged = []
    for candle in sorted(batch1 + batch2, key=lambda c: c.timestamp):
        if candle.timestamp not in seen:
            seen.add(candle.timestamp)
            merged.append(candle)

    return [c for c in merged if c.timestamp >= cutoff]


# ---------------------------------------------------------------------------
# Indicatoren
# ---------------------------------------------------------------------------

def _ema(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1.0)
    result[period - 1] = float(np.mean(values[:period]))
    for idx in range(period, len(values)):
        result[idx] = (values[idx] * alpha) + (result[idx - 1] * (1.0 - alpha))
    return result


def _rolling_mean(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for idx in range(period - 1, len(values)):
        result[idx] = float(np.mean(values[idx - period + 1:idx + 1]))
    return result


def _rolling_std(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for idx in range(period - 1, len(values)):
        result[idx] = float(np.std(values[idx - period + 1:idx + 1]))
    return result


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(closes), np.nan)
    if len(closes) < 2:
        return result
    true_ranges = np.full(len(closes), np.nan)
    for idx in range(1, len(closes)):
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
    for idx in range(period, len(closes)):
        window = true_ranges[idx - period + 1:idx + 1]
        if not np.isnan(window).any():
            result[idx] = float(np.mean(window))
    return result


def _compute_squeeze_state(candles: list) -> dict[str, np.ndarray]:
    closes = np.array([c.close for c in candles], dtype=float)
    highs = np.array([c.high for c in candles], dtype=float)
    lows = np.array([c.low for c in candles], dtype=float)

    sma20 = _rolling_mean(closes, BB_WINDOW)
    std20 = _rolling_std(closes, BB_WINDOW)
    ema20 = _ema(closes, EMA_WINDOW)
    atr14 = _atr(highs, lows, closes, ATR_WINDOW)

    bb_upper = sma20 + (BB_STD_MULT * std20)
    bb_lower = sma20 - (BB_STD_MULT * std20)
    kc_upper = ema20 + (KC_ATR_MULT * atr14)
    kc_lower = ema20 - (KC_ATR_MULT * atr14)

    squeeze_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    valid = ~(
        np.isnan(bb_upper)
        | np.isnan(bb_lower)
        | np.isnan(kc_upper)
        | np.isnan(kc_lower)
        | np.isnan(ema20)
    )
    squeeze_on = squeeze_on & valid
    release = np.zeros(len(closes), dtype=bool)
    release[1:] = squeeze_on[:-1] & (~squeeze_on[1:]) & valid[1:]

    return {
        "close": closes,
        "ema20": ema20,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "kc_upper": kc_upper,
        "kc_lower": kc_lower,
        "squeeze_on": squeeze_on,
        "release": release,
    }


# ---------------------------------------------------------------------------
# Backtest simulatie
# ---------------------------------------------------------------------------

def _backtest_symbol(candles: list) -> list[dict]:
    """
    Simuleer volatility-squeeze trades.

    Geen overlappende posities per symbool.
    """
    n = len(candles)
    if n < max(BB_WINDOW, EMA_WINDOW, ATR_WINDOW) + 2:
        return []

    state = _compute_squeeze_state(candles)
    closes = state["close"]
    ema20 = state["ema20"]
    release = state["release"]

    trades: list[dict] = []
    i = max(BB_WINDOW, EMA_WINDOW, ATR_WINDOW)

    while i < n:
        if not release[i] or np.isnan(ema20[i]):
            i += 1
            continue

        if closes[i] > ema20[i]:
            direction = "long"
        elif closes[i] < ema20[i]:
            direction = "short"
        else:
            i += 1
            continue

        entry_idx = i
        entry_price = closes[entry_idx]
        exit_idx = min(entry_idx + MAX_BARS, n - 1)
        exit_reason = "max_bars"

        for j in range(entry_idx + 1, min(entry_idx + MAX_BARS + 1, n)):
            close = closes[j]
            if direction == "long":
                pct = (close - entry_price) / entry_price
            else:
                pct = (entry_price - close) / entry_price

            if pct <= -SL_PCT:
                exit_idx = j
                exit_reason = "sl"
                break
            if pct >= TP_PCT:
                exit_idx = j
                exit_reason = "tp"
                break

        exit_price = closes[exit_idx]
        if direction == "long":
            ret_pct = (exit_price - entry_price) / entry_price
        else:
            ret_pct = (entry_price - exit_price) / entry_price

        trades.append({
            "direction": direction,
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "return_pct": float(ret_pct),
            "bars_held": exit_idx - entry_idx,
            "exit_reason": exit_reason,
        })
        i = exit_idx + 1

    return trades


# ---------------------------------------------------------------------------
# Metrieken
# ---------------------------------------------------------------------------

def _compute_metrics(trades: list[dict]) -> dict:
    """
    Bereken win rate, annualized Sharpe en max drawdown.
    """
    if not trades:
        return {"n": 0, "win_rate": 0.0, "sharpe": 0.0, "max_dd_pct": 0.0}

    returns = np.array([t["return_pct"] for t in trades], dtype=float)
    n = len(returns)
    wins = int(np.sum(returns > 0))
    win_rate = wins / n * 100.0

    std_ret = float(np.std(returns))
    if n >= 2 and std_ret > 0.0:
        avg_bars = float(np.mean([t["bars_held"] for t in trades]))
        trades_per_year = BARS_PER_YEAR / max(avg_bars, 1.0)
        sharpe = float((np.mean(returns) / std_ret) * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    equity = np.concatenate([[0.0], np.cumsum(returns)])
    peak = np.maximum.accumulate(equity)
    max_dd_pct = float(abs(np.min(equity - peak)) * 100.0)

    return {"n": n, "win_rate": win_rate, "sharpe": sharpe, "max_dd_pct": max_dd_pct}


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def build_report(results: list[tuple[str, list[dict], dict]]) -> str:
    lines = ["=== VOLATILITY SQUEEZE HARNESS ==="]

    all_trades: list[dict] = []
    for symbol, trades, metrics in results:
        lines.append(
            f"Symbool: {symbol} | "
            f"Trades: {metrics['n']} | "
            f"Win rate: {metrics['win_rate']:.1f}% | "
            f"Sharpe: {metrics['sharpe']:.2f} | "
            f"Max DD: {metrics['max_dd_pct']:.1f}%"
        )
        all_trades.extend(trades)

    total = _compute_metrics(all_trades)
    verdict = (
        "GO"
        if total["sharpe"] > 1.0 and total["max_dd_pct"] < 10.0 and total["win_rate"] > 55.0
        else "NO-GO"
    )

    lines.append("")
    lines.append(
        f"TOTAAL: trades={total['n']} "
        f"winrate={total['win_rate']:.1f}% "
        f"sharpe={total['sharpe']:.2f} "
        f"max_dd={total['max_dd_pct']:.1f}%"
    )
    lines.append("Criteria: Sharpe > 1.0 EN max DD < 10% EN winrate > 55%")
    lines.append(f"Verdict: {verdict}")
    return "\n".join(lines)


def write_report(report: str, logs_root: Path = DEFAULT_LOGS_ROOT) -> Path:
    out_dir = logs_root / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"volatility_squeeze_{stamp}.txt"
    out_path.write_text(report + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("BITVAVO_API_KEY", "")
    api_secret = os.environ.get("BITVAVO_API_SECRET", "")
    adapter = CryptoAdapter(api_key=api_key, api_secret=api_secret, paper_only=True)

    results: list[tuple[str, list[dict], dict]] = []
    for symbol in SYMBOLS:
        print(f"Ophalen: {symbol} ...", flush=True)
        candles = _fetch_candles(adapter, symbol)
        if not candles:
            print(f"  WAARSCHUWING: geen data voor {symbol}, overgeslagen.")
            continue
        print(f"  {len(candles)} candles ({LOOKBACK_DAYS}d lookback)")
        trades = _backtest_symbol(candles)
        metrics = _compute_metrics(trades)
        results.append((symbol, trades, metrics))

    if not results:
        print("FOUT: geen data beschikbaar. Controleer Bitvavo connectie.")
        return 1

    report = build_report(results)
    out_path = write_report(report)
    print()
    print(report)
    print(f"\nRapport opgeslagen: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
