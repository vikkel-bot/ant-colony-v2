"""
Fetch Watchtower replay signals and run the Colony WatchtowerBacktestAnt.

This script is read-only toward exchanges: it fetches Watchtower signals,
loads historical candles through Colony adapters, writes a replay report, and
prints the decision metrics Queen needs later.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("BITVAVO_PAPER_MODE", "true")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.ants.watchtower_backtest_ant import WatchtowerBacktestAnt  # noqa: E402
from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter  # noqa: E402
from ant_colony.biome.biome_registry import BiomeRegistry  # noqa: E402
from ant_colony.clients.watchtower_client import WatchtowerClient  # noqa: E402
from ant_colony.lab.watchtower_backtester import (  # noqa: E402
    WatchtowerBacktestAssumptions,
    append_watchtower_export,
)
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logs_root = Path(args.logs_root or os.getenv("ANT_LOGS", "logs")).resolve()
    logs_root.mkdir(parents=True, exist_ok=True)

    if not args.no_fetch:
        client = WatchtowerClient(base_url=args.watchtower_url, timeout=args.timeout)
        if args.signal_source == "seed":
            seed_signals = client.get_seed_signals(
                limit=args.limit,
                asset=args.asset,
                from_dt=args.from_ts,
                to_dt=args.to_ts,
                min_entry_score=args.min_entry_score,
            )
            seed_signals = _filter_seed_signals(seed_signals, args)
            packet = _seed_export_packet(seed_signals, args)
        else:
            packet = client.get_backtest_signals(
                limit=args.limit,
                asset=args.asset,
                asset_class=args.asset_class,
                exchange=args.exchange,
                region=args.region,
                from_ts=args.from_ts,
                to_ts=args.to_ts,
            )
        if not client.last_get_succeeded:
            print(f"Watchtower export ophalen mislukt: {client.base_url}")
            return 2

        replay_path = append_watchtower_export(logs_root, packet)
        print(f"Watchtower {args.signal_source} export opgehaald: {len(packet.get('signals', []))} signalen")
        print(f"Replay input: {replay_path}")
    else:
        print("Watchtower fetch overgeslagen; bestaande replay logs worden gebruikt.")

    registry = BiomeRegistry()
    registry.register(BitvavoAdapter(paper_only=True))

    ant = WatchtowerBacktestAnt(
        ant_id=f"wtbt-{uuid.uuid4().hex[:12]}",
        mission=_mission(_symbols(args.symbols)),
        scheduler=None,
        logs_root=logs_root,
        biome_registry=registry,
        assumptions=_assumptions(args),
        timeframe=args.timeframe,
        candle_limit=args.candle_limit,
        signal_source=args.signal_source,
        min_market_quality=args.min_market_quality,
        include_neutral=args.include_neutral,
    )
    report = ant.run_once()
    _print_report(report)

    if args.print_json:
        print(json.dumps(report, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Watchtower signals and run a conservative replay backtest.")
    parser.add_argument("--watchtower-url", default=os.getenv("WATCHTOWER_URL", "http://127.0.0.1:8011"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("WATCHTOWER_TIMEOUT", "10")))
    parser.add_argument("--logs-root", default=os.getenv("ANT_LOGS", "logs"))
    parser.add_argument("--asset-class", default="crypto")
    parser.add_argument("--exchange", default="BITVAVO")
    parser.add_argument("--asset")
    parser.add_argument("--region")
    parser.add_argument("--from", dest="from_ts")
    parser.add_argument("--to", dest="to_ts")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--signal-source", choices=["live", "seed"], default="live")
    parser.add_argument("--min-entry-score", type=float, default=0.5)
    parser.add_argument("--min-market-quality", choices=["historical"], help="Skip seed signals without historical market candles.")
    parser.add_argument("--include-neutral", action="store_true", help="Replay neutral Watchtower signals as long for edge analysis.")
    parser.add_argument("--symbols", default="GLOBAL", help="Comma-separated symbols, or GLOBAL for all exported signals.")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--candle-limit", type=int, default=1000)
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument("--notional-per-trade", type=float, default=500.0)
    parser.add_argument("--fee-pct-per-side", type=float, default=0.0025)
    parser.add_argument("--slippage-pct", type=float, default=0.0010)
    parser.add_argument("--take-profit-pct", type=float, default=0.03)
    parser.add_argument("--stop-loss-pct", type=float, default=0.015)
    parser.add_argument("--max-bars-held", type=int, default=24)
    parser.add_argument("--max-concurrent-positions", type=int, default=1)
    parser.add_argument("--allow-short", action="store_true")
    parser.add_argument("--no-fetch", action="store_true", help="Use existing ANT_LOGS/watchtower/signals.jsonl only.")
    parser.add_argument("--print-json", action="store_true")
    return parser.parse_args(argv)


def _seed_export_packet(signals: list[dict], args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source": "watchtower",
        "export_type": "seed_signals",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(signals),
        "filters": {
            "asset": args.asset,
            "asset_class": args.asset_class,
            "exchange": args.exchange,
            "region": args.region,
            "from": args.from_ts,
            "to": args.to_ts,
            "min_entry_score": args.min_entry_score,
            "min_market_quality": args.min_market_quality,
            "include_neutral": args.include_neutral,
            "signal_source": "seed",
        },
        "signals": signals,
    }


def _filter_seed_signals(signals: list[dict], args: argparse.Namespace) -> list[dict]:
    filtered = []
    for signal in signals:
        if args.asset_class and signal.get("asset_class") and str(signal["asset_class"]).lower() != args.asset_class.lower():
            continue
        if args.exchange and signal.get("exchange") and str(signal["exchange"]).upper() != args.exchange.upper():
            continue
        if args.region and signal.get("region") and str(signal["region"]).lower() != args.region.lower():
            continue
        if args.min_market_quality == "historical":
            quality = str(signal.get("seed_market_quality") or "").lower()
            if quality != "historical":
                continue
        filtered.append(signal)
    return filtered


def _symbols(value: str) -> list[str]:
    symbols = [item.strip().upper() for item in value.split(",") if item.strip()]
    return symbols or ["GLOBAL"]


def _mission(symbols: list[str]) -> Mission:
    return Mission(
        mission_id="watchtower-backtest-cli",
        ant_type="watchtower_backtest_ant",
        allowed_node="local-cli",
        allowed_actions=["read_data", "backtest", "report"],
        market_scope=MarketScope(biome="crypto", symbols=symbols),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=1.0,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="Replay Watchtower exported signals."),
    )


def _assumptions(args: argparse.Namespace) -> WatchtowerBacktestAssumptions:
    return WatchtowerBacktestAssumptions(
        starting_equity=args.starting_equity,
        notional_per_trade=args.notional_per_trade,
        fee_pct_per_side=args.fee_pct_per_side,
        slippage_pct=args.slippage_pct,
        default_take_profit_pct=args.take_profit_pct,
        default_stop_loss_pct=args.stop_loss_pct,
        max_bars_held=args.max_bars_held,
        max_concurrent_positions=args.max_concurrent_positions,
        allow_short=args.allow_short,
    )


def _print_report(report: dict[str, Any]) -> None:
    print("\nWatchtower Backtest Report")
    print("=" * 28)
    print(f"Report file       : {report.get('report_path', 'n/a')}")
    print(f"Signals           : {report.get('total_signals', 0)}")
    print(f"Trades            : {report.get('total_trades', 0)}")
    print(f"Skipped signals   : {report.get('skipped_signals', 0)}")
    skipped_by_reason = report.get("skipped_by_reason") or {}
    if skipped_by_reason:
        print("Skipped by reason : " + ", ".join(f"{key}={value}" for key, value in skipped_by_reason.items()))
    print(f"Win rate          : {_pct(report.get('win_rate'))}")
    print(f"Expectancy        : {_pct(report.get('expectancy_pct'))}")
    print(f"Avg return/trade  : {_pct(report.get('avg_net_return_pct'))}")
    print(f"Max drawdown      : {_pct(report.get('max_drawdown_pct'))}")
    print(f"Profit factor     : {_value(report.get('profit_factor'))}")
    print(f"Sharpe            : {_value(report.get('sharpe_ratio'))}")
    print(f"Total net PnL     : {_money(report.get('total_net_pnl'))}")

    print("\nPer asset")
    by_symbol = report.get("by_symbol") or {}
    if not by_symbol:
        print("  n/a")
    for symbol, stats in by_symbol.items():
        print(
            f"  {symbol}: trades={stats.get('trade_count', 0)} "
            f"win={_pct(stats.get('win_rate'))} "
            f"avg={_pct(stats.get('avg_net_return_pct'))} "
            f"pnl={_money(stats.get('total_net_pnl'))}"
        )

    print("\nCross-field")
    cross = report.get("cross_field") or {}
    print(f"  Signals with linked assets : {cross.get('signals_with_linked_assets', 0)}")
    print(f"  Trades with linked assets  : {cross.get('trades_with_linked_assets', 0)}")
    print(f"  Cross-field win rate       : {_pct(cross.get('win_rate'))}")
    print(f"  Cross-field avg return     : {_pct(cross.get('avg_net_return_pct'))}")


def _pct(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value) * 100:.2f}%"


def _money(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}"


def _value(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
