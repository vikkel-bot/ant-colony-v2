"""SIT-1A runner (docs/PREREG_SIT1_20260926.md). Fail-closed volgorde-sloten:

  --check-data          vintage- en publicatiecontrole (§1) -> results/sit1/data_check.json
  --window development  stap 4: één run -> results/sit1/sit1a_development.json
  --window lhe          stap 5: alleen als sit1a_development.json GECOMMIT is -> sit1a_lhe.json

Weigert: bij afwijkende data-hash t.o.v. data/sit1/MANIFEST.json; bij niet-gecommitte
wijzigingen in code of data; bij een bestaand resultaatbestand (één run per venster).

Gebruik:  py -3.14 scripts/sit1_run.py --window development
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from ant_colony.lab import sit1 as S  # noqa: E402

RAW = ROOT / "data" / "sit1" / "raw"
OUT = ROOT / "results" / "sit1"
GUARDED = ["ant_colony/lab/sit1.py", "scripts/sit1_run.py", "data/sit1"]


def git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, check=True).stdout.strip()


def preflight() -> dict:
    man = json.loads((ROOT / "data/sit1/MANIFEST.json").read_text(encoding="utf-8"))
    for e in man["bestanden"]:
        h = hashlib.sha256((ROOT / e["file"]).read_bytes()).hexdigest()
        if h != e["sha256"]:
            raise SystemExit(f"ABORT: hash wijkt af voor {e['file']}")
    dirty = git("status", "--porcelain", "--", *GUARDED)
    if dirty:
        raise SystemExit(f"ABORT: niet-gecommitte wijzigingen in code/data:\n{dirty}")
    return {"commit": git("rev-parse", "HEAD"),
            "manifest_sha256": hashlib.sha256((ROOT / "data/sit1/MANIFEST.json").read_bytes()).hexdigest(),
            "run_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def write(path: Path, obj: dict) -> None:
    if path.exists():
        raise SystemExit(f"ABORT: {path.relative_to(ROOT)} bestaat al (één run per venster)")
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"geschreven: {path.relative_to(ROOT)}")


def load():
    daily = S.read_ff_daily(RAW / "ff_factors_daily.zip")
    rv = S.monthly_rv(daily)
    cs = S.credit_spread_bp(S.read_fred(RAW / "fred_BAA.csv"), S.read_fred(RAW / "fred_AAA.csv"))
    return daily, rv, cs


def check_data(meta: dict) -> None:
    _, _, cs = load()
    b = S.alfred_records(RAW / "alfred_BAA_all_vintages.json")
    a = S.alfred_records(RAW / "alfred_AAA_all_vintages.json")
    res = {"stap": "SIT-1 §1 vintage- en publicatiecontrole", **meta,
           "vintage": S.vintage_check(b, a, cs),
           "publicatie_BAA": S.publication_check(b), "publicatie_AAA": S.publication_check(a)}
    write(OUT / "data_check.json", res)
    print(json.dumps(res["vintage"], ensure_ascii=False))


def run_window(window: str, meta: dict) -> None:
    if window == "lhe":
        dev = "results/sit1/sit1a_development.json"
        if not git("ls-files", "--", dev):
            raise SystemExit("ABORT: LHE pas na een GECOMMIT development-resultaat (§2 stap 5)")
    daily, rv, cs = load()
    start, end = S.WINDOWS[window]
    rows = S.build_rows(rv, cs)
    fc = S.recursive_forecasts(rows, start, end)
    expected = end - start + 1
    if len(fc.target) != expected:
        raise SystemExit(f"ABORT: {len(fc.target)} forecasts, verwacht {expected}")
    if window == "development" and fc.n_est[0] != S.INLOOP_N:
        raise SystemExit(f"ABORT: inloop {fc.n_est[0]} observaties, verwacht {S.INLOOP_N}")

    cw = S.clark_west(fc)
    delta, q0 = S.qlike_series(fc)
    gain = S.qlike_gain(delta, q0)
    dh = S.d_hat(rows, end)
    thr = S.THR[window]
    verdict = S.decide(cw["p"], thr, dh["d"], gain["delta"], gain["ci_upper"])

    pert = {}
    for h in S.PERTURB_H:
        fh = S.recursive_forecasts(S.build_rows(rv, cs, h), start, end)
        dl, qq = S.qlike_series(fh)
        cwh = S.clark_west(fh)
        pert[f"dcs_h{h}"] = {"cw": cwh["cw"], "p": cwh["p"], "delta": float(dl.mean() / qq.mean())}

    res = {
        "stap": f"SIT-1A {window}", "prereg": "docs/PREREG_SIT1_20260926.md", **meta,
        "venster": {"eerste_target": S.label(start), "laatste_target": S.label(end), "n": expected,
                    "inloop_n": fc.n_est[0], "p_drempel": thr},
        "uitslag": verdict,
        "clark_west": cw,
        "qlike": gain,
        "d_hat": dh,
        "power": {"mde_cw": cw["mde_cw"], "mde_delta": gain["mde_delta"]},
        "beschrijvend": {
            "episodes": S.episode_attribution(fc, S.episodes(S.monthly_growth(daily))),
            "reverse_causality": S.reverse_causality(rows, cs, end),
            "perturbaties_alleen_rapportage": pert,
        },
        "forecasts_sha256": hashlib.sha256(np.array([fc.yhat0, fc.yhat1, fc.s2_0, fc.s2_1]).tobytes()).hexdigest(),
    }
    write(OUT / f"sit1a_{window}.json", res)
    print(f"UITSLAG SIT-1A {window}: {verdict}  (CW p={cw['p']:.5f}, drempel {thr:.4f}; "
          f"d={dh['d']:+.5f}; Δ={gain['delta']:+.4f} [{gain['ci_lower']:+.4f}, {gain['ci_upper']:+.4f}])")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check-data", action="store_true")
    g.add_argument("--window", choices=sorted(S.WINDOWS))
    args = ap.parse_args()
    meta = preflight()
    if args.check_data:
        check_data(meta)
    else:
        run_window(args.window, meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
