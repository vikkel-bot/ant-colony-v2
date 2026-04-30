from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.pc2_validation import default_logs_root, default_output_dir, run_live_trade_validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare PC2 equity paper trades against backtest expectancy.")
    parser.add_argument("--logs-root", default=str(default_logs_root()))
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    result = run_live_trade_validation(output_dir=output_dir, logs_root=Path(args.logs_root).resolve())
    print(f"live_trade_validation.csv: {output_dir / 'live_trade_validation.csv'}")
    print(f"live_trade_validation_summary.md: {output_dir / 'live_trade_validation_summary.md'}")
    print(f"equity paper trades inspected: {len(result['trades'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
