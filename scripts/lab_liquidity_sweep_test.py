"""
Isolated lab test for the Liquidity Sweep Sniper rule on BTC-EUR and ETH-EUR.

No colony, order, brain, or live code is imported here. Data is fetched from
public Binance 4H klines. Optional diagnostic windows are skipped per-window
when full 4H data is unavailable.
"""

from __future__ import annotations

import csv
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ASSETS = {
    "BTC-EUR": "BTCEUR",
    "ETH-EUR": "ETHEUR",
}
INTERVAL = "4h"
INTERVAL_MS = 4 * 60 * 60 * 1000
ANNUAL_BARS = 365.25 * 6
LOOKBACK_BARS = 180
LIMIT_DISCOUNT = 0.985
STOP_MULTIPLIER = 0.98
TP_MULTIPLIER = 1.01
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_MAKER_FEE = 0.0025
TAKER_FEE = 0.0025
TRAIN_FRACTION = 0.60
REQUIRED_FETCH_START = datetime(2017, 12, 1, tzinfo=timezone.utc)
MAIN_START = datetime(2018, 1, 1, tzinfo=timezone.utc)
CALENDAR_YEAR_WINDOWS = tuple(
    (str(year), datetime(year, 1, 1, tzinfo=timezone.utc), datetime(year + 1, 1, 1, tzinfo=timezone.utc))
    for year in range(2020, 2026)
)
REGIME_WINDOWS = (
    ("BEAR_2018", datetime(2018, 1, 1, tzinfo=timezone.utc), datetime(2019, 1, 1, tzinfo=timezone.utc)),
    ("BULL_2021", datetime(2021, 1, 1, tzinfo=timezone.utc), datetime(2022, 1, 1, tzinfo=timezone.utc)),
    ("BEAR_2022", datetime(2022, 1, 1, tzinfo=timezone.utc), datetime(2023, 1, 1, tzinfo=timezone.utc)),
)
DIAGNOSTIC_WINDOWS = (*CALENDAR_YEAR_WINDOWS, *REGIME_WINDOWS)
RESULTS_PATH = Path("results") / "liquidity_sweep_results.csv"
BINANCE_ENDPOINTS = (
    "https://api.binance.com/api/v3/klines",
    "https://data-api.binance.vision/api/v3/klines",
)


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class BacktestResult:
    asset: str
    fee_regime: str
    split: str
    start: datetime
    end: datetime
    trades: int
    wins: int
    rr_values: list[float]
    returns: list[float]
    benchmark_returns: list[float]
    total_return: float
    sharpe: float | None
    max_drawdown: float
    benchmark_return: float
    benchmark_sharpe: float | None


class DataUnavailable(RuntimeError):
    pass


def utc_ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def parse_bar(row: list) -> Bar:
    return Bar(
        timestamp=datetime.fromtimestamp(int(row[0]) / 1000.0, tz=timezone.utc),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
    )


def request_klines(endpoint: str, symbol: str, start_ms: int, end_ms: int) -> list:
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    })
    request = urllib.request.Request(
        f"{endpoint}?{params}",
        headers={"User-Agent": "ant-colony-liquidity-sweep-lab/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and "code" in payload:
        raise DataUnavailable(f"Binance returned {payload}")
    if not isinstance(payload, list):
        raise DataUnavailable(f"Unexpected Binance payload type: {type(payload).__name__}")
    return payload


def fetch_binance_4h(symbol: str, start: datetime, end: datetime) -> list[Bar]:
    last_error: Exception | None = None
    for endpoint in BINANCE_ENDPOINTS:
        try:
            bars: list[Bar] = []
            cursor = utc_ms(start)
            end_ms = utc_ms(end)
            while cursor < end_ms:
                rows = request_klines(endpoint, symbol, cursor, end_ms)
                if not rows:
                    break
                parsed = [parse_bar(row) for row in rows]
                bars.extend(parsed)
                next_cursor = int(rows[-1][0]) + INTERVAL_MS
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                time.sleep(0.05)
            deduped = {bar.timestamp: bar for bar in bars}
            return sorted(deduped.values(), key=lambda bar: bar.timestamp)
        except (urllib.error.URLError, TimeoutError, DataUnavailable, OSError) as exc:
            last_error = exc
            continue
    raise DataUnavailable(f"Could not fetch {symbol}: {last_error}")


def window_bars(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [bar for bar in bars if start <= bar.timestamp < end]


def has_full_window_data(
    asset: str,
    bars: list[Bar],
    label: str,
    start: datetime,
    end: datetime,
) -> tuple[bool, str | None]:
    if not bars:
        return False, f"{asset}: no 4H bars returned"
    subset = window_bars(bars, start, end)
    expected = int((utc_ms(end) - utc_ms(start)) / INTERVAL_MS)
    if not subset:
        return False, f"{asset}: no bars for {label}"
    if subset[0].timestamp > start:
        return False, f"{asset}: {label} starts late at {subset[0].timestamp.isoformat()}"
    if subset[-1].timestamp < end - timedelta_ms(INTERVAL_MS):
        return False, f"{asset}: {label} ends early at {subset[-1].timestamp.isoformat()}"
    if len(subset) < expected:
        return False, f"{asset}: {label} has {len(subset)} bars, expected {expected}"
    return True, None


def timedelta_ms(value: int):
    from datetime import timedelta

    return timedelta(milliseconds=value)


def annualized_sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return None
    return mean / std * math.sqrt(ANNUAL_BARS)


def max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def compound_return(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value
    return equity - 1.0


def benchmark_returns(period_bars: list[Bar]) -> list[float]:
    values: list[float] = []
    for prev, current in zip(period_bars, period_bars[1:]):
        if prev.close > 0:
            values.append(current.close / prev.close - 1.0)
    return values


def run_strategy(
    asset: str,
    bars: list[Bar],
    *,
    start: datetime,
    end: datetime,
    fee: float,
    fee_regime: str,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> BacktestResult:
    equity = 1.0
    units = 0.0
    entry_price = 0.0
    stop_price = 0.0
    take_profit = 0.0
    initial_risk = 0.0
    in_position = False
    returns: list[float] = []
    rr_values: list[float] = []
    wins = 0
    trades = 0
    last_bar_in_period: Bar | None = None
    period_bars = window_bars(bars, start, end)

    for idx, bar in enumerate(bars):
        if bar.timestamp < start:
            continue
        if bar.timestamp >= end:
            break
        if idx < LOOKBACK_BARS:
            continue

        last_bar_in_period = bar
        equity_start = equity
        exited = False

        if in_position:
            exit_price: float | None = None
            hit_stop = bar.low <= stop_price
            hit_tp = bar.high >= take_profit
            # Worst case: if both stop and TP are reachable inside one OHLC bar,
            # assume the stop was hit first.
            if hit_stop:
                exit_price = stop_price
            elif hit_tp:
                exit_price = take_profit
            if exit_price is not None:
                equity = units * exit_price * (1.0 - fee)
                rr = (exit_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
                rr_values.append(rr)
                wins += 1 if rr > 0 else 0
                trades += 1
                units = 0.0
                in_position = False
                exited = True

        if not in_position and not exited:
            low30 = min(previous.low for previous in bars[idx - LOOKBACK_BARS:idx])
            buy_limit = low30 * LIMIT_DISCOUNT
            if bar.low <= buy_limit:
                fill_price = buy_limit * (1.0 + slippage_bps / 10_000.0)
                units = equity * (1.0 - fee) / fill_price
                entry_price = fill_price
                stop_price = fill_price * STOP_MULTIPLIER
                take_profit = low30 * TP_MULTIPLIER
                initial_risk = max(entry_price - stop_price, 0.0)
                in_position = True

                exit_price = None
                hit_stop = bar.low <= stop_price
                hit_tp = bar.high >= take_profit
                # Worst case: if both stop and TP are reachable inside one OHLC bar,
                # assume the stop was hit first.
                if hit_stop:
                    exit_price = stop_price
                elif hit_tp:
                    exit_price = take_profit
                if exit_price is not None:
                    equity = units * exit_price * (1.0 - fee)
                    rr = (exit_price - entry_price) / initial_risk if initial_risk > 0 else 0.0
                    rr_values.append(rr)
                    wins += 1 if rr > 0 else 0
                    trades += 1
                    units = 0.0
                    in_position = False
                else:
                    equity = units * bar.close

        elif in_position:
            equity = units * bar.close

        returns.append(equity / equity_start - 1.0 if equity_start > 0 else 0.0)

    if in_position and last_bar_in_period is not None:
        equity_start = equity
        equity = units * last_bar_in_period.close * (1.0 - fee)
        rr = (last_bar_in_period.close - entry_price) / initial_risk if initial_risk > 0 else 0.0
        rr_values.append(rr)
        wins += 1 if rr > 0 else 0
        trades += 1
        returns.append(equity / equity_start - 1.0 if equity_start > 0 else 0.0)

    bench_returns = benchmark_returns(period_bars)
    return BacktestResult(
        asset=asset,
        fee_regime=fee_regime,
        split="",
        start=start,
        end=end,
        trades=trades,
        wins=wins,
        rr_values=rr_values,
        returns=returns,
        benchmark_returns=bench_returns,
        total_return=compound_return(returns),
        sharpe=annualized_sharpe(returns),
        max_drawdown=max_drawdown(returns),
        benchmark_return=compound_return(bench_returns),
        benchmark_sharpe=annualized_sharpe(bench_returns),
    )


def split_train_test(bars: list[Bar]) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    main_bars = [bar for bar in bars if bar.timestamp >= MAIN_START]
    split_idx = int(len(main_bars) * TRAIN_FRACTION)
    if split_idx <= 0 or split_idx >= len(main_bars):
        raise DataUnavailable("not enough bars for 60/40 train/test split")
    train_start = main_bars[0].timestamp
    test_start = main_bars[split_idx].timestamp
    end = main_bars[-1].timestamp + timedelta_ms(INTERVAL_MS)
    return (train_start, test_start), (test_start, end)


def result_row(result: BacktestResult) -> dict[str, str]:
    win_rate = result.wins / result.trades if result.trades else 0.0
    avg_rr = sum(result.rr_values) / len(result.rr_values) if result.rr_values else 0.0
    sharpe_delta = (
        result.sharpe - result.benchmark_sharpe
        if result.sharpe is not None and result.benchmark_sharpe is not None
        else None
    )
    return {
        "asset": result.asset,
        "regime": result.fee_regime,
        "window": result.split,
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "trades": str(result.trades),
        "win_rate": f"{win_rate:.6f}",
        "avg_realized_rr": f"{avg_rr:.6f}",
        "total_return": f"{result.total_return:.6f}",
        "sharpe_annualized": "" if result.sharpe is None else f"{result.sharpe:.6f}",
        "max_drawdown": f"{result.max_drawdown:.6f}",
        "benchmark_return": f"{result.benchmark_return:.6f}",
        "benchmark_sharpe": "" if result.benchmark_sharpe is None else f"{result.benchmark_sharpe:.6f}",
        "sharpe_delta": "" if sharpe_delta is None else f"{sharpe_delta:.6f}",
    }


def pct(value: float) -> str:
    return f"{value * 100.0:,.2f}%"


def num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def print_table(results: list[BacktestResult], title: str = "=== RESULTATEN ===") -> None:
    print("")
    print(title)
    header = (
        f"{'Asset':<8} {'Regime':<11} {'Split':<10} {'Trades':>7} "
        f"{'Win%':>8} {'Avg R':>8} {'Return':>10} {'Sharpe':>8} "
        f"{'Max DD':>10} {'BH Ret':>10} {'BH Sh':>8} {'Delta':>8}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        win_rate = result.wins / result.trades if result.trades else 0.0
        avg_rr = sum(result.rr_values) / len(result.rr_values) if result.rr_values else 0.0
        delta = (
            result.sharpe - result.benchmark_sharpe
            if result.sharpe is not None and result.benchmark_sharpe is not None
            else None
        )
        print(
            f"{result.asset:<8} {result.fee_regime:<11} {result.split:<10} "
            f"{result.trades:>7} {pct(win_rate):>8} {avg_rr:>8.2f} "
            f"{pct(result.total_return):>10} {num(result.sharpe):>8} "
            f"{pct(result.max_drawdown):>10} {pct(result.benchmark_return):>10} "
            f"{num(result.benchmark_sharpe):>8} {num(delta):>8}"
        )


def write_results(results: list[BacktestResult]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    rows = [result_row(result) for result in results]
    with RESULTS_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV geschreven: {RESULTS_PATH}")


def print_verdicts(results: list[BacktestResult]) -> None:
    print("")
    print("=== GO/NO-GO VERDICT TESTSET ===")
    for asset in ASSETS:
        result = next(
            item for item in results
            if item.asset == asset and item.fee_regime == "maker" and item.split == "test"
        )
        win_rate = result.wins / result.trades if result.trades else 0.0
        delta = (
            result.sharpe - result.benchmark_sharpe
            if result.sharpe is not None and result.benchmark_sharpe is not None
            else None
        )
        if result.trades < 30:
            verdict = "INCONCLUSIVE — te weinig trades"
        elif (
            result.sharpe is not None
            and result.sharpe > 1.0
            and result.max_drawdown > -0.10
            and win_rate > 0.55
            and delta is not None
            and delta > 0
        ):
            verdict = "PLAUSIBEL"
        else:
            verdict = "AFGESCHREVEN"
        print(
            f"{asset}: {verdict} | trades={result.trades}, "
            f"Sharpe={num(result.sharpe)}, maxDD={pct(result.max_drawdown)}, "
            f"winRate={pct(win_rate)}, Sharpe-delta={num(delta)}"
        )


def run_train_test_for_asset(asset: str, bars: list[Bar], fee: float, fee_regime: str) -> list[BacktestResult]:
    (train_start, train_end), (test_start, test_end) = split_train_test(bars)
    windows = [
        ("train", train_start, train_end),
        ("test", test_start, test_end),
    ]
    results: list[BacktestResult] = []
    for label, start, end in windows:
        result = run_strategy(asset, bars, start=start, end=end, fee=fee, fee_regime=fee_regime)
        result.split = label
        results.append(result)
    return results


def run_diagnostic_windows_for_asset(
    asset: str,
    bars: list[Bar],
    windows: tuple[tuple[str, datetime, datetime], ...],
    fee: float,
    fee_regime: str,
) -> list[BacktestResult]:
    results: list[BacktestResult] = []
    for label, start, end in windows:
        result = run_strategy(asset, bars, start=start, end=end, fee=fee, fee_regime=fee_regime)
        result.split = label
        results.append(result)
    return results


def available_diagnostic_windows(asset: str, bars: list[Bar]) -> tuple[tuple[str, datetime, datetime], ...]:
    windows: list[tuple[str, datetime, datetime]] = []
    for label, start, end in DIAGNOSTIC_WINDOWS:
        ok, reason = has_full_window_data(asset, bars, label, start, end)
        if not ok:
            print(f"WINDOW {label} SKIPPED — DATA INSUFFICIENT | {reason}")
            continue
        windows.append((label, start, end))
    return tuple(windows)


def main() -> int:
    print("=== LAB LIQUIDITY SWEEP TEST ===")
    print("Geisoleerd script: geen colony-, data-, order-, brain- of live-integratie.")
    print(f"Assets: {', '.join(ASSETS)}")
    print("Bars: Binance public REST 4H klines; geen dagbar fallback.")
    print(f"Regel: rolling {LOOKBACK_BARS}-bar low, buy-limit low30*{LIMIT_DISCOUNT}, SL fill*0.98, TP low30_at_entry*1.01")
    print(f"Entry slippage: {DEFAULT_SLIPPAGE_BPS:.1f} bps tegen nadeel")
    print(f"Maker-fee run: {DEFAULT_MAKER_FEE:.4f} per kant")
    print(f"Taker-fee stress run: {TAKER_FEE:.4f} per kant")
    print("WAARSCHUWING: gegarandeerde maker-fills in een cascade zijn optimistisch; taker-run wordt apart getoond.")

    fetch_end = datetime.now(timezone.utc)
    data: dict[str, list[Bar]] = {}
    for asset, symbol in ASSETS.items():
        try:
            bars = fetch_binance_4h(symbol, REQUIRED_FETCH_START, fetch_end)
        except DataUnavailable as exc:
            bars = []
            print(f"DATA WARNING | {asset}: {exc}")
        data[asset] = bars
        first = bars[0].timestamp.isoformat() if bars else "n/a"
        last = bars[-1].timestamp.isoformat() if bars else "n/a"
        print(f"Loaded {asset} ({symbol}): bars={len(bars)} first={first} last={last}")

    results: list[BacktestResult] = []
    diagnostic_results: list[BacktestResult] = []
    for asset, bars in data.items():
        if not bars:
            continue
        results.extend(run_train_test_for_asset(asset, bars, DEFAULT_MAKER_FEE, "maker"))
        results.extend(run_train_test_for_asset(asset, bars, TAKER_FEE, "taker"))
        diagnostic_windows = available_diagnostic_windows(asset, bars)
        diagnostic_results.extend(run_diagnostic_windows_for_asset(asset, bars, diagnostic_windows, DEFAULT_MAKER_FEE, "maker"))
        diagnostic_results.extend(run_diagnostic_windows_for_asset(asset, bars, diagnostic_windows, TAKER_FEE, "taker"))

    print_table(results)
    print_table(diagnostic_results, "=== PER-JAAR / REGIME ===")
    write_results([*results, *diagnostic_results])
    print_verdicts(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
