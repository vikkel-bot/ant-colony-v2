"""
One-off lab test for 12-month ETF momentum.

Hypothesis:
On an ETF basket, 12-month price momentum (buy top 3, equal weight, rebalance
monthly) delivers a higher risk-adjusted return than buy-and-hold SPY after
costs.

Train and test are reported separately and never combined.
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
from ant_colony.lab.backtester import BacktestConfig, OHLCVBar

try:
    from ant_colony.lab.edge_audit import load_bars_for_asset as edge_load_bars
except Exception:
    edge_load_bars = None


ASSETS = ("SPY", "QQQ", "EEM", "GLD", "TLT", "IWM", "DBC")
LOAD_START = date(2004, 1, 1)
TRAIN_START = date(2005, 1, 1)
TRAIN_END = date(2016, 12, 31)
TEST_START = date(2017, 1, 1)
TEST_END = date(2023, 12, 31)
LOOKBACK_DAYS = 252
REBALANCE_DAYS = 21
TOP_N = 3
TRADING_DAYS_PER_YEAR = 252
CACHE_DIR = REPO_ROOT / ".tmp" / "lab_momentum_cache"
COST_CONFIG = BacktestConfig(
    direction="long",
    take_profit_pct=0.01,
    stop_loss_pct=0.01,
    max_bars_held=1,
    fee_pct=0.0025,
    slippage_pct=0.0010,
)


@dataclass(frozen=True)
class Metrics:
    cagr: float | None
    sharpe: float | None
    max_drawdown: float
    total_return: float


@dataclass(frozen=True)
class MomentumRun:
    returns: list[tuple[date, float]]
    transactions: int
    fee_paid: float
    slippage_paid: float

    @property
    def total_cost_paid(self) -> float:
        return self.fee_paid + self.slippage_paid


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def configure_yfinance_cache() -> None:
    """Keep yfinance's sqlite timezone cache inside the repo's ignored .tmp dir."""
    try:
        import yfinance as yf

        cache_dir = REPO_ROOT / ".tmp" / "yfinance_cache" / "lab_momentum"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(cache_dir.resolve()))
    except Exception:
        return


def _market_data_to_bar(candle: MarketData) -> OHLCVBar | None:
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        return None
    day = _utc(candle.timestamp).date()
    if day < LOAD_START or day > TEST_END:
        return None
    return OHLCVBar(
        timestamp=_utc(candle.timestamp),
        open=float(candle.open),
        high=float(candle.high),
        low=float(candle.low),
        close=float(candle.close),
        volume=float(candle.volume),
    )


def _bar_day(bar: OHLCVBar) -> date:
    return _utc(bar.timestamp).date()


def _edge_loader_has_enough_history(bars: list[OHLCVBar]) -> bool:
    if not bars:
        return False
    return _bar_day(min(bars, key=lambda b: b.timestamp)) <= TRAIN_START


def load_asset_bars(symbol: str, adapter: YahooFinanceAdapter) -> list[OHLCVBar]:
    from_dt = datetime(LOAD_START.year, LOAD_START.month, LOAD_START.day, tzinfo=timezone.utc)
    to_dt = datetime(TEST_END.year, TEST_END.month, TEST_END.day, 23, 59, 59, tzinfo=timezone.utc)

    bars: list[OHLCVBar] = []
    if edge_load_bars is not None:
        try:
            bars = edge_load_bars(
                symbol,
                timeframe="1d",
                from_dt=from_dt,
                to_dt=to_dt,
                cache_dir=CACHE_DIR,
                use_network=True,
            )
            bars = [
                bar for bar in bars
                if LOAD_START <= _bar_day(bar) <= TEST_END and bar.close > 0
            ]
        except Exception:
            bars = []
    if _edge_loader_has_enough_history(bars):
        return sorted(bars, key=lambda b: b.timestamp)

    candles = adapter.get_candles(symbol, period="max", interval="1d")
    fallback_bars: list[OHLCVBar] = []
    for candle in candles:
        bar = _market_data_to_bar(candle)
        if bar is not None:
            fallback_bars.append(bar)
    return sorted(fallback_bars, key=lambda b: b.timestamp)


def load_price_table() -> tuple[list[date], dict[str, dict[date, float]]]:
    configure_yfinance_cache()
    adapter = YahooFinanceAdapter()
    prices: dict[str, dict[date, float]] = {}
    for symbol in ASSETS:
        bars = load_asset_bars(symbol, adapter)
        prices[symbol] = {_bar_day(bar): float(bar.close) for bar in bars}
        first = min(prices[symbol]) if prices[symbol] else None
        last = max(prices[symbol]) if prices[symbol] else None
        print(f"Loaded {symbol}: bars={len(prices[symbol])} first={first} last={last}")

    spy_days = sorted(day for day in prices["SPY"] if LOAD_START <= day <= TEST_END)
    return spy_days, prices


def _price(prices: dict[str, dict[date, float]], symbol: str, day: date) -> float | None:
    return prices.get(symbol, {}).get(day)


def _return_between(
    prices: dict[str, dict[date, float]],
    symbol: str,
    previous_day: date,
    current_day: date,
) -> float | None:
    previous_price = _price(prices, symbol, previous_day)
    current_price = _price(prices, symbol, current_day)
    if previous_price is None or current_price is None or previous_price <= 0:
        return None
    return current_price / previous_price - 1.0


def _momentum_scores(
    calendar: list[date],
    prices: dict[str, dict[date, float]],
    previous_idx: int,
) -> dict[str, float]:
    if previous_idx < LOOKBACK_DAYS:
        return {}
    current_day = calendar[previous_idx]
    lookback_day = calendar[previous_idx - LOOKBACK_DAYS]
    scores: dict[str, float] = {}
    for symbol in ASSETS:
        current_price = _price(prices, symbol, current_day)
        lookback_price = _price(prices, symbol, lookback_day)
        if current_price is None or lookback_price is None or lookback_price <= 0:
            continue
        scores[symbol] = current_price / lookback_price - 1.0
    return scores


def _target_weights(scores: dict[str, float]) -> dict[str, float]:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    selected = [symbol for symbol, _ in ranked[:TOP_N]]
    if not selected:
        return {symbol: 0.0 for symbol in ASSETS}
    weight = 1.0 / len(selected)
    return {symbol: (weight if symbol in selected else 0.0) for symbol in ASSETS}


def run_momentum_period(
    calendar: list[date],
    prices: dict[str, dict[date, float]],
    *,
    start_day: date,
    end_day: date,
) -> MomentumRun:
    current_weights = {symbol: 0.0 for symbol in ASSETS}
    returns: list[tuple[date, float]] = []
    transactions = 0
    fee_paid = 0.0
    slippage_paid = 0.0
    equity = 1.0
    last_rebalance_idx: int | None = None

    for idx in range(1, len(calendar)):
        current_day = calendar[idx]
        previous_day = calendar[idx - 1]
        if current_day < start_day:
            continue
        if current_day > end_day:
            break

        if last_rebalance_idx is None or idx - last_rebalance_idx >= REBALANCE_DAYS:
            scores = _momentum_scores(calendar, prices, idx - 1)
            target_weights = _target_weights(scores)
            asset_turnovers = {
                symbol: abs(target_weights[symbol] - current_weights[symbol])
                for symbol in ASSETS
            }
            traded_assets = [symbol for symbol, turnover in asset_turnovers.items() if turnover > 1e-12]
            turnover = sum(asset_turnovers.values())
            if turnover > 0:
                transactions += len(traded_assets)
                fee = equity * turnover * COST_CONFIG.fee_pct
                slippage = equity * turnover * COST_CONFIG.slippage_pct
                fee_paid += fee
                slippage_paid += slippage
                equity -= fee + slippage
                current_weights = target_weights
            last_rebalance_idx = idx

        portfolio_return = 0.0
        for symbol, weight in current_weights.items():
            if weight == 0:
                continue
            asset_return = _return_between(prices, symbol, previous_day, current_day)
            if asset_return is not None:
                portfolio_return += weight * asset_return

        before_return_equity = equity
        equity *= 1.0 + portfolio_return
        daily_return = equity / before_return_equity - 1.0 if before_return_equity > 0 else 0.0
        returns.append((current_day, daily_return))

        if equity > 0:
            gross_values = {}
            for symbol, weight in current_weights.items():
                asset_return = _return_between(prices, symbol, previous_day, current_day)
                gross_values[symbol] = weight * (1.0 + (asset_return or 0.0))
            total_weight_value = sum(gross_values.values())
            cash_weight = max(0.0, 1.0 - sum(current_weights.values()))
            denominator = total_weight_value + cash_weight
            if denominator > 0:
                current_weights = {
                    symbol: gross_values[symbol] / denominator
                    for symbol in ASSETS
                }

    return MomentumRun(
        returns=returns,
        transactions=transactions,
        fee_paid=fee_paid,
        slippage_paid=slippage_paid,
    )


def run_spy_benchmark(
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
        spy_return = _return_between(prices, "SPY", previous_day, current_day)
        if spy_return is not None:
            returns.append((current_day, spy_return))
    return returns


def compute_metrics(returns: list[tuple[date, float]], *, start_day: date, end_day: date) -> Metrics:
    if not returns:
        return Metrics(cagr=None, sharpe=None, max_drawdown=0.0, total_return=0.0)

    values = [value for _, value in returns]
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
        cagr=cagr,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        total_return=total_return,
    )


def _fmt_num(value: float | None, decimals: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{decimals}f}"


def _fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100.0:,.2f}%"


def print_period_report(
    label: str,
    *,
    start_day: date,
    end_day: date,
    momentum: MomentumRun,
    momentum_metrics: Metrics,
    benchmark_metrics: Metrics,
) -> None:
    print("")
    print(f"=== {label} {start_day} t/m {end_day} ===")
    print(f"{'Metric':<24} {'Momentum top-3':>18} {'SPY buy-hold':>18}")
    print("-" * 62)
    print(f"{'CAGR':<24} {_fmt_pct(momentum_metrics.cagr):>18} {_fmt_pct(benchmark_metrics.cagr):>18}")
    print(f"{'Sharpe ratio':<24} {_fmt_num(momentum_metrics.sharpe):>18} {_fmt_num(benchmark_metrics.sharpe):>18}")
    print(f"{'Max drawdown':<24} {_fmt_pct(momentum_metrics.max_drawdown):>18} {_fmt_pct(benchmark_metrics.max_drawdown):>18}")
    print(f"{'Total return':<24} {_fmt_pct(momentum_metrics.total_return):>18} {_fmt_pct(benchmark_metrics.total_return):>18}")
    print(f"{'Transactions':<24} {momentum.transactions:>18} {'n/a':>18}")
    print(f"{'Total fees paid':<24} {_fmt_pct(momentum.fee_paid):>18} {'0.00%':>18}")
    print(f"{'Total slippage paid':<24} {_fmt_pct(momentum.slippage_paid):>18} {'0.00%':>18}")
    print(f"{'Total costs paid':<24} {_fmt_pct(momentum.total_cost_paid):>18} {'0.00%':>18}")


def print_conclusion(test_momentum_metrics: Metrics, test_benchmark_metrics: Metrics) -> None:
    sharpe_ok = test_momentum_metrics.sharpe is not None and test_momentum_metrics.sharpe > 0.6
    cagr_outperformance = (
        test_momentum_metrics.cagr is not None
        and test_benchmark_metrics.cagr is not None
        and test_momentum_metrics.cagr > test_benchmark_metrics.cagr
    )
    verdict = "GESTEUND" if sharpe_ok and cagr_outperformance else "VERWORPEN"
    cagr_delta = None
    if test_momentum_metrics.cagr is not None and test_benchmark_metrics.cagr is not None:
        cagr_delta = test_momentum_metrics.cagr - test_benchmark_metrics.cagr

    print("")
    print("=== CONCLUSIE TEST-PERIODE ===")
    print(f"Hypothese: {verdict}")
    print(f"Criteria: Sharpe > 0.6 = {sharpe_ok}; CAGR outperformance vs SPY = {cagr_outperformance}")
    print(f"Test CAGR outperformance momentum - SPY: {_fmt_pct(cagr_delta)}")


def main() -> int:
    print("=== LAB MOMENTUM TEST ===")
    print("Hypothese: 12-maands momentum top-3 ETF-mandje vs buy-and-hold SPY")
    print(f"Assets: {', '.join(ASSETS)}")
    print(f"Train: {TRAIN_START} t/m {TRAIN_END}")
    print(f"Test:  {TEST_START} t/m {TEST_END}")
    print("Momentum: 252 handelsdagen lookback, top-3 gelijkgewogen, rebalance elke 21 handelsdagen")
    print(f"Kosten momentum via BacktestConfig: fee_pct={COST_CONFIG.fee_pct:.4f}, slippage_pct={COST_CONFIG.slippage_pct:.4f}")
    print("Benchmark: SPY buy-and-hold, geen fees")
    print("Loader: ant_colony.lab.edge_audit.load_bars_for_asset met Yahoo max fallback")

    calendar, prices = load_price_table()
    if not calendar:
        print("Geen SPY kalenderdata geladen; momentumtest kan niet draaien.")
        return 2

    train_momentum = run_momentum_period(calendar, prices, start_day=TRAIN_START, end_day=TRAIN_END)
    train_benchmark = run_spy_benchmark(calendar, prices, start_day=TRAIN_START, end_day=TRAIN_END)
    train_momentum_metrics = compute_metrics(train_momentum.returns, start_day=TRAIN_START, end_day=TRAIN_END)
    train_benchmark_metrics = compute_metrics(train_benchmark, start_day=TRAIN_START, end_day=TRAIN_END)

    test_momentum = run_momentum_period(calendar, prices, start_day=TEST_START, end_day=TEST_END)
    test_benchmark = run_spy_benchmark(calendar, prices, start_day=TEST_START, end_day=TEST_END)
    test_momentum_metrics = compute_metrics(test_momentum.returns, start_day=TEST_START, end_day=TEST_END)
    test_benchmark_metrics = compute_metrics(test_benchmark, start_day=TEST_START, end_day=TEST_END)

    print_period_report(
        "TRAIN",
        start_day=TRAIN_START,
        end_day=TRAIN_END,
        momentum=train_momentum,
        momentum_metrics=train_momentum_metrics,
        benchmark_metrics=train_benchmark_metrics,
    )
    print_period_report(
        "TEST",
        start_day=TEST_START,
        end_day=TEST_END,
        momentum=test_momentum,
        momentum_metrics=test_momentum_metrics,
        benchmark_metrics=test_benchmark_metrics,
    )
    print_conclusion(test_momentum_metrics, test_benchmark_metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
