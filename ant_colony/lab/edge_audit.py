"""
ant_colony/lab/edge_audit.py

Read-only edge audit tooling for ANT COLONY v2.

This module deliberately does not change live trading behaviour. It reuses the
existing Backtester entry-signal logic where possible, adds audit-only fee,
slippage, R-multiple and walk-forward accounting, and writes structured reports
that can be used before changing thresholds or pipeline routing.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.lab.backtester import Backtester, OHLCVBar
from ant_colony.lab.watchtower_backtester import load_watchtower_signals
from ant_colony.paper.paper_ledger import BROKER_FEE_PCT
from ant_colony.strategies.mean_reversion_v2 import MeanReversionV2Parameters


CRYPTO_ASSETS = (
    "BTC-EUR",
    "ETH-EUR",
    "SOL-EUR",
    "XRP-EUR",
    "ADA-EUR",
    "LINK-EUR",
    "DOT-EUR",
    "LTC-EUR",
)

EQUITY_ASSETS = (
    "XLK",
    "XLE",
    "XLV",
    "XLF",
    "XLI",
    "XLB",
    "XLP",
    "XLY",
    "XLU",
    "XLRE",
    "XLC",
    "AAPL",
    "MSFT",
    "JNJ",
    "KO",
    "QQQ",
    "GLD",
)

EQUITY_3YR_ASSETS = (
    "XLU",
    "XLK",
    "XLRE",
    "XLE",
    "XLB",
    "XLF",
    "XLP",
    "XLI",
    "XLY",
    "AAPL",
    "MSFT",
    "GLD",
    "JNJ",
    "QQQ",
)

EQUITY_3YR_REGIMES = (
    "ALL",
    "RISK_ON",
    "RISK_OFF",
    "VOLATILE_BULL",
    "SIDEWAYS",
)

CRYPTO_MR_ASSETS = CRYPTO_ASSETS

CRYPTO_MR_REGIMES = (
    "ALL",
    "TRENDING",
    "SIDEWAYS",
    "HIGH_VOLATILITY",
)


@dataclass(frozen=True)
class AuditAssumptions:
    """Execution assumptions used only for audit reports."""

    starting_equity: float = 10_000.0
    risk_per_trade: float = 0.01
    fee_pct_per_side: float = BROKER_FEE_PCT
    slippage_pct: float = 0.0010
    risk_free_rate: float = 0.0
    benchmark_annual_return: float = 0.106
    atr_period: int = 14
    atr_stop_multiple: float = 1.5
    watchtower_take_profit_r: float = 2.0
    watchtower_max_hold_bars: int = 120


@dataclass(frozen=True)
class StrategySpec:
    name: str
    strategy_type: str
    direction: str
    take_profit_pct: float
    stop_loss_pct: float
    max_bars_held: int
    entry_variant: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditTrade:
    strategy: str
    asset: str
    biome: str
    direction: str
    regime: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    bars_held: int
    gross_return_pct: float
    net_return_pct: float
    pnl_r: float
    pnl_eur: float
    window_id: str
    source: str = "matrix"
    signal_id: str | None = None
    confidence: float | None = None
    entry_score: float | None = None
    theme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "asset": self.asset,
            "biome": self.biome,
            "direction": self.direction,
            "regime": self.regime,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": round(self.entry_price, 8),
            "exit_price": round(self.exit_price, 8),
            "exit_reason": self.exit_reason,
            "bars_held": self.bars_held,
            "gross_return_pct": round(self.gross_return_pct, 8),
            "net_return_pct": round(self.net_return_pct, 8),
            "pnl_r": round(self.pnl_r, 6),
            "pnl_eur": round(self.pnl_eur, 6),
            "window_id": self.window_id,
            "source": self.source,
            "signal_id": self.signal_id,
            "confidence": self.confidence,
            "entry_score": self.entry_score,
            "theme": self.theme,
        }


def default_strategy_specs() -> list[StrategySpec]:
    """Strategies present in the current codebase, expressed as audit specs."""

    base = [
        ("sma_crossover", "sma_crossover", 0.06, 0.03, 20),
        ("rsi_based", "rsi_based", 0.04, 0.02, 7),
        ("bollinger_bands", "bollinger_bands", 0.03, 0.015, 5),
        ("mean_reversion", "mean_reversion", 0.03, 0.015, 5),
        ("momentum", "momentum", 0.08, 0.04, 15),
    ]
    specs: list[StrategySpec] = []
    for name, strategy_type, tp, sl, held in base:
        specs.append(StrategySpec(name, strategy_type, "long", tp, sl, held))
        specs.append(StrategySpec(name, strategy_type, "short", tp, sl, held))

    # ResearchAnt has a "lower approach" behaviour that is wider than the
    # Backtester exact Bollinger touch. Keep it separate so the audit can show
    # whether this currently generated setup has edge.
    specs.append(
        StrategySpec(
            "bb_lower_approach",
            "bollinger_bands",
            "long",
            0.03,
            0.015,
            5,
            entry_variant="bb_lower_approach",
        )
    )
    return specs


def equity_long_strategy_specs() -> list[StrategySpec]:
    """Long-only strategies used by the current equities system."""

    return [
        spec
        for spec in default_strategy_specs()
        if spec.direction == "long"
        and spec.name
        in {
            "momentum",
            "sma_crossover",
            "bollinger_bands",
            "mean_reversion",
            "rsi_based",
            "bb_lower_approach",
        }
    ]


def mean_reversion_v2_specs(*, include_short: bool = False) -> list[StrategySpec]:
    """Fixed, non-optimized mean_reversion_v2 parameter sets."""

    definitions = [
        (
            "conservative",
            MeanReversionV2Parameters(
                zscore_entry_threshold=2.0,
                rsi_oversold=30,
                rsi_overbought=70,
                max_holding_hours=36,
            ),
        ),
        (
            "base",
            MeanReversionV2Parameters(
                zscore_entry_threshold=1.5,
                rsi_oversold=35,
                rsi_overbought=65,
                max_holding_hours=48,
            ),
        ),
        (
            "aggressive",
            MeanReversionV2Parameters(
                zscore_entry_threshold=1.0,
                rsi_oversold=40,
                rsi_overbought=60,
                max_holding_hours=72,
            ),
        ),
    ]
    specs: list[StrategySpec] = []
    directions = ["long", "short"] if include_short else ["long"]
    for label, params in definitions:
        for direction in directions:
            specs.append(
                StrategySpec(
                    name=f"mean_reversion_v2_{label}",
                    strategy_type="mean_reversion_v2",
                    direction=direction,
                    take_profit_pct=0.0,
                    stop_loss_pct=0.0,
                    max_bars_held=params.max_holding_hours,
                    entry_variant="mean_reversion_v2",
                    params={
                        "parameter_set": label,
                        "zscore_entry_threshold": params.zscore_entry_threshold,
                        "zscore_exit_threshold": params.zscore_exit_threshold,
                        "rsi_period": params.rsi_period,
                        "rsi_oversold": params.rsi_oversold,
                        "rsi_overbought": params.rsi_overbought,
                        "rolling_window": params.rolling_window,
                        "atr_stop_multiplier": params.atr_stop_multiplier,
                        "max_holding_hours": params.max_holding_hours,
                        "volume_confirmation": params.volume_confirmation,
                    },
                )
            )
    return specs


def run_full_edge_audit(
    *,
    output_dir: Path,
    logs_root: Path,
    from_dt: datetime,
    to_dt: datetime,
    timeframe: str = "1h",
    crypto_assets: Iterable[str] = CRYPTO_ASSETS,
    equity_assets: Iterable[str] = EQUITY_ASSETS,
    assumptions: AuditAssumptions | None = None,
    use_network: bool = True,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Run all audit tasks and write every requested deliverable."""

    assumptions = assumptions or AuditAssumptions()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or (output_dir / "candle_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    data_from = from_dt - timedelta(days=185)
    assets = list(dict.fromkeys([*crypto_assets, *equity_assets]))
    bars_by_asset = {
        asset: load_bars_for_asset(
            asset,
            timeframe=timeframe if _is_crypto(asset) else "1d",
            from_dt=data_from,
            to_dt=to_dt,
            cache_dir=cache_dir,
            use_network=use_network,
        )
        for asset in assets
    }

    matrix_rows, matrix_trades = build_expectancy_matrix(
        bars_by_asset=bars_by_asset,
        specs=default_strategy_specs(),
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
    )
    write_csv(output_dir / "backtest_matrix.csv", matrix_rows)
    write_json(output_dir / "backtest_matrix.json", matrix_rows)

    decomposition = decompose_trade_logs(logs_root=logs_root, assumptions=assumptions)
    write_csv(output_dir / "expectancy_decomposition.csv", decomposition["rows"])

    wt_audit = audit_watchtower_signals(
        logs_root=logs_root,
        bars_by_asset=bars_by_asset,
        assumptions=assumptions,
    )
    write_json(output_dir / "watchtower_signal_audit.json", wt_audit["signals"])
    write_csv(output_dir / "watchtower_signal_summary.csv", wt_audit["summary_rows"])

    activity_rows, portfolio = minimum_viable_activity(
        matrix_rows,
        assumptions=assumptions,
        window_days=max(1.0, (to_dt - from_dt).total_seconds() / 86400.0),
    )
    write_csv(output_dir / "minimum_viable_activity.csv", activity_rows)

    summary = build_audit_summary(
        matrix_rows=matrix_rows,
        decomposition=decomposition,
        watchtower_summary=wt_audit,
        activity_rows=activity_rows,
        portfolio=portfolio,
        bars_by_asset=bars_by_asset,
        assumptions=assumptions,
        from_dt=from_dt,
        to_dt=to_dt,
        timeframe=timeframe,
    )
    (output_dir / "audit_summary.md").write_text(summary, encoding="utf-8")

    manifest = {
        "output_dir": str(output_dir),
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "timeframe": timeframe,
        "assumptions": assumptions.__dict__,
        "files": [
            "backtest_matrix.csv",
            "backtest_matrix.json",
            "expectancy_decomposition.csv",
            "watchtower_signal_audit.json",
            "watchtower_signal_summary.csv",
            "minimum_viable_activity.csv",
            "audit_summary.md",
        ],
        "matrix_rows": len(matrix_rows),
        "matrix_trades": len(matrix_trades),
        "watchtower_signals": len(wt_audit["signals"]),
        "trade_log_rows": len(decomposition["rows"]),
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run_equity_3yr_audit(
    *,
    output_dir: Path,
    from_dt: datetime,
    to_dt: datetime,
    equity_assets: Iterable[str] = EQUITY_3YR_ASSETS,
    data_source: str = "live",
    assumptions: AuditAssumptions | None = None,
    use_network: bool = True,
    cache_dir: Path | None = None,
    train_days: int = 360,
    test_days: int = 90,
) -> dict[str, Any]:
    """Run the equity-only 3-year extension for previously sparse setups."""

    data_source = data_source.lower().strip()
    if data_source not in {"live", "yfinance"}:
        raise ValueError("data_source must be 'live' or 'yfinance'")

    if assumptions is None:
        fee = 0.0010 if data_source == "yfinance" else BROKER_FEE_PCT
        assumptions = AuditAssumptions(fee_pct_per_side=fee, slippage_pct=0.0010)

    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or (output_dir / "candle_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    data_from = from_dt - timedelta(days=train_days + 10)
    assets = list(dict.fromkeys(equity_assets))
    loaded: dict[str, tuple[list[OHLCVBar], str, str]] = {}
    for asset in assets:
        loaded[asset] = load_equity_bars_for_audit(
            asset,
            from_dt=data_from,
            to_dt=to_dt,
            cache_dir=cache_dir,
            use_network=use_network,
            data_source=data_source,
        )

    reference_bars, reference_source, reference_flag = load_equity_bars_for_audit(
        "SPY",
        from_dt=data_from,
        to_dt=to_dt,
        cache_dir=cache_dir,
        use_network=use_network,
        data_source=data_source,
    )
    if not reference_bars:
        reference_bars, reference_source, reference_flag = load_equity_bars_for_audit(
            "VWRL.AS",
            from_dt=data_from,
            to_dt=to_dt,
            cache_dir=cache_dir,
            use_network=use_network,
            data_source="yfinance",
        )
    reference_lookup = build_equity_regime_lookup(reference_bars, assumptions)

    def regime_func(bars: list[OHLCVBar], idx: int) -> str:
        return lookup_equity_regime(reference_lookup, bars[idx].timestamp) or derive_equity_regime_from_asset(
            bars, idx, assumptions
        )

    bars_by_asset = {asset: bars for asset, (bars, _, _) in loaded.items()}
    metadata_by_asset = {
        asset: {
            "data_source": actual_source,
            "data_source_flag": flag,
            "regime_source": reference_source or "asset_fallback",
            "regime_source_flag": reference_flag or "asset_fallback",
            "train_window": f"{train_days}d",
            "test_window": f"{test_days}d",
            "time_window": f"walk_forward_{train_days}d_train_{test_days}d_test",
            "canary_candidate": False,
            "confirmed_negative": False,
        }
        for asset, (_, actual_source, flag) in loaded.items()
    }

    rows, trades = build_expectancy_matrix(
        bars_by_asset=bars_by_asset,
        specs=equity_long_strategy_specs(),
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
        train_days=train_days,
        test_days=test_days,
        regimes=EQUITY_3YR_REGIMES,
        regime_func=regime_func,
        row_metadata_by_asset=metadata_by_asset,
    )
    rows = [_mark_equity_extension_verdicts(row) for row in rows]
    all_rows = [row for row in rows if row["regime"] == "ALL"]
    regime_rows = [row for row in rows if row["regime"] != "ALL"]

    write_csv(output_dir / "equity_matrix_3yr.csv", all_rows)
    write_csv(output_dir / "equity_matrix_3yr_by_regime.csv", regime_rows)
    write_json(output_dir / "equity_matrix_3yr.json", rows)
    summary = build_equity_3yr_summary(
        all_rows=all_rows,
        regime_rows=regime_rows,
        bars_by_asset=bars_by_asset,
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
        requested_data_source=data_source,
        reference_source=reference_source,
    )
    (output_dir / "equity_audit_summary.md").write_text(summary, encoding="utf-8")

    manifest = {
        "output_dir": str(output_dir),
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "timeframe": "1d",
        "requested_data_source": data_source,
        "reference_source": reference_source,
        "assumptions": assumptions.__dict__,
        "files": [
            "equity_matrix_3yr.csv",
            "equity_matrix_3yr_by_regime.csv",
            "equity_matrix_3yr.json",
            "equity_audit_summary.md",
        ],
        "matrix_rows": len(rows),
        "matrix_trades": len(trades),
        "assets": assets,
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run_mean_reversion_v2_audit(
    *,
    output_dir: Path,
    from_dt: datetime,
    to_dt: datetime,
    crypto_assets: Iterable[str] = CRYPTO_MR_ASSETS,
    assumptions: AuditAssumptions | None = None,
    use_network: bool = True,
    cache_dir: Path | None = None,
    train_days: int = 90,
    test_days: int = 30,
    include_short: bool = False,
) -> dict[str, Any]:
    """Run the fixed-parameter crypto mean_reversion_v2 audit."""

    assumptions = assumptions or AuditAssumptions(fee_pct_per_side=BROKER_FEE_PCT, slippage_pct=0.0010)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_dir or (output_dir / "candle_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    data_from = from_dt - timedelta(days=train_days + 10)
    assets = list(dict.fromkeys(crypto_assets))
    bars_by_asset = {
        asset: load_bars_for_asset(
            asset,
            timeframe="1h",
            from_dt=data_from,
            to_dt=to_dt,
            cache_dir=cache_dir,
            use_network=use_network,
        )
        for asset in assets
    }
    metadata = {
        asset: {
            "data_source": "bitvavo_public",
            "timeframe": "1h",
            "train_window": f"{train_days}d",
            "test_window": f"{test_days}d",
        }
        for asset in assets
    }

    regime_func = lambda bars, idx: derive_crypto_mean_reversion_regime(bars, idx, assumptions)
    long_rows, long_trades = build_expectancy_matrix(
        bars_by_asset=bars_by_asset,
        specs=mean_reversion_v2_specs(include_short=False),
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
        train_days=train_days,
        test_days=test_days,
        regimes=CRYPTO_MR_REGIMES,
        regime_func=regime_func,
        row_metadata_by_asset=metadata,
    )
    long_positive = any(row.get("regime") == "ALL" and row.get("status") == "POSITIVE_EDGE" for row in long_rows)
    if include_short or long_positive:
        short_rows, short_trades = build_expectancy_matrix(
            bars_by_asset=bars_by_asset,
            specs=[spec for spec in mean_reversion_v2_specs(include_short=True) if spec.direction == "short"],
            from_dt=from_dt,
            to_dt=to_dt,
            assumptions=assumptions,
            train_days=train_days,
            test_days=test_days,
            regimes=CRYPTO_MR_REGIMES,
            regime_func=regime_func,
            row_metadata_by_asset=metadata,
        )
        rows = long_rows + short_rows
        trades = long_trades + short_trades
        short_tested = True
    else:
        rows = long_rows
        trades = long_trades
        short_tested = False

    rows = [_mark_mean_reversion_v2_row(row) for row in rows]
    write_csv(output_dir / "mean_reversion_v2_matrix.csv", rows)
    write_json(output_dir / "mean_reversion_v2_matrix.json", rows)

    baseline_rows, _ = build_expectancy_matrix(
        bars_by_asset=bars_by_asset,
        specs=_crypto_baseline_specs(),
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
        train_days=train_days,
        test_days=test_days,
        regimes=("ALL",),
        regime_func=regime_func,
        row_metadata_by_asset=metadata,
    )
    comparison_rows = build_mean_reversion_v2_comparison(rows, baseline_rows, assets)
    write_csv(output_dir / "mean_reversion_v2_comparison.csv", comparison_rows)

    summary = build_mean_reversion_v2_summary(
        rows=rows,
        comparison_rows=comparison_rows,
        bars_by_asset=bars_by_asset,
        from_dt=from_dt,
        to_dt=to_dt,
        assumptions=assumptions,
        short_tested=short_tested,
    )
    (output_dir / "mean_reversion_v2_summary.md").write_text(summary, encoding="utf-8")

    manifest = {
        "output_dir": str(output_dir),
        "from": from_dt.isoformat(),
        "to": to_dt.isoformat(),
        "timeframe": "1h",
        "assumptions": assumptions.__dict__,
        "files": [
            "mean_reversion_v2_matrix.csv",
            "mean_reversion_v2_matrix.json",
            "mean_reversion_v2_summary.md",
            "mean_reversion_v2_comparison.csv",
        ],
        "matrix_rows": len(rows),
        "matrix_trades": len(trades),
        "short_tested": short_tested,
        "assets": assets,
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


# ---------------------------------------------------------------------------
# Task 1: strategy x asset expectancy matrix
# ---------------------------------------------------------------------------


def build_expectancy_matrix(
    *,
    bars_by_asset: dict[str, list[OHLCVBar]],
    specs: list[StrategySpec],
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
    train_days: int = 180,
    test_days: int = 30,
    regimes: Iterable[str] = ("ALL", "TRENDING", "SIDEWAYS", "RISK_ON", "RISK_OFF", "HIGH_RISK"),
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
    row_metadata_by_asset: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[AuditTrade]]:
    rows: list[dict[str, Any]] = []
    all_trades: list[AuditTrade] = []

    for asset, bars in bars_by_asset.items():
        biome = "crypto" if _is_crypto(asset) else "equities"
        for spec in specs:
            trades = walk_forward_trades(
                bars=bars,
                asset=asset,
                biome=biome,
                spec=spec,
                from_dt=from_dt,
                to_dt=to_dt,
                assumptions=assumptions,
                train_days=train_days,
                test_days=test_days,
                regime_func=regime_func,
            )
            all_trades.extend(trades)
            for regime in regimes:
                subset = trades if regime == "ALL" else [t for t in trades if t.regime == regime]
                rows.append(
                    _matrix_row(
                        asset,
                        biome,
                        spec,
                        regime,
                        subset,
                        from_dt,
                        to_dt,
                        assumptions,
                        train_days=train_days,
                        test_days=test_days,
                        metadata=(row_metadata_by_asset or {}).get(asset),
                    )
                )

    rows.sort(
        key=lambda r: (
            r.get("expectancy_r") is not None,
            r.get("expectancy_r") or -999.0,
            r.get("profit_factor") or -999.0,
        ),
        reverse=True,
    )
    return rows, all_trades


def walk_forward_trades(
    *,
    bars: list[OHLCVBar],
    asset: str,
    biome: str,
    spec: StrategySpec,
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
    train_days: int = 180,
    test_days: int = 30,
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
) -> list[AuditTrade]:
    bars = sorted([b for b in bars if _bar_valid(b)], key=lambda b: b.timestamp)
    if not bars:
        return []

    tester = Backtester()
    trades: list[AuditTrade] = []
    test_start = from_dt
    while test_start < to_dt:
        test_end = min(test_start + timedelta(days=test_days), to_dt)
        train_start = test_start - timedelta(days=train_days)
        window_bars = [b for b in bars if train_start <= _utc(b.timestamp) <= test_end]
        if len(window_bars) >= 2:
            trades.extend(
                simulate_strategy_trades(
                    bars=window_bars,
                    asset=asset,
                    biome=biome,
                    spec=spec,
                    assumptions=assumptions,
                    entry_start=test_start,
                    entry_end=test_end,
                    window_id=f"{test_start.date()}_{test_end.date()}",
                    tester=tester,
                    regime_func=regime_func,
                )
            )
        test_start = test_end
    return _dedupe_trades(trades)


def simulate_strategy_trades(
    *,
    bars: list[OHLCVBar],
    asset: str,
    biome: str,
    spec: StrategySpec,
    assumptions: AuditAssumptions,
    entry_start: datetime,
    entry_end: datetime,
    window_id: str,
    tester: Backtester | None = None,
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
) -> list[AuditTrade]:
    if spec.entry_variant == "mean_reversion_v2":
        return simulate_mean_reversion_v2_trades(
            bars=bars,
            asset=asset,
            biome=biome,
            spec=spec,
            assumptions=assumptions,
            entry_start=entry_start,
            entry_end=entry_end,
            window_id=window_id,
            regime_func=regime_func,
        )
    tester = tester or Backtester()
    closes = [b.close for b in bars]
    trades: list[AuditTrade] = []
    i = 0
    while i < len(bars) - 1:
        bar = bars[i]
        ts = _utc(bar.timestamp)
        if ts < entry_start:
            i += 1
            continue
        if ts >= entry_end:
            break
        if bar.close <= 0:
            i += 1
            continue
        if not _has_audit_entry_signal(tester, closes, i, spec):
            i += 1
            continue

        exit_idx, raw_exit, reason = _find_exit_with_ohlc(bars, i, bar.close, spec)
        trade = _build_strategy_trade(
            asset=asset,
            biome=biome,
            spec=spec,
            bars=bars,
            entry_idx=i,
            exit_idx=exit_idx,
            raw_exit=raw_exit,
            exit_reason=reason,
            assumptions=assumptions,
            window_id=window_id,
            regime_func=regime_func,
        )
        trades.append(trade)
        i = exit_idx + 1
    return trades


def simulate_mean_reversion_v2_trades(
    *,
    bars: list[OHLCVBar],
    asset: str,
    biome: str,
    spec: StrategySpec,
    assumptions: AuditAssumptions,
    entry_start: datetime,
    entry_end: datetime,
    window_id: str,
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
) -> list[AuditTrade]:
    params = _mean_reversion_params(spec)
    bars = sorted([b for b in bars if _bar_valid(b)], key=lambda b: b.timestamp)
    closes = [b.close for b in bars]
    trades: list[AuditTrade] = []
    i = 0
    while i < len(bars) - 1:
        ts = _utc(bars[i].timestamp)
        if ts < entry_start:
            i += 1
            continue
        if ts >= entry_end:
            break
        setup = _mean_reversion_setup(bars, closes, i, params, spec.direction)
        if setup is None:
            i += 1
            continue

        exit_idx, raw_exit, reason = _find_mean_reversion_exit(bars, closes, i, setup["stop_loss"], params, spec.direction)
        trade = _build_mean_reversion_trade(
            asset=asset,
            biome=biome,
            spec=spec,
            bars=bars,
            entry_idx=i,
            exit_idx=exit_idx,
            raw_exit=raw_exit,
            exit_reason=reason,
            stop_loss=setup["stop_loss"],
            assumptions=assumptions,
            window_id=window_id,
            regime_func=regime_func,
        )
        trades.append(trade)
        i = exit_idx + 1
    return trades


def _mean_reversion_params(spec: StrategySpec) -> MeanReversionV2Parameters:
    params = dict(spec.params)
    params.pop("parameter_set", None)
    return MeanReversionV2Parameters(**params)


def _mean_reversion_setup(
    bars: list[OHLCVBar],
    closes: list[float],
    idx: int,
    params: MeanReversionV2Parameters,
    direction: str,
) -> dict[str, float] | None:
    if idx < max(params.rolling_window, params.rsi_period, 2):
        return None
    mean = _sma_at(closes, idx, params.rolling_window)
    std = _std_at(closes, idx, params.rolling_window)
    if mean is None or std is None or std <= 0:
        return None
    z_score = (closes[idx] - mean) / std
    rsi = _rsi_at(closes, idx, params.rsi_period)
    atr = _atr_abs_at(bars, idx, params.rsi_period)
    if atr <= 0:
        return None
    volume_ratio = _volume_ratio_at(bars, idx, params.rolling_window)
    if params.volume_confirmation and volume_ratio <= 1.0:
        return None

    if direction == "long":
        if not (z_score < -params.zscore_entry_threshold and rsi < params.rsi_oversold):
            return None
        stop_loss = bars[idx].close - (params.atr_stop_multiplier * atr)
    else:
        if not (z_score > params.zscore_entry_threshold and rsi > params.rsi_overbought):
            return None
        stop_loss = bars[idx].close + (params.atr_stop_multiplier * atr)
    return {
        "z_score": z_score,
        "rsi": rsi,
        "atr": atr,
        "volume_ratio": volume_ratio,
        "rolling_mean": mean,
        "stop_loss": stop_loss,
    }


def _find_mean_reversion_exit(
    bars: list[OHLCVBar],
    closes: list[float],
    entry_idx: int,
    stop_loss: float,
    params: MeanReversionV2Parameters,
    direction: str,
) -> tuple[int, float, str]:
    end_idx = min(entry_idx + params.max_holding_hours, len(bars) - 1)
    for idx in range(entry_idx + 1, end_idx + 1):
        bar = bars[idx]
        if direction == "long" and bar.low <= stop_loss:
            return idx, stop_loss, "atr_stop"
        if direction == "short" and bar.high >= stop_loss:
            return idx, stop_loss, "atr_stop"

        mean = _sma_at(closes, idx, params.rolling_window)
        std = _std_at(closes, idx, params.rolling_window)
        if mean is None or std is None or std <= 0:
            continue
        z_score = (bar.close - mean) / std
        if direction == "long" and z_score >= params.zscore_exit_threshold:
            return idx, bar.close, "mean_reversion"
        if direction == "short" and z_score <= -params.zscore_exit_threshold:
            return idx, bar.close, "mean_reversion"
    return end_idx, bars[end_idx].close, "timeout"


def _build_mean_reversion_trade(
    *,
    asset: str,
    biome: str,
    spec: StrategySpec,
    bars: list[OHLCVBar],
    entry_idx: int,
    exit_idx: int,
    raw_exit: float,
    exit_reason: str,
    stop_loss: float,
    assumptions: AuditAssumptions,
    window_id: str,
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
) -> AuditTrade:
    raw_entry = bars[entry_idx].close
    entry = _apply_slippage(raw_entry, spec.direction, "entry", assumptions.slippage_pct)
    exit_price = _apply_slippage(raw_exit, spec.direction, "exit", assumptions.slippage_pct)
    gross = _directional_return(entry, exit_price, spec.direction)
    net = gross - (assumptions.fee_pct_per_side * 2.0)
    stop_pct = abs(raw_entry - stop_loss) / raw_entry if raw_entry > 0 else 0.0
    pnl_r = net / stop_pct if stop_pct > 0 else 0.0
    pnl_eur = assumptions.starting_equity * assumptions.risk_per_trade * pnl_r
    return AuditTrade(
        strategy=spec.name,
        asset=asset,
        biome=biome,
        direction=spec.direction,
        regime=regime_func(bars, entry_idx) if regime_func else derive_regime(bars, entry_idx, assumptions),
        entry_time=_utc(bars[entry_idx].timestamp),
        exit_time=_utc(bars[exit_idx].timestamp),
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=exit_reason,
        bars_held=exit_idx - entry_idx + 1,
        gross_return_pct=gross,
        net_return_pct=net,
        pnl_r=pnl_r,
        pnl_eur=pnl_eur,
        window_id=window_id,
    )


def _has_audit_entry_signal(tester: Backtester, closes: list[float], i: int, spec: StrategySpec) -> bool:
    if spec.entry_variant == "bb_lower_approach":
        if i < 20:
            return False
        window = closes[i - 19 : i + 1]
        mean = sum(window) / len(window)
        std = math.sqrt(sum((c - mean) ** 2 for c in window) / len(window))
        lower = mean - 2.0 * std
        upper = mean + 2.0 * std
        proximity = 0.10 * max(0.0, upper - lower)
        return closes[i] <= lower + proximity
    return tester._has_entry_signal(closes, i, spec.strategy_type, spec.direction)


def _find_exit_with_ohlc(
    bars: list[OHLCVBar],
    entry_idx: int,
    entry_price: float,
    spec: StrategySpec,
) -> tuple[int, float, str]:
    end_idx = min(entry_idx + spec.max_bars_held, len(bars) - 1)
    if spec.direction == "long":
        tp = entry_price * (1.0 + spec.take_profit_pct)
        sl = entry_price * (1.0 - spec.stop_loss_pct)
    else:
        tp = entry_price * (1.0 - spec.take_profit_pct)
        sl = entry_price * (1.0 + spec.stop_loss_pct)

    for idx in range(entry_idx + 1, end_idx + 1):
        bar = bars[idx]
        if spec.direction == "long":
            if bar.low <= sl:
                return idx, sl, "stop_loss"
            if bar.high >= tp:
                return idx, tp, "take_profit"
        else:
            if bar.high >= sl:
                return idx, sl, "stop_loss"
            if bar.low <= tp:
                return idx, tp, "take_profit"
    return end_idx, bars[end_idx].close, "ttl"


def _build_strategy_trade(
    *,
    asset: str,
    biome: str,
    spec: StrategySpec,
    bars: list[OHLCVBar],
    entry_idx: int,
    exit_idx: int,
    raw_exit: float,
    exit_reason: str,
    assumptions: AuditAssumptions,
    window_id: str,
    regime_func: Callable[[list[OHLCVBar], int], str] | None = None,
) -> AuditTrade:
    raw_entry = bars[entry_idx].close
    entry = _apply_slippage(raw_entry, spec.direction, "entry", assumptions.slippage_pct)
    exit_price = _apply_slippage(raw_exit, spec.direction, "exit", assumptions.slippage_pct)
    gross = _directional_return(entry, exit_price, spec.direction)
    net = gross - (assumptions.fee_pct_per_side * 2.0)
    pnl_r = net / spec.stop_loss_pct if spec.stop_loss_pct > 0 else 0.0
    pnl_eur = assumptions.starting_equity * assumptions.risk_per_trade * pnl_r
    return AuditTrade(
        strategy=spec.name,
        asset=asset,
        biome=biome,
        direction=spec.direction,
        regime=regime_func(bars, entry_idx) if regime_func else derive_regime(bars, entry_idx, assumptions),
        entry_time=_utc(bars[entry_idx].timestamp),
        exit_time=_utc(bars[exit_idx].timestamp),
        entry_price=entry,
        exit_price=exit_price,
        exit_reason=exit_reason,
        bars_held=exit_idx - entry_idx + 1,
        gross_return_pct=gross,
        net_return_pct=net,
        pnl_r=pnl_r,
        pnl_eur=pnl_eur,
        window_id=window_id,
    )


def _matrix_row(
    asset: str,
    biome: str,
    spec: StrategySpec,
    regime: str,
    trades: list[AuditTrade],
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
    train_days: int = 180,
    test_days: int = 30,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = metrics_from_trades(trades, from_dt=from_dt, to_dt=to_dt, assumptions=assumptions)
    status = edge_status(metrics["expectancy_r"], metrics["profit_factor"], metrics["trade_count"])
    row = {
        "asset": asset,
        "biome": biome,
        "strategy": spec.name,
        "strategy_type": spec.strategy_type,
        "direction": spec.direction,
        "regime": regime,
        "time_window": f"walk_forward_{train_days}d_train_{test_days}d_test",
        "train_window": f"{train_days}d",
        "test_window": f"{test_days}d",
        "take_profit_pct": spec.take_profit_pct,
        "stop_loss_pct": spec.stop_loss_pct,
        "max_bars_held": spec.max_bars_held,
        **metrics,
        "status": status,
        "verdict": _matrix_verdict(status),
    }
    if metadata:
        row.update(metadata)
    if spec.params:
        row.update(spec.params)
    return row


def metrics_from_trades(
    trades: list[AuditTrade],
    *,
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
) -> dict[str, Any]:
    count = len(trades)
    pnl_rs = [t.pnl_r for t in trades]
    wins = [r for r in pnl_rs if r > 0]
    losses = [abs(r) for r in pnl_rs if r < 0]
    win_rate = len(wins) / count if count else None
    loss_rate = len(losses) / count if count else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    expectancy = (sum(pnl_rs) / count) if count else None
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    profit_factor = (gross_profit / gross_loss) if gross_loss else (math.inf if gross_profit else None)
    years = max((to_dt - from_dt).total_seconds() / (365.25 * 86400.0), 1 / 365.25)
    account_returns = [r * assumptions.risk_per_trade for r in pnl_rs]
    equity = _equity_curve(account_returns)
    cagr = (equity[-1] ** (1.0 / years) - 1.0) if len(equity) > 1 and equity[-1] > 0 else 0.0
    max_dd = _max_drawdown(equity)
    sharpe = _annualized_sharpe(account_returns, years, assumptions.risk_free_rate)
    sortino = _annualized_sortino(account_returns, years, assumptions.risk_free_rate)
    streaks = _loss_streaks(pnl_rs)
    avg_hours = (
        sum((t.exit_time - t.entry_time).total_seconds() / 3600.0 for t in trades) / count
        if count
        else None
    )
    return {
        "trade_count": count,
        "expectancy_r": _round_or_none(expectancy, 6),
        "profit_factor": _round_or_none(profit_factor, 6),
        "cagr": round(cagr, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": _round_or_none(sharpe, 6),
        "sortino": _round_or_none(sortino, 6),
        "win_rate": _round_or_none(win_rate, 6),
        "avg_win_r": _round_or_none(avg_win, 6),
        "loss_rate": _round_or_none(loss_rate, 6),
        "avg_loss_r": _round_or_none(avg_loss, 6),
        "avg_holding_hours": _round_or_none(avg_hours, 3),
        "max_consecutive_losses": max(streaks) if streaks else 0,
        "p95_consecutive_losses": _percentile(streaks, 0.95) if streaks else 0,
        "recovery_factor": _round_or_none((cagr / max_dd) if max_dd > 0 else None, 6),
        "total_pnl_eur": round(sum(t.pnl_eur for t in trades), 6),
    }


def edge_status(expectancy_r: float | None, profit_factor: float | None, trade_count: int) -> str:
    if trade_count < 15:
        return "INSUFFICIENT_DATA"
    if expectancy_r is None:
        return "INSUFFICIENT_DATA"
    if expectancy_r > 0.05 and (profit_factor or 0.0) > 1.2 and trade_count >= 30:
        return "POSITIVE_EDGE"
    if expectancy_r > 0:
        return "MARGINAL"
    return "NEGATIVE_EDGE"


# ---------------------------------------------------------------------------
# Task 2: trade-log decomposition
# ---------------------------------------------------------------------------


def decompose_trade_logs(*, logs_root: Path, assumptions: AuditAssumptions) -> dict[str, Any]:
    trades = load_closed_trades(logs_root)
    rows: list[dict[str, Any]] = []
    groupers: dict[str, Callable[[dict[str, Any]], str]] = {
        "strategy": lambda t: str(t.get("strategy_type") or t.get("strategy") or "unknown"),
        "asset": lambda t: str(t.get("asset") or t.get("symbol") or "unknown"),
        "regime": lambda t: str(t.get("regime") or t.get("market_regime") or "unknown"),
        "source": lambda t: str(t.get("source") or t.get("signal_source") or "unknown"),
        "hour": lambda t: f"{_trade_dt(t).hour:02d}:00" if _trade_dt(t) else "unknown",
        "weekday": lambda t: _trade_dt(t).strftime("%A") if _trade_dt(t) else "unknown",
    }
    for dimension, grouper in groupers.items():
        bucket: dict[str, list[dict[str, Any]]] = {}
        for trade in trades:
            bucket.setdefault(grouper(trade), []).append(trade)
        for key, items in bucket.items():
            rows.append(_decomposition_row(dimension, key, items, assumptions))

    rows.sort(
        key=lambda r: (
            r["expectancy_r"] if r["expectancy_r"] is not None else 999.0,
            r["contribution_pnl_eur"],
        )
    )
    worst = [r for r in rows if r["dimension"] in {"strategy", "asset", "regime", "source"}][:3]
    remaining = _remove_worst_combinations(trades, worst)
    aggregate = _decomposition_row("aggregate", "all_trades", trades, assumptions)
    aggregate_without_worst = _decomposition_row("aggregate", "without_top3_worst", remaining, assumptions)
    rows.extend([aggregate, aggregate_without_worst])
    return {
        "rows": rows,
        "trade_count": len(trades),
        "aggregate": aggregate,
        "aggregate_without_worst": aggregate_without_worst,
        "top3_worst": worst,
    }


def load_closed_trades(logs_root: Path) -> list[dict[str, Any]]:
    if not logs_root.exists():
        return []
    trades: list[dict[str, Any]] = []
    for path in logs_root.rglob("*.jsonl"):
        if "paper" not in {p.lower() for p in path.parts} and "live" not in {p.lower() for p in path.parts}:
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = record.get("payload") if isinstance(record, dict) else None
                payload = payload if isinstance(payload, dict) else record
                if not isinstance(payload, dict):
                    continue
                if payload.get("action") == "trade_closed" or payload.get("closed_at") or payload.get("exit_price"):
                    item = dict(payload)
                    item.setdefault("timestamp", record.get("timestamp") if isinstance(record, dict) else None)
                    trades.append(item)
        except OSError:
            continue
    return trades


def _decomposition_row(
    dimension: str,
    key: str,
    trades: list[dict[str, Any]],
    assumptions: AuditAssumptions,
) -> dict[str, Any]:
    pairs = [(trade, _trade_pnl_r(trade)) for trade in trades]
    pairs = [(trade, pnl_r) for trade, pnl_r in pairs if pnl_r is not None]
    pnl_rs = [pnl_r for _, pnl_r in pairs]
    count = len(pnl_rs)
    wins = [r for r in pnl_rs if r > 0]
    losses = [abs(r) for r in pnl_rs if r < 0]
    expectancy = sum(pnl_rs) / count if count else None
    pnl_eur = sum(_trade_pnl_eur(trade, pnl_r, assumptions) for trade, pnl_r in pairs)
    pf = sum(wins) / sum(losses) if losses else (math.inf if wins else None)
    status = "ISOLATE" if count >= 15 and (expectancy or 0) <= 0 else ("RETAIN" if count >= 15 and (expectancy or 0) > 0.05 and (pf or 0) > 1.2 else "INVESTIGATE")
    return {
        "dimension": dimension,
        "combination": key,
        "trade_count": count,
        "expectancy_r": _round_or_none(expectancy, 6),
        "profit_factor": _round_or_none(pf, 6),
        "contribution_pnl_eur": round(pnl_eur, 6),
        "total_pnl_eur": round(pnl_eur, 6),
        "win_rate": _round_or_none((len(wins) / count) if count else None, 6),
        "status": status,
    }


def _remove_worst_combinations(trades: list[dict[str, Any]], worst_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = list(trades)
    for row in worst_rows:
        dim = row["dimension"]
        combo = row["combination"]
        if dim == "strategy":
            result = [t for t in result if str(t.get("strategy_type") or t.get("strategy") or "unknown") != combo]
        elif dim == "asset":
            result = [t for t in result if str(t.get("asset") or t.get("symbol") or "unknown") != combo]
        elif dim == "regime":
            result = [t for t in result if str(t.get("regime") or t.get("market_regime") or "unknown") != combo]
        elif dim == "source":
            result = [t for t in result if str(t.get("source") or t.get("signal_source") or "unknown") != combo]
    return result


# ---------------------------------------------------------------------------
# Task 3: Watchtower retrospective
# ---------------------------------------------------------------------------


def audit_watchtower_signals(
    *,
    logs_root: Path,
    bars_by_asset: dict[str, list[OHLCVBar]],
    assumptions: AuditAssumptions,
) -> dict[str, Any]:
    signals = load_watchtower_signals(logs_root, include_neutral_as_long=True)
    trades: list[AuditTrade] = []
    skipped: dict[str, int] = {}
    for signal in signals:
        bars = bars_by_asset.get(signal.symbol) or []
        trade = simulate_watchtower_signal(signal, bars, assumptions)
        if trade is None:
            reason = "no_bars_or_no_entry"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        trades.append(trade)

    signal_rows = [t.to_dict() for t in trades]
    summary_rows = _watchtower_summary_rows(trades)
    recommendation = _watchtower_calibration(summary_rows, len(signals))
    summary_rows.append(recommendation)
    return {
        "signals": signal_rows,
        "summary_rows": summary_rows,
        "signal_count": len(signals),
        "trade_count": len(trades),
        "skipped": skipped,
        "calibration": recommendation,
    }


def simulate_watchtower_signal(signal: Any, bars: list[OHLCVBar], assumptions: AuditAssumptions) -> AuditTrade | None:
    bars = sorted([b for b in bars if _bar_valid(b)], key=lambda b: b.timestamp)
    if not bars:
        return None
    entry_idx = _bar_at_or_before(bars, signal.timestamp)
    if entry_idx is None:
        return None
    entry_bar = bars[entry_idx]
    direction = "long" if signal.direction == "neutral" else signal.direction
    if direction not in {"long", "short"}:
        return None
    atr = _atr_pct(bars, entry_idx, assumptions.atr_period)
    sl_pct = max(atr * assumptions.atr_stop_multiple, 0.005)
    tp_pct = sl_pct * assumptions.watchtower_take_profit_r
    spec = StrategySpec("watchtower_signal", "watchtower_signal", direction, tp_pct, sl_pct, assumptions.watchtower_max_hold_bars)
    exit_idx, raw_exit, reason = _find_exit_with_ohlc(bars, entry_idx, entry_bar.close, spec)
    trade = _build_strategy_trade(
        asset=signal.symbol,
        biome=signal.asset_class,
        spec=spec,
        bars=bars,
        entry_idx=entry_idx,
        exit_idx=exit_idx,
        raw_exit=raw_exit,
        exit_reason=reason,
        assumptions=assumptions,
        window_id="watchtower_replay",
    )
    return AuditTrade(
        **{
            **trade.__dict__,
            "source": signal.source_field,
            "signal_id": signal.signal_id,
            "confidence": signal.confidence,
            "entry_score": signal.entry_score,
            "theme": _signal_theme(signal.raw),
        }
    )


def _watchtower_summary_rows(trades: list[AuditTrade]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    groupers: dict[str, Callable[[AuditTrade], str]] = {
        "confidence_bucket": lambda t: _confidence_bucket(t.confidence),
        "source": lambda t: t.source or "unknown",
        "theme": lambda t: t.theme or "unknown",
        "asset": lambda t: t.asset,
        "regime": lambda t: t.regime,
    }
    now = datetime.now(timezone.utc)
    start = min((t.entry_time for t in trades), default=now)
    end = max((t.exit_time for t in trades), default=now)
    assumptions = AuditAssumptions()
    for dimension, grouper in groupers.items():
        bucket: dict[str, list[AuditTrade]] = {}
        for trade in trades:
            bucket.setdefault(grouper(trade), []).append(trade)
        for key, items in bucket.items():
            metrics = metrics_from_trades(items, from_dt=start, to_dt=end, assumptions=assumptions)
            rows.append({"dimension": dimension, "bucket": key, **metrics})
    rows.sort(key=lambda r: (r.get("expectancy_r") is not None, r.get("expectancy_r") or -999), reverse=True)
    return rows


def _watchtower_calibration(summary_rows: list[dict[str, Any]], signal_count: int) -> dict[str, Any]:
    confidence_rows = [r for r in summary_rows if r.get("dimension") == "confidence_bucket"]
    viable = [
        r for r in confidence_rows
        if (r.get("trade_count") or 0) >= 15
        and (r.get("expectancy_r") or 0) > 0
        and (r.get("profit_factor") or 0) > 1.0
    ]
    if signal_count < 50:
        threshold = None
        recommendation = "INSUFFICIENT_SIGNAL_HISTORY"
    elif viable:
        threshold = min(_bucket_floor(r["bucket"]) for r in viable)
        recommendation = "ALLOW_ABOVE_THRESHOLD_CANARY"
    else:
        threshold = None
        recommendation = "DO_NOT_USE_FOR_ENTRIES_YET"
    return {
        "dimension": "calibration",
        "bucket": "recommendation",
        "signal_count": signal_count,
        "minimum_confidence_threshold": threshold,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# Task 4: minimum viable activity
# ---------------------------------------------------------------------------


def minimum_viable_activity(
    matrix_rows: list[dict[str, Any]],
    *,
    assumptions: AuditAssumptions,
    window_days: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    positives = [r for r in matrix_rows if r.get("status") == "POSITIVE_EDGE" and r.get("regime") == "ALL"]
    rows: list[dict[str, Any]] = []
    total_expected_return = 0.0
    for row in positives:
        exp_r = float(row.get("expectancy_r") or 0.0)
        trades = int(row.get("trade_count") or 0)
        frequency = trades / max(window_days / 365.25, 1 / 365.25)
        required = {
            "required_trades_0_5pct": _required_trades(assumptions.benchmark_annual_return, 0.005, exp_r),
            "required_trades_1_0pct": _required_trades(assumptions.benchmark_annual_return, 0.010, exp_r),
            "required_trades_1_5pct": _required_trades(assumptions.benchmark_annual_return, 0.015, exp_r),
        }
        verdict = "FEASIBLE" if frequency >= (required["required_trades_1_0pct"] or math.inf) else "NEEDS_DIVERSIFICATION"
        total_expected_return += frequency * assumptions.risk_per_trade * exp_r
        rows.append({
            "asset": row["asset"],
            "strategy": row["strategy"],
            "direction": row["direction"],
            "expectancy_r": exp_r,
            "profit_factor": row.get("profit_factor"),
            "historical_trades_per_year": round(frequency, 3),
            **required,
            "verdict": verdict,
        })
    rows.sort(key=lambda r: r["expectancy_r"], reverse=True)
    portfolio = {
        "positive_edge_combinations": len(positives),
        "estimated_portfolio_return_at_1pct_risk": round(total_expected_return, 6),
        "benchmark_annual_return": assumptions.benchmark_annual_return,
        "verdict": "FEASIBLE" if total_expected_return >= assumptions.benchmark_annual_return else "NEEDS_DIVERSIFICATION_OR_MORE_EDGE",
    }
    return rows, portfolio


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_bars_for_asset(
    asset: str,
    *,
    timeframe: str,
    from_dt: datetime,
    to_dt: datetime,
    cache_dir: Path,
    use_network: bool,
) -> list[OHLCVBar]:
    cached = _read_cached_bars(cache_dir, asset, timeframe)
    cached = [b for b in cached if from_dt <= _utc(b.timestamp) <= to_dt]
    if cached:
        return cached
    if not use_network:
        return []
    if _is_crypto(asset):
        bars = _fetch_bitvavo_history(asset, timeframe, from_dt, to_dt)
    else:
        bars = _fetch_yahoo_history(asset, from_dt, to_dt, cache_dir / "_yfinance_cache")
    if bars:
        _write_cached_bars(cache_dir, asset, timeframe, bars)
    return bars


def load_equity_bars_for_audit(
    asset: str,
    *,
    from_dt: datetime,
    to_dt: datetime,
    cache_dir: Path,
    use_network: bool,
    data_source: str,
) -> tuple[list[OHLCVBar], str, str]:
    """Load equity bars and return (bars, actual_source, source_flag)."""

    timeframe = "1d"
    requested = data_source.lower().strip()
    cached = _read_cached_bars(cache_dir, asset, timeframe)
    cached = [b for b in cached if from_dt <= _utc(b.timestamp) <= to_dt]
    if cached:
        actual = "yfinance" if requested == "yfinance" else requested
        return cached, actual, _source_flag(actual)
    if not use_network:
        return [], requested, _source_flag(requested)

    if requested == "yfinance":
        bars = _fetch_yahoo_history(asset, from_dt, to_dt, cache_dir / "_yfinance_cache")
        actual = "yfinance"
    else:
        bars = _fetch_ibkr_history(asset, from_dt, to_dt)
        actual = "live"
        if not _has_enough_history(bars, from_dt, to_dt):
            bars = _fetch_yahoo_history(asset, from_dt, to_dt, cache_dir / "_yfinance_cache")
            actual = "yfinance"
    if bars:
        _write_cached_bars(cache_dir, asset, timeframe, bars)
    return bars, actual, _source_flag(actual)


def _source_flag(data_source: str) -> str:
    return "EXTERNAL_DATA_SOURCE" if data_source == "yfinance" else "BROKER_DATA_SOURCE"


def _has_enough_history(bars: list[OHLCVBar], from_dt: datetime, to_dt: datetime) -> bool:
    if not bars:
        return False
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)
    return _utc(sorted_bars[0].timestamp) <= from_dt and _utc(sorted_bars[-1].timestamp) >= to_dt - timedelta(days=7)


def _fetch_ibkr_history(asset: str, from_dt: datetime, to_dt: datetime) -> list[OHLCVBar]:
    try:
        from ant_colony.biome.adapters.ibkr_adapter import IBKRAdapter

        adapter = IBKRAdapter()
        candles = adapter.get_candles(asset, period="5y", interval="1d")
    except Exception:
        return []
    bars: list[OHLCVBar] = []
    for candle in candles:
        bar = _market_to_bar(candle)
        if bar is not None and from_dt <= _utc(bar.timestamp) <= to_dt:
            bars.append(bar)
    return bars


def _fetch_bitvavo_history(asset: str, timeframe: str, from_dt: datetime, to_dt: datetime) -> list[OHLCVBar]:
    interval_ms = _interval_ms(timeframe)
    if interval_ms is None:
        return []
    start_ms = int(_utc(from_dt).timestamp() * 1000)
    end_ms = int(_utc(to_dt).timestamp() * 1000)
    chunk = interval_ms * 1000
    bars_by_ts: dict[int, OHLCVBar] = {}
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + chunk, end_ms)
        params = urllib.parse.urlencode({
            "interval": timeframe,
            "limit": 1000,
            "start": cursor,
            "end": chunk_end,
        })
        url = f"https://api.bitvavo.com/v2/{urllib.parse.quote(asset)}/candles?{params}"
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except Exception:
            raw = []
        if isinstance(raw, list):
            for row in raw:
                try:
                    ts_ms, open_, high, low, close, volume = row
                    ts_int = int(float(ts_ms))
                    bars_by_ts[ts_int] = OHLCVBar(
                        timestamp=datetime.fromtimestamp(ts_int / 1000.0, tz=timezone.utc),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume),
                    )
                except (TypeError, ValueError):
                    continue
        cursor = chunk_end
        time.sleep(0.1)
    return sorted(bars_by_ts.values(), key=lambda b: b.timestamp)


def _fetch_yahoo_history(asset: str, from_dt: datetime, to_dt: datetime, cache_location: Path | None = None) -> list[OHLCVBar]:
    _configure_yfinance_cache(cache_location)
    adapter = YahooFinanceAdapter()
    candles = adapter.get_candles(asset, period=_yahoo_period_for_range(from_dt, to_dt), interval="1d")
    bars: list[OHLCVBar] = []
    for candle in candles:
        bar = _market_to_bar(candle)
        if bar is not None and from_dt <= _utc(bar.timestamp) <= to_dt:
            bars.append(bar)
    return bars


def _configure_yfinance_cache(cache_location: Path | None) -> None:
    try:
        import yfinance as yf

        location = cache_location or Path(os.getenv("YFINANCE_CACHE_DIR", ".yfinance_cache"))
        location.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(location.resolve()))
    except Exception:
        return


def _yahoo_period_for_range(from_dt: datetime, to_dt: datetime) -> str:
    years = (to_dt - from_dt).total_seconds() / (365.25 * 86400.0)
    if years <= 1:
        return "1y"
    if years <= 2:
        return "2y"
    return "5y"


def _market_to_bar(candle: MarketData) -> OHLCVBar | None:
    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        return None
    return OHLCVBar(
        timestamp=_utc(candle.timestamp),
        open=float(candle.open),
        high=float(candle.high),
        low=float(candle.low),
        close=float(candle.close),
        volume=float(candle.volume or 0.0),
    )


def _read_cached_bars(cache_dir: Path, asset: str, timeframe: str) -> list[OHLCVBar]:
    path = cache_dir / f"{_safe_name(asset)}_{_safe_name(timeframe)}.csv"
    if not path.exists():
        return []
    bars: list[OHLCVBar] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    bars.append(OHLCVBar(
                        timestamp=_parse_dt(row["timestamp"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return []
    return sorted(bars, key=lambda b: b.timestamp)


def _write_cached_bars(cache_dir: Path, asset: str, timeframe: str, bars: list[OHLCVBar]) -> None:
    path = cache_dir / f"{_safe_name(asset)}_{_safe_name(timeframe)}.csv"
    try:
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            for b in bars:
                writer.writerow({
                    "timestamp": _utc(b.timestamp).isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })
    except OSError:
        return


# ---------------------------------------------------------------------------
# Summary/report helpers
# ---------------------------------------------------------------------------


def build_audit_summary(
    *,
    matrix_rows: list[dict[str, Any]],
    decomposition: dict[str, Any],
    watchtower_summary: dict[str, Any],
    activity_rows: list[dict[str, Any]],
    portfolio: dict[str, Any],
    bars_by_asset: dict[str, list[OHLCVBar]],
    assumptions: AuditAssumptions,
    from_dt: datetime,
    to_dt: datetime,
    timeframe: str,
) -> str:
    all_rows = [r for r in matrix_rows if r.get("regime") == "ALL"]
    traded_rows = [r for r in all_rows if int(r.get("trade_count") or 0) > 0]
    matrix_trades = sum(int(r.get("trade_count") or 0) for r in traded_rows)
    weighted_expectancy = None
    if matrix_trades:
        weighted_expectancy = sum(
            float(r.get("expectancy_r") or 0.0) * int(r.get("trade_count") or 0)
            for r in traded_rows
        ) / matrix_trades
    status_counts = {
        status: sum(1 for r in all_rows if r.get("status") == status)
        for status in ("POSITIVE_EDGE", "MARGINAL", "NEGATIVE_EDGE", "INSUFFICIENT_DATA")
    }
    positive = [r for r in matrix_rows if r.get("status") == "POSITIVE_EDGE" and r.get("regime") == "ALL"]
    marginal = [r for r in matrix_rows if r.get("status") == "MARGINAL" and r.get("regime") == "ALL"]
    negative = [r for r in matrix_rows if r.get("status") == "NEGATIVE_EDGE" and r.get("regime") == "ALL"]
    missing_assets = [asset for asset, bars in bars_by_asset.items() if not bars]
    lines = [
        "# ANT COLONY v2 Edge Audit",
        "",
        f"Periode: `{from_dt.isoformat()}` tot `{to_dt.isoformat()}`",
        f"Timeframe crypto: `{timeframe}`; equities: `1d`",
        f"Kosten: fee per side `{assumptions.fee_pct_per_side:.4f}`, slippage per side `{assumptions.slippage_pct:.4f}`",
        "",
        "## Datakwaliteit",
        f"- Assets zonder candles: {len(missing_assets)}" + (f" ({', '.join(missing_assets[:20])})" if missing_assets else ""),
        f"- Matrix rows: {len(matrix_rows)}",
        f"- Matrix trades: {matrix_trades}; gewogen expectancy: `{_round_or_none(weighted_expectancy, 6)}` R",
        "- Status counts: "
        + ", ".join(f"{status}={count}" for status, count in status_counts.items()),
        f"- Watchtower signalen gereplayed: {watchtower_summary.get('trade_count', 0)} van {watchtower_summary.get('signal_count', 0)}",
        f"- Trade-log regels voor decompositie: {decomposition.get('trade_count', 0)}",
        "",
        "## Verdict",
    ]
    if positive:
        lines.append("- PROCEED_CANARY: er zijn combinaties met POSITIVE_EDGE. Alleen kleine canary allocation, niet full deployment.")
    else:
        lines.append("- DO_NOT_PROCEED: geen combinatie voldoet aan POSITIVE_EDGE op de beschikbare out-of-sample data.")
    if watchtower_summary.get("signal_count", 0) < 50:
        lines.append("- WATCHTOWER: onvoldoende signal history (<50); eerst data verzamelen voordat Watchtower live entries mag sturen.")
    else:
        lines.append(f"- WATCHTOWER: {watchtower_summary.get('calibration', {}).get('recommendation', 'UNKNOWN')}.")
    lines.extend([
        f"- Portfolio haalbaarheid bij 1% risk/trade: `{portfolio.get('verdict')}`; geschatte return `{portfolio.get('estimated_portfolio_return_at_1pct_risk')}` vs benchmark `{assumptions.benchmark_annual_return}`.",
        "",
        "## POSITIVE_EDGE combinaties",
    ])
    lines.extend(_summary_table(positive[:25], ["asset", "strategy", "direction", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown"]))
    lines.extend(["", "## MARGINAL combinaties"])
    lines.extend(_summary_table(marginal[:25], ["asset", "strategy", "direction", "expectancy_r", "profit_factor", "trade_count"]))
    lines.extend(["", "## Te isoleren uit trade-log"])
    isolate = [r for r in decomposition.get("rows", []) if r.get("status") == "ISOLATE"][:20]
    lines.extend(_summary_table(isolate, ["dimension", "combination", "trade_count", "expectancy_r", "contribution_pnl_eur", "status"]))
    lines.extend(["", "## Disable entirely"])
    disable = negative[:25]
    lines.extend(_summary_table(disable, ["asset", "strategy", "direction", "expectancy_r", "profit_factor", "trade_count", "status"]))
    lines.extend(["", "## Need more data"])
    insufficient = [r for r in matrix_rows if r.get("status") == "INSUFFICIENT_DATA" and r.get("regime") == "ALL"][:25]
    lines.extend(_summary_table(insufficient, ["asset", "strategy", "direction", "trade_count", "status"]))
    lines.extend([
        "",
        "## Expliciete deploy-regel",
        "Alleen combinaties met `POSITIVE_EDGE` mogen vandaag eventueel in een canary bucket. `MARGINAL` blijft research-only. `NEGATIVE_EDGE` moet uit live routing blijven. `INSUFFICIENT_DATA` krijgt geen live kapitaal tot er minimaal 30 out-of-sample trades zijn.",
    ])
    return "\n".join(lines) + "\n"


def build_equity_3yr_summary(
    *,
    all_rows: list[dict[str, Any]],
    regime_rows: list[dict[str, Any]],
    bars_by_asset: dict[str, list[OHLCVBar]],
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
    requested_data_source: str,
    reference_source: str,
) -> str:
    missing_assets = [asset for asset, bars in bars_by_asset.items() if not bars]
    positive = [r for r in all_rows if r.get("status") == "POSITIVE_EDGE"]
    marginal = [r for r in all_rows if r.get("status") == "MARGINAL"]
    confirmed_negative = [r for r in all_rows if r.get("verdict") == "CONFIRMED_NEGATIVE"]
    insufficient = [r for r in all_rows if r.get("status") == "INSUFFICIENT_DATA"]
    canaries = [r for r in all_rows if r.get("canary_candidate")]
    lines = [
        "# ANT COLONY v2 Equity 3-Year Audit",
        "",
        f"Periode: `{from_dt.isoformat()}` tot `{to_dt.isoformat()}`",
        "Timeframe: `1d`; walk-forward: `360d train / 90d test`",
        f"Requested data source: `{requested_data_source}`; regime reference: `{reference_source or 'asset_fallback'}`",
        f"Kosten: fee per side `{assumptions.fee_pct_per_side:.4f}`, slippage per side `{assumptions.slippage_pct:.4f}`",
        "",
        "## Datakwaliteit",
        f"- Assets zonder candles: {len(missing_assets)}" + (f" ({', '.join(missing_assets)})" if missing_assets else ""),
        f"- Combinaties: {len(all_rows)}",
        f"- Regime rows: {len(regime_rows)}",
        "",
        "## Verdict",
    ]
    if positive:
        lines.append("- PROCEED_CANARY: er zijn 3-jaars equity combinaties met POSITIVE_EDGE.")
    else:
        lines.append("- DO_NOT_PROCEED_FULL: geen equity combinatie voldoet aan POSITIVE_EDGE op 3-jaars out-of-sample data.")
    if canaries:
        lines.append("- AAPL/GLD re-evaluatie: CANARY_CANDIDATE gevonden, pending PC2 log validation.")
    else:
        lines.append("- AAPL/GLD re-evaluatie: geen CANARY_CANDIDATE op basis van de 3-jaars criteria.")
    lines.extend([
        "",
        "## CANARY_CANDIDATE",
    ])
    lines.extend(_summary_table(canaries, ["asset", "strategy", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown", "data_source_flag"]))
    lines.extend(["", "## POSITIVE_EDGE"])
    lines.extend(_summary_table(positive, ["asset", "strategy", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown", "data_source_flag"]))
    lines.extend(["", "## MARGINAL"])
    lines.extend(_summary_table(marginal[:40], ["asset", "strategy", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown", "data_source_flag"]))
    lines.extend(["", "## CONFIRMED_NEGATIVE"])
    lines.extend(_summary_table(confirmed_negative[:60], ["asset", "strategy", "expectancy_r", "profit_factor", "trade_count", "verdict"]))
    lines.extend(["", "## INSUFFICIENT_DATA"])
    lines.extend(_summary_table(insufficient[:60], ["asset", "strategy", "trade_count", "status"]))
    lines.extend([
        "",
        "## Expliciete deploy-regel",
        "Alleen `CANARY_CANDIDATE` of `POSITIVE_EDGE` mag naar kleine canary allocation, en alleen na PC2 log validation. `CONFIRMED_NEGATIVE` niet opnieuw testen en niet deployen. `MARGINAL` blijft research-only.",
    ])
    return "\n".join(lines) + "\n"


def build_mean_reversion_v2_summary(
    *,
    rows: list[dict[str, Any]],
    comparison_rows: list[dict[str, Any]],
    bars_by_asset: dict[str, list[OHLCVBar]],
    from_dt: datetime,
    to_dt: datetime,
    assumptions: AuditAssumptions,
    short_tested: bool,
) -> str:
    all_rows = [r for r in rows if r.get("regime") == "ALL"]
    positive = [r for r in all_rows if r.get("status") == "POSITIVE_EDGE"]
    marginal = [r for r in all_rows if r.get("status") == "MARGINAL"]
    negative = [r for r in all_rows if r.get("status") == "NEGATIVE_EDGE"]
    insufficient = [r for r in all_rows if r.get("status") == "INSUFFICIENT_DATA"]
    missing_assets = [asset for asset, bars in bars_by_asset.items() if not bars]
    lines = [
        "# Crypto Mean Reversion v2 Audit",
        "",
        f"Periode: `{from_dt.isoformat()}` tot `{to_dt.isoformat()}`",
        "Timeframe: `1h`; walk-forward: `90d train / 30d test`",
        f"Kosten: fee per side `{assumptions.fee_pct_per_side:.4f}`, slippage per side `{assumptions.slippage_pct:.4f}`",
        f"Short getest: `{short_tested}`",
        "",
        "## Datakwaliteit",
        f"- Assets zonder candles: {len(missing_assets)}" + (f" ({', '.join(missing_assets)})" if missing_assets else ""),
        f"- Combinaties: {len(all_rows)}",
        f"- Regime rows: {len(rows) - len(all_rows)}",
        "",
        "## Verdict",
    ]
    if positive:
        lines.append("- CANDIDATE: minimaal één mean_reversion_v2 combinatie haalt POSITIVE_EDGE. Dit is een canary-kandidaat, geen full deployment.")
    else:
        lines.append("- NO_CRYPTO_MR_EDGE_CONFIRMED: geen mean_reversion_v2 combinatie haalt POSITIVE_EDGE op deze out-of-sample test.")
    if not positive and not marginal:
        lines.append("- Als alle drie vaste parameterprofielen negatief blijven, is de volgende stap fundamenteel onderzoek, niet nog een strategievariant zonder nieuwe hypothese.")
    lines.extend(["", "## POSITIVE_EDGE"])
    lines.extend(_summary_table(positive, ["asset", "parameter_set", "direction", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown"]))
    lines.extend(["", "## MARGINAL"])
    lines.extend(_summary_table(marginal[:50], ["asset", "parameter_set", "direction", "expectancy_r", "profit_factor", "trade_count", "cagr", "max_drawdown"]))
    lines.extend(["", "## NEGATIVE_EDGE / CONFIRMED_NEGATIVE"])
    lines.extend(_summary_table(negative[:50], ["asset", "parameter_set", "direction", "expectancy_r", "profit_factor", "trade_count", "verdict"]))
    lines.extend(["", "## INSUFFICIENT_DATA"])
    lines.extend(_summary_table(insufficient[:50], ["asset", "parameter_set", "direction", "trade_count", "status"]))
    sideway_positive = [r for r in rows if r.get("regime") == "SIDEWAYS" and r.get("status") in {"POSITIVE_EDGE", "MARGINAL"}]
    trending_negative = [r for r in rows if r.get("regime") == "TRENDING" and r.get("status") == "NEGATIVE_EDGE"]
    lines.extend([
        "",
        "## Regime sanity check",
        f"- SIDEWAYS positive/marginal rows: {len(sideway_positive)}",
        f"- TRENDING negative rows: {len(trending_negative)}",
        "",
        "## Old vs new comparison",
    ])
    lines.extend(_summary_table(comparison_rows, ["asset", "old_strategy", "old_expectancy_r", "new_strategy", "new_expectancy_r", "improvement"]))
    return "\n".join(lines) + "\n"


def build_mean_reversion_v2_comparison(
    new_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    assets: Iterable[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for asset in assets:
        old_candidates = [r for r in baseline_rows if r.get("asset") == asset and r.get("regime") == "ALL"]
        new_candidates = [r for r in new_rows if r.get("asset") == asset and r.get("regime") == "ALL"]
        old = _best_expectancy_row(old_candidates)
        new = _best_expectancy_row(new_candidates)
        old_exp = old.get("expectancy_r") if old else None
        new_exp = new.get("expectancy_r") if new else None
        result.append({
            "asset": asset,
            "old_strategy": f"{old.get('strategy')}:{old.get('direction')}" if old else "",
            "old_expectancy_r": old_exp,
            "new_strategy": f"{new.get('parameter_set')}:{new.get('direction')}" if new else "",
            "new_expectancy_r": new_exp,
            "improvement": _round_or_none((float(new_exp) - float(old_exp)) if old_exp is not None and new_exp is not None else None, 6),
            "new_status": new.get("status") if new else "",
        })
    return result


def _best_expectancy_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    with_expectancy = [r for r in rows if r.get("expectancy_r") is not None]
    if not with_expectancy:
        return rows[0] if rows else None
    return max(with_expectancy, key=lambda r: float(r.get("expectancy_r") or -999.0))


def _crypto_baseline_specs() -> list[StrategySpec]:
    return [
        spec
        for spec in default_strategy_specs()
        if spec.name in {"momentum", "sma_crossover"}
    ]


def _mark_mean_reversion_v2_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    trade_count = int(result.get("trade_count") or 0)
    if trade_count < 30:
        result["status"] = "INSUFFICIENT_DATA"
        result["verdict"] = "COLLECT_MORE_DATA"
    elif result.get("status") == "NEGATIVE_EDGE":
        result["confirmed_negative"] = True
        result["verdict"] = "CONFIRMED_NEGATIVE"
    else:
        result["confirmed_negative"] = False
    return result


def _mark_equity_extension_verdicts(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    trade_count = int(result.get("trade_count") or 0)
    if result.get("status") == "NEGATIVE_EDGE" and trade_count >= 30:
        result["confirmed_negative"] = True
        result["verdict"] = "CONFIRMED_NEGATIVE"
    else:
        result["confirmed_negative"] = False
    if (
        result.get("asset") in {"AAPL", "GLD"}
        and result.get("status") == "POSITIVE_EDGE"
        and trade_count >= 30
        and result.get("regime") == "ALL"
    ):
        result["canary_candidate"] = True
        result["verdict"] = "CANARY_CANDIDATE: ready for small live allocation pending PC2 log validation"
    else:
        result["canary_candidate"] = False
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    if not fieldnames:
        fieldnames = ["empty"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, default=str, allow_nan=False),
        encoding="utf-8",
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _summary_table(rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    if not rows:
        return ["Geen."]
    output = ["|" + "|".join(columns) + "|", "|" + "|".join(["---"] * len(columns)) + "|"]
    for row in rows:
        output.append("|" + "|".join(str(row.get(c, "")) for c in columns) + "|")
    return output


# ---------------------------------------------------------------------------
# Generic math helpers
# ---------------------------------------------------------------------------


def derive_regime(bars: list[OHLCVBar], idx: int, assumptions: AuditAssumptions) -> str:
    if idx < 50:
        return "SIDEWAYS"
    close = bars[idx].close
    atr_pct = _atr_pct(bars, idx, assumptions.atr_period)
    atr_values = [_atr_pct(bars, j, assumptions.atr_period) for j in range(max(assumptions.atr_period, idx - 200), idx + 1)]
    high_risk_threshold = _percentile([v for v in atr_values if v > 0], 0.90) or 0.05
    if atr_pct >= max(high_risk_threshold, 0.05):
        return "HIGH_RISK"
    sma50 = _sma_at([b.close for b in bars], idx, 50)
    sma200 = _sma_at([b.close for b in bars], idx, 200)
    if sma50 is None:
        return "SIDEWAYS"
    slope = _slope_pct([b.close for b in bars], idx, 50)
    if sma200 is not None and close > sma200 * 1.02 and slope > 0.005:
        return "RISK_ON"
    if sma200 is not None and close < sma200 * 0.98 and slope < -0.005:
        return "RISK_OFF"
    if abs(slope) > 0.015:
        return "TRENDING"
    return "SIDEWAYS"


def build_equity_regime_lookup(bars: list[OHLCVBar], assumptions: AuditAssumptions) -> dict[datetime.date, str]:
    sorted_bars = sorted([b for b in bars if _bar_valid(b)], key=lambda b: b.timestamp)
    return {
        _utc(bar.timestamp).date(): derive_equity_regime_from_asset(sorted_bars, idx, assumptions)
        for idx, bar in enumerate(sorted_bars)
    }


def lookup_equity_regime(regimes_by_date: dict[Any, str], timestamp: datetime) -> str | None:
    if not regimes_by_date:
        return None
    target = _utc(timestamp).date()
    if target in regimes_by_date:
        return regimes_by_date[target]
    earlier = [date for date in regimes_by_date.keys() if date <= target]
    if not earlier:
        return None
    return regimes_by_date[max(earlier)]


def derive_equity_regime_from_asset(bars: list[OHLCVBar], idx: int, assumptions: AuditAssumptions) -> str:
    if idx < 200:
        return "SIDEWAYS"
    closes = [b.close for b in bars]
    close = bars[idx].close
    sma200 = _sma_at(closes, idx, 200)
    if sma200 is None:
        return "SIDEWAYS"
    atr_pct = _atr_pct(bars, idx, assumptions.atr_period)
    atr_values = [
        _atr_pct(bars, j, assumptions.atr_period)
        for j in range(max(assumptions.atr_period, idx - 252), idx + 1)
    ]
    high_vol_threshold = _percentile([v for v in atr_values if v > 0], 0.75) or 0.02
    trending_up = close >= sma200
    high_vol = atr_pct >= high_vol_threshold
    if trending_up and not high_vol:
        return "RISK_ON"
    if not trending_up and high_vol:
        return "RISK_OFF"
    if trending_up and high_vol:
        return "VOLATILE_BULL"
    return "SIDEWAYS"


def derive_crypto_mean_reversion_regime(bars: list[OHLCVBar], idx: int, assumptions: AuditAssumptions) -> str:
    if idx < 50:
        return "SIDEWAYS"
    atr_pct = _atr_pct(bars, idx, assumptions.atr_period)
    atr_values = [_atr_pct(bars, j, assumptions.atr_period) for j in range(max(assumptions.atr_period, idx - 500), idx + 1)]
    high_vol_threshold = _percentile([v for v in atr_values if v > 0], 0.75) or 0.03
    if atr_pct >= high_vol_threshold:
        return "HIGH_VOLATILITY"
    closes = [b.close for b in bars]
    slope = _slope_pct(closes, idx, 50)
    if abs(slope) >= 0.04:
        return "TRENDING"
    return "SIDEWAYS"


def _atr_pct(bars: list[OHLCVBar], idx: int, period: int) -> float:
    if idx <= 0:
        return 0.0
    start = max(1, idx - period + 1)
    trs: list[float] = []
    for i in range(start, idx + 1):
        prev_close = bars[i - 1].close
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        )
        if bars[i].close > 0:
            trs.append(tr / bars[i].close)
    return sum(trs) / len(trs) if trs else 0.0


def _atr_abs_at(bars: list[OHLCVBar], idx: int, period: int) -> float:
    if idx <= 0:
        return 0.0
    start = max(1, idx - period + 1)
    trs: list[float] = []
    for i in range(start, idx + 1):
        prev_close = bars[i - 1].close
        trs.append(max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - prev_close),
            abs(bars[i].low - prev_close),
        ))
    return sum(trs) / len(trs) if trs else 0.0


def _sma_at(closes: list[float], idx: int, period: int) -> float | None:
    if idx < period - 1:
        return None
    window = closes[idx - period + 1 : idx + 1]
    return sum(window) / period if window else None


def _std_at(closes: list[float], idx: int, period: int) -> float | None:
    if idx < period - 1:
        return None
    window = closes[idx - period + 1 : idx + 1]
    if not window:
        return None
    mean = sum(window) / len(window)
    return math.sqrt(sum((value - mean) ** 2 for value in window) / len(window))


def _rsi_at(closes: list[float], idx: int, period: int) -> float:
    if idx < period:
        return 50.0
    window = closes[idx - period : idx + 1]
    gains = [max(0.0, window[i] - window[i - 1]) for i in range(1, len(window))]
    losses = [max(0.0, window[i - 1] - window[i]) for i in range(1, len(window))]
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _volume_ratio_at(bars: list[OHLCVBar], idx: int, period: int) -> float:
    if idx < period - 1:
        return 0.0
    window = [b.volume for b in bars[idx - period + 1 : idx + 1]]
    avg = sum(window) / len(window) if window else 0.0
    if avg <= 0:
        return 0.0
    return bars[idx].volume / avg


def _slope_pct(closes: list[float], idx: int, period: int) -> float:
    if idx < period:
        return 0.0
    old = closes[idx - period]
    if old <= 0:
        return 0.0
    return (closes[idx] - old) / old


def _equity_curve(returns: list[float]) -> list[float]:
    equity = [1.0]
    for ret in returns:
        equity.append(max(0.000001, equity[-1] * (1.0 + ret)))
    return equity


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0] if equity else 1.0
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return max_dd


def _annualized_sharpe(returns: list[float], years: float, risk_free_rate: float) -> float | None:
    if len(returns) < 2:
        return None
    excess_per_trade = (sum(returns) / len(returns)) - (risk_free_rate / max(len(returns) / years, 1.0))
    std = statistics.stdev(returns)
    if std == 0:
        return 0.0
    return (excess_per_trade / std) * math.sqrt(len(returns) / years)


def _annualized_sortino(returns: list[float], years: float, risk_free_rate: float) -> float | None:
    if len(returns) < 2:
        return None
    downside = [min(0.0, r) for r in returns]
    downside_dev = math.sqrt(sum(r * r for r in downside) / max(1, len(downside) - 1))
    if downside_dev == 0:
        return None
    mean = sum(returns) / len(returns)
    return (mean / downside_dev) * math.sqrt(len(returns) / years)


def _loss_streaks(values: list[float]) -> list[int]:
    streaks: list[int] = []
    current = 0
    for value in values:
        if value < 0:
            current += 1
        elif current:
            streaks.append(current)
            current = 0
    if current:
        streaks.append(current)
    return streaks


def _percentile(values: list[float] | list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo))


def _required_trades(benchmark: float, risk: float, expectancy_r: float) -> float | None:
    denom = risk * expectancy_r
    if denom <= 0:
        return None
    return round(benchmark / denom, 3)


def _trade_pnl_r(trade: dict[str, Any]) -> float | None:
    for key in ("pnl_r", "r_multiple", "net_r"):
        if trade.get(key) is not None:
            return _safe_float(trade.get(key))
    pnl_pct = _safe_float(trade.get("pnl_pct") or trade.get("net_return_pct") or trade.get("return_pct"))
    if pnl_pct is not None:
        if abs(pnl_pct) > 2:
            pnl_pct /= 100.0
        return pnl_pct / 0.02
    pnl = _safe_float(trade.get("pnl_net") or trade.get("realized_pnl") or trade.get("pnl_eur"))
    entry = _safe_float(trade.get("entry_price"))
    qty = _safe_float(trade.get("quantity"))
    sl = _safe_float(trade.get("stop_loss") or trade.get("stop_loss_price"))
    if None not in (pnl, entry, qty, sl) and entry and qty and sl:
        risk = abs(entry - sl) * qty
        if risk > 0:
            return pnl / risk
    return None


def _trade_pnl_eur(trade: dict[str, Any], pnl_r: float | None, assumptions: AuditAssumptions) -> float:
    for key in ("pnl_net", "realized_pnl", "pnl_eur", "net_pnl"):
        value = _safe_float(trade.get(key))
        if value is not None:
            return value
    return (pnl_r or 0.0) * assumptions.starting_equity * assumptions.risk_per_trade


def _trade_dt(trade: dict[str, Any]) -> datetime | None:
    for key in ("opened_at", "entry_time", "entry_timestamp", "timestamp", "closed_at"):
        if trade.get(key):
            try:
                return _parse_dt(str(trade[key]))
            except ValueError:
                continue
    return None


def _confidence_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.6:
        return "0.5-0.6"
    if value < 0.7:
        return "0.6-0.7"
    if value < 0.8:
        return "0.7-0.8"
    return "0.8-1.0"


def _bucket_floor(bucket: str) -> float:
    try:
        return float(bucket.split("-")[0])
    except (ValueError, IndexError):
        return 1.0


def _signal_theme(raw: dict[str, Any]) -> str:
    for key in ("theme", "source", "source_field"):
        if raw.get(key):
            return str(raw[key])
    for key in ("tags", "risk_flags", "intermarket_drivers"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            return str(value[0])
    return "unknown"


def _bar_at_or_before(bars: list[OHLCVBar], ts: datetime) -> int | None:
    ts = _utc(ts)
    chosen = None
    for idx, bar in enumerate(bars):
        if _utc(bar.timestamp) <= ts:
            chosen = idx
        else:
            break
    return chosen


def _dedupe_trades(trades: list[AuditTrade]) -> list[AuditTrade]:
    seen: set[tuple[str, str, str, datetime]] = set()
    result: list[AuditTrade] = []
    for trade in trades:
        key = (trade.asset, trade.strategy, trade.direction, trade.entry_time)
        if key in seen:
            continue
        seen.add(key)
        result.append(trade)
    return result


def _directional_return(entry: float, exit_price: float, direction: str) -> float:
    if entry <= 0:
        return 0.0
    if direction == "short":
        return (entry - exit_price) / entry
    return (exit_price - entry) / entry


def _apply_slippage(price: float, direction: str, phase: str, slippage_pct: float) -> float:
    if direction == "long":
        return price * (1.0 + slippage_pct) if phase == "entry" else price * (1.0 - slippage_pct)
    return price * (1.0 - slippage_pct) if phase == "entry" else price * (1.0 + slippage_pct)


def _bar_valid(bar: OHLCVBar) -> bool:
    return min(bar.open, bar.high, bar.low, bar.close) > 0


def _is_crypto(asset: str) -> bool:
    return "-" in asset


def _interval_ms(timeframe: str) -> int | None:
    units = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "1d": 86_400_000,
    }
    return units.get(timeframe)


def _parse_dt(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return _utc(dt)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isinf(value):
        return value
    return round(value, digits)


def _matrix_verdict(status: str) -> str:
    if status == "POSITIVE_EDGE":
        return "PROCEED_CANARY"
    if status == "MARGINAL":
        return "RESEARCH_ONLY"
    if status == "NEGATIVE_EDGE":
        return "DISABLE"
    return "COLLECT_MORE_DATA"


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)
