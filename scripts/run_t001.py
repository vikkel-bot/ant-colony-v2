"""Voert T001 uit op de gecachte Bitvavo-dagcandles.

Holdout (vanaf 2025-10-01) wordt vóór alles fysiek afgeknipt.
Gebruik: py -3.14 scripts/run_t001.py --out <pad>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ant_colony.lab import xs_rank_test as X  # noqa: E402
from ant_colony.lab.sensor_momentum import mom21  # noqa: E402


def load_cache(cache: Path) -> dict:
    markets = json.loads((cache / "_markets.json").read_text(encoding="utf-8"))
    data = {}
    for m in markets:
        if m.get("quote") != "EUR":
            continue
        f = cache / f"{m['market']}.json"
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))[:-1]  # lopende dag weg
            if rows:
                data[m["market"]] = rows
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="t001_result.json")
    args = ap.parse_args()
    data = X.truncate_before(load_cache(Path(args.cache)), X.HOLDOUT_START)
    latest = max(int(r[0]) for c in data.values() for r in c)
    assert latest < X.HOLDOUT_START, "holdout-data aanwezig na afknippen"
    result = X.run(data, mom21)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
