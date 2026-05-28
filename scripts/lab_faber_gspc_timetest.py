"""
Single-asset time robustness test for the Faber trend-cash switch.

This script reuses scripts/lab_faber_test.py for the Faber calculation and
changes the universe to one clean yfinance source: ^GSPC.
"""

from __future__ import annotations

import math
from datetime import date

import lab_faber_test as faber


SYMBOL = "^GSPC"
PERIOD_START = date(1990, 1, 1)
PERIOD_END = date(2004, 12, 31)
LOAD_START = date(1900, 1, 1)
MAX_DD_FLOOR = -0.30


def _fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def configure_imported_faber_module() -> None:
    faber.ASSETS = (SYMBOL,)
    faber.TARGET_WEIGHT = 1.0
    faber.LOAD_START = LOAD_START
    faber.TRAIN_START = PERIOD_START
    faber.TRAIN_END = PERIOD_END
    faber.TEST_START = PERIOD_START
    faber.TEST_END = PERIOD_END
    faber.CACHE_DIR = faber.REPO_ROOT / ".tmp" / "lab_faber_gspc_timetest_cache"


def load_gspc_prices() -> tuple[list[date], dict[str, dict[date, float]], date | None, date | None]:
    faber.configure_yfinance_cache()
    adapter = faber.YahooFinanceAdapter()
    bars = faber.load_asset_bars(SYMBOL, adapter)
    prices = {SYMBOL: {faber._bar_day(bar): float(bar.close) for bar in bars}}
    first = min(prices[SYMBOL]) if prices[SYMBOL] else None
    last = max(prices[SYMBOL]) if prices[SYMBOL] else None
    calendar = sorted(day for day in prices[SYMBOL] if LOAD_START <= day <= PERIOD_END)
    return calendar, prices, first, last


def run_buy_hold(
    calendar: list[date],
    prices: dict[str, dict[date, float]],
    *,
    start_day: date,
    end_day: date,
) -> list[tuple[date, float]]:
    returns: list[tuple[date, float]] = []
    for idx in range(1, len(calendar)):
        current_day = calendar[idx]
        previous_day = calendar[idx - 1]
        if current_day < start_day:
            continue
        if current_day > end_day:
            break
        daily_return = faber._return_between(prices, SYMBOL, previous_day, current_day)
        if daily_return is not None:
            returns.append((current_day, daily_return))
    return returns


def print_period_report(faber_result: dict, faber_metrics: dict, buy_hold_metrics: dict) -> None:
    print("")
    print(f"=== TEST {PERIOD_START} t/m {PERIOD_END} ===")
    print(f"{'Metric':<28} {'Faber ^GSPC':>18} {'Buy-hold ^GSPC':>18}")
    print("-" * 68)
    print(f"{'CAGR':<28} {_fmt_pct(faber_metrics['cagr']):>18} {_fmt_pct(buy_hold_metrics['cagr']):>18}")
    print(f"{'Sharpe':<28} {_fmt_num(faber_metrics['sharpe']):>18} {_fmt_num(buy_hold_metrics['sharpe']):>18}")
    print(f"{'Max drawdown':<28} {_fmt_pct(faber_metrics['max_drawdown']):>18} {_fmt_pct(buy_hold_metrics['max_drawdown']):>18}")
    print(f"{'Transactions':<28} {faber_result['transactions']:>18} {'n/a':>18}")
    print(f"{'Total fees paid':<28} {_fmt_pct(faber_result['fee_paid']):>18} {'0.00%':>18}")
    print(f"{'Total slippage paid':<28} {_fmt_pct(faber_result['slippage_paid']):>18} {'0.00%':>18}")
    print(f"{'Total costs paid':<28} {_fmt_pct(faber_result['fee_paid'] + faber_result['slippage_paid']):>18} {'0.00%':>18}")
    print(f"{'Avg time in cash':<28} {_fmt_pct(faber_result['avg_cash_total']):>18} {'0.00%':>18}")


def print_time_robustness_judgement(faber_metrics: dict, buy_hold_metrics: dict) -> None:
    faber_sharpe = faber_metrics["sharpe"]
    buy_hold_sharpe = buy_hold_metrics["sharpe"]
    faber_max_dd = faber_metrics["max_drawdown"]

    sharpe_condition = (
        faber_sharpe is not None
        and buy_hold_sharpe is not None
        and faber_sharpe >= buy_hold_sharpe
    )
    drawdown_condition = faber_max_dd > MAX_DD_FLOOR
    robust = sharpe_condition and drawdown_condition

    print("")
    print("=== TIJDSROBUUSTHEIDSOORDEEL ===")
    print(f"Faber Sharpe: {_fmt_num(faber_sharpe)}")
    print(f"Buy-and-hold Sharpe: {_fmt_num(buy_hold_sharpe)}")
    print(f"Voorwaarde 1, Faber-Sharpe >= buy-and-hold-Sharpe: {sharpe_condition}")
    print(f"Faber max DD: {_fmt_pct(faber_max_dd)}")
    print(f"Voorwaarde 2, Faber max DD beter dan -30%: {drawdown_condition}")
    print(f"Oordeel: {'TIJDSROBUUST' if robust else 'NIET TIJDSROBUUST'}")


def main() -> int:
    configure_imported_faber_module()

    print("=== LAB FABER ^GSPC TIMETEST ===")
    print("Doel: Faber trend-cash-switch testen in 1990-2004 op een single asset")
    print(f"Asset: {SYMBOL}")
    print("Databron: yfinance via bestaande lab-loader; geen ETF-proxy en geen gemengde bronnen")
    print(f"Periode: {PERIOD_START} t/m {PERIOD_END}; geen train/test split")
    print(f"Warmup start voor MA: {LOAD_START}")
    print(f"Trendfilter: close > {faber.MA_DAYS}-daags MA, long 100% anders cash")
    print(f"Rebalance/evaluatie: elke {faber.REBALANCE_DAYS} handelsdagen")
    print(
        "Kosten Faber via BacktestConfig: "
        f"fee_pct={faber.COST_CONFIG.fee_pct:.4f}, "
        f"slippage_pct={faber.COST_CONFIG.slippage_pct:.4f}"
    )
    print("Vergelijking: Faber-^GSPC vs buy-and-hold ^GSPC, buy-and-hold zonder fees")

    calendar, prices, first, last = load_gspc_prices()
    print(f"Loaded {SYMBOL}: bars={len(prices[SYMBOL])} first={first} last={last}")
    print(f"Werkelijke startdatum {SYMBOL} data: {first}")
    if first is None:
        print("Geen ^GSPC data geladen; timetest kan niet draaien.")
        return 2
    if first > PERIOD_START:
        print(f"LET OP: data start na gevraagde periode-start {PERIOD_START}; periode wordt niet aangepast.")
    else:
        print(f"Historiecheck: data start op/voor gevraagde periode-start {PERIOD_START}.")

    faber_result = faber.run_faber_period(calendar, prices, start_day=PERIOD_START, end_day=PERIOD_END)
    buy_hold_returns = run_buy_hold(calendar, prices, start_day=PERIOD_START, end_day=PERIOD_END)
    faber_metrics = faber.compute_metrics(faber_result["returns"], start_day=PERIOD_START, end_day=PERIOD_END)
    buy_hold_metrics = faber.compute_metrics(buy_hold_returns, start_day=PERIOD_START, end_day=PERIOD_END)

    print_period_report(faber_result, faber_metrics, buy_hold_metrics)
    print_time_robustness_judgement(faber_metrics, buy_hold_metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
