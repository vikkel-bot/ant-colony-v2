"""
scripts/harness/harness_mean_reversion.py

Standalone backtest harness — Mean Reversion (Z-score strategie).

Strategie:
  - Z-score van close over 20-bar rolling window
  - LONG als Z < -1.5, SHORT als Z > +1.5
  - Entry: volgende bar open
  - Exit: Z keert terug naar 0, SL 3%, TP 6%, of max 48 bars

Gebruik:
  python scripts/harness/harness_mean_reversion.py

Vereisten:
  - python-bitvavo-api geïnstalleerd (PC2)
  - BITVAVO_API_KEY / BITVAVO_API_SECRET (optioneel, candles zijn publiek)
  - ANT_LOGS env var (standaard: C:\\Trading\\ANT_LOGS)
"""

from __future__ import annotations

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
ZSCORE_WINDOW = 20
ZSCORE_ENTRY = 1.5       # |Z| drempel voor entry
SL_PCT = 0.03            # stop-loss 3%
TP_PCT = 0.06            # take-profit 6%
MAX_BARS = 48            # maximale holdingperiode in bars
BARS_PER_YEAR = 365 * 24

DEFAULT_LOGS_ROOT = Path(os.environ.get("ANT_LOGS", r"C:\Trading\ANT_LOGS"))


# ---------------------------------------------------------------------------
# Data ophalen
# ---------------------------------------------------------------------------

def _fetch_candles(adapter: CryptoAdapter, symbol: str) -> list:
    """
    Haal ~90 dagen uurlijkse candles op voor symbol.

    Voert twee opeenvolgende requests uit als één batch onvoldoende is
    om de volledige lookback-periode te dekken.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    batch1 = adapter.get_candles(symbol, TIMEFRAME, limit=1440)
    if not batch1:
        return []

    # batch1 is gesorteerd oud→nieuw; [0] is de oudste candle
    if batch1[0].timestamp <= cutoff:
        return [c for c in batch1 if c.timestamp >= cutoff]

    # Nog verder terug in de tijd nodig
    oldest_ms = int(batch1[0].timestamp.timestamp() * 1000)
    batch2 = adapter.get_candles(symbol, TIMEFRAME, limit=1440, end_ms=oldest_ms - 1)

    seen: set = set()
    merged = []
    for c in sorted(batch1 + batch2, key=lambda x: x.timestamp):
        if c.timestamp not in seen:
            seen.add(c.timestamp)
            merged.append(c)

    return [c for c in merged if c.timestamp >= cutoff]


# ---------------------------------------------------------------------------
# Z-score berekening
# ---------------------------------------------------------------------------

def _compute_zscores(closes: np.ndarray, window: int) -> np.ndarray:
    """
    Bereken rolling Z-scores over een window van `window` bars.

    zscores[i] = (closes[i] - mean(closes[i-window:i])) / std(closes[i-window:i])
    NaN voor i < window of std == 0.
    """
    n = len(closes)
    zs = np.full(n, np.nan)
    for i in range(window, n):
        w = closes[i - window:i]
        mu = w.mean()
        sigma = w.std()
        if sigma > 0.0:
            zs[i] = (closes[i] - mu) / sigma
    return zs


# ---------------------------------------------------------------------------
# Backtest simulatie
# ---------------------------------------------------------------------------

def _backtest_symbol(candles: list) -> list[dict]:
    """
    Simuleer Mean Reversion trades op basis van Z-score signalen.

    Geen overlappende posities per symbool.
    Retourneert lijst van trade-dicts.
    """
    n = len(candles)
    if n < ZSCORE_WINDOW + 2:
        return []

    closes = np.array([c.close for c in candles], dtype=float)
    opens = np.array([c.open for c in candles], dtype=float)
    zscores = _compute_zscores(closes, ZSCORE_WINDOW)

    trades: list[dict] = []
    i = ZSCORE_WINDOW

    while i < n - 1:
        z = zscores[i]
        if np.isnan(z):
            i += 1
            continue

        if z < -ZSCORE_ENTRY:
            direction = "long"
        elif z > ZSCORE_ENTRY:
            direction = "short"
        else:
            i += 1
            continue

        entry_idx = i + 1
        if entry_idx >= n:
            break
        entry_price = opens[entry_idx]

        exit_idx = min(entry_idx + MAX_BARS - 1, n - 1)
        exit_reason = "max_bars"

        for j in range(entry_idx, min(entry_idx + MAX_BARS, n)):
            c_price = closes[j]
            z_j = zscores[j]

            if direction == "long":
                pct = (c_price - entry_price) / entry_price
                reverted = (not np.isnan(z_j)) and z_j >= 0.0
            else:
                pct = (entry_price - c_price) / entry_price
                reverted = (not np.isnan(z_j)) and z_j <= 0.0

            if pct <= -SL_PCT:
                exit_idx = j
                exit_reason = "sl"
                break
            if pct >= TP_PCT:
                exit_idx = j
                exit_reason = "tp"
                break
            if reverted:
                exit_idx = j
                exit_reason = "mean_reversion"
                break

        exit_price = closes[exit_idx]
        if direction == "long":
            ret_pct = (exit_price - entry_price) / entry_price
        else:
            ret_pct = (entry_price - exit_price) / entry_price

        trades.append({
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "return_pct": ret_pct,
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

    Sharpe annualisering: gebaseerd op gemiddelde holdingperiode in uren.
    """
    if not trades:
        return {"n": 0, "win_rate": 0.0, "sharpe": 0.0, "max_dd_pct": 0.0}

    returns = np.array([t["return_pct"] for t in trades], dtype=float)
    n = len(returns)
    wins = int(np.sum(returns > 0))
    win_rate = wins / n * 100.0

    # Annualized Sharpe
    std_ret = float(np.std(returns))
    if n >= 2 and std_ret > 0.0:
        avg_bars = float(np.mean([t["bars_held"] for t in trades]))
        trades_per_year = BARS_PER_YEAR / max(avg_bars, 1.0)
        sharpe = float((np.mean(returns) / std_ret) * np.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    # Max drawdown op cumulatieve equity curve (in %-punten)
    equity = np.concatenate([[0.0], np.cumsum(returns)])
    peak = np.maximum.accumulate(equity)
    max_dd_pct = float(abs(np.min(equity - peak)) * 100.0)

    return {"n": n, "win_rate": win_rate, "sharpe": sharpe, "max_dd_pct": max_dd_pct}


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

def build_report(results: list[tuple[str, list[dict], dict]]) -> str:
    lines = ["=== MEAN REVERSION HARNESS ==="]

    all_trades: list[dict] = []
    for symbol, trades, m in results:
        lines.append(
            f"Symbool: {symbol} | "
            f"Trades: {m['n']} | "
            f"Win rate: {m['win_rate']:.1f}% | "
            f"Sharpe: {m['sharpe']:.2f} | "
            f"Max DD: {m['max_dd_pct']:.1f}%"
        )
        all_trades.extend(trades)

    totaal = _compute_metrics(all_trades)
    lines.append("")
    lines.append(
        f"TOTAAL: trades={totaal['n']} "
        f"winrate={totaal['win_rate']:.1f}% "
        f"sharpe={totaal['sharpe']:.2f}"
    )
    return "\n".join(lines)


def write_report(report: str, logs_root: Path = DEFAULT_LOGS_ROOT) -> Path:
    out_dir = logs_root / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"mean_reversion_{stamp}.txt"
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
