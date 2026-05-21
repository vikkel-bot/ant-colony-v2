from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.biome.biome_adapter import MarketData
from scripts.harness.harness_trend_following import (
    build_report,
    calculate_metrics,
    simulate_symbol,
    write_report,
)


def _candle(
    idx: int,
    *,
    symbol: str = "BTC-EUR",
    close: float,
    high: float | None = None,
    low: float | None = None,
) -> MarketData:
    high = high if high is not None else close + 1.0
    low = low if low is not None else close - 1.0
    return MarketData(
        symbol=symbol,
        timeframe="1h",
        timestamp=datetime(2026, 5, 21, tzinfo=timezone.utc) + timedelta(hours=idx),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100.0,
        biome_id="crypto",
    )


def test_simulate_symbol_opens_long_on_20_bar_high_breakout() -> None:
    candles = [_candle(i, close=99.0, high=100.0, low=95.0) for i in range(20)]
    candles.append(_candle(20, close=101.0, high=102.0, low=100.0))
    candles.append(_candle(21, close=94.0, high=95.0, low=93.0))

    trades = simulate_symbol("BTC-EUR", candles)

    assert len(trades) == 1
    assert trades[0].direction == "long"
    assert trades[0].entry_price == 101.0
    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].pnl_pct < 0


def test_simulate_symbol_opens_short_on_20_bar_low_breakdown() -> None:
    candles = [_candle(i, close=102.0, high=105.0, low=100.0) for i in range(20)]
    candles.append(_candle(20, close=99.0, high=100.0, low=98.0))
    candles.append(_candle(21, close=105.0, high=106.0, low=104.0))

    trades = simulate_symbol("BTC-EUR", candles)

    assert len(trades) == 1
    assert trades[0].direction == "short"
    assert trades[0].entry_price == 99.0
    assert trades[0].exit_reason == "stop_loss"
    assert trades[0].pnl_pct < 0


def test_build_report_contains_go_no_go_and_writes_file(tmp_path: Path) -> None:
    candles = [_candle(i, close=99.0, high=100.0, low=95.0) for i in range(20)]
    candles.extend(_candle(i, close=101.0 + i, high=102.0 + i, low=100.0 + i) for i in range(20, 25))
    trades_by_symbol = {"BTC-EUR": simulate_symbol("BTC-EUR", candles)}
    report = build_report(trades_by_symbol, {"BTC-EUR": candles})
    out_path = write_report(report, logs_root=tmp_path)
    metrics = calculate_metrics(trades_by_symbol["BTC-EUR"])

    assert "=== TREND FOLLOWING HARNESS RAPPORT ===" in report
    assert "--- GO/NO-GO ---" in report
    assert "Verdict:" in report
    assert metrics.trades >= 1
    assert out_path.exists()
    assert out_path.parent == tmp_path / "harness"
