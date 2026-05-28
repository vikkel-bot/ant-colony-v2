"""
One-off robustness test for volatility targeting across asset classes.

Strategies:
  A: buy-and-hold
  B: volatility target 15%, cap 1.0
  C: volatility target 15%, cap 1.5

Each asset is loaded from Yahoo Finance through the equities adapter. Data is
restricted to 2008-2024, split 60% train / 40% test by available trading days,
and the reported metrics are for the test period.

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


START_DATE = date(2008, 1, 1)
END_DATE = date(2024, 12, 31)
TRAIN_FRACTION = 0.60
TRADING_DAYS_PER_YEAR = 252
REALIZED_VOL_WINDOW = 20
TARGET_VOL = 0.15
REBALANCE_THRESHOLD = 0.10
CAP_B = 1.0
CAP_C = 1.5
FEE_PCT = 0.0010
SLIPPAGE_PCT = 0.0005
TRANSITION_COST_PCT = FEE_PCT + SLIPPAGE_PCT


@dataclass(frozen=True)
class AssetSpec:
    label: str
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class PriceBar:
    day: date
    close: float


@dataclass(frozen=True)
class DailyReturn:
    day: date
    value: float
    position: float
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


@dataclass(frozen=True)
class AssetResult:
    label: str
    symbol: str
    data_start: date
    data_end: date
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_bars: int
    test_bars: int
    metrics_a: Metrics
    metrics_b: Metrics
    metrics_c: Metrics
    run_b: StrategyRun
    run_c: StrategyRun


ASSETS = (
    AssetSpec("US large cap", ("SPY",)),
    AssetSpec("Emerging markets", ("EEM", "VWO")),
    AssetSpec("Developed ex-US", ("EFA",)),
    AssetSpec("Commodity/broad", ("DBC", "GLD")),
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def configure_yfinance_cache() -> None:
    """Keep yfinance's sqlite timezone cache inside the repo's ignored .tmp dir."""
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_voltarget_robustness"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir.resolve()))
    except Exception:
        return


def _market_data_to_bar(candle: MarketData) -> PriceBar | None:
    if candle.close <= 0:
        return None
    day = _utc(candle.timestamp).date()
    if day < START_DATE or day > END_DATE:
        return None
    return PriceBar(day=day, close=float(candle.close))


def load_asset_daily_bars(adapter: YahooFinanceAdapter, spec: AssetSpec) -> tuple[str, list[PriceBar]]:
    for symbol in spec.candidates:
        candles = adapter.get_candles(symbol, period="max", interval="1d")
        bars_by_day: dict[date, PriceBar] = {}
        for candle in candles:
            bar = _market_data_to_bar(candle)
            if bar is not None:
                bars_by_day[bar.day] = bar
        bars = [bars_by_day[d] for d in sorted(bars_by_day)]
        if bars:
            return symbol, bars
    return spec.candidates[0], []


def close_to_close_returns(bars: list[PriceBar]) -> list[float | None]:
    returns: list[float | None] = [None]
    for idx in range(1, len(bars)):
        returns.append(bars[idx].close / bars[idx - 1].close - 1.0)
    return returns


def realized_vols(bars: list[PriceBar]) -> list[float | None]:
    raw_returns = close_to_close_returns(bars)
    vols: list[float | None] = []
    for idx in range(len(bars)):
        if idx < REALIZED_VOL_WINDOW:
            vols.append(None)
            continue
        window_returns = raw_returns[idx - REALIZED_VOL_WINDOW + 1:idx + 1]
        values = [r for r in window_returns if r is not None]
        if len(values) < REALIZED_VOL_WINDOW:
            vols.append(None)
            continue
        mean = sum(values) / len(values)
        variance = sum((r - mean) ** 2 for r in values) / (len(values) - 1)
        vols.append(math.sqrt(variance) * math.sqrt(TRADING_DAYS_PER_YEAR))
    return vols


def target_position(target_vol: float, realized_vol: float | None, cap: float) -> float:
    if realized_vol is None or realized_vol <= 0 or not math.isfinite(realized_vol):
        return 0.0
    return min(cap, max(0.0, target_vol / realized_vol))


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
    cap: float,
    name: str,
) -> StrategyRun:
    vols = realized_vols(bars)
    current_position = 0.0
    returns: list[DailyReturn] = []
    transition_count = 0
    total_turnover = 0.0

    for idx in range(1, len(bars)):
        current_day = bars[idx].day
        if current_day < start_day:
            continue
        if current_day > end_day:
            break

        wanted_position = target_position(TARGET_VOL, vols[idx - 1], cap)
        turnover = 0.0
        switched = False
        if abs(wanted_position - current_position) > REBALANCE_THRESHOLD:
            turnover = abs(wanted_position - current_position)
            current_position = wanted_position
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
                switched=switched,
                turnover=turnover,
            )
        )

    return StrategyRun(
        name=name,
        returns=returns,
        transition_count=transition_count,
        total_turnover=total_turnover,
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


def run_asset(adapter: YahooFinanceAdapter, spec: AssetSpec) -> AssetResult | None:
    symbol, bars = load_asset_daily_bars(adapter, spec)
    if len(bars) < REALIZED_VOL_WINDOW + 50:
        print(f"{spec.label}: geen of te weinig data voor {spec.candidates}")
        return None

    split_idx = int(len(bars) * TRAIN_FRACTION)
    if split_idx <= REALIZED_VOL_WINDOW or split_idx >= len(bars) - 2:
        print(f"{spec.label}: split onmogelijk met {len(bars)} bars")
        return None

    train_start = bars[0].day
    train_end = bars[split_idx - 1].day
    test_start = bars[split_idx].day
    test_end = bars[-1].day
    train_bars = split_idx
    test_bars = len(bars) - split_idx

    run_a = buy_and_hold_returns(bars, start_day=test_start, end_day=test_end)
    run_b = voltarget_returns(
        bars,
        start_day=test_start,
        end_day=test_end,
        cap=CAP_B,
        name="B Vol target cap 1.0",
    )
    run_c = voltarget_returns(
        bars,
        start_day=test_start,
        end_day=test_end,
        cap=CAP_C,
        name="C Vol target cap 1.5",
    )

    return AssetResult(
        label=spec.label,
        symbol=symbol,
        data_start=bars[0].day,
        data_end=bars[-1].day,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        train_bars=train_bars,
        test_bars=test_bars,
        metrics_a=compute_metrics(run_a.returns, start_day=test_start, end_day=test_end),
        metrics_b=compute_metrics(run_b.returns, start_day=test_start, end_day=test_end),
        metrics_c=compute_metrics(run_c.returns, start_day=test_start, end_day=test_end),
        run_b=run_b,
        run_c=run_c,
    )


def _fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def _beats_on_sharpe(a: Metrics, c: Metrics) -> str:
    if a.sharpe is None or c.sharpe is None:
        return "n/a"
    return "YES" if c.sharpe > a.sharpe else "NO"


def print_asset_table(result: AssetResult) -> None:
    c_beats = _beats_on_sharpe(result.metrics_a, result.metrics_c)
    print("")
    print(f"=== {result.symbol} - {result.label} ===")
    print(
        f"Data: {result.data_start}..{result.data_end} | "
        f"Train: {result.train_start}..{result.train_end} ({result.train_bars} bars) | "
        f"Test: {result.test_start}..{result.test_end} ({result.test_bars} bars)"
    )
    print(f"C beats A on Sharpe: {c_beats}")
    print(f"{'Metric':<16} {'A Buy-hold':>14} {'B cap 1.0':>14} {'C cap 1.5':>14}")
    print("-" * 62)
    print(
        f"{'Sharpe':<16} "
        f"{_fmt_num(result.metrics_a.sharpe):>14} "
        f"{_fmt_num(result.metrics_b.sharpe):>14} "
        f"{_fmt_num(result.metrics_c.sharpe):>14}"
    )
    print(
        f"{'Max DD':<16} "
        f"{_fmt_pct(result.metrics_a.max_drawdown):>14} "
        f"{_fmt_pct(result.metrics_b.max_drawdown):>14} "
        f"{_fmt_pct(result.metrics_c.max_drawdown):>14}"
    )
    print(
        f"{'CAGR':<16} "
        f"{_fmt_pct(result.metrics_a.cagr):>14} "
        f"{_fmt_pct(result.metrics_b.cagr):>14} "
        f"{_fmt_pct(result.metrics_c.cagr):>14}"
    )
    print(
        f"{'Total return':<16} "
        f"{_fmt_pct(result.metrics_a.total_return):>14} "
        f"{_fmt_pct(result.metrics_b.total_return):>14} "
        f"{_fmt_pct(result.metrics_c.total_return):>14}"
    )
    print(
        f"{'Transitions':<16} "
        f"{'n/a':>14} "
        f"{result.run_b.transition_count:>14} "
        f"{result.run_c.transition_count:>14}"
    )


def print_summary(results: list[AssetResult]) -> None:
    print("")
    print("=== SUMMARY: C VS A ON TEST SHARPE ===")
    print(f"{'Asset':<8} {'Label':<20} {'A Sharpe':>10} {'C Sharpe':>10} {'C beats A':>10}")
    print("-" * 64)
    wins = 0
    comparable = 0
    for result in results:
        beats = _beats_on_sharpe(result.metrics_a, result.metrics_c)
        if beats != "n/a":
            comparable += 1
            wins += 1 if beats == "YES" else 0
        print(
            f"{result.symbol:<8} {result.label:<20} "
            f"{_fmt_num(result.metrics_a.sharpe):>10} "
            f"{_fmt_num(result.metrics_c.sharpe):>10} "
            f"{beats:>10}"
        )
    print(f"C beat A on Sharpe in {wins}/{comparable} comparable assets.")


def main() -> int:
    print("=== LAB VOLTARGET ROBUSTNESS TEST ===")
    print(f"Periode: {START_DATE} t/m {END_DATE}")
    print(f"Split: eerste {TRAIN_FRACTION:.0%} train, laatste {1.0 - TRAIN_FRACTION:.0%} test per asset")
    print("Strategie A: buy-and-hold")
    print("Strategie B: target_vol 15%, cap 1.0, no short")
    print("Strategie C: target_vol 15%, cap 1.5, no short")
    print("Realized vol: 20-daags, geannualiseerd; rebalance bij >10 procentpunt afwijking")
    print(f"Kosten B/C: 0.10% fee + 0.05% slippage per 100% turnover ({TRANSITION_COST_PCT * 100.0:.2f}%)")
    print("WAARSCHUWING: leverage-financieringskosten zijn nog niet meegenomen.")
    print("Loader: ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles")

    configure_yfinance_cache()
    adapter = YahooFinanceAdapter()
    results: list[AssetResult] = []
    for spec in ASSETS:
        result = run_asset(adapter, spec)
        if result is not None:
            results.append(result)
            print_asset_table(result)

    if not results:
        print("Geen resultaten; robustness test kan niet worden beoordeeld.")
        return 2

    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
