from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.pc2_validation import (
    VALIDATION_ASSETS,
    default_logs_root,
    default_output_dir,
    parse_dt,
    run_price_validation,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare PC2 broker equity candles against yfinance candles.")
    parser.add_argument("--logs-root", default=str(default_logs_root()))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--broker-cache-dir", default="")
    parser.add_argument("--from", dest="from_dt", default="2023-04-29T00:00:00Z")
    parser.add_argument("--to", dest="to_dt", default="2026-04-29T00:00:00Z")
    parser.add_argument("--assets", default=",".join(VALIDATION_ASSETS))
    parser.add_argument("--no-broker-api", action="store_true", help="Only inspect local broker/cache files; do not call IBKR.")
    args = parser.parse_args()

    from_dt = parse_dt(args.from_dt)
    to_dt = parse_dt(args.to_dt)
    if from_dt is None or to_dt is None:
        raise SystemExit("Invalid --from/--to datetime")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    broker_cache = Path(args.broker_cache_dir).resolve() if args.broker_cache_dir else None
    result = run_price_validation(
        output_dir=output_dir,
        logs_root=Path(args.logs_root).resolve(),
        from_dt=from_dt,
        to_dt=to_dt,
        assets=[a.strip() for a in args.assets.split(",") if a.strip()],
        broker_cache_dir=broker_cache,
        use_broker_api=not args.no_broker_api,
    )
    blocked = [r for r in result["rows"] if r.get("canary_blocked")]
    print(f"price_validation.csv: {output_dir / 'price_validation.csv'}")
    print(f"price_validation_summary.md: {output_dir / 'price_validation_summary.md'}")
    print(f"CANARY_BLOCKED assets: {len(blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
