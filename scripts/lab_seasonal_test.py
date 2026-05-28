"""
One-off lab test for the S&P 500 "Sell in May" seasonal pattern.

Hypothesis:
A strategy that is long S&P 500 from November 1 through April 30 and cash
from May 1 through October 31 beats buy-and-hold on a risk-adjusted basis over
2000-2024.

Data:
SPY daily Yahoo Finance candles loaded through the equities adapter, with
^GSPC as fallback.

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
START_DATE = date(2000, 1, 1)
END_DATE = date(2024, 12, 31)
TRADING_DAYS_PER_YEAR = 252
TRANSITION_FEE_PCT = 0.0010


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
    cagr: float | None
    total_return: float


@dataclass(frozen=True)
class StrategyRun:
    name: str
    returns: list[DailyReturn]
    switch_count: int = 0


@dataclass(frozen=True)
class SeasonalYearRow:
    label: str
    start_day: date
    end_day: date
    return_a: float
    return_b: float

    @property
    def diff(self) -> float:
        return self.return_b - self.return_a

    @property
    def b_wins(self) -> bool:
        return self.diff > 0.0


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

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_seasonal"
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


def is_seasonally_long(day: date) -> bool:
    return day.month >= 11 or day.month <= 4


def buy_and_hold_returns(bars: list[PriceBar]) -> StrategyRun:
    returns: list[DailyReturn] = []
    for idx in range(1, len(bars)):
        value = bars[idx].close / bars[idx - 1].close - 1.0
        returns.append(DailyReturn(day=bars[idx].day, value=value, position=1))
    return StrategyRun(name="A Buy-and-hold", returns=returns)


def seasonal_returns(bars: list[PriceBar]) -> StrategyRun:
    if not bars:
        return StrategyRun(name="B Sell in May", returns=[])

    previous_position = 1 if is_seasonally_long(bars[0].day) else 0
    returns: list[DailyReturn] = []
    switch_count = 0

    for idx in range(1, len(bars)):
        current_day = bars[idx].day
        target_position = 1 if is_seasonally_long(current_day) else 0
        switched = target_position != previous_position
        if switched:
            switch_count += 1
            previous_position = target_position

        market_return = bars[idx].close / bars[idx - 1].close - 1.0
        held_return = market_return if previous_position == 1 else 0.0
        cost = TRANSITION_FEE_PCT if switched else 0.0
        net_return = (1.0 - cost) * (1.0 + held_return) - 1.0
        returns.append(
            DailyReturn(
                day=current_day,
                value=net_return,
                position=previous_position,
                switched=switched,
            )
        )

    return StrategyRun(name="B Sell in May", returns=returns, switch_count=switch_count)


def compound_return(returns: Iterable[DailyReturn]) -> float:
    equity = 1.0
    for daily_return in returns:
        equity *= 1.0 + daily_return.value
    return equity - 1.0


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


def build_seasonal_year_rows(run_a: StrategyRun, run_b: StrategyRun) -> list[SeasonalYearRow]:
    rows: list[SeasonalYearRow] = []
    for end_year in range(2001, 2025):
        start_day = date(end_year - 1, 11, 1)
        end_day = date(end_year, 10, 31)
        a_returns = _slice_returns(run_a.returns, start_day=start_day, end_day=end_day)
        b_returns = _slice_returns(run_b.returns, start_day=start_day, end_day=end_day)
        if not a_returns or not b_returns:
            continue
        rows.append(
            SeasonalYearRow(
                label=f"{end_year}",
                start_day=start_day,
                end_day=end_day,
                return_a=compound_return(a_returns),
                return_b=compound_return(b_returns),
            )
        )
    return rows


def stddev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


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
    print("=== VERGELIJKING 2000-2024 ===")
    print(f"{'Metric':<18} {'A Buy-and-hold':>18} {'B Sell in May':>18}")
    print("-" * 56)
    print(f"{'Sharpe':<18} {_fmt_num(metrics_a.sharpe):>18} {_fmt_num(metrics_b.sharpe):>18}")
    print(f"{'Max DD':<18} {_fmt_pct(metrics_a.max_drawdown):>18} {_fmt_pct(metrics_b.max_drawdown):>18}")
    print(f"{'CAGR':<18} {_fmt_pct(metrics_a.cagr):>18} {_fmt_pct(metrics_b.cagr):>18}")
    print(f"{'Total return':<18} {_fmt_pct(metrics_a.total_return):>18} {_fmt_pct(metrics_b.total_return):>18}")
    print(f"{'Transitions':<18} {'n/a':>18} {switch_count:>18}")


def print_year_by_year(rows: list[SeasonalYearRow]) -> None:
    diffs = [row.diff for row in rows]
    wins = sum(1 for row in rows if row.b_wins)
    diff_std = stddev(diffs)

    print("")
    print("=== JAAR-VOOR-JAAR: VOLLEDIGE NOV-OKT SEIZOENSJAREN ===")
    print(f"{'Seizoen':<8} {'Periode':<23} {'Return A':>10} {'Return B':>10} {'B-A':>10} {'Winnaar':>9}")
    print("-" * 77)
    for row in rows:
        winner = "B" if row.b_wins else "A"
        period = f"{row.start_day}..{row.end_day}"
        print(
            f"{row.label:<8} {period:<23} "
            f"{_fmt_pct(row.return_a):>10} {_fmt_pct(row.return_b):>10} "
            f"{_fmt_pct(row.diff):>10} {winner:>9}"
        )

    print("")
    print(f"B versloeg A in {wins}/{len(rows)} volledige seizoensjaren.")
    print(f"Standaarddeviatie van jaarlijks verschil (B-A): {_fmt_pct(diff_std)}")
    if diff_std is not None and diff_std > 0.10:
        print("Variantiesignaal: groot; het seizoenseffect is jaar-op-jaar niet stabiel.")
    elif diff_std is not None:
        print("Variantiesignaal: relatief beperkt; het jaar-op-jaar verschil is minder grillig.")


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
    print("=== LAB SEASONAL TEST ===")
    print("Hypothese: SPY buy-and-hold versus Sell in May long/cash")
    print(f"Periode: {START_DATE} t/m {END_DATE}")
    print("Loader: ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles")
    print("Strategie B: long 1 november t/m 30 april; cash 1 mei t/m 31 oktober")
    print(f"Kosten B: {TRANSITION_FEE_PCT * 100.0:.2f}% per long/cash-transitie")

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

    run_a = buy_and_hold_returns(bars)
    run_b = seasonal_returns(bars)
    metrics_a = compute_metrics(run_a.returns, start_day=bars[0].day, end_day=bars[-1].day)
    metrics_b = compute_metrics(run_b.returns, start_day=bars[0].day, end_day=bars[-1].day)
    yearly_rows = build_seasonal_year_rows(run_a, run_b)

    print_comparison_table(metrics_a, metrics_b, run_b.switch_count)
    print_year_by_year(yearly_rows)
    print_hypothesis_verdict(metrics_a, metrics_b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
