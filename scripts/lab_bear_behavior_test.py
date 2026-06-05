"""
Describe SPY cap-weight versus RSP equal-weight behavior in bear windows.

This is a descriptive measurement, not a fitted strategy, so there is no
train/test split by design.
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = REPO_ROOT / "results" / "bear_behavior_results.csv"
RISK_FREE_RATE = 0.0
TRADING_DAYS_PER_YEAR = 252
RSP_INCEPTION = date(2003, 4, 24)
WINDOWS = (
    ("Dot-com", date(1999, 3, 1), date(2002, 12, 31)),
    ("GFC", date(2007, 4, 1), date(2009, 9, 30)),
    ("2022", date(2021, 6, 1), date(2023, 6, 30)),
)
SYMBOLS = ("SPY", "RSP")


@dataclass(frozen=True)
class SeriesData:
    dates: list[date]
    prices: list[float]


@dataclass(frozen=True)
class MetricRow:
    window: str
    symbol: str
    start: date
    end: date
    first_date: date
    last_date: date
    total_return: float
    max_drawdown: float
    sharpe: float | None
    volatility: float | None


def configure_yfinance_cache() -> None:
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_bear_behavior"
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


def load_adj_close(symbol: str, start: date, end: date) -> SeriesData:
    try:
        import pandas as pd
        import yfinance as yf
    except Exception as exc:
        raise RuntimeError(f"yfinance/pandas import failed: {exc}") from exc

    download_end = pd.Timestamp(end) + pd.Timedelta(days=1)
    frame = yf.download(
        symbol,
        start=start.isoformat(),
        end=download_end.date().isoformat(),
        progress=False,
        auto_adjust=False,
        actions=False,
        threads=False,
    )
    frame = normalize_download_frame(frame)
    if frame is None or frame.empty:
        raise RuntimeError(f"yfinance download failed or returned empty data for {symbol}")

    if "Adj Close" not in frame:
        raise RuntimeError(f"yfinance data for {symbol} has no Adj Close column")
    adj_close = frame["Adj Close"]
    if hasattr(adj_close, "columns"):
        if symbol not in adj_close:
            raise RuntimeError(f"yfinance Adj Close data for {symbol} missing symbol column")
        adj_close = adj_close[symbol]

    adj_close = adj_close.dropna()
    if adj_close.empty:
        raise RuntimeError(f"yfinance Adj Close data for {symbol} is empty after dropna")

    dates = [ts.date() for ts in adj_close.index]
    prices = [float(value) for value in adj_close.to_list()]
    return SeriesData(dates=dates, prices=prices)


def daily_returns(prices: list[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in zip(prices, prices[1:]):
        if previous <= 0:
            raise RuntimeError("non-positive adjusted close encountered")
        returns.append(current / previous - 1.0)
    return returns


def max_drawdown(prices: list[float]) -> float:
    peak = prices[0]
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        worst = min(worst, price / peak - 1.0)
    return worst


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
    excess_daily = sum(returns) / len(returns) - (RISK_FREE_RATE / TRADING_DAYS_PER_YEAR)
    return excess_daily / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def compute_metrics(window: str, symbol: str, start: date, end: date, series: SeriesData) -> MetricRow:
    if not series.prices:
        raise RuntimeError(f"no prices available for {symbol} in {window}")
    returns = daily_returns(series.prices)
    total_return = series.prices[-1] / series.prices[0] - 1.0
    return MetricRow(
        window=window,
        symbol=symbol,
        start=start,
        end=end,
        first_date=series.dates[0],
        last_date=series.dates[-1],
        total_return=total_return,
        max_drawdown=max_drawdown(series.prices),
        sharpe=annualized_sharpe(returns),
        volatility=annualized_volatility(returns),
    )


def pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def num(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.2f}"


def row_to_dict(row: MetricRow) -> dict[str, str]:
    return {
        "window": row.window,
        "symbol": row.symbol,
        "requested_start": row.start.isoformat(),
        "requested_end": row.end.isoformat(),
        "first_data_date": row.first_date.isoformat(),
        "last_data_date": row.last_date.isoformat(),
        "total_return": f"{row.total_return:.8f}",
        "max_drawdown": f"{row.max_drawdown:.8f}",
        "sharpe_rf_0": "" if row.sharpe is None else f"{row.sharpe:.8f}",
        "volatility": "" if row.volatility is None else f"{row.volatility:.8f}",
    }


def print_window_table(window: str, rows: list[MetricRow]) -> None:
    print("")
    print(f"=== {window} ===")
    header = (
        f"{'ETF':<6} {'Data':<23} {'Total return':>14} "
        f"{'Max DD':>10} {'Sharpe':>8} {'Vol':>10}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        data_span = f"{row.first_date}..{row.last_date}"
        print(
            f"{row.symbol:<6} {data_span:<23} {pct(row.total_return):>14} "
            f"{pct(row.max_drawdown):>10} {num(row.sharpe):>8} {pct(row.volatility):>10}"
        )


def write_csv(rows: list[MetricRow]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row_to_dict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row_to_dict(row))


def main() -> int:
    configure_yfinance_cache()

    print("=== LAB BEAR BEHAVIOR TEST ===")
    print("Vergelijking: SPY cap-weight vs RSP equal-weight in echte bear-vensters.")
    print("Databron: yfinance Adj Close; dividenden herbelegd voor zover Adj Close dit bevat.")
    print("Risk-free rate voor Sharpe: 0.00% per jaar (bewuste aanname).")
    print("Geen train/test split: dit is beschrijvend, geen gefitte strategie.")

    all_rows: list[MetricRow] = []
    sharpe_spreads: list[tuple[str, float | None]] = []

    for window, start, end in WINDOWS:
        window_rows: list[MetricRow] = []
        for symbol in SYMBOLS:
            if symbol == "RSP" and end < RSP_INCEPTION:
                print('DOT-COM VENSTER: RSP niet beschikbaar (inceptie 2003-04), SPY-only resultaat')
                continue
            series = load_adj_close(symbol, start, end)
            if series.dates[0] > start + timedelta(days=7):
                if symbol == "RSP" and start < RSP_INCEPTION:
                    raise RuntimeError(
                        f"RSP data starts at {series.dates[0]} after requested {start}; "
                        "no backfill/proxy allowed"
                    )
                raise RuntimeError(f"{symbol} data starts at {series.dates[0]} after requested {start}")
            if series.dates[-1] < end:
                raise RuntimeError(f"{symbol} data ends at {series.dates[-1]} before requested {end}")
            row = compute_metrics(window, symbol, start, end, series)
            window_rows.append(row)
            all_rows.append(row)

        print_window_table(window, window_rows)
        by_symbol = {row.symbol: row for row in window_rows}
        if "SPY" in by_symbol and "RSP" in by_symbol:
            spy_sharpe = by_symbol["SPY"].sharpe
            rsp_sharpe = by_symbol["RSP"].sharpe
            spread = None if spy_sharpe is None or rsp_sharpe is None else rsp_sharpe - spy_sharpe
            sharpe_spreads.append((window, spread))
        else:
            sharpe_spreads.append((window, None))

    if not all_rows:
        raise RuntimeError("no results computed")
    write_csv(all_rows)

    print("")
    print("=== SHARPE-SPREAD RSP MINUS SPY ===")
    for window, spread in sharpe_spreads:
        print(f"{window}: {num(spread)}")
    print("")
    print(f"CSV geschreven: {RESULTS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
