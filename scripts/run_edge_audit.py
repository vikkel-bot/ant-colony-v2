"""
Run the ANT COLONY v2 read-only edge audit.

The script writes:
  - backtest_matrix.csv/json
  - expectancy_decomposition.csv
  - watchtower_signal_audit.json
  - watchtower_signal_summary.csv
  - minimum_viable_activity.csv
  - audit_summary.md

It does not change live trading logic, thresholds, positions, or Watchtower.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("BITVAVO_PAPER_MODE", "true")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.edge_audit import (  # noqa: E402
    CRYPTO_ASSETS,
    CRYPTO_MR_ASSETS,
    EQUITY_3YR_ASSETS,
    EQUITY_ASSETS,
    AuditAssumptions,
    run_mean_reversion_v2_audit,
    run_equity_3yr_audit,
    run_full_edge_audit,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mean_reversion_v2:
        from_dt = _parse_dt(args.from_ts) if args.from_ts else datetime(2023, 4, 29, tzinfo=timezone.utc)
        to_dt = _parse_dt(args.to_ts) if args.to_ts else datetime(2026, 4, 29, tzinfo=timezone.utc)
    elif args.equity_3yr:
        from_dt = _parse_dt(args.from_ts) if args.from_ts else datetime(2023, 4, 29, tzinfo=timezone.utc)
        to_dt = _parse_dt(args.to_ts) if args.to_ts else datetime(2026, 4, 29, tzinfo=timezone.utc)
    else:
        from_dt = _parse_dt(args.from_ts) if args.from_ts else datetime.now(timezone.utc) - timedelta(days=365)
        to_dt = _parse_dt(args.to_ts) if args.to_ts else datetime.now(timezone.utc)
    output_dir = Path(args.output_dir or _default_output_dir()).resolve()
    logs_root = Path(args.logs_root or os.getenv("ANT_LOGS", "logs")).resolve()
    fee_pct_per_side = args.fee_pct_per_side
    if args.equity_3yr and args.data_source == "yfinance" and fee_pct_per_side == 0.0025:
        fee_pct_per_side = 0.0010
    assumptions = AuditAssumptions(
        starting_equity=args.starting_equity,
        risk_per_trade=args.risk_per_trade,
        fee_pct_per_side=fee_pct_per_side,
        slippage_pct=args.slippage_pct,
        benchmark_annual_return=args.benchmark_annual_return,
        risk_free_rate=args.risk_free_rate,
        watchtower_max_hold_bars=args.watchtower_max_hold_bars,
    )

    if args.mean_reversion_v2:
        manifest = run_mean_reversion_v2_audit(
            output_dir=output_dir,
            from_dt=from_dt,
            to_dt=to_dt,
            crypto_assets=_split(args.crypto_assets) or list(CRYPTO_MR_ASSETS),
            assumptions=assumptions,
            use_network=not args.no_network,
            cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
            include_short=args.include_short,
        )
    elif args.equity_3yr:
        equity_assets = _split(args.equity_assets)
        if args.equity_assets == ",".join(EQUITY_ASSETS):
            equity_assets = list(EQUITY_3YR_ASSETS)
        manifest = run_equity_3yr_audit(
            output_dir=output_dir,
            from_dt=from_dt,
            to_dt=to_dt,
            equity_assets=equity_assets or list(EQUITY_3YR_ASSETS),
            data_source=args.data_source,
            assumptions=assumptions,
            use_network=not args.no_network,
            cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
        )
    else:
        manifest = run_full_edge_audit(
            output_dir=output_dir,
            logs_root=logs_root,
            from_dt=from_dt,
            to_dt=to_dt,
            timeframe=args.timeframe,
            crypto_assets=_split(args.crypto_assets),
            equity_assets=_split(args.equity_assets),
            assumptions=assumptions,
            use_network=not args.no_network,
            cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
        )

    print("Edge audit afgerond")
    print(f"Output map        : {manifest['output_dir']}")
    print(f"Matrix rows       : {manifest['matrix_rows']}")
    print(f"Matrix trades     : {manifest['matrix_trades']}")
    if not args.equity_3yr and not args.mean_reversion_v2:
        print(f"Watchtower trades : {manifest['watchtower_signals']}")
        print(f"Trade-log groepen : {manifest['trade_log_rows']}")
    print("Belangrijkste rapport:")
    if args.mean_reversion_v2:
        summary_name = "mean_reversion_v2_summary.md"
    else:
        summary_name = "equity_audit_summary.md" if args.equity_3yr else "audit_summary.md"
    print(f"  {Path(manifest['output_dir']) / summary_name}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only ANT COLONY v2 edge audit before pipeline repair.")
    parser.add_argument("--from", dest="from_ts", help="Out-of-sample start, ISO8601. Default: now - 365d.")
    parser.add_argument("--to", dest="to_ts", help="Out-of-sample end, ISO8601. Default: now.")
    parser.add_argument("--timeframe", default="1h", help="Crypto candle timeframe. Equities use 1d.")
    parser.add_argument("--crypto-assets", default=",".join(CRYPTO_ASSETS))
    parser.add_argument("--equity-assets", default=",".join(EQUITY_ASSETS))
    parser.add_argument("--logs-root", default=os.getenv("ANT_LOGS", "logs"))
    parser.add_argument("--output-dir")
    parser.add_argument("--cache-dir", help="Optional candle cache directory. CSV files are reused if present.")
    parser.add_argument("--no-network", action="store_true", help="Use only cached candle data and local logs.")
    parser.add_argument("--equity-3yr", action="store_true", help="Run the equity-only 3-year extension.")
    parser.add_argument("--mean-reversion-v2", action="store_true", help="Run the crypto mean_reversion_v2 fixed-parameter audit.")
    parser.add_argument("--include-short", action="store_true", help="For mean_reversion_v2, test shorts even when long has no POSITIVE_EDGE.")
    parser.add_argument("--data-source", choices=("live", "yfinance"), default="live", help="Equity data source. yfinance rows are marked EXTERNAL_DATA_SOURCE.")
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--fee-pct-per-side", type=float, default=0.0025)
    parser.add_argument("--slippage-pct", type=float, default=0.0010)
    parser.add_argument("--risk-free-rate", type=float, default=0.0)
    parser.add_argument("--benchmark-annual-return", type=float, default=0.106)
    parser.add_argument("--watchtower-max-hold-bars", type=int, default=120)
    return parser.parse_args(argv)


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _split(value: str) -> list[str]:
    return [item.strip().upper() for item in (value or "").split(",") if item.strip()]


def _default_output_dir() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return str(_REPO_ROOT / "logs" / "edge_audit" / stamp)


if __name__ == "__main__":
    raise SystemExit(main())
