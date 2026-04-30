from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.pc2_validation import default_logs_root, default_output_dir, run_fee_validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate broker fee and slippage assumptions from PC2 logs/config.")
    parser.add_argument("--logs-root", default=str(default_logs_root()))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--capital", type=float, default=150_000.0)
    parser.add_argument("--equity-fee", type=float, default=0.0010, help="Equity fee per side used for validation.")
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
    result = run_fee_validation(
        output_dir=output_dir,
        logs_root=Path(args.logs_root).resolve(),
        repo_root=repo_root,
        equity_fee_per_side=args.equity_fee,
        risk_per_trade=args.risk_per_trade,
        capital=args.capital,
    )
    print(f"fee_validation.md: {output_dir / 'fee_validation.md'}")
    print(f"fee_adjusted_expectancy.csv: {output_dir / 'fee_adjusted_expectancy.csv'}")
    print(f"FEE_ASSUMPTION_INVALID: {result['fee_invalid']}")
    print(f"SLIPPAGE_ASSUMPTION_INVALID: {result['slippage_invalid']}")
    print(f"CANARY_BLOCKED combinations: {len(result['canary_blocked'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
