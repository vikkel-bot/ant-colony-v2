"""Powercontrole universum — pre-registratie docs/PREREG_UNIVERSE_20260922.md.

Meet de wekelijkse spreiding van munten rond het gelijkgewogen gemiddelde van
U(t) en berekent het kleinste detecteerbare effect (delta_min) van een
bovenste-fractie-selectie. Rangschikt NIETS en kijkt niet welke munten winnen:
alleen ruis.

delta_min = 3.2 * sqrt(gemiddelde var_t) / sqrt(T),  var_t = s_t^2 * (1/k_t - 1/N_t)
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from pathlib import Path

import universe_census as uc

THRESHOLD_EUR = 50_000
Q = 1 / 5
WINDOW_MONTHS = 48
POWER_LIMIT = 0.0075
Z = 3.2  # alpha 0.01 eenzijdig + 80% power
WEEK_MS = 7 * uc.DAY_MS


class Coin:
    def __init__(self, candles: list[list]):
        self.ts = [int(r[0]) for r in candles]
        self.close = [float(r[4]) for r in candles]
        self.turn = [uc.eur_turnover(r) for r in candles]

    def admitted(self, t: int) -> tuple[bool, float | None]:
        """Zelfde semantiek als universe_census.admitted, via bisect."""
        i = bisect.bisect_left(self.ts, t)  # candles [0, i) hebben ts < t
        if i == 0:
            return False, None
        history_ok = self.ts[0] <= t - uc.MIN_HISTORY_DAYS * uc.DAY_MS
        j = bisect.bisect_left(self.ts, t - uc.LIQ_WINDOW_DAYS * uc.DAY_MS)
        window = self.turn[j:i]
        return history_ok, (statistics.median(window) if window else None)

    def close_before(self, t: int) -> tuple[int, float] | None:
        i = bisect.bisect_left(self.ts, t)
        return (self.ts[i - 1], self.close[i - 1]) if i else None


def weekly_residuals(coins: dict[str, Coin], start: int, ref: int, threshold: float):
    weeks = []
    t = start
    while t + WEEK_MS <= ref:
        rets = []
        for c in coins.values():
            ok, med = c.admitted(t)
            if not ok or med is None or med < threshold:
                continue
            a, b = c.close_before(t), c.close_before(t + WEEK_MS)
            if a is None or b is None or b[0] < t or a[1] <= 0:
                continue  # geen handel in die week
            rets.append(b[1] / a[1] - 1.0)
        if len(rets) >= 2:
            m = statistics.fmean(rets)
            weeks.append([r - m for r in rets])
        t += WEEK_MS
    return weeks


def power(weeks: list[list[float]], q: float = Q) -> dict:
    T = len(weeks)
    var = []
    for res in weeks:
        n = len(res)
        k = max(1, int(n * q))
        var.append(statistics.pvariance(res) * (1 / k - 1 / n))
    pooled = [x for res in weeks for x in res]
    sd = statistics.pstdev(pooled)
    med = statistics.median(pooled)
    mad_sd = 1.4826 * statistics.median(abs(x - med) for x in pooled)
    d_actual = Z * math.sqrt(statistics.fmean(var)) / math.sqrt(T)
    d_40_8 = Z * sd * math.sqrt(1 / 8 - 1 / 40) / math.sqrt(T)
    strict = max(d_actual, d_40_8)
    return {
        "weeks_T": T,
        "universe_size_median": statistics.median(len(r) for r in weeks),
        "universe_size_min": min(len(r) for r in weeks),
        "sigma_weekly_std": round(sd, 4),
        "sigma_weekly_robust_mad": round(mad_sd, 4),
        "delta_min_actual_universe": round(d_actual, 5),
        "delta_min_N40_k8": round(d_40_8, 5),
        "delta_min_strictest": round(strict, 5),
        "power_limit": POWER_LIMIT,
        "verdict": "PASS" if strict <= POWER_LIMIT else "FAIL",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="universe_power.json")
    args = ap.parse_args()
    cache = Path(args.cache)
    markets = json.loads((cache / "_markets.json").read_text(encoding="utf-8"))
    coins = {}
    for m in markets:
        if m.get("quote") != "EUR" or m.get("base") in uc.EXCLUDED_BASES:
            continue
        f = cache / f"{m['market']}.json"
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))[:-1]
            if rows:
                coins[m["market"]] = Coin(rows)
    ref = max(c.ts[-1] for c in coins.values()) + uc.DAY_MS
    start = uc.month_starts(min(c.ts[0] for c in coins.values()), ref)[-WINDOW_MONTHS]
    weeks = weekly_residuals(coins, start, ref, THRESHOLD_EUR)
    result = power(weeks)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
