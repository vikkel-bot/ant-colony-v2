"""Powermeting HAC en dependence-size-gate. Uitvoering van docs/DESIGN_N1_REPAIR_POWER_ADDENDUM_20261006.md (1945c1c).

Deel A: gepreregistreerde iid-power volgens e3a67bf (diagnostisch). Week-Rademacher, 3 hoog-paden,
        delta 0,4/0,6/0,8 %/week, 300 runs, detectie bij p < 0,0083.
Deel B: dependence-size-gate. Block-4- en block-8-Rademacher op de hele weekvector, delta = 0, 4.000 runs per
        (DGP, pad). Verwerping bij p < 0,05 en p < 0,0083. Eenzijdige exacte binomiale toets per cel, Holm 5%
        over alle 12. Ook R_native, R_block en de werkingskarakteristiek van de gate.
Deel C: power onder block-4/8, alleen na GATE PASS (fail-closed op het Deel-B-bestand). Delta 0,4/0,6/0,8/1,0,
        300 runs, uitbreiding naar 1.000 als het 95%-CP-interval 0,80 of 0,90 bevat. Raw MDE80/90 bindend;
        size-gecorrigeerde curve diagnostisch.

Verwerping is p < alpha, gelijk aan fase 2. Met N_DRAWS = 10.000 is (1 + draws) * 0,0083 = 83,008 geen geheel
getal, dus p < alpha en p <= alpha zijn hier identiek.
Vaste paden: exact de paden van fase 2 (placebo_selection met seed (n1_repair.MASTER_SEED, spawn_key=(98, p))).

Gebruik:
    py -3.14 scripts/n1_repair_power.py --part A --out docs/N1_REPAIR_POWER_A_<datum>.json
    py -3.14 scripts/n1_repair_power.py --part B --out docs/N1_REPAIR_POWER_B_<datum>.json
    py -3.14 scripts/n1_repair_power.py --part C --gate docs/N1_REPAIR_POWER_B_<datum>.json --out docs/N1_REPAIR_POWER_C_<datum>.json
Afgebroken run: zelfde commando opnieuw; afgeronde cellen in --out worden overgeslagen (deterministische seeds).
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

from ant_colony.lab.xs_rank_test import N_DRAWS, spreads  # noqa: E402
from n1_stress import PLACEBOS, _binom_cdf, build_panel, clopper_pearson, multipliers, placebo_selection  # noqa: E402
import n1_repair  # noqa: E402
from n1_repair import HAC_LAG, p_hac  # noqa: E402
from power_calibration import RESIDUALS, load_residuals  # noqa: E402

MASTER_SEED = 20261006
ADDENDUM = "docs/DESIGN_N1_REPAIR_POWER_ADDENDUM_20261006.md (1945c1c)"
PATHS = ("1w_hoog", "4w_hoog", "12w_hoog")
DGPS = ("iid", "block4", "block8")
BLOCK_DGPS = ("block4", "block8")
PARTS = ("A", "B", "C")
DELTAS_A = (0.004, 0.006, 0.008)
DELTAS_C = (0.004, 0.006, 0.008, 0.010)
ALPHA_DETECT = 0.0083
GATE_ALPHAS = (0.05, 0.0083)
HOLM_FWER = 0.05
REPS_A, REPS_B, REPS_C, REPS_EXT = 300, 4000, 300, 1000
EXT_TARGETS = (0.80, 0.90)
OC_FACTORS = (1.25, 1.5, 2.0)


def rng_for(part: int, dgp: int, path: int, delta: int, run: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(MASTER_SEED, spawn_key=(part, dgp, path, delta, run)))


def block_multipliers(L: int, n_weeks: int, rng: np.random.Generator) -> np.ndarray:
    """Eén teken per blok van L weken voor de hele weekvector; offset uniform uit {0..L-1} (als block3)."""
    offset = int(rng.integers(0, L))
    block = (np.arange(n_weeks) + offset) // L
    signs = rng.choice(np.array([-1.0, 1.0]), block.max() + 1)
    return signs[block]


def dgp_multipliers(dgp: str, n_weeks: int, rng: np.random.Generator) -> np.ndarray:
    if dgp == "iid":
        return multipliers("rademacher", n_weeks, rng)
    if dgp in BLOCK_DGPS:
        return block_multipliers(int(dgp[len("block"):]), n_weeks, rng)
    raise ValueError(f"onbekende DGP: {dgp}")


def fixed_path(m: np.ndarray, path: str) -> dict:
    """Exact het vaste pad van fase 2, op de oorspronkelijke residuen."""
    p = PLACEBOS.index(path)
    window = int(path.split("w_")[0])
    fill = np.random.default_rng(np.random.SeedSequence(n1_repair.MASTER_SEED, spawn_key=(98, p)))
    return placebo_selection(m, window, path.endswith("hoog"), fill)


def inject_path(panel: list[dict], delta: float) -> list[dict]:
    """Padmunten +delta, overige -delta*k/(n-k): universumgemiddelde gelijk, S1 per week exact +delta."""
    out = []
    for w in panel:
        n, k = w["n"], w["k"]
        if n <= k:
            raise ValueError(f"week {w['t']}: n={n} <= k={k}, injectie niet definieerbaar")
        out.append({**w, "r": w["r"] + np.where(w["top"], delta, -delta * k / (n - k))})
    return out


def var_ratio(series: np.ndarray, lag: int = HAC_LAG) -> float:
    """Var_HAC / Var_iid van het gemiddelde: Newey-West-lrv (zelfde formule als hac_t) gedeeld door gamma_0."""
    x = np.asarray(series, float)
    T = len(x)
    d = x - x.mean()
    g0 = float((d * d).mean())
    lrv = g0
    for j in range(1, min(lag, T - 1) + 1):
        lrv += 2.0 * (1.0 - j / (lag + 1.0)) * float((d[j:] * d[:-j]).sum()) / T
    return max(lrv, 1e-300) / max(g0, 1e-300)


def one_run(m: np.ndarray, sel: dict, dgp: str, delta: float, rng: np.random.Generator, draws: int) -> tuple[float, float]:
    """(p_HAC, R van de gebruikte spreadreeks). Volgorde: vast pad -> flip -> injectie -> toets."""
    panel = build_panel(m, sel, dgp_multipliers(dgp, m.shape[0], rng))
    if delta:
        panel = inject_path(panel, delta)
    s = spreads(panel)
    return p_hac(panel, draws, rng), var_ratio(s)


def binom_sf(x: int, n: int, p: float) -> float:
    """P(X >= x) bij X ~ Bin(n, p)."""
    return 1.0 if x <= 0 else max(0.0, 1.0 - _binom_cdf(x - 1, n, p))


def holm(pvals: list[float], fwer: float = HOLM_FWER) -> list[bool]:
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    rej = [False] * len(pvals)
    m = len(pvals)
    for rank, i in enumerate(order):
        if pvals[i] <= fwer / (m - rank):
            rej[i] = True
        else:
            break
    return rej


def critical_count(n: int, alpha: float, level: float) -> int:
    """Kleinste x met P(X >= x | alpha) <= level."""
    x = int(math.floor(n * alpha))
    while binom_sf(x, n, alpha) > level:
        x += 1
    while x > 0 and binom_sf(x - 1, n, alpha) <= level:
        x -= 1
    return x


def gate_oc(n: int, n_tests: int, fwer: float = HOLM_FWER) -> dict:
    """Kans dat één cel op de strengste Holm-stap verwerpt, bij ware size factor x alpha. Rapportage, geen beslissing."""
    out = {}
    for a in GATE_ALPHAS:
        xc = critical_count(n, a, fwer / n_tests)
        out[f"alpha_{a}"] = {"drempel_verwerpingen": xc,
                             **{f"ware_size_{f}x": binom_sf(xc, n, min(1.0, f * a)) for f in OC_FACTORS}}
    return out


def needs_extension(x: int, n: int) -> bool:
    lo, hi = clopper_pearson(x, n)
    return any(lo <= t <= hi for t in EXT_TARGETS)


def mde(deltas, powers, target: float) -> float | None:
    """Lineaire interpolatie tussen aangrenzende gridpunten; None = niet bereikt binnen het grid."""
    prev_d, prev_p = 0.0, None
    for d, p in zip(deltas, powers):
        if p >= target:
            if prev_p is None or p == prev_p:
                return float(d)
            return float(prev_d + (target - prev_p) / (p - prev_p) * (d - prev_d))
        prev_d, prev_p = d, p
    return None


def empirical_cutoff(p0: np.ndarray, alpha: float = ALPHA_DETECT) -> float:
    """Diagnostisch: drempel c met fractie(p0 < c) <= alpha (c = de (floor(alpha*n)+1)-de kleinste p0)."""
    s = np.sort(np.asarray(p0))
    j = int(math.floor(alpha * len(s)))
    return float(s[min(j, len(s) - 1)])


def check_gate(gate_payload: dict, diagnostic: bool) -> list[str]:
    """Fail-closed: geeft de DGP's terug waarvoor Deel C mag draaien."""
    g = gate_payload.get("gate")
    if not g or g.get("compleet") is not True:
        raise SystemExit("Deel B is niet compleet; Deel C geweigerd.")
    if g["oordeel"] == "PASS":
        return list(BLOCK_DGPS)
    if not diagnostic:
        raise SystemExit("GATE FAIL; Deel C geweigerd (alleen met --diagnostisch, en dan niet voor beleid).")
    rejected = {c.split("|")[0] for c, r in g["verworpen"].items() if r}
    return [d for d in BLOCK_DGPS if d not in rejected]


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _runs(m, sel, dgp, delta, part, path, di, start, stop, draws):
    pv, rr = [], []
    for run in range(start, stop):
        rng = rng_for(PARTS.index(part) + 1, DGPS.index(dgp), PATHS.index(path), di, run)
        p, r = one_run(m, sel, dgp, delta, rng, draws)
        pv.append(p)
        rr.append(r)
    return pv, rr


def _power_cell(pv: list[float]) -> dict:
    n = len(pv)
    x = int((np.array(pv) < ALPHA_DETECT).sum())
    lo, hi = clopper_pearson(x, n)
    return {"reps": n, "verwerpingen": x, "power": x / n, "cp95": [lo, hi], "p_mediaan": float(np.median(pv))}


def run_part_a(m, paths, draws, reps, payload, save):
    res = payload.setdefault("resultaten", {})
    for path in paths:
        sel = fixed_path(m, path)
        for di, delta in enumerate(DELTAS_A, start=1):
            key = f"iid|{path}|{delta:.3f}"
            if key in res and res[key]["reps"] == reps:
                continue
            t0 = time.time()
            pv, _ = _runs(m, sel, "iid", delta, "A", path, di, 0, reps, draws)
            res[key] = _power_cell(pv)
            print(f"A {key} power {res[key]['power']:.3f} {res[key]['cp95']} | {time.time() - t0:.0f}s", flush=True)
            save()


def run_part_b(m, paths, draws, reps, payload, save):
    res = payload.setdefault("resultaten", {})
    payload["R_native"] = {p: var_ratio(spreads(build_panel(m, fixed_path(m, p), np.ones(m.shape[0])))) for p in paths}
    for dgp in BLOCK_DGPS:
        for path in paths:
            key = f"{dgp}|{path}"
            if key in res and res[key]["reps"] == reps:
                continue
            t0 = time.time()
            pv, rr = _runs(m, fixed_path(m, path), dgp, 0.0, "B", path, 0, 0, reps, draws)
            cell = {"reps": reps, "p": pv,
                    "R_block": {"mediaan": float(np.median(rr)), "q25": float(np.quantile(rr, 0.25)),
                                "q75": float(np.quantile(rr, 0.75))}}
            for a in GATE_ALPHAS:
                x = int((np.array(pv) < a).sum())
                lo, hi = clopper_pearson(x, reps)
                cell[f"alpha_{a}"] = {"verwerpingen": x, "fractie": x / reps, "cp95": [lo, hi],
                                      "binom_p": binom_sf(x, reps, a)}
            res[key] = cell
            print(f"B {key} a=.05 {cell['alpha_0.05']['fractie']:.4f} a=.0083 {cell['alpha_0.0083']['fractie']:.4f}"
                  f" | {time.time() - t0:.0f}s", flush=True)
            save()
    keys = [f"{d}|{p}|{a}" for d in BLOCK_DGPS for p in paths for a in GATE_ALPHAS]
    complete = len(paths) == len(PATHS) and all(f"{d}|{p}" in res for d in BLOCK_DGPS for p in paths)
    pvals = [res[k.rsplit("|", 1)[0]][f"alpha_{k.rsplit('|', 1)[1]}"]["binom_p"] for k in keys]
    rej = holm(pvals)
    payload["gate"] = {"compleet": complete, "familie": len(keys), "fwer": HOLM_FWER,
                       "binom_p": dict(zip(keys, pvals)), "verworpen": dict(zip(keys, rej)),
                       "oordeel": "FAIL" if any(rej) else "PASS",
                       "werkingskarakteristiek": gate_oc(reps, len(keys))}
    save()
    print(f"GATE {payload['gate']['oordeel']} (compleet={complete})", flush=True)


def run_part_c(m, paths, draws, gate_payload, dgps, payload, save, reps=REPS_C, reps_ext=REPS_EXT):
    res = payload.setdefault("resultaten", {})
    for dgp in dgps:
        for path in paths:
            sel = fixed_path(m, path)
            p0 = np.array(gate_payload["resultaten"][f"{dgp}|{path}"]["p"])
            cut = empirical_cutoff(p0)
            powers, adj = [], []
            for di, delta in enumerate(DELTAS_C, start=1):
                key = f"{dgp}|{path}|{delta:.3f}"
                if key not in res:
                    t0 = time.time()
                    pv, _ = _runs(m, sel, dgp, delta, "C", path, di, 0, reps, draws)
                    x = int((np.array(pv) < ALPHA_DETECT).sum())
                    if needs_extension(x, reps):
                        more, _ = _runs(m, sel, dgp, delta, "C", path, di, reps, reps_ext, draws)
                        pv += more
                    cell = _power_cell(pv)
                    cell["uitgebreid"] = len(pv) > reps
                    cell["power_size_gecorrigeerd"] = float((np.array(pv) < cut).mean())
                    cell["p"] = pv
                    res[key] = cell
                    print(f"C {key} power {cell['power']:.3f} (n={cell['reps']}) | {time.time() - t0:.0f}s", flush=True)
                    save()
                powers.append(res[key]["power"])
                adj.append(res[key]["power_size_gecorrigeerd"])
            res[f"{dgp}|{path}|MDE"] = {"MDE80": mde(DELTAS_C, powers, 0.80), "MDE90": mde(DELTAS_C, powers, 0.90),
                                        "MDE80_size_gecorrigeerd_diag": mde(DELTAS_C, adj, 0.80),
                                        "empirische_drempel_diag": cut}
    vals80 = [res[f"{d}|{p}|MDE"]["MDE80"] for d in dgps for p in paths]
    vals90 = [res[f"{d}|{p}|MDE"]["MDE90"] for d in dgps for p in paths]
    worst = lambda v: None if any(x is None for x in v) else max(v)  # noqa: E731
    payload["eligibility"] = {"MDE80": worst(vals80), "MDE90": worst(vals90),
                              "niet_bereikt": "None = niet bereikt binnen grid (> 1,0 %/week)"}
    save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=PARTS, required=True)
    ap.add_argument("--draws", type=int, default=N_DRAWS)
    ap.add_argument("--reps", type=int, default=None, help="alleen voor tests; standaard volgens addendum")
    ap.add_argument("--paths", nargs="*", default=list(PATHS))
    ap.add_argument("--residuals", type=Path, default=RESIDUALS)
    ap.add_argument("--gate", type=Path, default=None, help="Deel-B-resultaat (verplicht voor Deel C)")
    ap.add_argument("--diagnostisch", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    _, _, m = load_residuals(a.residuals)
    payload = json.loads(a.out.read_text(encoding="utf-8")) if a.out.exists() else {}
    payload.update(ontwerp=ADDENDUM, deel=a.part, master_seed=MASTER_SEED, draws=a.draws)
    save = lambda: _write_atomic(a.out, payload)  # noqa: E731
    if a.part == "A":
        payload["status"] = "gepreregistreerde diagnostiek (e3a67bf); geen rol in eligibility"
        run_part_a(m, a.paths, a.draws, a.reps or REPS_A, payload, save)
    elif a.part == "B":
        run_part_b(m, a.paths, a.draws, a.reps or REPS_B, payload, save)
    else:
        if a.gate is None:
            raise SystemExit("Deel C vereist --gate met het Deel-B-resultaat.")
        gate_payload = json.loads(a.gate.read_text(encoding="utf-8"))
        dgps = check_gate(gate_payload, a.diagnostisch)
        payload["status"] = "beleid" if gate_payload["gate"]["oordeel"] == "PASS" else "diagnostisch_niet_beleid"
        run_part_c(m, a.paths, a.draws, gate_payload, dgps, payload, save,
                   reps=a.reps or REPS_C, reps_ext=REPS_EXT if a.reps is None else a.reps)
    print(f"geschreven: {a.out}")


if __name__ == "__main__":
    main()
