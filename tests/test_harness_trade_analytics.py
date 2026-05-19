from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.harness.harness_trade_analytics import (
    build_report,
    load_closed_trades,
    write_report,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_loads_last_closed_trades_from_paper_event_logs(tmp_path: Path) -> None:
    now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    rows: list[dict] = []
    for idx in range(6):
        opened = now + timedelta(hours=idx)
        closed = opened + timedelta(hours=2)
        rows.append({
            "timestamp": opened.isoformat(),
            "action": "trade_opened",
            "position_id": f"p-{idx}",
            "symbol": "BTC-EUR",
            "strategy_type": "sma_crossover",
            "biome": "crypto",
            "entry_price": 100.0,
        })
        rows.append({
            "timestamp": closed.isoformat(),
            "action": "trade_closed",
            "position_id": f"p-{idx}",
            "symbol": "BTC-EUR",
            "pnl_net": 10.0 if idx % 2 == 0 else -5.0,
            "exit_reason": "take_profit" if idx % 2 == 0 else "stop_loss",
            "exit_time": closed.isoformat(),
        })

    _write_jsonl(tmp_path / "paper" / "paper-test.jsonl", rows)

    trades, sources = load_closed_trades(logs_root=tmp_path, repo_root=tmp_path)

    assert len(trades) == 6
    assert sources == [tmp_path / "paper" / "paper-test.jsonl"]
    assert trades[-1]["duration_hours"] == 2.0
    assert trades[-1]["strategy_type"] == "sma_crossover"
    assert trades[-1]["biome"] == "crypto"


def test_supports_nested_payload_audit_events(tmp_path: Path) -> None:
    now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    rows = [
        {
            "timestamp": now.isoformat(),
            "payload": {
                "action": "trade_opened",
                "position_id": "p-1",
                "symbol": "XLK",
                "strategy_type": "momentum",
                "biome": "equities",
            },
        },
        {
            "timestamp": (now + timedelta(hours=3)).isoformat(),
            "payload": {
                "action": "trade_closed",
                "position_id": "p-1",
                "symbol": "XLK",
                "pnl_gross": 3.25,
                "exit_reason": "trailing_stop",
            },
        },
    ]
    _write_jsonl(tmp_path / "paper" / "paper-test.jsonl", rows)

    trades, _ = load_closed_trades(logs_root=tmp_path, repo_root=tmp_path)

    assert len(trades) == 1
    assert trades[0]["pnl"] == 3.25
    assert trades[0]["strategy_type"] == "momentum"
    assert trades[0]["biome"] == "equities"


def test_build_report_contains_required_sections_and_writes_file(tmp_path: Path) -> None:
    now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    trades = [
        {
            "symbol": "BTC-EUR",
            "strategy_type": "rsi_based",
            "biome": "crypto",
            "exit_reason": "take_profit",
            "pnl": 2.0,
            "closed_at": now + timedelta(hours=i),
            "duration_hours": 1.5,
        }
        for i in range(5)
    ]

    report = build_report(trades)
    out_path = write_report(report, logs_root=tmp_path)

    assert "=== TRADE ANALYTICS RAPPORT ===" in report
    assert "--- WIN RATE PER STRATEGY_TYPE ---" in report
    assert "--- GEM. PnL PER BIOME ---" in report
    assert "--- TOP-3 EXIT-REDENEN ---" in report
    assert "--- GEM. TRADE DURATION PER STRATEGY_TYPE ---" in report
    assert out_path.exists()
    assert out_path.parent == tmp_path / "analytics"
