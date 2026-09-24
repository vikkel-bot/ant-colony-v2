"""Bouwt een versioned kostenmodel-artefact uit orderboek-snapshots.

Aggregatie:
  1. per markt en ordergrootte: percentiel over de snapshots van max(koop, verkoop);
  2. per omzetklasse (kwartielen van de 30-daagse mediane dagomzet):
     mediaan over de markten in die klasse.

Het artefact is bedoeld om gecommit en daarna niet meer gewijzigd te worden.
Een nieuwe meting levert een NIEUWE versie op; bestaande registraties blijven
verwijzen naar de versie die gold toen ze werden vastgelegd.

Gebruik:
  py -3.14 scripts/build_cost_model.py --snapshots C:\\colony-lab\\cost_snapshots.jsonl \
      --version COST_MODEL_V1_20260924 --out docs/COST_MODEL_V1_20260924.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

SIZES_EUR = (1_000, 5_000, 10_000, 25_000)
FEE_PER_SIDE = 0.0025          # Bitvavo taker, < EUR 100k/maand; bewust geen volumekorting
ESTIMATOR_FOR_GATE = "p75"     # voorlopige policy choice (kleine steekproef)
REFERENCE_NOTIONAL_EUR = 5_000  # research-referentieschaal, GEEN sizing-regel
N_BUCKETS = 4

LIMITATIONS = [
    "Snapshots meten zichtbare rustende liquiditeit op een moment; bij grote orders kan de "
    "werkelijke impact hoger zijn door terugtrekkende quotes en adverse selection.",
    "Huidige orderboeken; historische orderboeken (2022-2025) zijn niet beschikbaar.",
    "Tarief 0,25% per kant: geen volumekorting waarvan structureel bereiken nog niet is aangetoond.",
    "estimator_for_gate = p75 is een VOORLOPIGE policy choice vanwege de kleine steekproef; "
    "bij voldoende snapshots wordt vooraf besloten of p75 of p90 de norm wordt, in een NIEUWE versie.",
    "Dit model bevat geen sizing-regel; order_notional is invoer.",
]


def percentile(values: list[float], q: float) -> float:
    """Lineair geïnterpoleerd percentiel (q tussen 0 en 1)."""
    if not values:
        raise ValueError("lege reeks")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (pos - lo) * (s[hi] - s[lo])


def per_market(rows: list[dict]) -> dict[str, dict]:
    by_market: dict[str, list[dict]] = {}
    for r in rows:
        by_market.setdefault(r["market"], []).append(r)
    out = {}
    for market, rs in by_market.items():
        rec = {"turnover_30d": statistics.median(r["turnover_30d"] for r in rs), "snapshots": len(rs), "cost": {}}
        usable = True
        for s in SIZES_EUR:
            vals = [max(r[f"buy_{s}"], r[f"sell_{s}"]) for r in rs
                    if r.get(f"buy_{s}") is not None and r.get(f"sell_{s}") is not None]
            if len(vals) != len(rs):
                usable = False
                break
            rec["cost"][s] = {
                "median": percentile(vals, 0.50),
                "p75": percentile(vals, 0.75),
                "p90": percentile(vals, 0.90),
                "max": max(vals),
            }
        if usable:
            out[market] = rec
    return out


def build(rows: list[dict], version: str) -> dict:
    markets = per_market(rows)
    if len(markets) < N_BUCKETS:
        raise ValueError("te weinig bruikbare markten voor kwartielen")
    ordered = sorted(markets.items(), key=lambda kv: kv[1]["turnover_30d"])
    size = len(ordered) // N_BUCKETS
    buckets = []
    for i in range(N_BUCKETS):
        grp = ordered[i * size:] if i == N_BUCKETS - 1 else ordered[i * size:(i + 1) * size]
        costs = {est: {str(s): round(statistics.median(m["cost"][s][est] for _, m in grp), 6)
                       for s in SIZES_EUR}
                 for est in ("median", "p75", "p90", "max")}
        buckets.append({
            "label": f"Q{i + 1}",
            "n_markets": len(grp),
            "min_turnover": round(grp[0][1]["turnover_30d"]),
            "max_turnover": None if i == N_BUCKETS - 1 else round(grp[-1][1]["turnover_30d"]),
            "costs": costs,
        })
    stamps = sorted({r["taken_at"] for r in rows})
    return {
        "version": version,
        "created_from": {"snapshots": len(stamps), "first": stamps[0], "last": stamps[-1],
                         "markets": len(markets)},
        "fee_per_side": FEE_PER_SIDE,
        "estimator_for_gate": ESTIMATOR_FOR_GATE,
        "reference_notional_eur": REFERENCE_NOTIONAL_EUR,
        "sizes_eur": list(SIZES_EUR),
        "turnover_buckets": buckets,
        "limitations": LIMITATIONS,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"{out} bestaat al — een kostenmodelversie wordt nooit overschreven")
    rows = [json.loads(line) for line in Path(args.snapshots).read_text(encoding="utf-8").splitlines() if line.strip()]
    model = build(rows, args.version)
    out.write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in model.items() if k != "turnover_buckets"}, indent=2))
    for b in model["turnover_buckets"]:
        print(f"  {b['label']}: n={b['n_markets']} omzet {b['min_turnover']}-{b['max_turnover']} "
              f"p75 {b['costs']['p75']}")
    print(f"\ngeschreven: {out}")


if __name__ == "__main__":
    main()
