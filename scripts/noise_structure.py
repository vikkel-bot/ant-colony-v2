"""Ruisstructuur van relatieve weekreturns — outcome-vrij.

Meet de afhankelijkheidsstructuur van r_rel(i,t) = r(i,t) - gemiddelde van U(t),
over ALLE munten in het point-in-time universum. Er wordt NIET gerangschikt,
NIET geselecteerd en geen sensor gebruikt: dit karakteriseert de ruis, zoals de
eerdere sigma-meting. De holdout wordt vooraf fysiek afgeknipt.

Waarom dit nodig is: nulmodel N1 trekt elke week onafhankelijk een mand. Als
r_rel van week tot week samenhangt, is de waargenomen reeks verschillen
autogecorreleerd terwijl de nulverdeling dat niet is, en verwerpt N1 te vaak bij
persistente sensoren. N2 vangt dat op met blokken van 4 weken; of die lengte
volstaat, moet uit de gemeten afhankelijkheid volgen, niet uit de tenure van een
sensor (selectiepersistentie en returnafhankelijkheid zijn verschillende dingen).

Het artefact bewaart de volledige residuenmatrix, zodat een volgende stap kan
kiezen tussen een parametrische simulatie en een empirische resampling zonder
opnieuw te meten.

Gebruik:
    py -3.14 scripts/noise_structure.py --out docs/NOISE_STRUCTURE.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ant_colony.lab import universe as U  # noqa: E402
from ant_colony.lab import xs_rank_test as X  # noqa: E402

MAX_LAG = 8
MIN_OBS_PER_COIN = 30
BOOTSTRAP_DRAWS = 500
STICKY_TENURES = (1, 6, 12)   # 1 = onafhankelijke trekking per week
STICKY_REPEATS = 100


def weekly_relative_returns(candles: dict[str, list]) -> tuple[list[int], dict[str, dict[int, float]]]:
    """r_rel[munt][week] voor elke munt die op die week in U(t) zit."""
    data = X.truncate_before(candles, X.HOLDOUT_START)
    weeks = X.week_starts()
    rel: dict[str, dict[int, float]] = {}
    for t in weeks:
        members = sorted(U.universe(data, t))
        rets = {}
        for m in members:
            r, no_trade = X._fwd_return(data[m], t)
            if not no_trade:
                rets[m] = r
        if len(rets) < 10:
            continue
        mean = statistics.fmean(rets.values())
        for m, r in rets.items():
            rel.setdefault(m, {})[t] = r - mean
    return weeks, rel


def _pairs(rel: dict[str, dict[int, float]], weeks: list[int], lag: int,
           transform) -> tuple[list[float], list[float], dict[str, int]]:
    xs, ys, per_coin = [], [], {}
    step = X.WEEK_MS
    for m, series in rel.items():
        n = 0
        for t, v in series.items():
            w = series.get(t + lag * step)
            if w is not None:
                xs.append(transform(v))
                ys.append(transform(w))
                n += 1
        if n:
            per_coin[m] = n
    return xs, ys, per_coin


def corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (len(xs) * sx * sy)


def per_coin_acf(rel: dict[str, dict[int, float]], weeks: list[int], lag: int,
                 transform) -> list[float]:
    out = []
    step = X.WEEK_MS
    for m, series in rel.items():
        xs, ys = [], []
        for t, v in series.items():
            w = series.get(t + lag * step)
            if w is not None:
                xs.append(transform(v))
                ys.append(transform(w))
        if len(xs) >= MIN_OBS_PER_COIN:
            c = corr(xs, ys)
            if c is not None:
                out.append(c)
    return sorted(out)


def _coin_sums(rel: dict[str, dict[int, float]], lag: int, transform) -> dict[str, tuple]:
    """Per munt de somstatistieken van de (x, y)-paren, zodat de bootstrap
    lineair is in het aantal munten in plaats van in het aantal paren."""
    step = X.WEEK_MS
    out = {}
    for m, series in rel.items():
        n = sx = sy = sxx = syy = sxy = 0.0
        for t, v in series.items():
            w = series.get(t + lag * step)
            if w is None:
                continue
            x, y = transform(v), transform(w)
            n += 1
            sx += x
            sy += y
            sxx += x * x
            syy += y * y
            sxy += x * y
        if n:
            out[m] = (n, sx, sy, sxx, syy, sxy)
    return out


def _corr_from_sums(acc: tuple) -> float | None:
    n, sx, sy, sxx, syy, sxy = acc
    if n < 10:
        return None
    cov = sxy / n - (sx / n) * (sy / n)
    vx = sxx / n - (sx / n) ** 2
    vy = syy / n - (sy / n) ** 2
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def cluster_bootstrap_ci(rel: dict[str, dict[int, float]], weeks: list[int], lag: int,
                         transform, rng: random.Random, draws: int = BOOTSTRAP_DRAWS
                         ) -> tuple[float, float] | None:
    """Betrouwbaarheidsinterval door munten (clusters) te herbemonsteren."""
    sums = _coin_sums(rel, lag, transform)
    keys = list(sums)
    if len(keys) < 10:
        return None
    vals = []
    for _ in range(draws):
        acc = [0.0] * 6
        for _ in range(len(keys)):
            s = sums[keys[rng.randrange(len(keys))]]
            for i in range(6):
                acc[i] += s[i]
        c = _corr_from_sums(tuple(acc))
        if c is not None:
            vals.append(c)
    if len(vals) < 20:
        return None
    vals.sort()
    return round(vals[int(0.025 * (len(vals) - 1))], 4), round(vals[int(0.975 * (len(vals) - 1))], 4)


def quantiles(v: list[float]) -> dict:
    if not v:
        return {}
    return {"n_munten": len(v),
            "p10": round(v[int(0.10 * (len(v) - 1))], 4),
            "mediaan": round(v[len(v) // 2], 4),
            "p90": round(v[int(0.90 * (len(v) - 1))], 4)}


def sticky_random_spread_acf(rel: dict[str, dict[int, float]], weeks: list[int],
                             tenure: int, rng: random.Random, repeats: int = STICKY_REPEATS
                             ) -> dict:
    """ACF van de wekelijkse spread van een WILLEKEURIGE mand die `tenure` weken
    wordt vastgehouden. Geen sensor, geen rangschikking: dit meet hoeveel
    afhankelijkheid alleen al uit het vasthouden voortkomt — precies wat de
    bloklengte van N2 moet dekken."""
    by_week: dict[int, dict[str, float]] = {}
    for m, series in rel.items():
        for t, v in series.items():
            by_week.setdefault(t, {})[m] = v
    ordered = [t for t in weeks if len(by_week.get(t, {})) >= 20]
    acf_by_lag: dict[int, list[float]] = {lag: [] for lag in range(1, MAX_LAG + 1)}
    for _ in range(repeats):
        series_vals, held, age = [], [], 0
        for t in ordered:
            avail = list(by_week[t])
            k = max(8, len(avail) // 5)
            held = [m for m in held if m in by_week[t]]
            if age % tenure == 0 or len(held) < k:
                held = rng.sample(avail, min(k, len(avail)))
            age += 1
            series_vals.append(statistics.fmean(by_week[t][m] for m in held))
        for lag in range(1, MAX_LAG + 1):
            a, b = series_vals[:-lag], series_vals[lag:]
            c = corr(a, b)
            if c is not None:
                acf_by_lag[lag].append(c)
    return {str(lag): round(statistics.fmean(v), 4) if v else None for lag, v in acf_by_lag.items()}


def analyse(candles: dict[str, list], seed: int = 42) -> dict:
    rng = random.Random(seed)
    weeks, rel = weekly_relative_returns(candles)
    ident = lambda x: x            # noqa: E731
    absolute = abs
    squared = lambda x: x * x      # noqa: E731

    result = {
        "methode": {
            "definitie": "r_rel(i,t) = r(i,t) - gelijkgewogen gemiddelde van U(t) op t",
            "outcome_vrij": "geen rangschikking, geen selectie, geen sensor; ruiskarakterisering",
            "periode": "ontwikkelblok; holdout fysiek afgeknipt",
            "max_lag": MAX_LAG,
            "min_obs_per_coin": MIN_OBS_PER_COIN,
        },
        "weken": len(weeks),
        "munten": len(rel),
        "lags": {},
    }
    for transform, label in ((ident, "r_rel"), (absolute, "|r_rel|"), (squared, "r_rel^2")):
        per_lag = {}
        for lag in range(1, MAX_LAG + 1):
            xs, ys, per_coin = _pairs(rel, weeks, lag, transform)
            pooled = corr(xs, ys)
            ci = cluster_bootstrap_ci(rel, weeks, lag, transform, rng)
            per_lag[str(lag)] = {
                "pooled": round(pooled, 4) if pooled is not None else None,
                "ci95_cluster_bootstrap": ci,
                "n_paren": len(xs),
                "n_munten_met_paren": len(per_coin),
                "per_munt": quantiles(per_coin_acf(rel, weeks, lag, transform)),
            }
        result["lags"][label] = per_lag

    result["sticky_random_spread_acf"] = {
        "toelichting": "ACF van de weekspread van een WILLEKEURIGE mand die n weken wordt "
                       "vastgehouden; geen sensor. Bepaalt welke bloklengte N2 moet dekken.",
        **{f"tenure_{n}": sticky_random_spread_acf(rel, weeks, n, rng) for n in STICKY_TENURES},
    }

    result["residuen"] = {
        "toelichting": "volledige outcome-vrije residuenmatrix, zodat een volgende stap kan "
                       "kiezen tussen parametrische simulatie en empirische resampling",
        "per_munt": {m: {str(t): round(v, 6) for t, v in s.items()} for m, s in rel.items()},
    }
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="docs/NOISE_STRUCTURE.json")
    args = ap.parse_args()
    cache = Path(args.cache)
    markets = json.loads((cache / "_markets.json").read_text(encoding="utf-8"))
    candles = {}
    for m in markets:
        f = cache / f"{m['market']}.json"
        if m.get("quote") == "EUR" and f.exists():
            rows = json.loads(f.read_text(encoding="utf-8"))[:-1]
            if rows:
                candles[m["market"]] = rows
    res = analyse(candles)
    Path(args.out).write_text(json.dumps(res, indent=2), encoding="utf-8")

    print(f"weken {res['weken']} | munten {res['munten']}\n")
    for label in ("r_rel", "|r_rel|", "r_rel^2"):
        print(f"--- {label} ---")
        print(f"{'lag':>3} {'pooled':>8} {'ci95':>18} {'paren':>7} {'p10':>8} {'mediaan':>8} {'p90':>8}")
        for lag in range(1, MAX_LAG + 1):
            d = res["lags"][label][str(lag)]
            pc = d["per_munt"]
            ci = d["ci95_cluster_bootstrap"]
            ci_s = f"[{ci[0]:+.3f},{ci[1]:+.3f}]" if ci else "-"
            print(f"{lag:>3} {d['pooled']:>8.4f} {ci_s:>18} {d['n_paren']:>7} "
                  f"{pc.get('p10', float('nan')):>8.3f} {pc.get('mediaan', float('nan')):>8.3f} "
                  f"{pc.get('p90', float('nan')):>8.3f}")
        print()
    print("--- ACF van weekspread bij willekeurige mand die n weken wordt vastgehouden ---")
    for n in STICKY_TENURES:
        d = res["sticky_random_spread_acf"][f"tenure_{n}"]
        print(f"tenure {n:>2}: " + "  ".join(f"L{lag}={d[str(lag)]:+.3f}" for lag in range(1, MAX_LAG + 1)))
    print(f"\nvolledig: {args.out}")


if __name__ == "__main__":
    main()
