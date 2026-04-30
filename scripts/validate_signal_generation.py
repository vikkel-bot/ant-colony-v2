from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.lab.pc2_validation import POSITIVE_EDGE_ASSETS, default_logs_root, default_output_dir, run_signal_generation_validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate current signal flow for positive-edge equity canary assets.")
    parser.add_argument("--logs-root", default=str(default_logs_root()))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--assets", default=",".join(POSITIVE_EDGE_ASSETS))
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    result = run_signal_generation_validation(
        output_dir=output_dir,
        logs_root=Path(args.logs_root).resolve(),
        assets=[a.strip() for a in args.assets.split(",") if a.strip()],
    )
    blocked = [r for r in result["rows"] if r["verdict"] != "SIGNAL_FLOWING"]
    print(f"signal_generation.csv: {output_dir / 'signal_generation.csv'}")
    print(f"signal_generation_summary.md: {output_dir / 'signal_generation_summary.md'}")
    print(f"non-flowing assets: {len(blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
