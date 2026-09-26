"""Outcome-blinde screening van kandidaat-sensoren.

Meet UITSLUITEND eigenschappen van de rangschikking: hoe snel verandert de
volgorde, hoe vaak wisselt de mand, wat kost dat. Er wordt nergens een
rendement ná het selectiemoment berekend of gelezen. De holdout wordt vóór
alles fysiek afgeknipt.

Vooraf vastgelegde screening-aannames (voor de meting, niet erna gekozen):
  - Richting: elke sensor wordt in twee varianten gemeten, hoog en laag.
    Turnover verschilt per richting; één richting kiezen zou een hypothese zijn.
  - Bufferregel: symmetrisch, marge 25%. Instap bij rang <= 0.75*k, uitstap bij
    rang > 1.25*k, daarna aanvullen tot k met de best gerangschikte niet-leden.
  - Effectieve informatiedichtheid wordt gerapporteerd als TELLINGEN
    (verblijfsduur, selectie-episodes, unieke manden). Er wordt GEEN herziene
    delta_min afgeleid: daarvoor bestaat geen vastgelegde formule.

Kosten komen uit het geldende kostenmodel (v1, p75, EUR 5.000 per order).

Gebruik:
    py -3.14 scripts/screen_sensors.py --out docs/SCREENING_CANDIDATES.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ant_colony.lab import cost_model as CM  # noqa: E402
from ant_colony.lab import universe as U  # noqa: E402
from ant_colony.lab import xs_rank_test as X  # noqa: E402

BUFFER_MARGIN = 0.25          # vooraf vastgelegd, symmetrisch
ORDER_NOTIONAL_EUR = 5_000    # research-referentieschaal uit het kostenmodel
COST_RATIO_MIN = 3.0
VOL_WINDOW_DAYS = 60
BETA_WINDOW_DAYS = 90
HIGH_WINDOW_DAYS = 364


class Series:
    """Candles van één markt, met snelle toegang tot data strikt vóór t."""

    def __init__(self, candles):
        self.ts = [int(c[0]) for c in candles]
        self.close = [float(c[4]) for c in candles]
        self.turn = [U.eur_turnover(c) for c in candles]

    def upto(self, t_ms: int) -> int:
        return bisect.bisect_left(self.ts, t_ms)

    def log_returns(self, t_ms: int, days: int) -> list[float]:
        i = self.upto(t_ms)
        w = self.close[max(0, i - days - 1):i]
        return [math.log(b / a) for a, b in zip(w, w[1:]) if a > 0 and b > 0]

    def close_before(self, t_ms: int) -> float | None:
        i = self.upto(t_ms)
        return self.close[i - 1] if i else None

    def turnover_30d(self, t_ms: int) -> float:
        i = self.upto(t_ms)
        j = bisect.bisect_left(self.ts, t_ms - U.LIQ_WINDOW_DAYS * U.DAY_MS)
        w = self.turn[j:i]
        return statistics.median(w) if w else 0.0

    def first_ts(self) -> int:
        return self.ts[0]


# ---------------------------------------------------------------- sensoren

def s_realized_vol(s: Series, t: int, ctx) -> float | None:
    r = s.log_returns(t, VOL_WINDOW_DAYS)
    return statistics.pstdev(r) if len(r) >= VOL_WINDOW_DAYS // 2 else None


def _paired(s: Series, t: int, ctx) -> tuple[list[float], list[float]] | None:
    btc = ctx.get("btc")
    if btc is None:
        return None
    a, b = s.log_returns(t, BETA_WINDOW_DAYS), btc.log_returns(t, BETA_WINDOW_DAYS)
    n = min(len(a), len(b))
    return (a[-n:], b[-n:]) if n >= BETA_WINDOW_DAYS // 2 else None


def s_beta_btc(s: Series, t: int, ctx) -> float | None:
    p = _paired(s, t, ctx)
    if p is None:
        return None
    a, b = p
    var = statistics.pvariance(b)
    if var == 0:
        return None
    mb, ma = statistics.fmean(b), statistics.fmean(a)
    cov = sum((x - mb) * (y - ma) for x, y in zip(b, a)) / len(b)
    return cov / var


def s_corr_btc(s: Series, t: int, ctx) -> float | None:
    p = _paired(s, t, ctx)
    if p is None:
        return None
    a, b = p
    sa, sb = statistics.pstdev(a), statistics.pstdev(b)
    if sa == 0 or sb == 0:
        return None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) * sa * sb)


def s_dist_from_high(s: Series, t: int, ctx) -> float | None:
    i = s.upto(t)
    w = s.close[max(0, i - HIGH_WINDOW_DAYS):i]
    if len(w) < 30 or max(w) <= 0:
        return None
    return w[-1] / max(w) - 1.0


def s_liquidity(s: Series, t: int, ctx) -> float | None:
    v = s.turnover_30d(t)
    return v if v > 0 else None


def s_listing_age(s: Series, t: int, ctx) -> float | None:
    return (t - s.first_ts()) / U.DAY_MS


SENSORS = {
    "realized_vol_60d": (s_realized_vol, "prijs", "laag"),
    "beta_btc_90d": (s_beta_btc, "prijs", "laag"),
    "corr_btc_90d": (s_corr_btc, "prijs", "laag"),
    "dist_from_52w_high": (s_dist_from_high, "prijs", "laag"),
    "liquidity_30d": (s_liquidity, "prijs+volume", "middel"),
    "listing_age": (s_listing_age, "noteringsdatum", "HOOG"),
}

INELIGIBLE = {
    "size_marketcap": "Bitvavo levert geen point-in-time marktwaarde of circulerend aanbod; "
                      "elke proxy zou geimproviseerd zijn.",
}

DATA_NOTES = {
    "liquidity_30d": "Dit is tevens de toelatingsvariabele van U(t); rangschikken hierop meet "
                     "deels de universumgrens zelf.",
    "listing_age": "Sterk survivorship-gevoelig: gedelistte munten ontbreken, en juist oude "
                   "munten zijn per definitie overlevers.",
    "beta_btc_90d": "Vereist BTC-EUR als referentie; weken zonder BTC-data vallen uit.",
    "corr_btc_90d": "Idem.",
}


# ---------------------------------------------------------------- selectie

def select_plain(ranked: list[str], k: int) -> list[str]:
    return ranked[:k]


def select_buffered(ranked: list[str], k: int, held: set[str]) -> list[str]:
    enter_max = max(1, int(k * (1 - BUFFER_MARGIN)))
    hold_max = max(enter_max, int(k * (1 + BUFFER_MARGIN)))
    pos = {m: i for i, m in enumerate(ranked)}
    keep = [m for m in ranked if m in held and pos[m] < hold_max]
    out = list(dict.fromkeys(ranked[:enter_max] + keep))
    for m in ranked:
        if len(out) >= k:
            break
        if m not in out:
            out.append(m)
    return out[:k]


def spearman(prev: dict[str, float], cur: dict[str, float]) -> float | None:
    common = sorted(set(prev) & set(cur))
    if len(common) < 5:
        return None

    def ranks(d):
        order = sorted(common, key=lambda m: d[m])
        return {m: i for i, m in enumerate(order)}

    a, b = ranks(prev), ranks(cur)
    xs = [a[m] for m in common]
    ys = [b[m] for m in common]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) * sx * sy)


def screen_one(data: dict[str, Series], weeks: list[int], sensor, direction: str,
               cost: dict) -> dict:
    ctx = {"btc": data.get("BTC-EUR")}
    prev_scores: dict[str, float] | None = None
    held_plain: set[str] = set()
    held_buf: set[str] = set()
    persistence, turn_plain, turn_buf = [], [], []
    cost_week_plain, cost_week_buf = [], []
    tenure: dict[str, int] = {}
    tenures: list[int] = []
    episodes = 0
    baskets: set[frozenset] = set()
    weeks_used = 0

    for t in weeks:
        members = UNIVERSE_CACHE[t]
        scores = {}
        for m in members:
            v = sensor(data[m], t, ctx)
            if v is not None:
                scores[m] = v
        if len(scores) < 8:
            continue
        weeks_used += 1
        reverse = direction == "hoog"
        ranked = sorted(scores, key=lambda m: (-scores[m] if reverse else scores[m], m))
        k = min(U.top_k(len(scores)), len(scores))

        plain = select_plain(ranked, k)
        buf = select_buffered(ranked, k, held_buf)

        if held_plain:
            turn_plain.append(len(set(plain) - held_plain) / k)
            turn_buf.append(len(set(buf) - held_buf) / k)
        for m in set(plain) - held_plain:
            episodes += 1
            tenure[m] = 0
        for m in held_plain - set(plain):
            tenures.append(tenure.pop(m, 0))
        for m in plain:
            tenure[m] = tenure.get(m, 0) + 1

        baskets.add(frozenset(plain))
        for group, sink in ((plain, cost_week_plain), (buf, cost_week_buf)):
            per_side = statistics.fmean(
                CM.cost_per_side(cost, data[m].turnover_30d(t), ORDER_NOTIONAL_EUR) for m in group)
            sink.append(2 * per_side)

        if prev_scores is not None:
            r = spearman(prev_scores, scores)
            if r is not None:
                persistence.append(r)
        prev_scores = scores
        held_plain, held_buf = set(plain), set(buf)

    tenures += list(tenure.values())
    mean_turn = statistics.fmean(turn_plain) if turn_plain else float("nan")
    mean_turn_buf = statistics.fmean(turn_buf) if turn_buf else float("nan")
    rt_plain = statistics.fmean(cost_week_plain) if cost_week_plain else float("nan")
    rt_buf = statistics.fmean(cost_week_buf) if cost_week_buf else float("nan")
    return {
        "weeks_used": weeks_used,
        "rank_persistence_spearman": round(statistics.fmean(persistence), 4) if persistence else None,
        "turnover_per_rebalance": round(mean_turn, 4),
        "turnover_buffered": round(mean_turn_buf, 4),
        "mean_tenure_weeks": round(statistics.fmean(tenures), 2) if tenures else None,
        "selection_episodes": episodes,
        "unique_baskets": len(baskets),
        "round_trip_cost": round(rt_plain, 5),
        "cost_per_week": round(mean_turn * rt_plain, 5),
        "required_gross_per_week": round(COST_RATIO_MIN * mean_turn * rt_plain, 5),
        "cost_per_week_buffered": round(mean_turn_buf * rt_buf, 5),
        "required_gross_per_week_buffered": round(COST_RATIO_MIN * mean_turn_buf * rt_buf, 5),
    }


UNIVERSE_CACHE: dict[int, list[str]] = {}
DATA_AS_CANDLES: dict[str, list] = {}


def run(candles: dict[str, list], cost_model_path: Path | None = None) -> dict:
    global UNIVERSE_CACHE, DATA_AS_CANDLES
    DATA_AS_CANDLES = X.truncate_before(candles, X.HOLDOUT_START)
    weeks = X.week_starts()
    UNIVERSE_CACHE = {t: sorted(U.universe(DATA_AS_CANDLES, t)) for t in weeks}
    data = {m: Series(c) for m, c in DATA_AS_CANDLES.items()}
    cost = CM.load_model(cost_model_path) if cost_model_path else CM.load_model()

    rows = []
    for name, (fn, bron, surviv) in SENSORS.items():
        for direction in ("hoog", "laag"):
            res = screen_one(data, weeks, fn, direction, cost)
            rows.append({"sensor": f"{name}@{direction}", "databron": bron,
                         "survivorship_gevoeligheid": surviv,
                         "datanotitie": DATA_NOTES.get(name, ""), **res})
    for name, reden in INELIGIBLE.items():
        rows.append({"sensor": name, "status": "DATA INELIGIBLE", "reden": reden})
    return {
        "screening_assumpties": {
            "buffer_marge": BUFFER_MARGIN,
            "order_notional_eur": ORDER_NOTIONAL_EUR,
            "kostenmodel": cost["version"],
            "kosten_estimator": cost["estimator_for_gate"],
            "kostenhorde_factor": COST_RATIO_MIN,
            "richtingen": ["hoog", "laag"],
            "geen_rendementen": "er wordt nergens een rendement na het selectiemoment gelezen",
            "geen_afgeleide_delta_min": "informatiedichtheid wordt als telling gerapporteerd",
        },
        "weken": len(weeks),
        "kandidaten": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="docs/SCREENING_CANDIDATES.json")
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
    result = run(candles)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["screening_assumpties"], indent=2))
    print(f"\n{'sensor':<28} {'turn':>6} {'buf':>6} {'horde/wk':>9} {'buf':>8} "
          f"{'persist':>8} {'tenure':>7} {'episodes':>9} {'manden':>7}")
    for r in result["kandidaten"]:
        if r.get("status") == "DATA INELIGIBLE":
            print(f"{r['sensor']:<28} DATA INELIGIBLE — {r['reden'][:60]}")
            continue
        print(f"{r['sensor']:<28} {r['turnover_per_rebalance']:>6.3f} {r['turnover_buffered']:>6.3f} "
              f"{r['required_gross_per_week']*100:>8.2f}% {r['required_gross_per_week_buffered']*100:>7.2f}% "
              f"{str(r['rank_persistence_spearman']):>8} {str(r['mean_tenure_weeks']):>7} "
              f"{r['selection_episodes']:>9} {r['unique_baskets']:>7}")
    print(f"\nvolledig: {args.out}")


if __name__ == "__main__":
    main()
