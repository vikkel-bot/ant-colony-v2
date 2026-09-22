"""Cross-sectionele rangschikkingstoets — T001 (docs/PREREG_T001_MOMENTUM21_20260922.md).

Geen generieke runner: gebouwd voor T001, met de sensor als parameter zodat
toets 2 kan laten zien wat werkelijk gedeeld is.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

import numpy as np

from ant_colony.lab import universe as U

DAY_MS = U.DAY_MS
WEEK_MS = 7 * DAY_MS
DEV_START = int(datetime(2022, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
HOLDOUT_START = int(datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
N_DRAWS = 10_000
SEED = 42
BLOCK = 4
ALPHA = 0.05 / (3 * 2)
ROUND_TRIP_COST = 0.005
COST_RATIO_MIN = 3.0

Sensor = Callable[[Sequence[Sequence], int], "float | None"]


def truncate_before(data: Mapping[str, Sequence[Sequence]], boundary_ms: int) -> dict:
    """Verwijdert elke candle met timestamp >= boundary. Holdout-bewaking."""
    out = {}
    for m, c in data.items():
        kept = [r for r in c if int(r[0]) < boundary_ms]
        if kept:
            out[m] = kept
    return out


def week_starts(start_ms: int = DEV_START, end_ms: int = HOLDOUT_START) -> list[int]:
    out, t = [], start_ms
    while t + WEEK_MS <= end_ms:
        out.append(t)
        t += WEEK_MS
    return out


def _fwd_return(candles: Sequence[Sequence], t_ms: int) -> tuple[float, bool]:
    """(rendement, geen_handel). Entry: laatste close < t; exit: laatste close < t+7d."""
    ts = [int(c[0]) for c in candles]
    i = bisect.bisect_left(ts, t_ms)
    j = bisect.bisect_left(ts, t_ms + WEEK_MS)
    entry = float(candles[i - 1][4])
    if j == i:  # geen enkele candle in [t, t+7d)
        return 0.0, True
    return float(candles[j - 1][4]) / entry - 1.0, False


def build_panel(data: Mapping[str, Sequence[Sequence]], sensor: Sensor, weeks: list[int]) -> list[dict]:
    panel = []
    prev_top: set[str] = set()
    for t in weeks:
        members = sorted(U.universe(data, t))
        scored = []
        for m in members:
            s = sensor(data[m], t)
            if s is None:
                continue
            r, no_trade = _fwd_return(data[m], t)
            scored.append((m, s, r, no_trade))
        if len(scored) < 2:
            continue
        n = len(scored)
        k = min(U.top_k(n), n)
        ranked = sorted(scored, key=lambda x: (-x[1], x[0]))
        top = {x[0] for x in ranked[:k]}
        names = [x[0] for x in scored]
        r = np.array([x[2] for x in scored])
        nt = np.array([x[3] for x in scored])
        top_mask = np.array([nm in top for nm in names])
        turnover = len(top - prev_top) / k if prev_top else float("nan")
        prev_top = top
        panel.append({"t": t, "n": n, "k": k, "r": r, "no_trade": nt, "top": top_mask, "turnover": turnover})
    return panel


def spreads(panel: list[dict], no_trade_return: float | None = None) -> np.ndarray:
    out = []
    for w in panel:
        r = w["r"].copy()
        if no_trade_return is not None:
            r[w["no_trade"]] = no_trade_return
        out.append(r[w["top"]].mean() - r.mean())
    return np.array(out)


def stats(x: np.ndarray) -> np.ndarray:
    """S1 gemiddelde, S2 mediaan, S3 fractie > 0 — langs de laatste as."""
    return np.stack([x.mean(axis=-1), np.median(x, axis=-1), (x > 0).mean(axis=-1)], axis=-1)


def null_random_selection(panel: list[dict], draws: int, rng: np.random.Generator,
                          no_trade_return: float | None = None) -> np.ndarray:
    """N1: per week k(t) munten willekeurig uit U(t). Geeft (draws, 3)."""
    cols = []
    for w in panel:
        r = w["r"].copy()
        if no_trade_return is not None:
            r[w["no_trade"]] = no_trade_return
        idx = rng.random((draws, w["n"])).argsort(axis=1)[:, : w["k"]]
        cols.append(r[idx].mean(axis=1) - r.mean())
    return stats(np.stack(cols, axis=1))


def null_block_bootstrap(series: np.ndarray, draws: int, rng: np.random.Generator, block: int = BLOCK) -> np.ndarray:
    """N2: circulaire blok-bootstrap van de verschilreeks, gecentreerd op 0."""
    x = series - series.mean()
    n = len(x)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n, (draws, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(draws, -1)[:, :n] % n
    return stats(x[idx])


def p_values(observed: np.ndarray, null: np.ndarray) -> np.ndarray:
    return (1 + (null >= observed[None, :]).sum(axis=0)) / (1 + null.shape[0])


def subperiod_s1(series: np.ndarray) -> list[float]:
    n = len(series)
    b = n // 3
    cuts = [(0, b), (b, 2 * b), (2 * b, n)]
    return [float(series[a:e].mean()) for a, e in cuts]


def conditions(panel: list[dict], draws: int, seed: int, no_trade_return: float | None) -> dict:
    rng = np.random.default_rng(seed)
    s = spreads(panel, no_trade_return)
    obs = stats(s)
    p1 = p_values(obs, null_random_selection(panel, draws, rng, no_trade_return))
    p2 = p_values(obs, null_block_bootstrap(s, draws, rng))
    sub = subperiod_s1(s)
    return {
        "S": obs.tolist(),
        "p_N1": p1.tolist(),
        "p_N2": p2.tolist(),
        "subperiod_S1": sub,
        "c1": bool(p1[0] < ALPHA and p2[0] < ALPHA),
        "c2": bool(p1[1] < ALPHA or p1[2] < ALPHA),
        "c3": bool(sum(v > 0 for v in sub) >= 2),
        "s1_sig_N1": bool(p1[0] < ALPHA),
    }


def verdict(primary: dict, alt: dict, cost_ratio: float) -> str:
    if not primary["s1_sig_N1"]:
        return "FAIL"
    c123 = primary["c1"] and primary["c2"] and primary["c3"]
    c4 = (alt["c1"], alt["c2"], alt["c3"]) == (primary["c1"], primary["c2"], primary["c3"])
    if not (c123 and c4):
        return "INCONCLUSIVE"
    return "PASS" if cost_ratio >= COST_RATIO_MIN else "INFORMATIEF_NIET_VERHANDELBAAR"


def run(data: Mapping[str, Sequence[Sequence]], sensor: Sensor, draws: int = N_DRAWS, seed: int = SEED,
        start_ms: int = DEV_START, end_ms: int = HOLDOUT_START) -> dict:
    data = truncate_before(data, end_ms)  # holdout fysiek onbereikbaar
    weeks = week_starts(start_ms, end_ms)
    panel = build_panel(data, sensor, weeks)
    primary = conditions(panel, draws, seed, None)
    alt = conditions(panel, draws, seed, -1.0)
    s1 = primary["S"][0]
    turn = float(np.nanmean([w["turnover"] for w in panel]))
    cost_per_week = turn * ROUND_TRIP_COST
    cost_ratio = s1 / cost_per_week if cost_per_week > 0 else float("inf")
    return {
        "weeks": len(panel),
        "first_t": datetime.fromtimestamp(panel[0]["t"] / 1000, tz=timezone.utc).date().isoformat(),
        "last_t": datetime.fromtimestamp(panel[-1]["t"] / 1000, tz=timezone.utc).date().isoformat(),
        "universe_n_min": int(min(w["n"] for w in panel)),
        "universe_n_median": float(np.median([w["n"] for w in panel])),
        "k_min": int(min(w["k"] for w in panel)),
        "k_max": int(max(w["k"] for w in panel)),
        "no_trade_obs": int(sum(int(w["no_trade"].sum()) for w in panel)),
        "total_obs": int(sum(w["n"] for w in panel)),
        "alpha": ALPHA,
        "primary": primary,
        "alt_minus100": alt,
        "turnover_mean": turn,
        "cost_per_week": cost_per_week,
        "cost_ratio": cost_ratio,
        "verdict": verdict(primary, alt, cost_ratio),
    }
