"""
Robustness test for the Faber-style trend-following ETF basket.

This script reuses scripts/lab_faber_test.py and changes only the asset basket:
VTI, VWO, LQD, GLD, DBC. SPY is loaded only as the benchmark calendar/series.
"""

from __future__ import annotations

import math
from datetime import date

import lab_faber_test as faber


ASSETS = ("VTI", "VWO", "LQD", "GLD", "DBC")
BENCHMARK = "SPY"
LOAD_SYMBOLS = (BENCHMARK, *ASSETS)
ROBUST_SHARPE_CENTER = 0.70
ROBUST_SHARPE_BAND = 0.10
ROBUST_MAX_DD_FLOOR = -0.20


def _fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def configure_imported_faber_module() -> None:
    faber.ASSETS = ASSETS
    faber.TARGET_WEIGHT = 1.0 / len(ASSETS)
    faber.CACHE_DIR = faber.REPO_ROOT / ".tmp" / "lab_faber_robustness_cache"


def load_price_table() -> tuple[list[date], dict[str, dict[date, float]], dict[str, tuple[date | None, date | None]]]:
    faber.configure_yfinance_cache()
    adapter = faber.YahooFinanceAdapter()
    prices: dict[str, dict[date, float]] = {}
    spans: dict[str, tuple[date | None, date | None]] = {}

    for symbol in LOAD_SYMBOLS:
        bars = faber.load_asset_bars(symbol, adapter)
        prices[symbol] = {faber._bar_day(bar): float(bar.close) for bar in bars}
        first = min(prices[symbol]) if prices[symbol] else None
        last = max(prices[symbol]) if prices[symbol] else None
        spans[symbol] = (first, last)
        print(f"Loaded {symbol}: bars={len(prices[symbol])} first={first} last={last}")

    spy_days = sorted(day for day in prices[BENCHMARK] if faber.LOAD_START <= day <= faber.TEST_END)
    return spy_days, prices, spans


def print_history_warnings(spans: dict[str, tuple[date | None, date | None]]) -> None:
    print("")
    print("=== HISTORIECHECK ===")
    for symbol in ASSETS:
        first, last = spans.get(symbol, (None, None))
        starts_late = first is None or first > faber.TRAIN_START
        marker = "LET OP" if starts_late else "OK"
        print(
            f"{symbol}: first={first} last={last} | "
            f"start voor train {faber.TRAIN_START}: {marker}"
        )
    print("Periode wordt niet stilzwijgend aangepast; ontbrekende asset-data blijft cash tot signalen beschikbaar zijn.")


def print_period_report(
    label: str,
    *,
    start_day: date,
    end_day: date,
    basket: dict,
    basket_metrics: dict,
    benchmark_metrics: dict,
) -> None:
    print("")
    print(f"=== {label} {start_day} t/m {end_day} ===")
    print(f"{'Metric':<28} {'Mandje 2':>18} {'SPY buy-hold':>18}")
    print("-" * 68)
    print(f"{'CAGR':<28} {_fmt_pct(basket_metrics['cagr']):>18} {_fmt_pct(benchmark_metrics['cagr']):>18}")
    print(f"{'Sharpe':<28} {_fmt_num(basket_metrics['sharpe']):>18} {_fmt_num(benchmark_metrics['sharpe']):>18}")
    print(f"{'Max drawdown':<28} {_fmt_pct(basket_metrics['max_drawdown']):>18} {_fmt_pct(benchmark_metrics['max_drawdown']):>18}")
    print(f"{'Transactions':<28} {basket['transactions']:>18} {'n/a':>18}")
    print(f"{'Total fees paid':<28} {_fmt_pct(basket['fee_paid']):>18} {'0.00%':>18}")
    print(f"{'Total slippage paid':<28} {_fmt_pct(basket['slippage_paid']):>18} {'0.00%':>18}")
    print(f"{'Avg total cash allocation':<28} {_fmt_pct(basket['avg_cash_total']):>18} {'n/a':>18}")


def print_robustness_judgement(test_metrics: dict) -> None:
    test_sharpe = test_metrics["sharpe"]
    test_max_dd = test_metrics["max_drawdown"]
    low = ROBUST_SHARPE_CENTER - ROBUST_SHARPE_BAND
    high = ROBUST_SHARPE_CENTER + ROBUST_SHARPE_BAND
    sharpe_condition = test_sharpe is not None and low <= test_sharpe <= high
    drawdown_condition = test_max_dd > ROBUST_MAX_DD_FLOOR
    robust = sharpe_condition and drawdown_condition

    print("")
    print("=== ROBUUSTHEIDSOORDEEL ===")
    print(f"Test Sharpe mandje 2: {_fmt_num(test_sharpe)}")
    print(f"Voorwaarde 1, Sharpe binnen 0.60-0.80: {sharpe_condition}")
    print(f"Test max DD mandje 2: {_fmt_pct(test_max_dd)}")
    print(f"Voorwaarde 2, max DD beter dan -20%: {drawdown_condition}")
    print(f"Oordeel: {'ROBUUST' if robust else 'NIET ROBUUST'}")


def main() -> int:
    configure_imported_faber_module()

    print("=== LAB FABER ROBUSTNESS TEST ===")
    print("Doel: Faber-logica uit lab_faber_test.py herhalen op onafhankelijk mandje 2")
    print(f"Mandje 2 assets: {', '.join(ASSETS)}")
    print(f"Benchmark: {BENCHMARK} buy-and-hold, geen fees")
    print(f"Train: {faber.TRAIN_START} t/m {faber.TRAIN_END}")
    print(f"Test:  {faber.TEST_START} t/m {faber.TEST_END}")
    print(f"Trendfilter: close > {faber.MA_DAYS}-daags MA, elk asset max {faber.TARGET_WEIGHT:.0%}, cash verdient 0%")
    print(f"Rebalance/evaluatie: elke {faber.REBALANCE_DAYS} handelsdagen")
    print(
        "Kosten mandje via BacktestConfig: "
        f"fee_pct={faber.COST_CONFIG.fee_pct:.4f}, "
        f"slippage_pct={faber.COST_CONFIG.slippage_pct:.4f}"
    )
    print("Loader: ant_colony.lab.edge_audit.load_bars_for_asset met Yahoo max fallback")

    calendar, prices, spans = load_price_table()
    if not calendar:
        print("Geen SPY kalenderdata geladen; robustness-test kan niet draaien.")
        return 2
    print_history_warnings(spans)

    train_basket = faber.run_faber_period(calendar, prices, start_day=faber.TRAIN_START, end_day=faber.TRAIN_END)
    train_benchmark = faber.run_spy_benchmark(calendar, prices, start_day=faber.TRAIN_START, end_day=faber.TRAIN_END)
    train_basket_metrics = faber.compute_metrics(train_basket["returns"], start_day=faber.TRAIN_START, end_day=faber.TRAIN_END)
    train_benchmark_metrics = faber.compute_metrics(train_benchmark, start_day=faber.TRAIN_START, end_day=faber.TRAIN_END)

    test_basket = faber.run_faber_period(calendar, prices, start_day=faber.TEST_START, end_day=faber.TEST_END)
    test_benchmark = faber.run_spy_benchmark(calendar, prices, start_day=faber.TEST_START, end_day=faber.TEST_END)
    test_basket_metrics = faber.compute_metrics(test_basket["returns"], start_day=faber.TEST_START, end_day=faber.TEST_END)
    test_benchmark_metrics = faber.compute_metrics(test_benchmark, start_day=faber.TEST_START, end_day=faber.TEST_END)

    print_period_report(
        "TRAIN",
        start_day=faber.TRAIN_START,
        end_day=faber.TRAIN_END,
        basket=train_basket,
        basket_metrics=train_basket_metrics,
        benchmark_metrics=train_benchmark_metrics,
    )
    print_period_report(
        "TEST",
        start_day=faber.TEST_START,
        end_day=faber.TEST_END,
        basket=test_basket,
        basket_metrics=test_basket_metrics,
        benchmark_metrics=test_benchmark_metrics,
    )
    print_robustness_judgement(test_basket_metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
