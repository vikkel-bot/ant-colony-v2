"""Empirische powerkalibratie van xs_rank_test op de residuenmatrix.

Geen edge-meting. Er wordt geen enkele echte rangschikking gemaakt: per week wordt
een willekeurige deelverzameling van k(t) munten gekozen en daar een effect van
bekende grootte delta in gelegd (+delta op de gekozen munten, -delta*k/(n-k) op
de rest, zodat het weekgemiddelde exact gelijk blijft). De "sensor" selecteert
precies die deelverzameling: dit meet het plafond, wat een perfecte sensor haalt.

De statistiek, het nulmodel N1 en de p-waarde komen letterlijk uit
ant_colony.lab.xs_rank_test; ze worden niet nagebouwd. Verschil met een echt
panel: de residuenmatrix kent geen no_trade, dat staat hier overal op False.

Meeneemvraag: is de spreiding van het gemiddelde per munt groter dan wat ruis
oplevert? Nulverdeling: muntlabels binnen elke week herschikken (vernietigt
muntidentiteit, behoudt weekstructuur en aantal waarnemingen per munt).

Gebruik:
    py -3.14 scripts/power_calibration.py --reps 20            # rookproef
    py -3.14 scripts/power_calibration.py --reps 300 --out docs/POWER_CALIBRATION_<datum>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ant_colony.lab.universe import top_k  # noqa: E402
from ant_colony.lab.xs_rank_test import N_DRAWS, null_random_selection, p_values, spreads, stats

RESIDUALS = Path("docs/NOISE_STRUCTURE_20260926.json")
DELTAS = (0.0, 0.002, 0.004, 0.006, 0.008, 0.010)
ALPHAS = (0.05, 0.0083)
STAT_NAMES = ("S1_gemiddelde", "S2_mediaan", "S3_fractie_pos")
MIN_OBS = 30


def load_residuals(path: Path) -> tuple[list[int], list[str], np.ndarray]:
    """Weken oplopend, munten alfabetisch, matrix (weken x munten) met NaN buiten U(t)."""
    per_munt = json.loads(Path(path).read_text(encoding="utf-8"))["residuen"]["per_munt"]
    coins = sorted(per_munt)
    weeks = sorted({int(t) for v in per_munt.values() for t in v})
    row = {t: i for i, t in enumerate(weeks)}
    m = np.full((len(weeks), len(coins)), np.nan)
    for j, c in enumerate(coins):
        for t, r in per_munt[c].items():
            m[row[int(t)], j] = float(r)
    return weeks, coins, m


def base_panel(weeks: list[int], m: np.ndarray) -> list[dict]:
    """Panel zonder selectie: top is nog leeg, k = top_k(n) zoals in het instrument."""
    panel = []
    for i, t in enumerate(weeks):
        r = m[i][~np.isnan(m[i])]
        n = len(r)
        k = min(top_k(n), n)
        if n <= k:
            raise ValueError(f"week {t}: n={n} <= k={k}, injectie niet definieerbaar")
        panel.append({"t": t, "n": n, "k": k, "r": r, "no_trade": np.zeros(n, bool),
                      "top": np.zeros(n, bool), "turnover": float("nan")})
    return panel


def inject(panel: list[dict], delta: float, rng: np.random.Generator) -> list[dict]:
    """Willekeurige k(t) per week krijgen +delta, de rest -delta*k/(n-k); weekgemiddelde blijft."""
    out = []
    for w in panel:
        n, k = w["n"], w["k"]
        top = np.zeros(n, bool)
        top[rng.choice(n, k, replace=False)] = True
        r = w["r"] + np.where(top, delta, -delta * k / (n - k))
        out.append({**w, "r": r, "top": top})
    return out


def detect(panel: list[dict], draws: int, rng: np.random.Generator) -> np.ndarray:
    """p-waarden (S1, S2, S3) van het instrument voor dit panel."""
    observed = stats(spreads(panel))
    null = null_random_selection(panel, draws, rng)
    return p_values(observed, null)


def power_curve(base: list[dict], deltas, reps: int, draws: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    rows = {}
    for delta in deltas:
        p = np.array([detect(inject(base, delta, rng), draws, rng) for _ in range(reps)])
        rows[f"{delta:.4f}"] = {
            "reps": reps,
            "power": {f"alpha_{a}": {s: float((p[:, i] < a).mean()) for i, s in enumerate(STAT_NAMES)}
                      for a in ALPHAS},
            "p_mediaan": {s: float(np.median(p[:, i])) for i, s in enumerate(STAT_NAMES)},
        }
    return rows


def level_dispersion(m: np.ndarray, draws: int, seed: int, min_obs: int = MIN_OBS) -> dict:
    """Spreiding (std) van het gemiddelde per munt tegen herschikte muntlabels binnen de week."""
    rng = np.random.default_rng(seed)
    present = ~np.isnan(m)
    keep = present.sum(axis=0) >= min_obs

    def dispersion(x: np.ndarray) -> float:
        means = np.nanmean(x[:, keep], axis=0)
        return float(np.std(means, ddof=1))

    observed = dispersion(m)
    null = np.empty(draws)
    for d in range(draws):
        x = m.copy()
        for i in range(m.shape[0]):
            idx = np.flatnonzero(present[i])
            x[i, idx] = m[i, rng.permutation(idx)]
        null[d] = dispersion(x)
    return {"munten": int(keep.sum()), "min_obs": min_obs, "std_gemiddelde_per_munt": observed,
            "nul_mediaan": float(np.median(null)), "nul_p95": float(np.percentile(null, 95)),
            "p": float((1 + (null >= observed).sum()) / (1 + draws)), "draws": draws}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--residuals", type=Path, default=RESIDUALS)
    ap.add_argument("--reps", type=int, default=300)
    ap.add_argument("--draws", type=int, default=N_DRAWS)
    ap.add_argument("--level-draws", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()

    weeks, coins, m = load_residuals(a.residuals)
    base = base_panel(weeks, m)
    curve = power_curve(base, DELTAS, a.reps, a.draws, a.seed)
    level = level_dispersion(m, a.level_draws, a.seed)

    print(f"weken {len(weeks)}, munten {len(coins)}, reps {a.reps}, draws {a.draws}")
    print("delta/week | " + " | ".join(f"power {s} a={al}" for al in ALPHAS for s in STAT_NAMES))
    for d, row in curve.items():
        cells = [f"{row['power'][f'alpha_{al}'][s]:.3f}" for al in ALPHAS for s in STAT_NAMES]
        print(f"{float(d)*100:5.2f}%     | " + " | ".join(cells))
    print(f"niveau per munt: std {level['std_gemiddelde_per_munt']:.5f}, nul mediaan "
          f"{level['nul_mediaan']:.5f}, nul p95 {level['nul_p95']:.5f}, p {level['p']:.4f}")

    if a.out:
        payload = {"methode": {"instrument": "ant_colony.lab.xs_rank_test (spreads, N1, p_values)",
                               "injectie": "+delta op k(t) willekeurige munten, -delta*k/(n-k) op de rest",
                               "sensor": "perfect: selecteert de geinjecteerde deelverzameling (plafond)",
                               "no_trade": "overal False; delisting-afhandeling valt buiten deze meting",
                               "residuen": str(a.residuals), "seed": a.seed, "draws": a.draws},
                   "weken": len(weeks), "munten": len(coins), "alphas": ALPHAS,
                   "power": curve, "niveau_per_munt": level}
        a.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"geschreven: {a.out}")


if __name__ == "__main__":
    main()
