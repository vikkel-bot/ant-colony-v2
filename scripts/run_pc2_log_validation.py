from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.pc2_validation import (
    default_logs_root,
    default_output_dir,
    parse_dt,
    run_full_pc2_validation,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all PC2 canary validation checks and write validation_report.md.")
    parser.add_argument("--logs-root", default=str(default_logs_root()))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--broker-cache-dir", default="")
    parser.add_argument("--from", dest="from_dt", default="2023-04-29T00:00:00Z")
    parser.add_argument("--to", dest="to_dt", default="2026-04-29T00:00:00Z")
    parser.add_argument("--no-broker-api", action="store_true", help="Only inspect local broker/cache files; do not call IBKR.")
    parser.add_argument("--capital", type=float, default=150_000.0)
    parser.add_argument("--equity-fee", type=float, default=0.0010, help="Equity fee per side used for validation.")
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    args = parser.parse_args()

    from_dt = parse_dt(args.from_dt)
    to_dt = parse_dt(args.to_dt)
    if from_dt is None or to_dt is None:
        raise SystemExit("Invalid --from/--to datetime")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    broker_cache = Path(args.broker_cache_dir).resolve() if args.broker_cache_dir else None
    result = run_full_pc2_validation(
        output_dir=output_dir,
        logs_root=Path(args.logs_root).resolve(),
        from_dt=from_dt,
        to_dt=to_dt,
        broker_cache_dir=broker_cache,
        use_broker_api=not args.no_broker_api,
        capital=args.capital,
        equity_fee_per_side=args.equity_fee,
        risk_per_trade=args.risk_per_trade,
    )
    print(f"validation_report.md: {output_dir / 'validation_report.md'}")
    for line in result["report"].splitlines():
        if line in {"CLEAR_TO_DEPLOY", "BLOCKED_PENDING_SIGNAL_FLOW", "BLOCKED_PENDING_LIVE_DATA", "DO_NOT_DEPLOY", "BLOCKED_PENDING_VALIDATION"}:
            print(f"final verdict: {line}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
