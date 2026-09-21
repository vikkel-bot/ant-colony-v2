"""Permutatietoets voor strategie-trades.

Beantwoordt één vraag: is het resultaat van een strategie beter dan wat
toeval met dezelfde data oplevert?

Twee nulmodellen:
  A. return-herschikking: log-returns (met hun high/low-range) worden
     geschud en de strategie draait opnieuw. Vernietigt elke tijdsstructuur,
     behoudt de returnverdeling.
  B. willekeurige instap: de echte prijsreeks, maar instapmomenten willekeurig
     met dezelfde verdeling van houdduur. Vernietigt signaaltiming, behoudt
     het prijspad.

Drie grootheden, apart getoetst: gemiddelde, mediaan, trefkans per trade.

Lessen die hierin zijn vastgelegd (validatie 20-09-2026):
  - Blokherschikking met blokken >= de te toetsen structuur stopt het
    signaal in het nulmodel en verbergt het effect. Standaard block=1.
  - Onder ~80 trades komt zelfs een effect van 4% per trade niet tot
    significantie. Resultaten daaronder krijgen power_ok=False.

Alleen numpy nodig.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass
from typing import Callable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

MIN_TRADES_FOR_VERDICT = 80
STATISTICS = ("mean", "median", "hitrate")
NULL_MODELS = ("shuffle", "random_entry")


@dataclass(frozen=True)
class Trade:
    entry_idx: int
    exit_idx: int
    ret: float


TradeFn = Callable[[np.ndarray, np.ndarray, np.ndarray], list[Trade]]


# ------------------------------------------------------------------ strategie

def donchian_trades(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    entry_lookback: int = 20,
    exit_lookback: int = 10,
    stop_loss_pct: float = 0.05,
    max_bars_held: int = 72,
) -> list[Trade]:
    """Long-only Donchian breakout, geen overlappende posities.

    Instap op de close van de bar NA het uitbraaksignaal (geen look-ahead).
    """
    n = len(close)
    if n <= entry_lookback + 1:
        return []
    # prior_high[i] = max(high[i-entry_lookback : i]) — venster eindigt vóór i
    prior_high = np.full(n, np.inf)
    prior_high[entry_lookback:] = sliding_window_view(high, entry_lookback).max(axis=1)[: n - entry_lookback]
    prior_low = np.full(n, -np.inf)
    prior_low[exit_lookback:] = sliding_window_view(low, exit_lookback).min(axis=1)[: n - exit_lookback]

    trades: list[Trade] = []
    i = entry_lookback
    while i < n - 1:
        if close[i] <= prior_high[i]:
            i += 1
            continue
        entry_idx = i + 1
        entry_price = close[entry_idx]
        stop = entry_price * (1.0 - stop_loss_pct)
        exit_idx = min(entry_idx + max_bars_held, n - 1)
        exit_price = close[exit_idx]
        for j in range(entry_idx + 1, exit_idx + 1):
            if low[j] <= stop:
                exit_idx, exit_price = j, stop
                break
            if close[j] < prior_low[j]:
                exit_idx, exit_price = j, close[j]
                break
        trades.append(Trade(entry_idx, exit_idx, float((exit_price - entry_price) / entry_price)))
        i = exit_idx + 1
    return trades


# ------------------------------------------------------------------ grootheden

def trade_statistics(returns: np.ndarray) -> dict[str, float]:
    if len(returns) == 0:
        return {k: float("nan") for k in STATISTICS}
    return {
        "mean": float(np.mean(returns)),
        "median": float(np.median(returns)),
        "hitrate": float(np.mean(returns > 0.0)),
    }


# ------------------------------------------------------------------ nulmodellen

def shuffle_series(
    close: np.ndarray, high: np.ndarray, low: np.ndarray, rng: np.random.Generator, block: int = 1
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Nulmodel A. Schudt log-returns in blokken; high/low-range schuift mee
    met de return van dezelfde bar, zodat bar-vorm en beweging bij elkaar
    blijven."""
    logret = np.diff(np.log(close))
    rel_high = (high / close)[1:]
    rel_low = (low / close)[1:]
    m = len(logret)
    n_blocks = int(np.ceil(m / block))
    order = rng.permutation(n_blocks)
    idx = np.concatenate([np.arange(b * block, min((b + 1) * block, m)) for b in order])
    new_close = close[0] * np.exp(np.concatenate([[0.0], np.cumsum(logret[idx])]))
    new_high = new_close * np.concatenate([[high[0] / close[0]], rel_high[idx]])
    new_low = new_close * np.concatenate([[low[0] / close[0]], rel_low[idx]])
    return new_close, new_high, new_low


def random_entry_returns(
    close: np.ndarray, n_trades: int, holds: np.ndarray, rng: np.random.Generator, min_start: int
) -> np.ndarray:
    """Nulmodel B. Echte prijsreeks, willekeurige instap, houdduur getrokken
    uit de waargenomen verdeling."""
    n = len(close)
    hold = holds[rng.integers(0, len(holds), n_trades)]
    upper = np.maximum(min_start + 1, n - hold - 1)
    start = rng.integers(min_start, upper)
    end = np.minimum(start + hold, n - 1)
    return (close[end] - close[start]) / close[start]


# ------------------------------------------------------------------ toets

def _p_value(observed: float, null: np.ndarray) -> float:
    null = null[~np.isnan(null)]
    if np.isnan(observed) or len(null) == 0:
        return float("nan")
    return float((1 + np.sum(null >= observed)) / (1 + len(null)))


def permutation_test(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    trade_fn: TradeFn = donchian_trades,
    n_perm: int = 10_000,
    seed: int = 42,
    block: int = 1,
    min_start: int = 20,
    label: str = "",
) -> dict:
    close, high, low = (np.asarray(x, dtype=float) for x in (close, high, low))
    rng = np.random.default_rng(seed)
    trades = trade_fn(close, high, low)
    obs_returns = np.array([t.ret for t in trades])
    observed = trade_statistics(obs_returns)
    n_trades = len(trades)
    buy_and_hold = float((close[-1] - close[min_start]) / close[min_start]) if len(close) > min_start else float("nan")

    result = {
        "label": label,
        "n_bars": len(close),
        "n_trades": n_trades,
        "power_ok": n_trades >= MIN_TRADES_FOR_VERDICT,
        "observed": observed,
        "buy_and_hold_total": buy_and_hold,
        "n_perm": n_perm,
        "seed": seed,
        "block": block,
    }
    if n_trades < 2:
        result["p_values"] = {m: {k: float("nan") for k in STATISTICS} for m in NULL_MODELS}
        return result

    holds = np.array([t.exit_idx - t.entry_idx for t in trades])
    null = {m: {k: np.empty(n_perm) for k in STATISTICS} for m in NULL_MODELS}
    for p in range(n_perm):
        c2, h2, l2 = shuffle_series(close, high, low, rng, block)
        s = trade_statistics(np.array([t.ret for t in trade_fn(c2, h2, l2)]))
        for k in STATISTICS:
            null["shuffle"][k][p] = s[k]
        s = trade_statistics(random_entry_returns(close, n_trades, holds, rng, min_start))
        for k in STATISTICS:
            null["random_entry"][k][p] = s[k]

    result["p_values"] = {m: {k: _p_value(observed[k], null[m][k]) for k in STATISTICS} for m in NULL_MODELS}
    result["null_mean_of_mean"] = {m: float(np.nanmean(null[m]["mean"])) for m in NULL_MODELS}
    return result


def corrected_alpha(n_segments: int, alpha: float = 0.05) -> float:
    """Bonferroni over segmenten x grootheden x nulmodellen."""
    return alpha / (n_segments * len(STATISTICS) * len(NULL_MODELS))


def run_with_subperiods(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    n_subperiods: int = 3,
    **kwargs,
) -> dict:
    """Volledige reeks plus n deelperioden, elk met een EIGEN nulverdeling."""
    close, high, low = (np.asarray(x, dtype=float) for x in (close, high, low))
    segments = [("VOLLEDIG", 0, len(close))]
    step = len(close) // n_subperiods
    for k in range(n_subperiods):
        end = (k + 1) * step if k < n_subperiods - 1 else len(close)
        segments.append((f"DEEL{k + 1}", k * step, end))
    results = [
        permutation_test(close[a:b], high[a:b], low[a:b], label=name, **kwargs)
        for name, a, b in segments
    ]
    alpha = corrected_alpha(len(segments))
    for r in results:
        pv = r["p_values"]
        r["significant"] = {
            m: {k: bool(pv[m][k] < alpha) if not np.isnan(pv[m][k]) else False for k in STATISTICS}
            for m in NULL_MODELS
        }
    return {"corrected_alpha": alpha, "segments": results}


# ------------------------------------------------------------------ synthetisch

def synth_random_walk(n: int, seed: int, sigma: float = 0.03):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0, sigma, n)))
    rng_range = np.abs(rng.normal(0, sigma / 2, n))
    return close, close * (1 + rng_range), close * (1 - rng_range)


def synth_with_trend(n: int, seed: int, sigma: float = 0.03, persistence: float = 0.35):
    """Ingebouwde structuur: AR(1)-returns, dus trendvolgen hoort te werken."""
    rng = np.random.default_rng(seed)
    logret = np.zeros(n)
    shocks = rng.normal(0.0, sigma, n)
    for i in range(1, n):
        logret[i] = persistence * logret[i - 1] + shocks[i]
    close = 100 * np.exp(np.cumsum(logret))
    rng_range = np.abs(rng.normal(0, sigma / 2, n))
    return close, close * (1 + rng_range), close * (1 - rng_range)


# ------------------------------------------------------------------ data + CLI

def fetch_bitvavo_daily(market: str, limit: int = 1440):
    url = f"https://api.bitvavo.com/v2/{market}/candles?interval=1d&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "ant-colony-permtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = sorted(json.loads(resp.read().decode()), key=lambda r: int(r[0]))
    close = np.array([float(r[4]) for r in rows])
    high = np.array([float(r[2]) for r in rows])
    low = np.array([float(r[3]) for r in rows])
    return close, high, low


def main() -> None:
    ap = argparse.ArgumentParser(description="Permutatietoets Donchian op Bitvavo-dagcandles")
    ap.add_argument("--asset", default="BTC-EUR")
    ap.add_argument("--perm", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--subperiods", type=int, default=3)
    args = ap.parse_args()

    close, high, low = fetch_bitvavo_daily(args.asset)
    print(f"{args.asset}: {len(close)} dagcandles")
    out = run_with_subperiods(close, high, low, n_subperiods=args.subperiods, n_perm=args.perm, seed=args.seed)
    print(f"gecorrigeerde drempel: {out['corrected_alpha']:.6f}")
    for seg in out["segments"]:
        print(json.dumps(seg, indent=2))


if __name__ == "__main__":
    main()
