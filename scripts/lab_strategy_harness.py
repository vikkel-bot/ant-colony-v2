# Low-volatility is a broad, published anomaly; honest lab work asks whether it
# survives out-of-sample after fees on our chosen universe, not whether a pretty
# train result can be mistaken for a new edge.
"""
Reusable lab strategy harness.

The harness enforces the same train/test discipline, yfinance Adj Close loading,
fee accounting, metrics, Go/No-Go reporting, and central CSV append for every
plug-in strategy.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPARISON_PATH = REPO_ROOT / "results" / "strategy_comparison.csv"
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0


@dataclass(frozen=True)
class HarnessResult:
    strategy_name: str
    universe: tuple[str, ...]
    fee_pct: float
    train_period: tuple[date, date]
    test_period: tuple[date, date]
    total_return: float
    max_drawdown: float
    sharpe: float | None
    volatility: float | None
    trades: int
    win_rate: float
    verdict: str
    selected_params: dict[str, Any]


@dataclass(frozen=True)
class StrategyPlan:
    strategy_name: str
    weights: Any
    selected_params: dict[str, Any]


StrategyFn = Callable[..., StrategyPlan]


def configure_yfinance_cache() -> None:
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_strategy_harness"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir.resolve()))
    except Exception:
        return


def normalize_download_frame(frame):
    if frame is None or frame.empty:
        return frame
    if not hasattr(frame.columns, "nlevels") or frame.columns.nlevels == 1:
        return frame
    if "Adj Close" in frame.columns.get_level_values(0):
        return frame
    return frame.swaplevel(axis=1).sort_index(axis=1)


def load_adj_close(universe: tuple[str, ...], start: date, end: date):
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError(f"yfinance/pandas import failed: {exc}") from exc

    download_end = pd.Timestamp(end) + pd.Timedelta(days=1)
    frame = yf.download(
        list(universe),
        start=start.isoformat(),
        end=download_end.date().isoformat(),
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
    )
    frame = normalize_download_frame(frame)
    if frame is None or frame.empty:
        raise RuntimeError(f"yfinance download failed or returned empty data for {', '.join(universe)}")
    if "Adj Close" not in frame:
        raise RuntimeError("yfinance data has no Adj Close column")

    adj_close = frame["Adj Close"]
    if len(universe) == 1 and not hasattr(adj_close, "columns"):
        adj_close = adj_close.to_frame(universe[0])
    missing = [symbol for symbol in universe if symbol not in adj_close]
    if missing:
        raise RuntimeError(f"yfinance Adj Close missing symbols: {', '.join(missing)}")
    adj_close = adj_close.loc[:, list(universe)].dropna(how="any")
    if adj_close.empty:
        raise RuntimeError("Adj Close frame is empty after aligning symbols and dropna")
    return adj_close


def slice_period(frame, period: tuple[date, date]):
    start, end = period
    sliced = frame.loc[(frame.index.date >= start) & (frame.index.date <= end)]
    if sliced.empty:
        raise RuntimeError(f"empty period slice: {start}..{end}")
    return sliced


def annualized_volatility(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR)


def annualized_sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return None
    excess_daily = mean - RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
    return excess_daily / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def compound_return(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value
    return equity - 1.0


def max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def evaluate_weights(adj_close, weights, fee_pct: float) -> tuple[list[float], int, float]:
    weights = weights.reindex(adj_close.index).fillna(0.0)
    weights = weights.reindex(columns=adj_close.columns, fill_value=0.0)
    asset_returns = adj_close.pct_change().fillna(0.0)

    previous_target = weights.iloc[0] * 0.0
    holding_equity = 1.0
    holding_returns: list[float] = []
    wins = 0
    trades = 0
    portfolio_returns: list[float] = []

    for idx in range(1, len(adj_close)):
        target = weights.iloc[idx - 1]
        turnover = float((target - previous_target).abs().sum())
        if turnover > 1e-12:
            if holding_returns:
                holding_total = compound_return(holding_returns)
                wins += 1 if holding_total > 0 else 0
                holding_returns = []
            trades += 1
            previous_target = target

        gross_return = float((target * asset_returns.iloc[idx]).sum())
        fee_cost = fee_pct * turnover
        net_return = gross_return - fee_cost
        portfolio_returns.append(net_return)
        holding_returns.append(net_return)
        holding_equity *= 1.0 + net_return

    if holding_returns:
        wins += 1 if compound_return(holding_returns) > 0 else 0

    win_rate = wins / trades if trades else 0.0
    return portfolio_returns, trades, win_rate


def go_no_go(sharpe: float | None, max_dd: float, win_rate: float) -> tuple[str, dict[str, bool]]:
    criteria = {
        "Sharpe > 1.0": sharpe is not None and sharpe > 1.0,
        "max DD < 10%": max_dd > -0.10,
        "win rate > 55%": win_rate > 0.55,
    }
    verdict = "GO" if all(criteria.values()) else "NO-GO"
    return verdict, criteria


def pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def num(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.2f}"


def append_comparison(result: HarnessResult) -> None:
    COMPARISON_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "strategy_name": result.strategy_name,
        "universe": " ".join(result.universe),
        "fee_pct": f"{result.fee_pct:.8f}",
        "train_period": f"{result.train_period[0]}..{result.train_period[1]}",
        "test_period": f"{result.test_period[0]}..{result.test_period[1]}",
        "return": f"{result.total_return:.8f}",
        "max_dd": f"{result.max_drawdown:.8f}",
        "sharpe": "" if result.sharpe is None else f"{result.sharpe:.8f}",
        "vol": "" if result.volatility is None else f"{result.volatility:.8f}",
        "trades": str(result.trades),
        "win_rate": f"{result.win_rate:.8f}",
        "verdict": result.verdict,
        "commit_hash_placeholder": "PENDING",
        "run_date": datetime.now(timezone.utc).isoformat(),
    }
    write_header = not COMPARISON_PATH.exists()
    with COMPARISON_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def run_strategy(
    strategy_fn: StrategyFn,
    universe: list[str] | tuple[str, ...],
    fee_pct: float,
    train_period: tuple[date, date],
    test_period: tuple[date, date],
    params: dict[str, Any],
) -> HarnessResult:
    """Run one strategy under the shared lab protocol.

    `strategy_fn` receives only the train slice for parameter selection and only
    the test slice for final measurement. The harness never merges train and
    test data before passing it into the strategy.
    """
    configure_yfinance_cache()
    universe_tuple = tuple(universe)
    if train_period[1] >= test_period[0]:
        raise ValueError("train_period must end before test_period starts")

    fee_label = params.get("fee_label", f"fee_pct={fee_pct:.6f}")
    load_start = train_period[0]
    load_end = test_period[1]
    adj_close = load_adj_close(universe_tuple, load_start, load_end)
    train_prices = slice_period(adj_close, train_period)
    test_prices = slice_period(adj_close, test_period)

    train_plan = strategy_fn(train_prices, params=params, selected_params=None, phase="train")
    selected_params = dict(train_plan.selected_params)
    test_plan = strategy_fn(test_prices, params=params, selected_params=selected_params, phase="test")
    returns, trades, win_rate = evaluate_weights(test_prices, test_plan.weights, fee_pct)
    total_return = compound_return(returns)
    dd = max_drawdown(returns)
    sharpe = annualized_sharpe(returns)
    volatility = annualized_volatility(returns)
    verdict, criteria = go_no_go(sharpe, dd, win_rate)
    actual_test_period = (test_prices.index[0].date(), test_prices.index[-1].date())

    result = HarnessResult(
        strategy_name=test_plan.strategy_name,
        universe=universe_tuple,
        fee_pct=fee_pct,
        train_period=(train_prices.index[0].date(), train_prices.index[-1].date()),
        test_period=actual_test_period,
        total_return=total_return,
        max_drawdown=dd,
        sharpe=sharpe,
        volatility=volatility,
        trades=trades,
        win_rate=win_rate,
        verdict=verdict,
        selected_params=selected_params,
    )
    append_comparison(result)

    print("")
    print(f"=== HARNESS RESULT: {result.strategy_name} ===")
    print(f"Universe: {' '.join(universe_tuple)}")
    print(f"Fee: {fee_pct:.6f} ({fee_label})")
    print(f"Train: {result.train_period[0]} t/m {result.train_period[1]}")
    print(f"Test:  {result.test_period[0]} t/m {result.test_period[1]}")
    print(f"Selected params from train: {selected_params}")
    print("Risk-free rate for Sharpe: 0.00% per jaar (bewuste aanname).")
    print(f"Return={pct(total_return)} | MaxDD={pct(dd)} | Sharpe={num(sharpe)} | Vol={pct(volatility)}")
    print(f"Trades={trades} | Win rate={pct(win_rate)}")
    print("Go/No-Go criteria:")
    for label, ok in criteria.items():
        print(f"  {label}: {ok}")
    print(f"Verdict: {verdict}")
    print(f"Append CSV: {COMPARISON_PATH}")
    return result
