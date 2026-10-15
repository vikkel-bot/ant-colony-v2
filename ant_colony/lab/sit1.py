"""SIT-1A — mechanismegate (docs/PREREG_SIT1_20260926.md §1-§3, §5, §6).

Alleen stdlib + numpy. Geen discretie: alle getallen hieronder komen uit de
preregistratie. Waar de preregistratie de implementatie open laat, staat de
keuze hieronder onder IMPLEMENTATIEKEUZES, vastgelegd vóór de eerste run.

Maanden worden gerepresenteerd als integer m = jaar*12 + (maand-1).

IMPLEMENTATIEKEUZES (geen specificatiewijziging; vastgelegd vóór enige run):
  I1  s²_t (terugtransformatie §3.1) = SSR/(n-p) van het OLS-model op alle
      regressieobservaties met target <= t (p = aantal parameters).
  I2  Bootstrap van Δ (§3.3): (δ_t, QLIKE_M0,t) worden met dezelfde indices
      geresampled; Δ* = mean(δ*)/mean(QLIKE_M0*). SE_boot = std(Δ*, ddof=1).
      CI = 2,5e/97,5e percentiel. Stationary bootstrap (Politis-Romano),
      kans op nieuw blok 1/12, cirkelvormig.
  I3  d̂ met HAC-SE: Newey-West, Bartlett, 12 lags (gelijk aan CW).
  I4  OOS-R² (§3.8) = 1 - MSE_M1/MSE_M0 op log-RV.
  I5  Episodes (§5): drawdown van cumulatief (1+Mkt-RF_d), maandeinde, vanaf
      1926-07. "Grootste" episode = grootste maximale drawdown binnen de episode.
  I6  Reverse-causality (§1): OLS van ΔCS_{t+1} op [1, y_t, ȳ3, ȳ12, ΔCS_t] op
      alle observaties t/m venstereinde, NW12-t-waarden. Descriptief.
  I7  RV vereist ten minste 15 handelsdagen in de maand; anders ontbreekt y_t
      (komt in de Ken French-reeks niet voor; vangnet).
"""
from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --- preregistratie -----------------------------------------------------------
SEED = 20260926
N_BOOT = 10_000
BLOCK_MEAN = 12
NW_LAGS = 12
DCS_H = 3                      # ΔCS_t = CS_{t-1} - CS_{t-4}
PERTURB_H = (1, 6)
PRACTICAL = 0.02               # Δ̂ >= 2 %
THR = {"development": 0.05 / 6, "lhe": 0.05}
INLOOP_N = 120
MIN_DAYS = 15                  # I7
VINTAGE_MAX_BP = 5.0


def M(y: int, m: int) -> int:
    return y * 12 + (m - 1)


def label(m: int) -> str:
    return f"{m // 12:04d}-{m % 12 + 1:02d}"


WINDOWS = {
    "development": (M(1937, 7), M(1990, 12)),   # 642 forecasts
    "lhe": (M(1991, 1), M(2026, 9)),            # 429 forecasts
}
FIRST_TARGET = M(1927, 7)


# --- data ---------------------------------------------------------------------
def read_ff_daily(path: Path) -> list[tuple[int, int, float]]:
    """(maand, yyyymmdd, Mkt-RF als fractie) uit het Ken French daily-zip."""
    with zipfile.ZipFile(path) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or not row[0].strip().isdigit() or len(row[0].strip()) != 8:
            continue
        d = int(row[0])
        out.append((M(d // 10000, d // 100 % 100), d, float(row[1]) / 100.0))
    if not out:
        raise ValueError("geen dagregels in Ken French-bestand")
    return out


def monthly_rv(daily: list[tuple[int, int, float]]) -> dict[int, float]:
    acc: dict[int, list[float]] = {}
    for m, _, r in daily:
        acc.setdefault(m, []).append(r)
    return {m: float(np.sum(np.square(v))) for m, v in acc.items() if len(v) >= MIN_DAYS}


def monthly_growth(daily: list[tuple[int, int, float]]) -> dict[int, float]:
    acc: dict[int, float] = {}
    for m, _, r in daily:
        acc[m] = acc.get(m, 1.0) * (1.0 + r)
    return acc


def read_fred(path: Path) -> dict[int, float]:
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        rdr = csv.reader(f)
        next(rdr)
        for row in rdr:
            if len(row) < 2 or row[1].strip() in ("", "."):
                continue
            y, mo, _ = row[0].split("-")
            out[M(int(y), int(mo))] = float(row[1])
    return out


def credit_spread_bp(baa: dict[int, float], aaa: dict[int, float]) -> dict[int, float]:
    return {m: (baa[m] - aaa[m]) * 100.0 for m in baa if m in aaa}


def dcs(cs: dict[int, float], t: int, h: int = DCS_H) -> float | None:
    a, b = cs.get(t - 1), cs.get(t - 1 - h)
    return None if a is None or b is None else a - b


# --- regressieset -------------------------------------------------------------
@dataclass
class Row:
    t: int               # informatiemoment (regressoren bekend op slot maand t)
    target: int          # t + 1
    x0: np.ndarray       # [1, y_t, ȳ_{t-2:t}, ȳ_{t-11:t}]
    dcs: float
    y: float             # y_{t+1}
    rv: float            # RV_{t+1}
    max_y_month: int     # hoogste maand waarvan y in de regressoren zit
    max_cs_month: int    # hoogste maand waarvan CS in de regressoren zit


def build_rows(rv: dict[int, float], cs: dict[int, float], h: int = DCS_H) -> list[Row]:
    y = {m: math.log(v) for m, v in rv.items() if v > 0}
    rows = []
    for t in sorted(y):
        lags = [y.get(t - k) for k in range(12)]
        d = dcs(cs, t, h)
        if any(v is None for v in lags) or d is None or (t + 1) not in y:
            continue
        x0 = np.array([1.0, lags[0], float(np.mean(lags[:3])), float(np.mean(lags))])
        rows.append(Row(t, t + 1, x0, d, y[t + 1], rv[t + 1], t, t - 1))
    return rows


def _ols(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    s2 = float(resid @ resid) / (len(y) - X.shape[1])          # I1
    return beta, s2


def _x(r: Row, with_cs: bool) -> np.ndarray:
    return np.append(r.x0, r.dcs) if with_cs else r.x0


@dataclass
class Forecasts:
    target: list[int] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    rv: list[float] = field(default_factory=list)
    yhat0: list[float] = field(default_factory=list)
    yhat1: list[float] = field(default_factory=list)
    s2_0: list[float] = field(default_factory=list)
    s2_1: list[float] = field(default_factory=list)
    n_est: list[int] = field(default_factory=list)


def recursive_forecasts(rows: list[Row], start: int, end: int) -> Forecasts:
    """Expanding OLS; forecast voor target τ gebruikt uitsluitend informatie t/m τ-1."""
    by_target = {r.target: r for r in rows}
    out = Forecasts()
    for tau in range(start, end + 1):
        if tau not in by_target:
            raise ValueError(f"target {label(tau)} ontbreekt in de data (fail-closed)")
        t = tau - 1
        est = [r for r in rows if r.target <= t]
        cur = by_target[tau]
        # assert §2 stap 3: geen observatie met datum > t in forecast t
        assert all(r.target <= t for r in est), "toekomstlek in schattingsset"
        assert cur.t == t and cur.max_y_month <= t and cur.max_cs_month <= t - 1, "toekomstlek in regressoren"
        y_est = np.array([r.y for r in est])
        X0 = np.array([_x(r, False) for r in est])
        X1 = np.array([_x(r, True) for r in est])
        b0, s0 = _ols(X0, y_est)
        b1, s1 = _ols(X1, y_est)
        out.target.append(tau)
        out.y.append(cur.y)
        out.rv.append(cur.rv)
        out.yhat0.append(float(_x(cur, False) @ b0))
        out.yhat1.append(float(_x(cur, True) @ b1))
        out.s2_0.append(s0)
        out.s2_1.append(s1)
        out.n_est.append(len(est))
    return out


# --- statistiek ---------------------------------------------------------------
def nw_var_of_mean(x: np.ndarray, lags: int = NW_LAGS) -> float:
    x = np.asarray(x, float)
    T = len(x)
    u = x - x.mean()
    v = float(u @ u) / T
    for j in range(1, lags + 1):
        v += 2.0 * (1.0 - j / (lags + 1)) * float(u[j:] @ u[:-j]) / T
    return v / T


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def clark_west(fc: Forecasts) -> dict:
    y, f0, f1 = (np.array(v) for v in (fc.y, fc.yhat0, fc.yhat1))
    e0, e1 = y - f0, y - f1
    f = e0 ** 2 - (e1 ** 2 - (f0 - f1) ** 2)
    se = math.sqrt(nw_var_of_mean(f))
    cw = float(f.mean()) / se
    return {"cw": cw, "p": norm_sf(cw), "mean_f": float(f.mean()), "se_nw12": se,
            "mde_cw": 2.8 * se, "mse0": float(np.mean(e0 ** 2)), "mse1": float(np.mean(e1 ** 2)),
            "oos_r2_m1_vs_m0": 1.0 - float(np.mean(e1 ** 2)) / float(np.mean(e0 ** 2))}


def qlike(sig2: np.ndarray, rv: np.ndarray) -> np.ndarray:
    q = rv / sig2
    return q - np.log(q) - 1.0


def qlike_series(fc: Forecasts) -> tuple[np.ndarray, np.ndarray]:
    rv = np.array(fc.rv)
    s0 = np.exp(np.array(fc.yhat0) + 0.5 * np.array(fc.s2_0))
    s1 = np.exp(np.array(fc.yhat1) + 0.5 * np.array(fc.s2_1))
    q0, q1 = qlike(s0, rv), qlike(s1, rv)
    return q0 - q1, q0


def stationary_indices(T: int, n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    p = 1.0 / mean_block
    idx = np.empty((n, T), dtype=np.int64)
    idx[:, 0] = rng.integers(0, T, n)
    new = rng.random((n, T)) < p
    starts = rng.integers(0, T, (n, T))
    for j in range(1, T):
        idx[:, j] = np.where(new[:, j], starts[:, j], (idx[:, j - 1] + 1) % T)
    return idx


def qlike_gain(delta: np.ndarray, q0: np.ndarray, seed: int = SEED, n_boot: int = N_BOOT) -> dict:
    point = float(delta.mean() / q0.mean())
    rng = np.random.Generator(np.random.PCG64(seed))
    idx = stationary_indices(len(delta), n_boot, BLOCK_MEAN, rng)
    boots = delta[idx].mean(axis=1) / q0[idx].mean(axis=1)          # I2
    lo, hi = np.percentile(boots, [2.5, 97.5])
    se = float(np.std(boots, ddof=1))
    order = np.argsort(-np.abs(delta))
    top = order[: max(1, int(math.ceil(0.05 * len(delta))))]
    total = float(delta.sum())
    return {"delta": point, "ci_lower": float(lo), "ci_upper": float(hi), "se_boot": se,
            "mde_delta": 2.8 * se, "share_sum_delta_top5pct": (float(delta[top].sum()) / total) if total else None}


def ols_hac(X: np.ndarray, y: np.ndarray, lags: int = NW_LAGS) -> tuple[np.ndarray, np.ndarray]:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    u = y - X @ beta
    T = len(y)
    g = X * u[:, None]
    S = g.T @ g / T
    for j in range(1, lags + 1):
        G = g[j:].T @ g[:-j] / T
        S += (1.0 - j / (lags + 1)) * (G + G.T)
    Q = np.linalg.inv(X.T @ X / T)
    V = Q @ S @ Q / T
    return beta, np.sqrt(np.diag(V))


def d_hat(rows: list[Row], end: int) -> dict:
    est = [r for r in rows if r.target <= end]
    X = np.array([_x(r, True) for r in est])
    b, se = ols_hac(X, np.array([r.y for r in est]))                # I3
    return {"d": float(b[-1]), "se_hac": float(se[-1]), "t": float(b[-1] / se[-1]), "n": len(est)}


def decide(p: float, thr: float, d: float, delta: float, ci_upper: float) -> str:
    """§3.6, letterlijk."""
    if p <= thr and d > 0 and delta >= PRACTICAL:
        return "PASS"
    if ci_upper < PRACTICAL or (p <= thr and d < 0):
        return "FAIL"
    return "INCONCLUSIVE"


# --- descriptief --------------------------------------------------------------
def episodes(growth: dict[int, float], thresh: float = 0.20, gap: int = 6) -> list[dict]:
    level, peak, dd = 1.0, 1.0, {}
    for m in sorted(growth):
        level *= growth[m]
        peak = max(peak, level)
        dd[m] = 1.0 - level / peak
    deep = [m for m in sorted(dd) if dd[m] > thresh]
    eps: list[list[int]] = []
    for m in deep:
        if eps and (m - eps[-1][-1] - 1) < gap:        # gat = maanden ertussen
            eps[-1].append(m)
        else:
            eps.append([m])
    return [{"start": e[0], "end": e[-1], "max_dd": max(dd[m] for m in range(e[0], e[-1] + 1) if m in dd)} for e in eps]


def episode_attribution(fc: Forecasts, eps: list[dict]) -> dict:
    tg = np.array(fc.target)
    inwin = [e for e in eps if e["end"] >= tg[0] and e["start"] <= tg[-1]]
    ranked = sorted(inwin, key=lambda e: -e["max_dd"])                # I5
    out = {"episodes_in_window": [{"start": label(e["start"]), "end": label(e["end"]),
                                   "max_dd": round(e["max_dd"], 4)} for e in inwin]}
    for name, drop in (("volledig", []), ("zonder_grootste", ranked[:1]),
                       ("zonder_twee_grootste", ranked[:2]), ("zonder_alle", ranked)):
        keep = np.ones(len(tg), bool)
        for e in drop:
            keep &= ~((tg >= e["start"]) & (tg <= e["end"]))
        sub = Forecasts(*[list(np.array(getattr(fc, k))[keep]) for k in
                          ("target", "y", "rv", "yhat0", "yhat1", "s2_0", "s2_1", "n_est")])
        dl, q0 = qlike_series(sub)
        cw = clark_west(sub)
        out[name] = {"n": int(keep.sum()), "delta": float(dl.mean() / q0.mean()), "cw": cw["cw"], "p": cw["p"]}
    return out


def reverse_causality(rows: list[Row], cs: dict[int, float], end: int) -> dict:
    est = [r for r in rows if r.target <= end and dcs(cs, r.t + 1) is not None]
    X = np.array([_x(r, True) for r in est])
    yv = np.array([dcs(cs, r.t + 1) for r in est])
    b, se = ols_hac(X, yv)                                            # I6
    names = ["const", "y_t", "ybar3", "ybar12", "dcs_t"]
    return {"target": "dCS_{t+1}", "n": len(est),
            "coef": {n: float(v) for n, v in zip(names, b)},
            "t_nw12": {n: float(v / s) for n, v, s in zip(names, b, se)}}


# --- vintage- en publicatiecontrole (§1) --------------------------------------
def alfred_records(path: Path) -> list[dict]:
    obs = json.loads(Path(path).read_text(encoding="utf-8"))["observations"]
    return [o for o in obs if o["value"] not in (".", "")]


def _series_at(recs: list[dict], vintage: str) -> dict[int, float]:
    out = {}
    for o in recs:
        if o["realtime_start"] <= vintage <= o["realtime_end"]:
            y, mo, _ = o["date"].split("-")
            out[M(int(y), int(mo))] = float(o["value"])
    return out


def vintage_check(baa_recs: list[dict], aaa_recs: list[dict], cs_now: dict[int, float]) -> dict:
    vintages = sorted({o["realtime_start"] for o in baa_recs + aaa_recs})
    max_lvl, max_d, worst, over = 0.0, 0.0, None, set()
    for v in vintages:
        cs_v = credit_spread_bp(_series_at(baa_recs, v), _series_at(aaa_recs, v))
        for m in cs_v:
            if m in cs_now:
                max_lvl = max(max_lvl, abs(cs_v[m] - cs_now[m]))
            a, b = dcs(cs_v, m), dcs(cs_now, m)
            if a is not None and b is not None:
                dev = abs(a - b)
                if dev > max_d:
                    max_d, worst = dev, (v, label(m))
                if dev > VINTAGE_MAX_BP:
                    over.add(label(m))
    return {"n_vintages": len(vintages), "first_vintage": vintages[0], "last_vintage": vintages[-1],
            "max_dev_cs_bp": round(max_lvl, 4), "max_dev_dcs_bp": round(max_d, 4), "worst": worst,
            "months_dcs_dev_over_5bp": sorted(over),
            "action": "geen herberekening nodig" if not over else "HERBEREKENEN met vintage einde maand t"}


def publication_check(recs: list[dict]) -> dict:
    first: dict[str, str] = {}
    for o in recs:
        if o["date"] not in first or o["realtime_start"] < first[o["date"]]:
            first[o["date"]] = o["realtime_start"]
    from datetime import date
    lags, late = [], []
    for d, pub in first.items():
        y, mo, _ = (int(x) for x in d.split("-"))
        if y < 1997:
            continue                      # ALFRED begint 1996-12; eerdere publicatiedata onbekend
        m1 = M(y, mo) + 1                 # maand na de observatiemaand
        m2 = m1 + 1
        start_next = date(m1 // 12, m1 % 12 + 1, 1)     # eerste dag na het maandeinde
        deadline = date(m2 // 12, m2 % 12 + 1, 1)       # eerste dag van maand t+2
        p = date(*(int(x) for x in pub.split("-")))
        lags.append((p - start_next).days)
        if p >= deadline:
            late.append({"maand": d[:7], "eerste_publicatie": pub})
    lags.sort()
    return {"months_checked": len(lags), "median_days_after_month_end": lags[len(lags) // 2],
            "published_after_end_of_next_month": late}
