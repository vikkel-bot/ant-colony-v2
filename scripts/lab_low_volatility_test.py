# Low-volatility is a broad, published anomaly; honest lab work asks whether it
# survives out-of-sample after fees on our chosen universe, not whether a pretty
# train result can be mistaken for a new edge.
"""
First plug-in for lab_strategy_harness.py: low-volatility anomaly.

Universe note:
This fixed list uses liquid US large-cap names with yfinance history throughout
2010-now. It is still survivorship-biased: it is not a historical S&P 500
constituent database, so a good result must not be read as clean investable
index evidence.
"""

from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab_strategy_harness import StrategyPlan, run_strategy


LOW_VOL_UNIVERSE = (
    "AAPL", "MSFT", "IBM", "INTC", "CSCO", "ORCL", "QCOM", "TXN", "ADI", "AMAT",
    "JNJ", "PFE", "MRK", "ABT", "BMY", "AMGN", "GILD", "MDT",
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "CL", "MO", "NKE", "HD", "LOW", "DIS",
    "XOM", "CVX", "COP",
    "JPM", "BAC", "WFC", "GS", "AXP",
    "UNP", "UPS", "CAT", "DE", "MMM", "HON", "BA", "VZ",
)
TRAIN_PERIOD = (date(2010, 1, 1), date(2017, 12, 31))
TEST_PERIOD = (date(2018, 1, 1), date.today())
ETF_FEE_PCT = 0.0002


def month_end_rebalance_dates(index) -> set:
    dates = set()
    periods = index.to_period("M")
    for idx in range(len(index)):
        if idx == len(index) - 1 or periods[idx] != periods[idx + 1]:
            dates.add(index[idx])
    return dates


def build_low_vol_weights(adj_close, lookback: int, quintile: float):
    import pandas as pd

    returns = adj_close.pct_change()
    realized_vol = returns.rolling(lookback, min_periods=lookback).std()
    rebalance_dates = month_end_rebalance_dates(adj_close.index)
    weights = pd.DataFrame(0.0, index=adj_close.index, columns=adj_close.columns)
    current = pd.Series(0.0, index=adj_close.columns)
    selected_count = max(1, math.ceil(len(adj_close.columns) * quintile))

    for timestamp in adj_close.index:
        if timestamp in rebalance_dates:
            vol_row = realized_vol.loc[timestamp].dropna()
            if len(vol_row) == len(adj_close.columns):
                selected = list(vol_row.sort_values().head(selected_count).index)
                current = pd.Series(0.0, index=adj_close.columns)
                current.loc[selected] = 1.0 / len(selected)
        weights.loc[timestamp] = current
    return weights


def low_volatility_strategy(adj_close, *, params, selected_params=None, phase: str) -> StrategyPlan:
    candidates = list(params.get("vol_lookback_candidates", [60]))
    quintile = float(params.get("quintile", 0.20))
    if selected_params is None:
        # Pre-specified for this first plug-in: train records the chosen value,
        # but does not tune across many alternatives.
        selected_params = {"vol_lookback": int(candidates[0]), "quintile": quintile}
    lookback = int(selected_params["vol_lookback"])
    weights = build_low_vol_weights(adj_close, lookback, quintile)
    return StrategyPlan(
        strategy_name=f"Low-volatility bottom quintile ({lookback}d)",
        weights=weights,
        selected_params=dict(selected_params),
    )


def spy_buy_hold_strategy(adj_close, *, params, selected_params=None, phase: str) -> StrategyPlan:
    import pandas as pd

    weights = pd.DataFrame(1.0, index=adj_close.index, columns=adj_close.columns)
    return StrategyPlan(
        strategy_name="SPY buy-and-hold benchmark",
        weights=weights,
        selected_params={},
    )


def main() -> int:
    print("=== LAB LOW-VOLATILITY ANOMALY TEST ===")
    print("Bekende anomalie: Baker/Bradley/Wurgler low-volatility effect.")
    print("Eerlijke vraag: overleeft dit out-of-sample na fees op ons vaste universum?")
    print("Universum: 46 liquide US large-cap aandelen met yfinance-historie vanaf 2010.")
    print("Survivorship-bias waarschuwing: dit is geen historische index-constituent database.")
    print(f"Train: {TRAIN_PERIOD[0]} t/m {TRAIN_PERIOD[1]}; Test: {TEST_PERIOD[0]} t/m {TEST_PERIOD[1]}")
    print("Low-vol logica: maandelijks laagste 20% op trailing 60-daagse realized volatility, equal-weight.")
    print("Benchmark: SPY buy-and-hold over identieke train/test-perioden.")
    print(f"Fee: {ETF_FEE_PCT:.4%} ETF-rate.")

    low_vol = run_strategy(
        low_volatility_strategy,
        LOW_VOL_UNIVERSE,
        ETF_FEE_PCT,
        TRAIN_PERIOD,
        TEST_PERIOD,
        {
            "vol_lookback_candidates": [60],
            "quintile": 0.20,
            "fee_label": "ETF-rate 0.02%",
        },
    )
    spy = run_strategy(
        spy_buy_hold_strategy,
        ("SPY",),
        ETF_FEE_PCT,
        TRAIN_PERIOD,
        TEST_PERIOD,
        {"fee_label": "ETF-rate 0.02%"},
    )

    print("")
    print("=== LOW-VOL VS SPY TEST COMPARISON ===")
    print(f"Low-vol Sharpe: {low_vol.sharpe:.2f}" if low_vol.sharpe is not None else "Low-vol Sharpe: n/a")
    print(f"SPY Sharpe: {spy.sharpe:.2f}" if spy.sharpe is not None else "SPY Sharpe: n/a")
    if low_vol.sharpe is not None and spy.sharpe is not None:
        print(f"Sharpe spread low-vol minus SPY: {low_vol.sharpe - spy.sharpe:.4f}")
    print(f"Low-vol verdict: {low_vol.verdict}")
    print(f"SPY benchmark verdict: {spy.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
