"""
One-off lab test for a simple SPY SMA200 regime filter.

Hypothesis:
A simple regime filter (long only when close > 200-day moving average,
otherwise cash) on the S&P 500 beats buy-and-hold on a risk-adjusted basis
over 2015-2024.

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


PRIMARY_SYMBOL = "SPY"
FALLBACK_SYMBOL = "^GSPC"
START_DATE = date(2015, 1, 1)
END_DATE = date(2024, 12, 31)
SMA_WINDOW = 200
TRADING_DAYS_PER_YEAR = 252

# Conservative equity costs used elsewhere in the repo.
EQUITY_FEE_PCT = 0.0010
SLIPPAGE_PCT = 0.0010
TRANSITION_COST_PCT = EQUITY_FEE_PCT + SLIPPAGE_PCT


@dataclass(frozen=True)
class PriceBar:
    day: date
    close: float


@dataclass(frozen=True)
class DailyReturn:
    day: date
    value: float
    position: int
    switched: bool = False


@dataclass(frozen=True)
class Metrics:
    sharpe: float | None
    max_drawdown: float
    total_return: float
    cagr: float | None


@dataclass(frozen=True)
class StrategyRun:
    name: str
    returns: list[DailyReturn]
    switch_count: int = 0


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

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_regime_filter"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir.resolve()))
    except Exception:
        return


def load_spy_daily_bars() -> tuple[str, list[PriceBar]]:
    configure_yfinance_cache()
    adapter = YahooFinanceAdapter()
    for symbol in (PRIMARY_SYMBOL, FALLBACK_SYMBOL):
        candles = adapter.get_candles(symbol, period="max", interval="1d")
        bars_by_day: dict[date, PriceBar] = {}
        for candle in candles:
            bar = _market_data_to_bar(candle)
            if bar is not None:
                bars_by_day[bar.day] = bar
        bars = [bars_by_day[d] for d in sorted(bars_by_day)]
        if bars:
            return symbol, bars
    return PRIMARY_SYMBOL, []


def rolling_sma(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = []
    running_sum = 0.0
    for idx, value in enumerate(values):
        running_sum += value
        if idx >= window:
            running_sum -= values[idx - window]
        if idx >= window - 1:
            result.append(running_sum / window)
        else:
            result.append(None)
    return result


def buy_and_hold_returns(bars: list[PriceBar]) -> StrategyRun:
    returns: list[DailyReturn] = []
    for idx in range(1, len(bars)):
        ret = bars[idx].close / bars[idx - 1].close - 1.0
        returns.append(DailyReturn(day=bars[idx].day, value=ret, position=1))
    return StrategyRun(name="A Buy-and-hold", returns=returns)


def regime_filter_returns(bars: list[PriceBar]) -> StrategyRun:
    closes = [bar.close for bar in bars]
    smas = rolling_sma(closes, SMA_WINDOW)
    signals = [
        1 if sma is not None and close > sma else 0
        for close, sma in zip(closes, smas)
    ]

    returns: list[DailyReturn] = []
    previous_position = 0
    switch_count = 0

    for idx in range(1, len(bars)):
        target_position = signals[idx - 1]
        switched = target_position != previous_position
        if switched:
            switch_count += 1
            previous_position = target_position

        market_return = bars[idx].close / bars[idx - 1].close - 1.0
        held_return = market_return if previous_position == 1 else 0.0
        cost = TRANSITION_COST_PCT if switched else 0.0
        net_return = (1.0 - cost) * (1.0 + held_return) - 1.0
        returns.append(
            DailyReturn(
                day=bars[idx].day,
                value=net_return,
                position=previous_position,
                switched=switched,
            )
        )

    return StrategyRun(
        name=f"B Regime filter: close > SMA{SMA_WINDOW}",
        returns=returns,
        switch_count=switch_count,
    )


def compute_metrics(
    returns: list[DailyReturn],
    *,
    start_day: date,
    end_day: date,
) -> Metrics:
    if not returns:
        return Metrics(sharpe=None, max_drawdown=0.0, total_return=0.0, cagr=None)

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
        total_return=total_return,
        cagr=cagr,
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


def print_comparison_table(metrics_a: Metrics, metrics_b: Metrics, switch_count: int) -> None:
    print("")
    print("=== VERGELIJKING 2015-2024 ===")
    print(f"{'Metric':<18} {'A Buy-and-hold':>18} {'B Regime-filter':>18}")
    print("-" * 56)
    print(f"{'Sharpe':<18} {_fmt_num(metrics_a.sharpe):>18} {_fmt_num(metrics_b.sharpe):>18}")
    print(f"{'Max DD':<18} {_fmt_pct(metrics_a.max_drawdown):>18} {_fmt_pct(metrics_b.max_drawdown):>18}")
    print(f"{'Total return':<18} {_fmt_pct(metrics_a.total_return):>18} {_fmt_pct(metrics_b.total_return):>18}")
    print(f"{'CAGR':<18} {_fmt_pct(metrics_a.cagr):>18} {_fmt_pct(metrics_b.cagr):>18}")
    print(f"{'Regime switches':<18} {'n/a':>18} {switch_count:>18}")


def print_stress_periods(run_a: StrategyRun, run_b: StrategyRun) -> None:
    periods = [
        ("2018 Q4 crash", date(2018, 10, 1), date(2018, 12, 31)),
        ("2020 Covid crash", date(2020, 2, 19), date(2020, 3, 23)),
        ("2022 bear", date(2022, 1, 3), date(2022, 12, 30)),
    ]
    print("")
    print("=== STRESS-PERIODES ===")
    header = (
        f"{'Periode':<19} {'Datums':<23} "
        f"{'Return A':>10} {'Return B':>10} "
        f"{'Max DD A':>10} {'Max DD B':>10} {'Sharpe A':>9} {'Sharpe B':>9} {'Switches':>8}"
    )
    print(header)
    print("-" * len(header))
    for label, start_day, end_day in periods:
        a_returns = _slice_returns(run_a.returns, start_day=start_day, end_day=end_day)
        b_returns = _slice_returns(run_b.returns, start_day=start_day, end_day=end_day)
        metrics_a = compute_metrics(a_returns, start_day=start_day, end_day=end_day)
        metrics_b = compute_metrics(b_returns, start_day=start_day, end_day=end_day)
        switches = sum(1 for r in b_returns if r.switched)
        date_range = f"{start_day}..{end_day}"
        print(
            f"{label:<19} {date_range:<23} "
            f"{_fmt_pct(metrics_a.total_return):>10} {_fmt_pct(metrics_b.total_return):>10} "
            f"{_fmt_pct(metrics_a.max_drawdown):>10} {_fmt_pct(metrics_b.max_drawdown):>10} "
            f"{_fmt_num(metrics_a.sharpe):>9} {_fmt_num(metrics_b.sharpe):>9} {switches:>8}"
        )


def print_hypothesis_verdict(metrics_a: Metrics, metrics_b: Metrics) -> None:
    sharper = (
        metrics_a.sharpe is not None
        and metrics_b.sharpe is not None
        and metrics_b.sharpe > metrics_a.sharpe
    )
    lower_drawdown = abs(metrics_b.max_drawdown) < abs(metrics_a.max_drawdown)
    verdict = "GESTEUND" if sharper and lower_drawdown else "NIET GESTEUND"

    print("")
    print("=== HYPOTHESE ===")
    print(
        f"{verdict}: B heeft "
        f"{'een hogere' if sharper else 'geen hogere'} Sharpe dan A en "
        f"{'een lagere' if lower_drawdown else 'geen lagere'} max drawdown dan A."
    )


def main() -> int:
    print("=== LAB REGIME FILTER TEST ===")
    print(
        "Hypothese: SPY buy-and-hold versus long/cash regime-filter "
        "op close > SMA200"
    )
    print(f"Periode: {START_DATE} t/m {END_DATE}")
    print("Loader: ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles")
    print(
        "Kosten B: 0.10% equity fee + 0.10% slippage = "
        f"{TRANSITION_COST_PCT * 100.0:.2f}% per in/uit-transitie"
    )
    print("Signaal-timing: positie voor handelsdag t gebruikt close/SMA200 van handelsdag t-1")

    symbol, bars = load_spy_daily_bars()
    if not bars:
        print("Geen SPY/^GSPC daily bars geladen; test kan niet draaien.")
        return 2
    if symbol != PRIMARY_SYMBOL:
        print(f"SPY data niet beschikbaar; fallback gebruikt: {symbol}")
    print(
        f"Bars geladen: symbol={symbol} count={len(bars)} "
        f"eerste={bars[0].day} laatste={bars[-1].day}"
    )
    if len(bars) < SMA_WINDOW + 2:
        print(f"Te weinig bars voor SMA{SMA_WINDOW}; test stopt.")
        return 2

    run_a = buy_and_hold_returns(bars)
    run_b = regime_filter_returns(bars)
    metrics_a = compute_metrics(run_a.returns, start_day=bars[0].day, end_day=bars[-1].day)
    metrics_b = compute_metrics(run_b.returns, start_day=bars[0].day, end_day=bars[-1].day)

    print_comparison_table(metrics_a, metrics_b, run_b.switch_count)
    print_stress_periods(run_a, run_b)
    print_hypothesis_verdict(metrics_a, metrics_b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
