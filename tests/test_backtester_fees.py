from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ant_colony.lab.backtester import BacktestConfig, Backtester, OHLCVBar


def _bars_for_repeated_wins() -> list[OHLCVBar]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prices = []
    for _ in range(8):
        prices.extend([100.0, 106.0, 100.0, 104.0])
    return [
        OHLCVBar(
            timestamp=base + timedelta(days=i),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1000.0,
        )
        for i, price in enumerate(prices)
    ]


def _run_with_costs(fee_pct: float, slippage_pct: float):
    config = BacktestConfig(
        direction="long",
        take_profit_pct=0.50,
        stop_loss_pct=0.50,
        max_bars_held=1,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    return Backtester().run(_bars_for_repeated_wins(), config)


def test_fees_and_slippage_reduce_sharpe_and_total_profit() -> None:
    no_costs = _run_with_costs(fee_pct=0.0, slippage_pct=0.0)
    with_costs = _run_with_costs(fee_pct=0.0025, slippage_pct=0.001)

    assert no_costs.sharpe_ratio is not None
    assert with_costs.sharpe_ratio is not None
    assert with_costs.sharpe_ratio < no_costs.sharpe_ratio
    assert (
        with_costs.extra["total_return_pct"]
        < no_costs.extra["total_return_pct"]
    )
    assert no_costs.total_fees_pct == pytest.approx(0.0)
    assert with_costs.total_fees_pct > 0.0
