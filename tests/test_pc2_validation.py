from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.lab import pc2_validation as v


def _local_tmp(name: str) -> Path:
    path = Path.cwd() / ".codex_test_tmp" / f"{name}_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _candle(asset: str, day: int, close: float) -> v.Candle:
    return v.Candle(
        symbol=asset,
        timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000,
    )


def test_price_comparison_flags_match_and_no_broker_data() -> None:
    broker = [_candle("JNJ", 1, 100), _candle("JNJ", 2, 101), _candle("JNJ", 3, 102)]
    yahoo = [_candle("JNJ", 1, 100.01), _candle("JNJ", 2, 101.01), _candle("JNJ", 3, 102.01)]

    row = v.compare_price_series("JNJ", broker, yahoo)
    assert row["verdict"] == "DATA_MATCH"
    assert row["canary_blocked"] is False

    missing = v.compare_price_series("JNJ", [], yahoo)
    assert missing["verdict"] == "NO_BROKER_DATA"
    assert missing["canary_blocked"] is True


def test_price_validation_treats_ibkr_connection_refused_as_no_broker_data() -> None:
    out = _local_tmp("pc2_price_ibkr_refused")
    logs = _local_tmp("pc2_price_ibkr_refused_logs")
    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = from_dt + timedelta(days=3)

    def broker_refused(asset, from_dt, to_dt, **kwargs):
        raise ConnectionRefusedError("TWS niet actief")

    def yahoo(asset, from_dt, to_dt):
        return [_candle(asset, 1, 100), _candle(asset, 2, 101), _candle(asset, 3, 102)]

    result = v.run_price_validation(
        output_dir=out,
        logs_root=logs,
        from_dt=from_dt,
        to_dt=to_dt,
        assets=("JNJ", "GLD"),
        yfinance_loader=yahoo,
        broker_loader=broker_refused,
    )

    assert [row["verdict"] for row in result["rows"]] == ["NO_BROKER_DATA", "NO_BROKER_DATA"]
    assert all(row["canary_blocked"] for row in result["rows"])
    assert result["warnings"]
    assert "IBKR niet bereikbaar" in result["summary"]


def test_adjusted_expectancy_blocks_when_real_fees_are_too_high() -> None:
    rows = v.adjusted_expectancy_rows(actual_fee_per_side=0.0025, actual_slippage_per_side=0.0010)
    jnj = next(row for row in rows if row["combo_id"] == "JNJ:momentum long")
    assert jnj["adjusted_expectancy_r"] < 0
    assert jnj["canary_blocked"] is True


def test_fee_validation_uses_equity_fee_override_not_crypto_broker_fee() -> None:
    out = _local_tmp("pc2_fee_override")
    logs = _local_tmp("pc2_fee_logs")

    result = v.run_fee_validation(
        output_dir=out,
        logs_root=logs,
        equity_fee_per_side=0.0010,
        risk_per_trade=0.01,
        capital=150_000,
    )

    assert result["fee_invalid"] is False
    assert result["canary_blocked"] == []
    assert "Crypto fee_per_side found" in result["summary"]
    assert "Equity fee_per_side used for validation" in result["summary"]


def test_live_trade_summary_confirms_when_observed_matches_backtest() -> None:
    trades = [
        {"asset": "XLK", "strategy": "momentum long", "pnl_r": 0.20, "pnl_eur": 2},
        {"asset": "XLK", "strategy": "momentum long", "pnl_r": 0.25, "pnl_eur": 2},
        {"asset": "XLK", "strategy": "momentum long", "pnl_r": 0.30, "pnl_eur": 3},
        {"asset": "XLK", "strategy": "momentum long", "pnl_r": 0.22, "pnl_eur": 2},
        {"asset": "XLK", "strategy": "momentum long", "pnl_r": 0.26, "pnl_eur": 3},
    ]
    rows = v.summarize_live_trades(trades)
    assert rows[0]["combo_id"] == "XLK:momentum long"
    assert rows[0]["verdict"] == "LIVE_CONFIRMS"


def test_signal_flow_reads_research_and_paper_logs() -> None:
    logs = _local_tmp("pc2_signal_logs")
    research = logs / "research"
    paper = logs / "paper"
    research.mkdir(parents=True)
    paper.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    (research / "ant.jsonl").write_text(
        json.dumps(
            {
                "timestamp": now,
                "payload": {
                    "action": "candidate_accepted",
                    "symbol": "XLK",
                    "strategy_type": "momentum",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (paper / "ant.jsonl").write_text(
        json.dumps(
            {
                "timestamp": now,
                "payload": {
                    "action": "trade_opened",
                    "symbol": "XLK",
                    "biome": "equities",
                    "entry_price": 100,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    row = v.signal_flow_for_asset(logs, "XLK")
    assert row["verdict"] == "SIGNAL_FLOWING"
    assert row["signals_generated"] == 1
    assert row["paper_opens"] == 1


def test_equities_config_validation_confirms_env_and_startup_markers() -> None:
    repo = _local_tmp("pc2_equities_config_repo")
    logs = _local_tmp("pc2_equities_config_logs")
    (repo / ".env").write_text(
        "EQUITIES_ENABLED=true\nIBKR_PAPER_MODE=true\n",
        encoding="utf-8",
    )
    (logs / "startup.log").write_text(
        "\n".join(
            [
                "Equities ant gestart | type=sector_scout_ant  ant_id=abc  ttl=86400",
                "Equities ant gestart | type=fundamental_ant  ant_id=def  ttl=86400",
                "Equities ant gestart | type=dividend_scout_ant  ant_id=ghi  ttl=86400",
                "EquitiesPaperAnt gestart | ant_id=eq-paper  capital=500.00  symbols=XLK,GLD,QQQ",
            ]
        ),
        encoding="utf-8",
    )

    result = v.run_equities_config_validation(logs_root=logs, repo_root=repo)

    assert result["status"] == "CONFIRMED"
    assert result["env_confirmed"] is True
    assert result["startup_confirmed"] is True
    assert result["tradable_symbols_confirmed"] is True


def test_full_runner_writes_report_with_blocked_verdict_without_pc2_data() -> None:
    out = _local_tmp("pc2_validation_out")
    logs = _local_tmp("pc2_validation_empty_logs")
    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = from_dt + timedelta(days=3)

    def no_yahoo(asset, from_dt, to_dt):
        return []

    def no_broker(asset, from_dt, to_dt, **kwargs):
        return []

    price = v.run_price_validation(
        output_dir=out,
        logs_root=logs,
        from_dt=from_dt,
        to_dt=to_dt,
        assets=("JNJ",),
        use_broker_api=False,
        yfinance_loader=no_yahoo,
        broker_loader=no_broker,
    )
    fees = v.run_fee_validation(output_dir=out, logs_root=logs)
    live = v.run_live_trade_validation(output_dir=out, logs_root=logs)
    signals = v.run_signal_generation_validation(output_dir=out, logs_root=logs, assets=("JNJ",))
    report = v.build_validation_report(price=price, fees=fees, live=live, signals=signals, logs_root=logs)

    assert "CANARY DEPLOYMENT VERDICT:" in report
    assert "EQUITIES CONFIG:" in report
    assert "DO_NOT_DEPLOY" in report
