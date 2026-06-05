"""
Test a concentration-triggered SPY/RSP rotation rule.

Data: yfinance Adj Close for SPY and RSP from RSP inception onward.
Train: 2003-2014 chooses only the MA length N from a fixed grid.
Test: 2015-now compares concentration rotation, SPY buy-and-hold, and a
naive 12-month SPY/RSP momentum benchmark. Train and test are never merged.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = REPO_ROOT / "results" / "concentration_rotation_results.csv"
RSP_INCEPTION_MONTH = date(2003, 4, 1)
TRAIN_START = date(2003, 4, 1)
TRAIN_END = date(2014, 12, 31)
TEST_START = date(2015, 1, 1)
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0
HIGH_LOOKBACK = 252
MOMENTUM_LOOKBACK = 252
N_GRID = (50, 100, 150, 200, 252)
SYMBOLS = ("SPY", "RSP")
MEASURABLE_SHARPE_DELTA = 0.05


@dataclass(frozen=True)
class MetricRow:
    strategy: str
    period: str
    start: date
    end: date
    total_return: float
    max_drawdown: float
    sharpe: float | None
    trades: int
    volatility: float | None


def configure_yfinance_cache() -> None:
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_concentration_rotation"
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


def load_adj_close():
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError(f"yfinance/pandas import failed: {exc}") from exc

    frame = yf.download(
        list(SYMBOLS),
        start=RSP_INCEPTION_MONTH.isoformat(),
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
    )
    frame = normalize_download_frame(frame)
    if frame is None or frame.empty:
        raise RuntimeError("yfinance download failed or returned empty data for SPY/RSP")
    if "Adj Close" not in frame:
        raise RuntimeError("yfinance data has no Adj Close column")

    adj_close = frame["Adj Close"]
    missing = [symbol for symbol in SYMBOLS if symbol not in adj_close]
    if missing:
        raise RuntimeError(f"yfinance Adj Close missing symbols: {', '.join(missing)}")
    adj_close = adj_close.loc[:, list(SYMBOLS)].dropna()
    if adj_close.empty:
        raise RuntimeError("SPY/RSP Adj Close is empty after aligning and dropna")
    return adj_close


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


def max_drawdown_from_returns(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def metrics(strategy: str, period: str, returns: list[float], trades: int, start: date, end: date) -> MetricRow:
    return MetricRow(
        strategy=strategy,
        period=period,
        start=start,
        end=end,
        total_return=compound_return(returns),
        max_drawdown=max_drawdown_from_returns(returns),
        sharpe=annualized_sharpe(returns),
        trades=trades,
        volatility=annualized_volatility(returns),
    )


def slice_period(frame, start: date, end: date | None):
    if end is None:
        return frame.loc[frame.index.date >= start]
    return frame.loc[(frame.index.date >= start) & (frame.index.date <= end)]


def concentration_positions(adj_close, n: int):
    ratio = adj_close["SPY"] / adj_close["RSP"]
    ma = ratio.rolling(n, min_periods=n).mean()
    rolling_high = ratio.rolling(HIGH_LOOKBACK, min_periods=HIGH_LOOKBACK).max()
    positions: list[str] = []
    current = "SPY"
    armed_after_high = False

    for idx in range(len(ratio)):
        if idx > 0 and not math.isnan(float(rolling_high.iloc[idx - 1])):
            if ratio.iloc[idx - 1] >= rolling_high.iloc[idx - 1]:
                armed_after_high = True

        positions.append(current)

        if idx == 0 or math.isnan(float(ma.iloc[idx])) or math.isnan(float(ma.iloc[idx - 1])):
            continue
        crossed_under = ratio.iloc[idx - 1] >= ma.iloc[idx - 1] and ratio.iloc[idx] < ma.iloc[idx]
        crossed_over = ratio.iloc[idx - 1] <= ma.iloc[idx - 1] and ratio.iloc[idx] > ma.iloc[idx]
        if current == "SPY" and armed_after_high and crossed_under:
            current = "RSP"
            armed_after_high = False
        elif current == "RSP" and crossed_over:
            current = "SPY"

    return positions


def momentum_positions(adj_close):
    ratio = adj_close["SPY"] / adj_close["RSP"]
    positions: list[str] = []
    current = "SPY"
    for idx in range(len(ratio)):
        positions.append(current)
        if idx >= MOMENTUM_LOOKBACK:
            current = "SPY" if ratio.iloc[idx] > ratio.iloc[idx - MOMENTUM_LOOKBACK] else "RSP"
    return positions


def spy_positions(adj_close):
    return ["SPY"] * len(adj_close)


def strategy_returns(adj_close, positions: list[str]) -> tuple[list[float], int]:
    spy_returns = adj_close["SPY"].pct_change().fillna(0.0)
    rsp_returns = adj_close["RSP"].pct_change().fillna(0.0)
    returns: list[float] = []
    trades = 0
    previous_position = positions[0] if positions else "SPY"
    for idx in range(1, len(adj_close)):
        position = positions[idx]
        if position != previous_position:
            trades += 1
        value = spy_returns.iloc[idx] if position == "SPY" else rsp_returns.iloc[idx]
        returns.append(float(value))
        previous_position = position
    return returns, trades


def evaluate_strategy(adj_close, positions: list[str], strategy: str, period: str, start: date, end: date) -> MetricRow:
    returns, trades = strategy_returns(adj_close, positions)
    return metrics(strategy, period, returns, trades, start, end)


def choose_n_on_train(adj_close) -> tuple[int, list[MetricRow]]:
    train_frame = slice_period(adj_close, TRAIN_START, TRAIN_END)
    if train_frame.empty:
        raise RuntimeError("train frame is empty")
    actual_train_start = train_frame.index[0].date()
    actual_train_end = train_frame.index[-1].date()
    rows: list[MetricRow] = []
    for n in N_GRID:
        row = evaluate_strategy(
            train_frame,
            concentration_positions(train_frame, n),
            f"Concentration rotation N={n}",
            "train",
            actual_train_start,
            actual_train_end,
        )
        rows.append(row)
    valid = [row for row in rows if row.sharpe is not None]
    if not valid:
        raise RuntimeError("no train Sharpe values available for N selection")
    best = max(valid, key=lambda row: row.sharpe if row.sharpe is not None else -math.inf)
    return int(best.strategy.rsplit("=", 1)[1]), rows


def pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def num(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.2f}"


def print_table(title: str, rows: list[MetricRow]) -> None:
    print("")
    print(title)
    header = (
        f"{'Strategy':<34} {'Period':<6} {'Return':>12} {'Max DD':>10} "
        f"{'Sharpe':>8} {'Trades':>8} {'Vol':>10}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.strategy:<34} {row.period:<6} {pct(row.total_return):>12} "
            f"{pct(row.max_drawdown):>10} {num(row.sharpe):>8} "
            f"{row.trades:>8} {pct(row.volatility):>10}"
        )


def row_to_dict(row: MetricRow) -> dict[str, str]:
    return {
        "strategy": row.strategy,
        "period": row.period,
        "start": row.start.isoformat(),
        "end": row.end.isoformat(),
        "total_return": f"{row.total_return:.8f}",
        "max_drawdown": f"{row.max_drawdown:.8f}",
        "sharpe_rf_0": "" if row.sharpe is None else f"{row.sharpe:.8f}",
        "trades": str(row.trades),
        "volatility": "" if row.volatility is None else f"{row.volatility:.8f}",
    }


def write_csv(rows: list[MetricRow]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row_to_dict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row_to_dict(row))


def main() -> int:
    configure_yfinance_cache()
    adj_close = load_adj_close()
    first_date = adj_close.index[0].date()
    last_date = adj_close.index[-1].date()

    print("=== LAB CONCENTRATION ROTATION TEST ===")
    print("Data: yfinance Adj Close, SPY/RSP aligned from RSP inception.")
    print(f"Data range: {first_date} t/m {last_date}")
    print("Risk-free rate for Sharpe: 0.00% per jaar (bewuste aanname).")
    print(f"Train: {TRAIN_START} t/m {TRAIN_END}; Test: {TEST_START} t/m {last_date}.")
    print(f"N-grid op train: {', '.join(str(value) for value in N_GRID)}")
    print("Momentum benchmark: koop winnaar van afgelopen 252 handelsdagen op SPY/RSP-spread.")

    selected_n, train_rows = choose_n_on_train(adj_close)
    print_table("=== TRAIN N-SELECTIE (ALLEEN VOOR N) ===", train_rows)
    print(f"\nGekozen N op train: {selected_n}")

    test_frame = slice_period(adj_close, TEST_START, None)
    if test_frame.empty:
        raise RuntimeError("test frame is empty")
    test_start = test_frame.index[0].date()
    test_end = test_frame.index[-1].date()

    concentration = evaluate_strategy(
        test_frame,
        concentration_positions(test_frame, selected_n),
        f"Concentration rotation N={selected_n}",
        "test",
        test_start,
        test_end,
    )
    spy = evaluate_strategy(test_frame, spy_positions(test_frame), "SPY buy-and-hold", "test", test_start, test_end)
    momentum = evaluate_strategy(
        test_frame,
        momentum_positions(test_frame),
        "12m SPY/RSP momentum",
        "test",
        test_start,
        test_end,
    )
    test_rows = [concentration, spy, momentum]
    print_table("=== TEST RESULTATEN ===", test_rows)

    if concentration.sharpe is None or spy.sharpe is None or momentum.sharpe is None:
        raise RuntimeError("missing Sharpe value in test comparison")
    concentration_minus_momentum = concentration.sharpe - momentum.sharpe
    beats_spy = concentration.sharpe > spy.sharpe
    beats_momentum = concentration_minus_momentum > MEASURABLE_SHARPE_DELTA
    verdict = "KANDIDAAT-EDGE" if beats_spy and beats_momentum else "GEEN EDGE"

    print("")
    print("=== GO/NO-GO ===")
    print(f"Concentratie Sharpe > SPY buy-and-hold Sharpe: {beats_spy}")
    print(
        "Concentratie Sharpe meetbaar > momentum Sharpe "
        f"(delta > {MEASURABLE_SHARPE_DELTA:.2f}): {beats_momentum}"
    )
    print(f"Sharpe strategie 1 minus strategie 3: {concentration_minus_momentum:.4f}")
    if not beats_momentum:
        print("Conclusie: concentratie-verhaal is niet duidelijk beter dan momentum; dit is momentum in een verkleedpak.")
    print(f"Verdict: {verdict}")

    write_csv([*train_rows, *test_rows])
    print(f"\nCSV geschreven: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
