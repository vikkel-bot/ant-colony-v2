"""
One-off lab smoke test for the in-memory Backtester.

Hypothesis:
SMA crossover 20/50 on BTC-EUR daily, long-only, TP 6%, SL 3%,
max 10 bars held over 2021-01-01 through 2024-12-31.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ant_colony.lab.backtester import BacktestConfig, Backtester
from ant_colony.lab.edge_audit import load_bars_for_asset


ASSET = "BTC-EUR"
TIMEFRAME = "1d"
FROM_DT = datetime(2021, 1, 1, tzinfo=timezone.utc)
TO_DT = datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
CACHE_DIR = Path(os.environ.get("ANT_LAB_CACHE", "logs/lab_smoke_cache"))


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


def main() -> int:
    print("=== LAB SMOKE TEST ===")
    print(
        "Hypothese: SMA crossover 20/50 op BTC-EUR daily, long-only, "
        "take-profit 6%, stop-loss 3%, max 10 bars held"
    )
    print(f"Periode: {FROM_DT.isoformat()} -> {TO_DT.isoformat()}")
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
        print("Geen bars geladen; smoke test kan niet draaien.")
        return 2

    print(
        f"Bars geladen: {len(bars)} | eerste={bars[0].timestamp.isoformat()} "
        f"laatste={bars[-1].timestamp.isoformat()}"
    )

    config = BacktestConfig(
        direction="long",
        take_profit_pct=0.06,
        stop_loss_pct=0.03,
        max_bars_held=10,
        strategy_type="sma_crossover",
    )
    result = Backtester().run(bars, config)
    profit_factor = _profit_factor(result)

    print("")
    print("=== RESULTATEN ===")
    print(f"Sharpe: {result.sharpe_ratio}")
    print(f"Max drawdown: {result.max_drawdown_pct}")
    print(f"Total trades: {result.total_trades}")
    print(f"Win rate: {result.win_rate}")
    print(f"Avg win: {result.avg_win}")
    print(f"Avg loss: {result.avg_loss}")
    print(f"Profit factor: {profit_factor}")
    print(f"Best streak: {result.best_streak}")
    print(f"Best regime: {result.best_regime}")
    print(f"Regime breakdown: {result.regime_stats}")

    print("")
    print("=== COMPLETE BacktestResults OBJECT ===")
    payload = result.model_dump()
    payload["profit_factor_derived"] = profit_factor
    print(json.dumps(_json_safe(payload), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
