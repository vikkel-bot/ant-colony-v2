"""
One-off lab test for SPY volatility targeting with a train/test split.

Hypothesis:
Volatility targeting can beat buy-and-hold SPY on a risk-adjusted basis when
parameters are selected on 2000-2014 train data and run once on 2015-2024 test
data.

Data:
SPY daily Yahoo Finance candles loaded through the equities adapter.

No lab modules are modified; this script is console-only.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData


SYMBOL = "SPY"
START_DATE = date(2000, 1, 1)
END_DATE = date(2024, 12, 31)
TRAIN_START = date(2000, 1, 1)
TRAIN_END = date(2014, 12, 31)
TEST_START = date(2015, 1, 1)
TEST_END = date(2024, 12, 31)
COVID_START = date(2020, 2, 19)
COVID_END = date(2020, 3, 23)
BEAR_2022_START = date(2022, 1, 3)
BEAR_2022_END = date(2022, 12, 30)

TRADING_DAYS_PER_YEAR = 252
REALIZED_VOL_WINDOW = 20
REBALANCE_THRESHOLD = 0.10
TARGET_VOLS = (0.10, 0.12, 0.15, 0.20)
FEE_PCT = 0.0010
SLIPPAGE_PCT = 0.0005
TRANSITION_COST_PCT = FEE_PCT + SLIPPAGE_PCT


@dataclass(frozen=True)
class PriceBar:
    day: date
    close: float


@dataclass(frozen=True)
class DailyReturn:
    day: date
    value: float
    position: float
    target_position: float = 0.0
    realized_vol: float | None = None
    switched: bool = False
    turnover: float = 0.0


@dataclass(frozen=True)
class Metrics:
    sharpe: float | None
    max_drawdown: float
    cagr: float | None
    total_return: float


@dataclass(frozen=True)
class StrategyRun:
    name: str
    returns: list[DailyReturn]
    transition_count: int = 0
    total_turnover: float = 0.0
    target_vol: float | None = None


@dataclass(frozen=True)
class TrainSweepRow:
    target_vol: float
    run: StrategyRun
    metrics: Metrics


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _market_data_to_bar(candle: MarketData) -> PriceBar | None:
    if candle.close <= 0:
        return None
    day = _utc(candle.timestamp).date()
    if day < START_DATE or day > END_DATE:
        return None
    return PriceBar(day=day, close=float(candle.close))


def configure_yfinance_cache() -> None:
    """Keep yfinance's sqlite timezone cache inside the repo's ignored .tmp dir."""
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_voltarget"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir.resolve()))
    except Exception:
        return


def load_spy_daily_bars() -> list[PriceBar]:
    configure_yfinance_cache()
    adapter = YahooFinanceAdapter()
    candles = adapter.get_candles(SYMBOL, period="max", interval="1d")
    bars_by_day: dict[date, PriceBar] = {}
    for candle in candles:
        bar = _market_data_to_bar(candle)
        if bar is not None:
            bars_by_day[bar.day] = bar
    return [bars_by_day[d] for d in sorted(bars_by_day)]


def close_to_close_returns(bars: list[PriceBar]) -> list[float | None]:
    returns: list[float | None] = [None]
    for idx in range(1, len(bars)):
        returns.append(bars[idx].close / bars[idx - 1].close - 1.0)
    return returns


def realized_vols(bars: list[PriceBar], window: int = REALIZED_VOL_WINDOW) -> list[float | None]:
    raw_returns = close_to_close_returns(bars)
    vols: list[float | None] = []
    for idx in range(len(bars)):
        if idx < window:
            vols.append(None)
            continue
        window_returns = raw_returns[idx - window + 1:idx + 1]
        values = [r for r in window_returns if r is not None]
        if len(values) < window:
            vols.append(None)
            continue
        mean = sum(values) / len(values)
        variance = sum((r - mean) ** 2 for r in values) / (len(values) - 1)
        vols.append(math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR))
    return vols


def target_position_for_vol(target_vol: float, realized_vol: float | None) -> float:
    if realized_vol is None or realized_vol <= 0 or not math.isfinite(realized_vol):
        return 0.0
    return min(1.0, max(0.0, target_vol / realized_vol))


def buy_and_hold_returns(
    bars: list[PriceBar],
    *,
    start_day: date,
    end_day: date,
) -> StrategyRun:
    returns: list[DailyReturn] = []
    for idx in range(1, len(bars)):
        current_day = bars[idx].day
        if current_day < start_day:
            continue
        if current_day > end_day:
            break
        value = bars[idx].close / bars[idx - 1].close - 1.0
        returns.append(DailyReturn(day=current_day, value=value, position=1.0))
    return StrategyRun(name="A Buy-and-hold", returns=returns)


def voltarget_returns(
    bars: list[PriceBar],
    *,
    start_day: date,
    end_day: date,
    target_vol: float,
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
        target_position = target_position_for_vol(target_vol, signal_vol)
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
        name=f"B Vol target {target_vol:.0%}",
        returns=returns,
        transition_count=transition_count,
        total_turnover=total_turnover,
        target_vol=target_vol,
    )


def compute_metrics(
    returns: list[DailyReturn],
    *,
    start_day: date,
    end_day: date,
) -> Metrics:
    if not returns:
        return Metrics(sharpe=None, max_drawdown=0.0, cagr=None, total_return=0.0)

    values = [r.value for r in returns]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        max_drawdown = min(max_drawdown, drawdown)

    total_return = equity - 1.0
    years = max((end_day - start_day).days / 365.25, len(values) / TRADING_DAYS_PER_YEAR)
    cagr = (equity ** (1.0 / years) - 1.0) if years > 0 and equity > 0 else None

    sharpe = None
    if len(values) >= 2:
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        std = math.sqrt(variance)
        if std > 0:
            sharpe = mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)

    return Metrics(
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        cagr=cagr,
        total_return=total_return,
    )


def _slice_returns(
    returns: Iterable[DailyReturn],
    *,
    start_day: date,
    end_day: date,
) -> list[DailyReturn]:
    return [r for r in returns if start_day <= r.day <= end_day]


def select_best_train_target(bars: list[PriceBar]) -> tuple[TrainSweepRow, list[TrainSweepRow]]:
    rows: list[TrainSweepRow] = []
    for target_vol in TARGET_VOLS:
        run = voltarget_returns(
            bars,
            start_day=TRAIN_START,
            end_day=TRAIN_END,
            target_vol=target_vol,
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
    print("=== TRAIN SWEEP 2000-2014 ===")
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


def print_test_comparison(metrics_a: Metrics, metrics_b: Metrics, run_b: StrategyRun) -> None:
    print("")
    print("=== TEST VERGELIJKING 2015-2024 ===")
    print(f"{'Metric':<18} {'A Buy-and-hold':>18} {'B Vol target':>18}")
    print("-" * 56)
    print(f"{'Sharpe':<18} {_fmt_num(metrics_a.sharpe):>18} {_fmt_num(metrics_b.sharpe):>18}")
    print(f"{'Max DD':<18} {_fmt_pct(metrics_a.max_drawdown):>18} {_fmt_pct(metrics_b.max_drawdown):>18}")
    print(f"{'CAGR':<18} {_fmt_pct(metrics_a.cagr):>18} {_fmt_pct(metrics_b.cagr):>18}")
    print(f"{'Total return':<18} {_fmt_pct(metrics_a.total_return):>18} {_fmt_pct(metrics_b.total_return):>18}")
    print(f"{'Transitions':<18} {'n/a':>18} {run_b.transition_count:>18}")
    print(f"{'Total turnover':<18} {'n/a':>18} {run_b.total_turnover:>18.2f}")


def print_generalization(best: TrainSweepRow, test_metrics: Metrics) -> None:
    train_sharpe = best.metrics.sharpe
    test_sharpe = test_metrics.sharpe
    degradation = None
    if train_sharpe is not None and test_sharpe is not None:
        degradation = train_sharpe - test_sharpe

    print("")
    print("=== GENERALISATIE ===")
    print(f"Gekozen target_vol op train: {_fmt_pct(best.target_vol)}")
    print(f"Train Sharpe B: {_fmt_num(train_sharpe)}")
    print(f"Test Sharpe B:  {_fmt_num(test_sharpe)}")
    print(f"Degradatie train-test: {_fmt_num(degradation)}")


def print_test_stress_periods(run_a: StrategyRun, run_b: StrategyRun) -> None:
    periods = [
        ("2020 Covid", COVID_START, COVID_END),
        ("2022 bear", BEAR_2022_START, BEAR_2022_END),
    ]
    print("")
    print("=== B IN TEST-STRESSPERIODES ===")
    header = (
        f"{'Periode':<12} {'Datums':<23} {'A return':>10} "
        f"{'B return':>10} {'B max DD':>10} {'B Sharpe':>9} {'B transitions':>13}"
    )
    print(header)
    print("-" * len(header))
    for label, start_day, end_day in periods:
        a_returns = _slice_returns(run_a.returns, start_day=start_day, end_day=end_day)
        b_returns = _slice_returns(run_b.returns, start_day=start_day, end_day=end_day)
        a_metrics = compute_metrics(a_returns, start_day=start_day, end_day=end_day)
        b_metrics = compute_metrics(b_returns, start_day=start_day, end_day=end_day)
        transitions = sum(1 for r in b_returns if r.switched)
        date_range = f"{start_day}..{end_day}"
        print(
            f"{label:<12} {date_range:<23} "
            f"{_fmt_pct(a_metrics.total_return):>10} "
            f"{_fmt_pct(b_metrics.total_return):>10} "
            f"{_fmt_pct(b_metrics.max_drawdown):>10} "
            f"{_fmt_num(b_metrics.sharpe):>9} "
            f"{transitions:>13}"
        )


def print_verdict(metrics_a: Metrics, metrics_b: Metrics) -> None:
    sharper = (
        metrics_a.sharpe is not None
        and metrics_b.sharpe is not None
        and metrics_b.sharpe > metrics_a.sharpe
    )
    lower_drawdown = abs(metrics_b.max_drawdown) < abs(metrics_a.max_drawdown)
    verdict = "GESTEUND" if sharper and lower_drawdown else "NIET GESTEUND"

    print("")
    print("=== HYPOTHESE OP TEST ===")
    print(
        f"{verdict}: B heeft "
        f"{'een hogere' if sharper else 'geen hogere'} Sharpe dan A en "
        f"{'een lagere' if lower_drawdown else 'geen lagere'} max drawdown dan A."
    )


def main() -> int:
    print("=== LAB VOLTARGET TEST ===")
    print("Hypothese: SPY buy-and-hold versus volatility targeting")
    print(f"Data: {START_DATE} t/m {END_DATE}")
    print(f"Train: {TRAIN_START} t/m {TRAIN_END}")
    print(f"Test:  {TEST_START} t/m {TEST_END}")
    print("Loader: ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles")
    print(
        "Strategie B: position = target_vol / realized_vol20, capped op 1.0, "
        "rebalance bij >10 procentpunt afwijking"
    )
    print(
        "Kosten B: 0.10% fee + 0.05% slippage per 100% turnover "
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

    best, sweep_rows = select_best_train_target(bars)
    test_run_a = buy_and_hold_returns(bars, start_day=TEST_START, end_day=TEST_END)
    test_run_b = voltarget_returns(
        bars,
        start_day=TEST_START,
        end_day=TEST_END,
        target_vol=best.target_vol,
    )
    test_metrics_a = compute_metrics(test_run_a.returns, start_day=TEST_START, end_day=TEST_END)
    test_metrics_b = compute_metrics(test_run_b.returns, start_day=TEST_START, end_day=TEST_END)

    print_train_sweep(sweep_rows, best)
    print_generalization(best, test_metrics_b)
    print_test_comparison(test_metrics_a, test_metrics_b, test_run_b)
    print_test_stress_periods(test_run_a, test_run_b)
    print_verdict(test_metrics_a, test_metrics_b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
