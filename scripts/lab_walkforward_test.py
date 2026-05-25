"""
One-off walk-forward lab test for BTC-EUR daily SMA crossover variants.

Train: 2021-2022
Test:  2023-2024

No lab code is modified. The script temporarily adjusts the Backtester SMA
module constants for each sweep variant because BacktestConfig does not expose
SMA periods.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ant_colony.lab.backtester as backtester_module
from ant_colony.lab.backtester import BacktestConfig, Backtester, OHLCVBar
from ant_colony.lab.edge_audit import load_bars_for_asset


ASSET = "BTC-EUR"
TIMEFRAME = "1d"
FROM_DT = datetime(2021, 1, 1, tzinfo=timezone.utc)
TO_DT = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
TRAIN_END = datetime(2022, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
TEST_START = datetime(2023, 1, 1, tzinfo=timezone.utc)
CACHE_DIR = Path(os.environ.get("ANT_LAB_CACHE", "logs/lab_smoke_cache"))

SMA_VARIANTS = ((10, 30), (20, 50), (50, 100), (50, 200))
TPSL_VARIANTS = ((0.04, 0.02), (0.06, 0.03), (0.08, 0.04))


@dataclass(frozen=True)
class SweepRow:
    fast: int
    slow: int
    take_profit_pct: float
    stop_loss_pct: float
    sharpe: float | None
    trades: int | None
    win_rate: float | None
    max_drawdown: float | None
    profit_factor: float | None
    result: Any


def _profit_factor(result: Any) -> float | None:
    win_rate = result.win_rate
    avg_win = result.avg_win
    avg_loss = result.avg_loss
    if win_rate is None or avg_win is None or avg_loss is None:
        return None
    loss_rate = 1.0 - float(win_rate)
    gross_loss = loss_rate * float(avg_loss)
    if gross_loss <= 0:
        return math.inf if float(avg_win) > 0 else None
    return (float(win_rate) * float(avg_win)) / gross_loss


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _run_variant(
    bars: list[OHLCVBar],
    *,
    fast: int,
    slow: int,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> SweepRow:
    old_fast = backtester_module._SMA_FAST
    old_slow = backtester_module._SMA_SLOW
    try:
        backtester_module._SMA_FAST = fast
        backtester_module._SMA_SLOW = slow
        config = BacktestConfig(
            direction="long",
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            max_bars_held=10,
            strategy_type="sma_crossover",
        )
        result = Backtester().run(bars, config)
    finally:
        backtester_module._SMA_FAST = old_fast
        backtester_module._SMA_SLOW = old_slow

    return SweepRow(
        fast=fast,
        slow=slow,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        sharpe=result.sharpe_ratio,
        trades=result.total_trades,
        win_rate=result.win_rate,
        max_drawdown=result.max_drawdown_pct,
        profit_factor=_profit_factor(result),
        result=result,
    )


def _rank_key(row: SweepRow) -> tuple[float, int, float]:
    sharpe = row.sharpe if row.sharpe is not None else float("-inf")
    trades = row.trades or 0
    win_rate = row.win_rate or 0.0
    return (sharpe, trades, win_rate)


def _fee_slippage_visibility(result: Any) -> str:
    payload = result.model_dump()
    text = json.dumps(_json_safe(payload), sort_keys=True).lower()
    markers = ("fee", "fees", "fee_cost", "slippage", "cost")
    found = [marker for marker in markers if marker in text]
    if not found:
        return "geen fees/slippage in resultaat zichtbaar"
    return f"fees/slippage velden zichtbaar: {', '.join(found)}"


def main() -> int:
    print("=== LAB WALK-FORWARD TEST ===")
    print("Hypothese-familie: SMA crossover op BTC-EUR daily, long-only")
    print(f"Periode totaal: {FROM_DT.isoformat()} -> {TO_DT.isoformat()}")
    print(f"Train: 2021-01-01 -> 2022-12-31")
    print(f"Test:  2023-01-01 -> 2024-12-31")
    print(f"Loader: ant_colony.lab.edge_audit.load_bars_for_asset")
    print(f"Cache: {CACHE_DIR}")

    bars = load_bars_for_asset(
        ASSET,
        timeframe=TIMEFRAME,
        from_dt=FROM_DT,
        to_dt=TO_DT,
        cache_dir=CACHE_DIR,
        use_network=True,
    )
    bars = sorted(bars, key=lambda b: b.timestamp)
    if not bars:
        print("Geen bars geladen; walk-forward test kan niet draaien.")
        return 2

    train_bars = [b for b in bars if FROM_DT <= b.timestamp <= TRAIN_END]
    test_bars = [b for b in bars if TEST_START <= b.timestamp <= TO_DT]
    print(
        f"Bars geladen: totaal={len(bars)} train={len(train_bars)} test={len(test_bars)} "
        f"| eerste={bars[0].timestamp.isoformat()} laatste={bars[-1].timestamp.isoformat()}"
    )
    if not train_bars or not test_bars:
        print("Train- of test-set is leeg; walk-forward test stopt.")
        return 2

    rows: list[SweepRow] = []
    for fast, slow in SMA_VARIANTS:
        for tp, sl in TPSL_VARIANTS:
            rows.append(_run_variant(
                train_bars,
                fast=fast,
                slow=slow,
                take_profit_pct=tp,
                stop_loss_pct=sl,
            ))

    rows.sort(key=_rank_key, reverse=True)
    best = rows[0]
    test = _run_variant(
        test_bars,
        fast=best.fast,
        slow=best.slow,
        take_profit_pct=best.take_profit_pct,
        stop_loss_pct=best.stop_loss_pct,
    )
    degradation = None
    if best.sharpe is not None and test.sharpe is not None:
        degradation = best.sharpe - test.sharpe

    print("")
    print("=== TRAIN SWEEP RESULTATEN (gesorteerd op Sharpe) ===")
    for idx, row in enumerate(rows, start=1):
        print(
            f"{idx:02d}. SMA {row.fast}/{row.slow} TP={row.take_profit_pct:.2f} "
            f"SL={row.stop_loss_pct:.2f} | sharpe={row.sharpe} trades={row.trades} "
            f"win_rate={row.win_rate} max_dd={row.max_drawdown} pf={row.profit_factor}"
        )

    print("")
    print("=== GESELECTEERDE PARAMETERCOMBINATIE ===")
    print(
        f"SMA {best.fast}/{best.slow}, TP={best.take_profit_pct:.2f}, "
        f"SL={best.stop_loss_pct:.2f}, max_bars_held=10"
    )
    print(f"Train Sharpe: {best.sharpe}")
    print(f"Test Sharpe: {test.sharpe}")
    print(f"Degradatie (train - test): {degradation}")
    print(f"Train trades/winrate/max_dd/pf: {best.trades} / {best.win_rate} / {best.max_drawdown} / {best.profit_factor}")
    print(f"Test trades/winrate/max_dd/pf: {test.trades} / {test.win_rate} / {test.max_drawdown} / {test.profit_factor}")

    print("")
    print("=== FEES / SLIPPAGE ZICHTBAARHEID ===")
    print(_fee_slippage_visibility(test.result))

    print("")
    print("=== TEST BacktestResults OBJECT ===")
    payload = test.result.model_dump()
    payload["profit_factor_derived"] = test.profit_factor
    payload["selected_params"] = {
        "sma_fast": best.fast,
        "sma_slow": best.slow,
        "take_profit_pct": best.take_profit_pct,
        "stop_loss_pct": best.stop_loss_pct,
        "max_bars_held": 10,
    }
    payload["train_sharpe"] = best.sharpe
    payload["test_sharpe"] = test.sharpe
    payload["sharpe_degradation_train_minus_test"] = degradation
    print(json.dumps(_json_safe(payload), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
