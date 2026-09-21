from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.lab.backtester import OHLCVBar
from ant_colony.lab.edge_audit import (
    AuditAssumptions,
    StrategySpec,
    audit_watchtower_signals,
    build_expectancy_matrix,
    decompose_trade_logs,
    edge_status,
    minimum_viable_activity,
    run_equity_3yr_audit,
    run_full_edge_audit,
    run_mean_reversion_v2_audit,
)
from ant_colony.strategies.mean_reversion_v2 import MeanReversionStrategy


def _bars(n: int = 260, start: float = 100.0, step: float = 0.8) -> list[OHLCVBar]:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = []
    price = start
    for i in range(n):
        price += step
        result.append(
            OHLCVBar(
                timestamp=base + timedelta(hours=i),
                open=price * 0.995,
                high=price * 1.02,
                low=price * 0.99,
                close=price,
                volume=1000.0,
            )
        )
    return result


def _daily_bars(n: int = 900, start: float = 100.0, step: float = 0.08) -> list[OHLCVBar]:
    base = datetime(2022, 1, 1, tzinfo=timezone.utc)
    result = []
    price = start
    for i in range(n):
        price += step
        result.append(
            OHLCVBar(
                timestamp=base + timedelta(days=i),
                open=price * 0.995,
                high=price * 1.015,
                low=price * 0.985,
                close=price,
                volume=1000.0,
            )
        )
    return result


def _sideways_crypto_bars(n: int = 2600) -> list[OHLCVBar]:
    base = datetime(2022, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        cycle = (i % 48) / 48.0
        price = 100.0 + (4.0 * (0.5 - abs(cycle - 0.5)) * 2.0)
        if i % 53 == 0:
            price -= 6.0
        volume = 2000.0 if i % 53 == 0 else 1000.0
        result.append(
            OHLCVBar(
                timestamp=base + timedelta(hours=i),
                open=price * 1.002,
                high=price * 1.015,
                low=price * 0.985,
                close=price,
                volume=volume,
            )
        )
    return result


def _local_tmp(name: str) -> Path:
    path = Path.cwd() / ".codex_test_tmp" / f"{name}_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def test_edge_status_flags_positive_edge() -> None:
    assert edge_status(0.08, 1.4, 30) == "POSITIVE_EDGE"
    assert edge_status(0.01, 1.1, 30) == "MARGINAL"
    assert edge_status(-0.01, 0.9, 30) == "NEGATIVE_EDGE"
    assert edge_status(1.0, 4.0, 10) == "INSUFFICIENT_DATA"


def test_build_expectancy_matrix_emits_rows_and_metrics() -> None:
    bars = _bars(360, start=20.0, step=1.2)
    spec = StrategySpec("always_entry", "audit_always", "long", 0.02, 0.01, 5)
    rows, trades = build_expectancy_matrix(
        bars_by_asset={"BTC-EUR": bars},
        specs=[spec],
        from_dt=bars[180].timestamp,
        to_dt=bars[-1].timestamp,
        assumptions=AuditAssumptions(fee_pct_per_side=0.0, slippage_pct=0.0),
    )
    all_row = next(row for row in rows if row["regime"] == "ALL")
    assert all_row["asset"] == "BTC-EUR"
    assert all_row["trade_count"] == len(trades)
    assert all_row["expectancy_r"] is not None


def test_decompose_trade_logs_groups_closed_trades() -> None:
    tmp_path = _local_tmp("decompose")
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "paper.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-01-01T10:00:00Z", "payload": {"action": "trade_closed", "symbol": "BTC-EUR", "strategy_type": "sma_crossover", "pnl_r": -1.0, "pnl_net": -10}}),
                json.dumps({"timestamp": "2026-01-02T10:00:00Z", "payload": {"action": "trade_closed", "symbol": "BTC-EUR", "strategy_type": "sma_crossover", "pnl_r": 2.0, "pnl_net": 20}}),
            ]
        ),
        encoding="utf-8",
    )
    result = decompose_trade_logs(logs_root=tmp_path, assumptions=AuditAssumptions())
    strategy_row = next(row for row in result["rows"] if row["dimension"] == "strategy")
    assert strategy_row["combination"] == "sma_crossover"
    assert strategy_row["trade_count"] == 2
    assert strategy_row["expectancy_r"] == 0.5


def test_watchtower_signal_audit_replays_snapshot() -> None:
    tmp_path = _local_tmp("watchtower")
    wt_dir = tmp_path / "watchtower"
    wt_dir.mkdir()
    ts = datetime(2025, 1, 3, tzinfo=timezone.utc)
    (wt_dir / "signals.jsonl").write_text(
        json.dumps(
            {
                "timestamp": ts.isoformat(),
                "signals": [
                    {
                        "id": "sig-1",
                        "asset": "BTC-EUR",
                        "direction": "long",
                        "timestamp": ts.isoformat(),
                        "confidence": 0.75,
                        "entry_score": 0.8,
                        "asset_class": "crypto",
                        "source_field": "crypto",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bars = _bars(140, start=100.0, step=0.8)
    result = audit_watchtower_signals(
        logs_root=tmp_path,
        bars_by_asset={"BTC-EUR": bars},
        assumptions=AuditAssumptions(fee_pct_per_side=0.0, slippage_pct=0.0, watchtower_max_hold_bars=24),
    )
    assert result["signal_count"] == 1
    assert result["trade_count"] == 1
    assert result["signals"][0]["signal_id"] == "sig-1"


def test_minimum_viable_activity_calculates_required_trades() -> None:
    rows, portfolio = minimum_viable_activity(
        [
            {
                "asset": "BTC-EUR",
                "strategy": "momentum",
                "direction": "long",
                "regime": "ALL",
                "status": "POSITIVE_EDGE",
                "expectancy_r": 0.1,
                "profit_factor": 1.5,
                "trade_count": 100,
            }
        ],
        assumptions=AuditAssumptions(),
        window_days=365,
    )
    assert rows[0]["required_trades_1_0pct"] == 106.0
    assert portfolio["positive_edge_combinations"] == 1


def test_run_full_edge_audit_writes_deliverables_without_network() -> None:
    tmp_path = _local_tmp("full")
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / "BTC_EUR_1h.csv"
    cache_file.write_text(
        "timestamp,open,high,low,close,volume\n"
        + "\n".join(
            f"{bar.timestamp.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}"
            for bar in _bars(260, start=20.0, step=1.2)
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    manifest = run_full_edge_audit(
        output_dir=out,
        logs_root=tmp_path / "logs",
        from_dt=datetime(2025, 1, 10, tzinfo=timezone.utc),
        to_dt=datetime(2025, 1, 12, tzinfo=timezone.utc),
        crypto_assets=["BTC-EUR"],
        equity_assets=[],
        use_network=False,
        cache_dir=cache,
        assumptions=AuditAssumptions(fee_pct_per_side=0.0, slippage_pct=0.0),
    )
    assert manifest["matrix_rows"] > 0
    assert (out / "backtest_matrix.csv").exists()
    assert (out / "audit_summary.md").exists()


def test_run_equity_3yr_audit_writes_source_flags_without_network() -> None:
    tmp_path = _local_tmp("equity3yr")
    cache = tmp_path / "cache"
    cache.mkdir()
    for symbol in ("AAPL", "SPY"):
        (cache / f"{symbol}_1d.csv").write_text(
            "timestamp,open,high,low,close,volume\n"
            + "\n".join(
                f"{bar.timestamp.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}"
                for bar in _daily_bars()
            ),
            encoding="utf-8",
        )
    out = tmp_path / "out"
    manifest = run_equity_3yr_audit(
        output_dir=out,
        from_dt=datetime(2023, 4, 29, tzinfo=timezone.utc),
        to_dt=datetime(2024, 1, 29, tzinfo=timezone.utc),
        equity_assets=["AAPL"],
        data_source="yfinance",
        use_network=False,
        cache_dir=cache,
        assumptions=AuditAssumptions(fee_pct_per_side=0.0010),
    )
    rows = json.loads((out / "equity_matrix_3yr.json").read_text(encoding="utf-8"))
    all_row = next(row for row in rows if row["regime"] == "ALL")
    assert manifest["matrix_rows"] > 0
    assert all_row["data_source"] == "yfinance"
    assert all_row["data_source_flag"] == "EXTERNAL_DATA_SOURCE"
    assert all_row["train_window"] == "360d"
    assert all_row["test_window"] == "90d"
    assert all_row["direction"] == "long"
    assert (out / "equity_audit_summary.md").exists()


def test_mean_reversion_strategy_generates_signal() -> None:
    import pandas as pd

    bars = _sideways_crypto_bars(80)
    df = pd.DataFrame(
        {
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )
    df.attrs["asset"] = "BTC-EUR"
    signals = MeanReversionStrategy(asset="BTC-EUR").generate_signals(df)
    assert signals
    assert signals[0].asset == "BTC-EUR"
    assert signals[0].direction == "long"
    assert signals[0].z_score < 0


def test_run_mean_reversion_v2_audit_writes_outputs_without_network() -> None:
    tmp_path = _local_tmp("meanrev")
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / "BTC_EUR_1h.csv"
    cache_file.write_text(
        "timestamp,open,high,low,close,volume\n"
        + "\n".join(
            f"{bar.timestamp.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}"
            for bar in _sideways_crypto_bars()
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    manifest = run_mean_reversion_v2_audit(
        output_dir=out,
        from_dt=datetime(2022, 4, 29, tzinfo=timezone.utc),
        to_dt=datetime(2022, 5, 29, tzinfo=timezone.utc),
        crypto_assets=["BTC-EUR"],
        use_network=False,
        cache_dir=cache,
    )
    assert manifest["matrix_rows"] > 0
    assert (out / "mean_reversion_v2_matrix.csv").exists()
    assert (out / "mean_reversion_v2_summary.md").exists()
    assert (out / "mean_reversion_v2_comparison.csv").exists()
