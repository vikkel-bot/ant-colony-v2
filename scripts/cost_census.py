"""Cost census — werkelijke handelskosten per munt en ordergrootte (Bitvavo).

Meet uit publieke orderboeken: spread en prijsimpact van een marktorder van
vaste grootte, voor de munten in U(ref). Geen rendementen.

Snapshots worden toegevoegd aan een append-only JSONL-bestand; draai het
script op verschillende momenten van de dag en vat daarna samen met --summary.
Kosten = halve spread + impact (t.o.v. midprijs); het tarief komt er apart bij.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ant_colony.lab import universe as U  # noqa: E402

API = "https://api.bitvavo.com/v2"
SIZES_EUR = (1_000, 5_000, 10_000, 25_000)
FEE_TAKER = {"<100k/maand": 0.0025, ">=100k/maand": 0.0020}


def _get(path: str):
    req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "ant-colony-cost/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def walk(levels: list[list], eur: float, mid: float) -> float | None:
    """Kosten (fractie t.o.v. mid) van een marktorder van `eur` door deze boekkant."""
    spent = qty = 0.0
    for price_s, amount_s in levels:
        price, amount = float(price_s), float(amount_s)
        take = min(amount, (eur - spent) / price)
        spent += take * price
        qty += take
        if spent >= eur - 1e-9:
            return abs(spent / qty - mid) / mid
    return None  # boek niet diep genoeg


def measure_book(book: dict) -> dict | None:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bid0, ask0 = float(bids[0][0]), float(asks[0][0])
    mid = (bid0 + ask0) / 2
    out = {"spread": (ask0 - bid0) / mid}
    for s in SIZES_EUR:
        buy, sell = walk(asks, s, mid), walk(bids, s, mid)
        out[f"buy_{s}"], out[f"sell_{s}"] = buy, sell
    return out


def load_cache(cache: Path) -> dict:
    markets = json.loads((cache / "_markets.json").read_text(encoding="utf-8"))
    data = {}
    for m in markets:
        f = cache / f"{m['market']}.json"
        if m.get("quote") == "EUR" and f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))[:-1]
            if rows:
                data[m["market"]] = rows
    return data


def turnover_30d(candles: list[list], t_ms: int) -> float:
    w = [U.eur_turnover(c) for c in candles if t_ms - 30 * U.DAY_MS <= int(c[0]) < t_ms]
    return statistics.median(w) if w else 0.0


def snapshot(cache: Path, out: Path, pause: float = 0.2) -> int:
    data = load_cache(cache)
    ref = max(int(c[-1][0]) for c in data.values()) + U.DAY_MS
    members = sorted(U.universe(data, ref))
    taken_at = datetime.now(timezone.utc).isoformat()  # alleen als label, niet in berekening
    n = 0
    with out.open("a", encoding="utf-8") as fh:
        for m in members:
            try:
                book = _get(f"/{m}/book?depth=1000")
            except Exception as exc:  # noqa: BLE001
                print(f"  {m}: FOUT {exc}")
                continue
            time.sleep(pause)
            meas = measure_book(book)
            if meas is None:
                continue
            fh.write(json.dumps({"taken_at": taken_at, "market": m,
                                 "turnover_30d": turnover_30d(data[m], ref), **meas}) + "\n")
            n += 1
    return n


def summary(out: Path) -> dict:
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_m: dict[str, list] = {}
    for r in rows:
        by_m.setdefault(r["market"], []).append(r)
    per = []
    for m, rs in by_m.items():
        rec = {"market": m, "turnover_30d": statistics.median(r["turnover_30d"] for r in rs),
               "snapshots": len(rs), "spread": statistics.median(r["spread"] for r in rs)}
        for s in SIZES_EUR:
            vals = [max(r[f"buy_{s}"], r[f"sell_{s}"]) for r in rs if r[f"buy_{s}"] is not None and r[f"sell_{s}"] is not None]
            rec[f"cost_{s}"] = statistics.median(vals) if len(vals) == len(rs) else None
        per.append(rec)
    per.sort(key=lambda r: r["turnover_30d"])
    q = len(per) // 4 or 1
    buckets = []
    for i, name in enumerate(("Q1 laagste omzet", "Q2", "Q3", "Q4 hoogste omzet")):
        grp = per[i * q:] if i == 3 else per[i * q:(i + 1) * q]
        b = {"bucket": name, "n": len(grp),
             "turnover_range": [round(grp[0]["turnover_30d"]), round(grp[-1]["turnover_30d"])]}
        for s in SIZES_EUR:
            ok = [r[f"cost_{s}"] for r in grp if r[f"cost_{s}"] is not None]
            b[f"median_cost_{s}"] = round(statistics.median(ok), 5) if ok else None
            b[f"book_too_thin_{s}"] = len(grp) - len(ok)
        buckets.append(b)
    return {"snapshots_total": len({r["taken_at"] for r in rows}), "markets": len(per),
            "fee_taker": FEE_TAKER, "note": "kosten per kant excl. tarief; tel tarief erbij",
            "buckets": buckets, "per_market": per}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="cost_snapshots.jsonl")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    out = Path(args.out)
    if args.summary:
        res = summary(out)
        print(json.dumps({k: v for k, v in res.items() if k != "per_market"}, indent=2))
        Path(str(out).replace(".jsonl", "_summary.json")).write_text(json.dumps(res, indent=2), encoding="utf-8")
    else:
        print(f"snapshot: {snapshot(Path(args.cache), out)} markten gemeten -> {out}")


if __name__ == "__main__":
    main()
