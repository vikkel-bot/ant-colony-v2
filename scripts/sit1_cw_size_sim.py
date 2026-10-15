"""SIT-1 diagnostiek: size van de Clark-West-toets onder H0 (d = 0), op SYNTHETISCHE data.

PREREG_SIT1_20260926.md §3.2 noemt de kwaliteit van de CW-normaalbenadering bij
P/R ≈ 642/120 en α = 0,0083 "niet gesimuleerd" en de grootste resterende onzekerheid.
Dit script meet die size vóór de development-run. Het leest geen echte data.
Uitkomst is diagnostisch: de beslisregel van de preregistratie verandert niet.

Twee nulprocessen voor log-RV (beide zonder ΔCS-effect):
  AR1 : y' = -6,2 + 0,6 (y + 6,2) + e,                       e ~ N(0, 0,40)
  HAR : y' = c + 0,35 y + 0,30 ȳ3 + 0,25 ȳ12 + e (gem. -6,2), e ~ N(0, 0,45)
ΔCS: random walk van CS met stappen N(0, 10 bp). Venster en inloop identiek aan development.

Gebruik:  py -3.14 scripts/sit1_cw_size_sim.py [--n 5000]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ant_colony.lab import sit1 as S  # noqa: E402

SEED = 20261015
START = S.M(1926, 7)
N_MONTHS = S.M(1990, 12) - START + 2


def simulate(kind: str, rng: np.random.Generator) -> tuple[dict, dict]:
    cs, level = {}, 100.0
    for m in range(S.M(1919, 1), START + N_MONTHS + 2):
        level += rng.normal(0, 10)
        cs[m] = level
    ys: list[float] = []
    for _ in range(N_MONTHS):
        if kind == "AR1" or len(ys) < 12:
            prev = ys[-1] if ys else -6.2
            y = -6.2 + 0.6 * (prev + 6.2) + rng.normal(0, 0.40)
        else:
            y = (-6.2 * 0.10 + 0.35 * ys[-1] + 0.30 * np.mean(ys[-3:])
                 + 0.25 * np.mean(ys[-12:]) + rng.normal(0, 0.45))
        ys.append(y)
    rv = {START + k: math.exp(v) for k, v in enumerate(ys)}
    return rv, cs


def fast_forecasts(rows: list[S.Row], start: int, end: int) -> S.Forecasts:
    """Identiek aan S.recursive_forecasts, via cumulatieve sommen (rijen zijn op target gesorteerd)."""
    tg = np.array([r.target for r in rows])
    X1 = np.array([np.append(r.x0, r.dcs) for r in rows])
    y = np.array([r.y for r in rows])
    fc = S.Forecasts()
    for X, key in ((X1[:, :4], 0), (X1, 1)):
        xx = np.cumsum(X[:, :, None] * X[:, None, :], axis=0)
        xy = np.cumsum(X * y[:, None], axis=0)
        yy = np.cumsum(y * y)
        yh, s2 = [], []
        for tau in range(start, end + 1):
            i = int(np.searchsorted(tg, tau))          # rijen 0..i-1 hebben target <= tau-1
            b = np.linalg.solve(xx[i - 1], xy[i - 1])
            ssr = yy[i - 1] - b @ xy[i - 1]
            yh.append(float(X[i] @ b))
            s2.append(float(ssr / (i - X.shape[1])))
        if key == 0:
            fc.yhat0, fc.s2_0 = yh, s2
        else:
            fc.yhat1, fc.s2_1 = yh, s2
    idx = [int(np.searchsorted(tg, t)) for t in range(start, end + 1)]
    fc.target = list(range(start, end + 1))
    fc.y = [rows[i].y for i in idx]
    fc.rv = [rows[i].rv for i in idx]
    fc.n_est = idx
    return fc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--out", default="results/sit1/cw_size_sim.json")
    args = ap.parse_args()
    start, end = S.WINDOWS["development"]
    rng = np.random.Generator(np.random.PCG64(SEED))

    # zelfcontrole: snelle methode == module
    rv, cs = simulate("HAR", rng)
    rows = S.build_rows(rv, cs)
    a, b = S.recursive_forecasts(rows, start, start + 24), fast_forecasts(rows, start, start + 24)
    assert np.allclose(a.yhat1, b.yhat1, atol=1e-9) and np.allclose(a.s2_0, b.s2_0, atol=1e-9)

    res = {"stap": "SIT-1 diagnostiek CW-size (synthetisch)", "seed": SEED, "n_per_dgp": args.n, "dgp": {}}
    for kind in ("AR1", "HAR"):
        ps = []
        for _ in range(args.n):
            rv, cs = simulate(kind, rng)
            ps.append(S.clark_west(fast_forecasts(S.build_rows(rv, cs), start, end))["p"])
        ps = np.array(ps)
        res["dgp"][kind] = {}
        for alpha in (0.05, 0.05 / 6):
            k = int((ps <= alpha).sum())
            se = math.sqrt(alpha * (1 - alpha) / args.n)
            res["dgp"][kind][f"alpha_{alpha:.4f}"] = {"verwerpingen": k, "fractie": k / args.n,
                                                       "nominaal": alpha, "mc_se": se}
            print(f"{kind} alpha {alpha:.4f}: {k}/{args.n} = {k / args.n:.4f} (nominaal {alpha:.4f}, MC-SE {se:.4f})")
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"geschreven: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
