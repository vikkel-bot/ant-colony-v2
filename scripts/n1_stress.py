"""N1-stresstest onder niet-uniforme selectie. Uitvoering van docs/DESIGN_N1_STRESS_20261001.md (97c3b69).

Week-vermenigvuldigers (Rademacher, Rademacher in blokken van 3, Mammen) op de residuenmatrix; placebosensoren
die op trailing |r_rel| selecteren, altijd op de OORSPRONKELIJKE residuen. Per run exact de productieketen:
spreads, null_random_selection (nieuwe N1-bank per run) en p_values uit ant_colony.lab.xs_rank_test.

Gebruik (een variant per nacht):
    py -3.14 scripts/n1_stress.py --variant rademacher --reps 1000 --out docs/N1_STRESS_RADEMACHER_<datum>.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ant_colony.lab.universe import top_k  # noqa: E402
from ant_colony.lab.xs_rank_test import N_DRAWS, null_random_selection, p_values, spreads, stats  # noqa: E402
from power_calibration import RESIDUALS, load_residuals  # noqa: E402

MASTER_SEED = 20261001
VARIANTS = ("rademacher", "block3", "mammen")
PLACEBOS = ("uniform", "1w_hoog", "1w_laag", "4w_hoog", "4w_laag", "12w_hoog", "12w_laag")
ALPHAS = (0.05, 0.0083)
SQ5 = math.sqrt(5.0)
MAMMEN_LO, MAMMEN_HI = (1 - SQ5) / 2, (1 + SQ5) / 2
MAMMEN_P_LO = (SQ5 + 1) / (2 * SQ5)


def rng_for(v: int, p: int, run: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(MASTER_SEED, spawn_key=(v, p, run)))


def multipliers(variant: str, n_weeks: int, rng: np.random.Generator) -> np.ndarray:
    if variant == "rademacher":
        return rng.choice(np.array([-1.0, 1.0]), n_weeks)
    if variant == "block3":
        offset = int(rng.integers(0, 3))
        block = (np.arange(n_weeks) + offset) // 3
        signs = rng.choice(np.array([-1.0, 1.0]), block.max() + 1)
        return signs[block]
    if variant == "mammen":
        return np.where(rng.random(n_weeks) < MAMMEN_P_LO, MAMMEN_LO, MAMMEN_HI)
    raise ValueError(f"onbekende variant: {variant}")


def placebo_selection(m: np.ndarray, window: int, high: bool, fill_rng: np.random.Generator) -> dict:
    """Vast selectiepad op de oorspronkelijke residuen. Per week: kolomindices van de gekozen munten.

    Rangschikking op gemiddelde |r| over weken t-window..t-1 (t zelf niet). Munten zonder residu in het venster
    doen niet mee; te weinig kandidaten wordt uniform aangevuld; week zonder enige kandidaat valt weg.
    Gelijke waarden: laagste kolomindex eerst (munten staan alfabetisch).
    """
    present = ~np.isnan(m)
    absm = np.abs(m)
    chosen, fill_weeks = {}, 0
    for i in range(m.shape[0]):
        members = np.flatnonzero(present[i])
        n = len(members)
        k = min(top_k(n), n)
        lo = max(0, i - window)
        if lo == i:
            continue
        hist = absm[lo:i, members]
        cnt = (~np.isnan(hist)).sum(axis=0)
        cand = cnt > 0
        if not cand.any():
            continue
        vals = np.where(cand, np.nansum(np.nan_to_num(hist, nan=0.0), axis=0) / np.maximum(cnt, 1), np.nan)
        idx = np.flatnonzero(cand)
        key = -vals[idx] if high else vals[idx]
        order = idx[np.lexsort((members[idx], key))]
        pick = list(order[:k])
        if len(pick) < k:
            fill_weeks += 1
            rest = np.flatnonzero(~cand)
            pick += list(fill_rng.choice(rest, k - len(pick), replace=False))
        chosen[i] = members[np.array(pick)]
    return {"weeks": chosen, "fill_weeks": fill_weeks}


def uniform_selection(m: np.ndarray, rng: np.random.Generator) -> dict:
    present = ~np.isnan(m)
    chosen = {}
    for i in range(m.shape[0]):
        members = np.flatnonzero(present[i])
        k = min(top_k(len(members)), len(members))
        chosen[i] = rng.choice(members, k, replace=False)
    return {"weeks": chosen, "fill_weeks": 0}


def build_panel(m: np.ndarray, sel: dict, w: np.ndarray) -> list[dict]:
    present = ~np.isnan(m)
    panel = []
    for i, cols in sorted(sel["weeks"].items()):
        members = np.flatnonzero(present[i])
        n = len(members)
        top = np.isin(members, cols)
        panel.append({"t": i, "n": n, "k": int(top.sum()), "r": w[i] * m[i, members],
                      "no_trade": np.zeros(n, bool), "top": top, "turnover": float("nan")})
    return panel


def run_once(m: np.ndarray, variant: str, sel: dict, rng: np.random.Generator, draws: int) -> np.ndarray:
    w = multipliers(variant, m.shape[0], rng)
    panel = build_panel(m, sel, w)
    return p_values(stats(spreads(panel)), null_random_selection(panel, draws, rng))


def q_diagnostic(m: np.ndarray, sel: dict) -> float:
    s = spreads(build_panel(m, sel, np.ones(m.shape[0])))
    return float(s.mean() ** 2 / np.mean(s ** 2))


def _binom_cdf(x: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if x >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    return min(1.0, sum(math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * lp + (n - j) * lq)
                        for j in range(0, x + 1)))


def clopper_pearson(x: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    a = (1 - conf) / 2

    def solve(f) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if f(mid):
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lower = 0.0 if x == 0 else solve(lambda p: 1 - _binom_cdf(x - 1, n, p) < a)
    upper = 1.0 if x == n else solve(lambda p: _binom_cdf(x, n, p) > a)
    return lower, upper


def classify(x: int, n: int, alpha: float) -> str:
    lo, hi = clopper_pearson(x, n)
    if lo > alpha:
        return "ANTI_CONSERVATIEF"
    if hi < alpha:
        return "CONSERVATIEF"
    return "GEEN_AANWIJZING"


def needs_more_runs(x: int, n: int, alpha: float = 0.0083) -> bool:
    lo, hi = clopper_pearson(x, n)
    return x / n > alpha and lo <= alpha <= hi


def evaluate(m: np.ndarray, variant: str, placebo: str, reps: int, draws: int) -> dict:
    v, p = VARIANTS.index(variant), PLACEBOS.index(placebo)
    fixed = None
    if placebo != "uniform":
        window = int(placebo.split("w_")[0])
        fixed = placebo_selection(m, window, placebo.endswith("hoog"),
                                  np.random.default_rng(np.random.SeedSequence(MASTER_SEED, spawn_key=(99, p))))
    pv = np.empty((reps, 3))
    for r in range(reps):
        rng = rng_for(v, p, r)
        sel = fixed if fixed is not None else uniform_selection(m, rng)
        pv[r] = run_once(m, variant, sel, rng, draws)
    out = {"reps": reps, "draws": draws}
    if fixed is not None:
        out.update(weken=len(fixed["weeks"]), aanvulweken=fixed["fill_weeks"], Q=q_diagnostic(m, fixed))
    for a in ALPHAS:
        x = int((pv[:, 0] < a).sum())
        lo, hi = clopper_pearson(x, reps)
        out[f"alpha_{a}"] = {"verwerpingen": x, "fractie": x / reps, "cp95": [lo, hi], "oordeel": classify(x, reps, a)}
    out["alpha_0.0083"]["meer_runs_nodig"] = needs_more_runs(out["alpha_0.0083"]["verwerpingen"], reps)
    return out


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=VARIANTS, required=True)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--draws", type=int, default=N_DRAWS)
    ap.add_argument("--placebos", nargs="*", default=list(PLACEBOS))
    ap.add_argument("--residuals", type=Path, default=RESIDUALS)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()

    _, _, m = load_residuals(a.residuals)
    payload = {"ontwerp": "docs/DESIGN_N1_STRESS_20261001.md (97c3b69)", "variant": a.variant,
               "master_seed": MASTER_SEED, "resultaten": {}}
    for placebo in a.placebos:
        t0 = time.time()
        res = evaluate(m, a.variant, placebo, a.reps, a.draws)
        payload["resultaten"][placebo] = res
        r5, r8 = res["alpha_0.05"], res["alpha_0.0083"]
        print(f"{placebo:9s} | a=.05 {r5['fractie']:.3f} [{r5['cp95'][0]:.3f}, {r5['cp95'][1]:.3f}] {r5['oordeel']:17s} | "
              f"a=.0083 {r8['fractie']:.4f} {r8['oordeel']:17s}{' MEER RUNS' if r8['meer_runs_nodig'] else ''} | "
              f"Q {res.get('Q', float('nan')):.4f} | {time.time() - t0:.0f}s", flush=True)
        if a.out:
            _write_atomic(a.out, payload)
    if a.out:
        print(f"geschreven: {a.out}")


if __name__ == "__main__":
    main()
