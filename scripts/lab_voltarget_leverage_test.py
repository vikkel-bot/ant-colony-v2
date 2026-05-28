"""
One-off lab test for SPY volatility targeting with controlled leverage.

This extends scripts/lab_voltarget_test.py with one deliberate change:
the volatility-targeted position is capped at 1.5 instead of 1.0, allowing
controlled leverage but no shorts.

No lab modules are modified; this script is console-only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from lab_voltarget_test import (
    BEAR_2022_END,
    BEAR_2022_START,
    COVID_END,
    COVID_START,
    END_DATE,
    FEE_PCT,
    REBALANCE_THRESHOLD,
    SLIPPAGE_PCT,
    START_DATE,
    SYMBOL,
    TEST_END,
    TEST_START,
    TRAIN_END,
    TRAIN_START,
    TRANSITION_COST_PCT,
    DailyReturn,
    Metrics,
    PriceBar,
    StrategyRun,
    TrainSweepRow,
    buy_and_hold_returns,
    compute_metrics,
    load_spy_daily_bars,
    realized_vols,
    select_best_train_target,
    voltarget_returns,
)


CAP_1X = 1.0
CAP_LEVERAGED = 1.5
LEVERAGED_TARGET_VOLS = (0.10, 0.12, 0.15)


@dataclass(frozen=True)
class LeverageStats:
    max_position: float
    pct_days_above_one: float


def target_position_for_vol_cap(
    target_vol: float,
    realized_vol: float | None,
    *,
    cap: float,
) -> float:
    if realized_vol is None or realized_vol <= 0 or not math.isfinite(realized_vol):
        return 0.0
    return min(cap, max(0.0, target_vol / realized_vol))


def voltarget_returns_with_cap(
    bars: list[PriceBar],
    *,
    start_day: date,
    end_day: date,
    target_vol: float,
    cap: float,
    label: str,
) -> StrategyRun:
    vols = realized_vols(bars)
    returns: list[DailyReturn] = []
    current_position = 0.0
    transition_count = 0
    total_turnover = 0.0

    for idx in range(1, len(bars)):
        current_day = bars[idx].day
        if current_day < start_day:
            continue
        if current_day > end_day:
            break

        signal_vol = vols[idx - 1]
        target_position = target_position_for_vol_cap(
            target_vol,
            signal_vol,
            cap=cap,
        )
        turnover = 0.0
        switched = False
        if abs(target_position - current_position) > REBALANCE_THRESHOLD:
            turnover = abs(target_position - current_position)
            current_position = target_position
            transition_count += 1
            total_turnover += turnover
            switched = True

        market_return = bars[idx].close / bars[idx - 1].close - 1.0
        gross_return = current_position * market_return
        cost = turnover * TRANSITION_COST_PCT
        net_return = (1.0 - cost) * (1.0 + gross_return) - 1.0
        returns.append(
            DailyReturn(
                day=current_day,
                value=net_return,
                position=current_position,
                target_position=target_position,
                realized_vol=signal_vol,
                switched=switched,
                turnover=turnover,
            )
        )

    return StrategyRun(
        name=label,
        returns=returns,
        transition_count=transition_count,
        total_turnover=total_turnover,
        target_vol=target_vol,
    )


def select_best_leveraged_train_target(
    bars: list[PriceBar],
) -> tuple[TrainSweepRow, list[TrainSweepRow]]:
    rows: list[TrainSweepRow] = []
    for target_vol in LEVERAGED_TARGET_VOLS:
        run = voltarget_returns_with_cap(
            bars,
            start_day=TRAIN_START,
            end_day=TRAIN_END,
            target_vol=target_vol,
            cap=CAP_LEVERAGED,
            label=f"C Vol target {target_vol:.0%} cap {CAP_LEVERAGED:.1f}",
        )
        metrics = compute_metrics(run.returns, start_day=TRAIN_START, end_day=TRAIN_END)
        rows.append(TrainSweepRow(target_vol=target_vol, run=run, metrics=metrics))

    best = max(
        rows,
        key=lambda row: (
            row.metrics.sharpe is not None,
            row.metrics.sharpe if row.metrics.sharpe is not None else float("-inf"),
            row.metrics.total_return,
        ),
    )
    return best, rows


def leverage_stats(run: StrategyRun) -> LeverageStats:
    if not run.returns:
        return LeverageStats(max_position=0.0, pct_days_above_one=0.0)
    positions = [r.position for r in run.returns]
    max_position = max(positions)
    days_above_one = sum(1 for position in positions if position > 1.0)
    return LeverageStats(
        max_position=max_position,
        pct_days_above_one=days_above_one / len(positions),
    )


def _slice_returns(
    returns: Iterable[DailyReturn],
    *,
    start_day: date,
    end_day: date,
) -> list[DailyReturn]:
    return [r for r in returns if start_day <= r.day <= end_day]


def _fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def print_train_sweep(rows: list[TrainSweepRow], best: TrainSweepRow) -> None:
    print("")
    print("=== TRAIN SWEEP C: CAP 1.5, 2000-2014 ===")
    print(f"{'Target vol':<12} {'Sharpe':>8} {'Max DD':>10} {'CAGR':>10} {'Return':>10} {'Transitions':>12}")
    print("-" * 68)
    for row in rows:
        marker = " <- gekozen" if row.target_vol == best.target_vol else ""
        print(
            f"{_fmt_pct(row.target_vol):<12} "
            f"{_fmt_num(row.metrics.sharpe):>8} "
            f"{_fmt_pct(row.metrics.max_drawdown):>10} "
            f"{_fmt_pct(row.metrics.cagr):>10} "
            f"{_fmt_pct(row.metrics.total_return):>10} "
            f"{row.run.transition_count:>12}{marker}"
        )


def print_generalization(best: TrainSweepRow, test_metrics: Metrics) -> None:
    train_sharpe = best.metrics.sharpe
    test_sharpe = test_metrics.sharpe
    degradation = None
    if train_sharpe is not None and test_sharpe is not None:
        degradation = train_sharpe - test_sharpe

    print("")
    print("=== GENERALISATIE C ===")
    print(f"Gekozen target_vol op train: {_fmt_pct(best.target_vol)}")
    print(f"Train Sharpe C: {_fmt_num(train_sharpe)}")
    print(f"Test Sharpe C:  {_fmt_num(test_sharpe)}")
    print(f"Degradatie train-test: {_fmt_num(degradation)}")


def print_test_comparison(
    metrics_a: Metrics,
    metrics_b: Metrics,
    metrics_c: Metrics,
    run_b: StrategyRun,
    run_c: StrategyRun,
) -> None:
    print("")
    print("=== TEST VERGELIJKING 2015-2024 ===")
    print(f"{'Metric':<18} {'A Buy-hold':>14} {'B cap 1.0':>14} {'C cap 1.5':>14}")
    print("-" * 64)
    print(
        f"{'Sharpe':<18} "
        f"{_fmt_num(metrics_a.sharpe):>14} "
        f"{_fmt_num(metrics_b.sharpe):>14} "
        f"{_fmt_num(metrics_c.sharpe):>14}"
    )
    print(
        f"{'Max DD':<18} "
        f"{_fmt_pct(metrics_a.max_drawdown):>14} "
        f"{_fmt_pct(metrics_b.max_drawdown):>14} "
        f"{_fmt_pct(metrics_c.max_drawdown):>14}"
    )
    print(
        f"{'CAGR':<18} "
        f"{_fmt_pct(metrics_a.cagr):>14} "
        f"{_fmt_pct(metrics_b.cagr):>14} "
        f"{_fmt_pct(metrics_c.cagr):>14}"
    )
    print(
        f"{'Total return':<18} "
        f"{_fmt_pct(metrics_a.total_return):>14} "
        f"{_fmt_pct(metrics_b.total_return):>14} "
        f"{_fmt_pct(metrics_c.total_return):>14}"
    )
    print(
        f"{'Transitions':<18} "
        f"{'n/a':>14} "
        f"{run_b.transition_count:>14} "
        f"{run_c.transition_count:>14}"
    )


def print_stress_periods(
    run_a: StrategyRun,
    run_b: StrategyRun,
    run_c: StrategyRun,
) -> None:
    periods = [
        ("2020 Covid", COVID_START, COVID_END),
        ("2022 bear", BEAR_2022_START, BEAR_2022_END),
    ]
    print("")
    print("=== TEST-STRESSPERIODES ===")
    header = (
        f"{'Periode':<12} {'Datums':<23} "
        f"{'A ret':>9} {'B ret':>9} {'C ret':>9} "
        f"{'A DD':>9} {'B DD':>9} {'C DD':>9} "
        f"{'A Sh':>6} {'B Sh':>6} {'C Sh':>6} {'C trans':>8}"
    )
    print(header)
    print("-" * len(header))
    for label, start_day, end_day in periods:
        a_returns = _slice_returns(run_a.returns, start_day=start_day, end_day=end_day)
        b_returns = _slice_returns(run_b.returns, start_day=start_day, end_day=end_day)
        c_returns = _slice_returns(run_c.returns, start_day=start_day, end_day=end_day)
        a_metrics = compute_metrics(a_returns, start_day=start_day, end_day=end_day)
        b_metrics = compute_metrics(b_returns, start_day=start_day, end_day=end_day)
        c_metrics = compute_metrics(c_returns, start_day=start_day, end_day=end_day)
        c_transitions = sum(1 for r in c_returns if r.switched)
        date_range = f"{start_day}..{end_day}"
        print(
            f"{label:<12} {date_range:<23} "
            f"{_fmt_pct(a_metrics.total_return):>9} "
            f"{_fmt_pct(b_metrics.total_return):>9} "
            f"{_fmt_pct(c_metrics.total_return):>9} "
            f"{_fmt_pct(a_metrics.max_drawdown):>9} "
            f"{_fmt_pct(b_metrics.max_drawdown):>9} "
            f"{_fmt_pct(c_metrics.max_drawdown):>9} "
            f"{_fmt_num(a_metrics.sharpe):>6} "
            f"{_fmt_num(b_metrics.sharpe):>6} "
            f"{_fmt_num(c_metrics.sharpe):>6} "
            f"{c_transitions:>8}"
        )


def print_leverage_report(stats: LeverageStats) -> None:
    print("")
    print("=== LEVERAGE C ===")
    print(f"Maximaal werkelijk positieniveau C: {stats.max_position:.2f}x")
    print(f"Percentage testdagen met C > 1.0x: {_fmt_pct(stats.pct_days_above_one)}")


def main() -> int:
    print("=== LAB VOLTARGET LEVERAGE TEST ===")
    print("Hypothese: SPY buy-and-hold versus volatility targeting met cap 1.5")
    print(f"Data: {START_DATE} t/m {END_DATE}")
    print(f"Train: {TRAIN_START} t/m {TRAIN_END}")
    print(f"Test:  {TEST_START} t/m {TEST_END}")
    print("Loader: ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles")
    print(
        "C: position = target_vol / realized_vol20, capped op 1.5, "
        "geen short, rebalance bij >10 procentpunt afwijking"
    )
    print(
        "Kosten B/C: 0.10% fee + 0.05% slippage per 100% turnover "
        f"({TRANSITION_COST_PCT * 100.0:.2f}%)"
    )
    print("Signaal-timing: positie voor handelsdag t gebruikt realized vol t-1")

    bars = load_spy_daily_bars()
    if not bars:
        print("Geen SPY daily bars geladen; test kan niet draaien.")
        return 2

    print(
        f"Bars geladen: symbol={SYMBOL} count={len(bars)} "
        f"eerste={bars[0].day} laatste={bars[-1].day}"
    )
    if bars[0].day.year > TRAIN_START.year or bars[-1].day < TEST_END:
        print("Onvoldoende SPY historie voor gevraagde train/test periode.")
        return 2

    best_b, _ = select_best_train_target(bars)
    best_c, c_sweep_rows = select_best_leveraged_train_target(bars)

    test_run_a = buy_and_hold_returns(bars, start_day=TEST_START, end_day=TEST_END)
    test_run_b = voltarget_returns(
        bars,
        start_day=TEST_START,
        end_day=TEST_END,
        target_vol=best_b.target_vol,
    )
    test_run_c = voltarget_returns_with_cap(
        bars,
        start_day=TEST_START,
        end_day=TEST_END,
        target_vol=best_c.target_vol,
        cap=CAP_LEVERAGED,
        label=f"C Vol target {best_c.target_vol:.0%} cap {CAP_LEVERAGED:.1f}",
    )

    test_metrics_a = compute_metrics(test_run_a.returns, start_day=TEST_START, end_day=TEST_END)
    test_metrics_b = compute_metrics(test_run_b.returns, start_day=TEST_START, end_day=TEST_END)
    test_metrics_c = compute_metrics(test_run_c.returns, start_day=TEST_START, end_day=TEST_END)
    stats_c = leverage_stats(test_run_c)

    print_train_sweep(c_sweep_rows, best_c)
    print("")
    print("=== B BASELINE UIT VORIGE CAP 1.0 TRAIN-SWEEP ===")
    print(f"Gekozen target_vol B: {_fmt_pct(best_b.target_vol)}")
    print(f"Train Sharpe B: {_fmt_num(best_b.metrics.sharpe)}")
    print_generalization(best_c, test_metrics_c)
    print_test_comparison(test_metrics_a, test_metrics_b, test_metrics_c, test_run_b, test_run_c)
    print_stress_periods(test_run_a, test_run_b, test_run_c)
    print_leverage_report(stats_c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
