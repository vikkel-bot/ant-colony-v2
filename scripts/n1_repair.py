"""Reparatiekandidaten voor N1 en hun blinde kalibratie. Uitvoering van docs/DESIGN_N1_REPAIR_20261004.md (e3a67bf).

Kandidaat A (hac): HAC-gestudentiseerde S1-toets. t = gem(s) / SE_NW(gem(s)), Newey-West met Bartlett-gewichten
en lag 3, identiek berekend op de waargenomen spreadreeks en op elke N1-nultrekking.
Kandidaat B (strat): gestratificeerd N1. Per week vier rangkwartielen (Q=4) binnen U(t) op gemiddelde |r_rel|
over t-12..t-1, plus een onbekend-stratum voor munten met minder dan 4 geldige waarnemingen in het venster;
de nultrekking trekt per stratum exact evenveel munten als het sensormandje daar heeft.

Selectie en strata altijd op de OORSPRONKELIJKE residuen. Kalibratie: zelfde stress-opzet als n1_stress
(week-Rademacher primair, per run een nieuwe nulbank). Fase 1: --reps 1000, oordeel bij alpha 0,05.
Fase 2: --reps 4000 (runs 0-999 identiek door deterministische substromen), oordeel bij 0,05 en 0,0083.
De powermeting (alleen bij dubbel size-PASS) volgt als aparte stap na de faseresultaten.

Gebruik:
    py -3.14 scripts/n1_repair.py --candidate hac --reps 1000 --out docs/N1_REPAIR_HAC_F1_<datum>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ant_colony.lab.xs_rank_test import N_DRAWS, p_values, spreads, stats  # noqa: E402
from n1_stress import (  # noqa: E402
    ALPHAS, PLACEBOS, build_panel, classify, clopper_pearson, multipliers,
    placebo_selection, uniform_selection,
)
from power_calibration import RESIDUALS, load_residuals  # noqa: E402

MASTER_SEED = 20261004
CANDIDATES = ("hac", "strat")
HAC_LAG = 3
Q_STRATA = 4
VOL_WINDOW = 12
MIN_OBS_STRAT = 4
UNKNOWN = Q_STRATA  # label van het onbekend-stratum


def rng_for(c: int, p: int, run: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(MASTER_SEED, spawn_key=(c, p, run)))


def hac_t(series: np.ndarray, lag: int = HAC_LAG) -> np.ndarray:
    """Newey-West t-statistiek van het gemiddelde, Bartlett-gewichten, langs de laatste as.

    Werkt op (weeks,) en op (draws, weeks). Bartlett garandeert een niet-negatieve variantie.
    """
    x = np.atleast_2d(series)
    T = x.shape[-1]
    d = x - x.mean(axis=-1, keepdims=True)
    lrv = (d * d).mean(axis=-1)
    for j in range(1, min(lag, T - 1) + 1):
        gamma = (d[..., j:] * d[..., :-j]).sum(axis=-1) / T
        lrv = lrv + 2.0 * (1.0 - j / (lag + 1.0)) * gamma
    lrv = np.maximum(lrv, 1e-300)
    t = x.mean(axis=-1) / np.sqrt(lrv / T)
    return t[0] if series.ndim == 1 else t


def null_spread_matrix(panel: list[dict], draws: int, rng: np.random.Generator) -> np.ndarray:
    """Uniforme N1-trekkingen als (draws, weeks)-spreadmatrix (zelfde trekwijze als productie-N1)."""
    cols = []
    for w in panel:
        r = w["r"]
        idx = rng.random((draws, w["n"])).argsort(axis=1)[:, : w["k"]]
        cols.append(r[idx].mean(axis=1) - r.mean())
    return np.stack(cols, axis=1)


def strata_for_weeks(m: np.ndarray, sel: dict) -> dict:
    """Per geselecteerde week: stratumlabel per universumlid, op de oorspronkelijke residuen.

    Rangkwartielen (gelijke rang: laagste kolomindex eerst, munten staan alfabetisch); label UNKNOWN bij
    minder dan MIN_OBS_STRAT waarnemingen in t-12..t-1.
    """
    present = ~np.isnan(m)
    absm = np.abs(m)
    out = {}
    for i in sel["weeks"]:
        members = np.flatnonzero(present[i])
        lo = max(0, i - VOL_WINDOW)
        hist = absm[lo:i, members]
        cnt = (~np.isnan(hist)).sum(axis=0)
        vol = np.nansum(np.nan_to_num(hist, nan=0.0), axis=0) / np.maximum(cnt, 1)
        labels = np.full(len(members), UNKNOWN)
        known = np.flatnonzero(cnt >= MIN_OBS_STRAT)
        if len(known):
            order = known[np.lexsort((members[known], vol[known]))]
            ranks = np.arange(len(order))
            labels[order] = (ranks * Q_STRATA) // len(order)
        out[i] = {"members": members, "labels": labels}
    return out


def stratified_null_matrix(panel: list[dict], strata: dict, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Nultrekkingen die per stratum exact de sensoraantallen matchen; (draws, weeks)-spreadmatrix."""
    cols = []
    for w in panel:
        info = strata[w["t"]]
        labels, r = info["labels"], w["r"]
        total = np.zeros(draws)
        k = 0
        for q in range(Q_STRATA + 1):
            in_q = np.flatnonzero(labels == q)
            c_q = int(w["top"][in_q].sum())
            if c_q == 0:
                continue
            k += c_q
            idx = rng.random((draws, len(in_q))).argsort(axis=1)[:, :c_q]
            total += r[in_q][idx].sum(axis=1)
        cols.append(total / k - r.mean())
    return np.stack(cols, axis=1)


def p_hac(panel: list[dict], draws: int, rng: np.random.Generator) -> float:
    t_obs = hac_t(spreads(panel))
    t_null = hac_t(null_spread_matrix(panel, draws, rng))
    return float((1 + (t_null >= t_obs).sum()) / (1 + draws))


def p_strat(panel: list[dict], strata: dict, draws: int, rng: np.random.Generator) -> float:
    observed = stats(spreads(panel))
    null = stats(stratified_null_matrix(panel, strata, draws, rng))
    return float(p_values(observed, null)[0])


def evaluate(m: np.ndarray, candidate: str, placebo: str, reps: int, draws: int) -> dict:
    c, p = CANDIDATES.index(candidate), PLACEBOS.index(placebo)
    fixed = None
    if placebo != "uniform":
        window = int(placebo.split("w_")[0])
        fixed = placebo_selection(m, window, placebo.endswith("hoog"),
                                  np.random.default_rng(np.random.SeedSequence(MASTER_SEED, spawn_key=(98, p))))
    pv = np.empty(reps)
    for run in range(reps):
        rng = rng_for(c, p, run)
        sel = fixed if fixed is not None else uniform_selection(m, rng)
        strata = strata_for_weeks(m, sel) if candidate == "strat" else None
        w = multipliers("rademacher", m.shape[0], rng)
        panel = build_panel(m, sel, w)
        pv[run] = p_hac(panel, draws, rng) if candidate == "hac" else p_strat(panel, strata, draws, rng)
    out = {"reps": reps, "draws": draws}
    if fixed is not None:
        out.update(weken=len(fixed["weeks"]), aanvulweken=fixed["fill_weeks"])
    for a in ALPHAS:
        x = int((pv < a).sum())
        lo, hi = clopper_pearson(x, reps)
        out[f"alpha_{a}"] = {"verwerpingen": x, "fractie": x / reps, "cp95": [lo, hi], "oordeel": classify(x, reps, a)}
    return out


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", choices=CANDIDATES, required=True)
    ap.add_argument("--reps", type=int, default=1000)
    ap.add_argument("--draws", type=int, default=N_DRAWS)
    ap.add_argument("--placebos", nargs="*", default=list(PLACEBOS))
    ap.add_argument("--residuals", type=Path, default=RESIDUALS)
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()

    _, _, m = load_residuals(a.residuals)
    payload = {"ontwerp": "docs/DESIGN_N1_REPAIR_20261004.md (e3a67bf)", "kandidaat": a.candidate,
               "variant": "rademacher", "master_seed": MASTER_SEED, "resultaten": {}}
    for placebo in a.placebos:
        t0 = time.time()
        res = evaluate(m, a.candidate, placebo, a.reps, a.draws)
        payload["resultaten"][placebo] = res
        r5, r8 = res["alpha_0.05"], res["alpha_0.0083"]
        print(f"{placebo:9s} | a=.05 {r5['fractie']:.3f} [{r5['cp95'][0]:.3f}, {r5['cp95'][1]:.3f}] {r5['oordeel']:17s} | "
              f"a=.0083 {r8['fractie']:.4f} {r8['oordeel']:17s} | {time.time() - t0:.0f}s", flush=True)
        if a.out:
            _write_atomic(a.out, payload)
    if a.out:
        print(f"geschreven: {a.out}")


if __name__ == "__main__":
    main()
