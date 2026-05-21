from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.biome.biome_adapter import MarketData
from scripts.harness.harness_volatility_squeeze import (
    SWEEP_BB_MULTIPLIERS,
    SWEEP_KC_MULTIPLIERS,
    SWEEP_SL_PCTS,
    SWEEP_TP_PCTS,
    TIMEFRAME,
    _backtest_symbol,
    _compute_squeeze_state,
    _fetch_candles,
    _run_sweep,
    build_report,
    build_sweep_report,
    write_report,
    write_sweep_report,
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


def _squeeze_base(count: int = 40) -> list[MarketData]:
    return [_candle(idx, close=100.0, high=101.0, low=99.0) for idx in range(count)]


def test_squeeze_release_generates_long_trade() -> None:
    candles = _squeeze_base()
    candles.append(_candle(40, close=130.0, high=130.5, low=129.5))
    candles.append(_candle(41, close=142.0, high=143.0, low=141.0))

    state = _compute_squeeze_state(candles)
    trades = _backtest_symbol(candles)

    assert state["squeeze_on"][39]
    assert state["release"][40]
    assert len(trades) == 1
    assert trades[0]["direction"] == "long"
    assert trades[0]["exit_reason"] == "tp"
    assert trades[0]["return_pct"] > 0


def test_squeeze_release_generates_short_trade() -> None:
    candles = _squeeze_base()
    candles.append(_candle(40, close=70.0, high=70.5, low=69.5))
    candles.append(_candle(41, close=63.0, high=64.0, low=62.0))

    state = _compute_squeeze_state(candles)
    trades = _backtest_symbol(candles)

    assert state["squeeze_on"][39]
    assert state["release"][40]
    assert len(trades) == 1
    assert trades[0]["direction"] == "short"
    assert trades[0]["exit_reason"] == "tp"
    assert trades[0]["return_pct"] > 0


def test_fetch_candles_paginates_when_first_batch_hits_bitvavo_limit() -> None:
    batch1 = [_candle(i, close=100.0) for i in range(1440)]
    batch2 = [_candle(i - 1440, close=90.0) for i in range(12)]
    calls: list[dict] = []

    class Adapter:
        def get_candles(self, symbol, timeframe, limit=1440, end_ms=None):
            calls.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "limit": limit,
                "end_ms": end_ms,
            })
            return batch1 if end_ms is None else batch2

    candles = _fetch_candles(Adapter(), "BTC-EUR")

    assert candles[:len(batch2)] == batch2
    assert candles[len(batch2):] == batch1
    assert calls[0] == {
        "symbol": "BTC-EUR",
        "timeframe": TIMEFRAME,
        "limit": 1440,
        "end_ms": None,
    }
    assert calls[1]["limit"] == 1440
    assert calls[1]["end_ms"] == int(batch1[0].timestamp.timestamp() * 1000) - 1


def test_report_contains_go_no_go_and_writes_file(tmp_path: Path) -> None:
    trades = [{"return_pct": 0.1, "bars_held": 2}, {"return_pct": -0.02, "bars_held": 2}]
    metrics = {"n": 2, "win_rate": 50.0, "sharpe": 0.5, "max_dd_pct": 2.0}
    report = build_report([("BTC-EUR", trades, metrics)])
    out_path = write_report(report, logs_root=tmp_path)

    assert "=== VOLATILITY SQUEEZE HARNESS ===" in report
    assert "Criteria: Sharpe > 1.0 EN max DD < 10% EN winrate > 55%" in report
    assert "Verdict:" in report
    assert out_path.exists()
    assert out_path.parent == tmp_path / "harness"


def test_sweep_runs_all_parameter_combinations_and_reports_top5(tmp_path: Path) -> None:
    candles = _squeeze_base()
    candles.append(_candle(40, close=130.0, high=130.5, low=129.5))
    candles.append(_candle(41, close=142.0, high=143.0, low=141.0))

    rows = _run_sweep(candles)
    report = build_sweep_report(rows)
    out_path = write_sweep_report(report, logs_root=tmp_path)

    assert len(rows) == (
        len(SWEEP_BB_MULTIPLIERS)
        * len(SWEEP_KC_MULTIPLIERS)
        * len(SWEEP_SL_PCTS)
        * len(SWEEP_TP_PCTS)
    )
    assert "=== VOLATILITY SQUEEZE PARAMETER SWEEP ===" in report
    assert "Gesorteerd op Sharpe, top-5:" in report
    top_lines = [line for line in report.splitlines() if line[:2] in {"1.", "2.", "3.", "4.", "5."}]
    assert len(top_lines) == 5
    assert out_path.exists()
    assert out_path.name.startswith("squeeze_sweep_")
