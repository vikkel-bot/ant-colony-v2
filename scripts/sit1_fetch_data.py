"""SIT-1 stap 2: data ophalen en vintages vastleggen (PREREG_SIT1_20260926.md §1, §2 stap 2).

Haalt de ruwe bronbestanden op, bewaart ze ONGEWIJZIGD in data/sit1/raw/ en schrijft
data/sit1/MANIFEST.json met per bestand: URL, downloadtijd (UTC), SHA256, grootte,
Last-Modified en (voor Ken French) de CRSP-vintage uit de bestandskop.

Leest geen rendementen, berekent niets. Fail-closed: exitcode 1 als iets ontbreekt.

Vintage-controle credit (§1, verplicht) vereist een gratis FRED-API-sleutel:
    $env:FRED_API_KEY = "<sleutel>"
Zonder sleutel wordt ALFRED overgeslagen en is de stap NIET compleet (exitcode 1).

Gebruik:  py -3.14 scripts/sit1_fetch_data.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "sit1" / "raw"
MANIFEST = ROOT / "data" / "sit1" / "MANIFEST.json"
UA = {"User-Agent": "ant-colony-sit1/1.0"}
FRENCH = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"

SOURCES = [
    ("ff_factors_daily.zip", FRENCH + "F-F_Research_Data_Factors_daily_CSV.zip"),
    ("ff_factors_monthly.zip", FRENCH + "F-F_Research_Data_Factors_CSV.zip"),
    ("ff_dev_ex_us_daily.zip", FRENCH + "Developed_ex_US_3_Factors_Daily_CSV.zip"),
    ("fred_BAA.csv", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAA"),
    ("fred_AAA.csv", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=AAA"),
]


def fetch(url: str) -> tuple[bytes, str]:
    """Download; bij een certificaatfout opnieuw via curl.exe (Windows-certificaatopslag)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
            return r.read(), r.headers.get("Last-Modified", "")
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError):
            raise
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise
        out = subprocess.run([curl, "-sSfL", "-A", UA["User-Agent"], url], capture_output=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(f"curl faalde: {out.stderr.decode(errors='replace').strip()}") from e
        return out.stdout, "(via curl, header niet vastgelegd)"


def french_vintage(blob: bytes) -> str:
    """Eerste regels van het CSV in de zip; daarin staat de CRSP-database (de vintage)."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        head = z.read(name).decode("utf-8", errors="replace").splitlines()[:6]
    return " | ".join(line.strip() for line in head if line.strip() and not line.strip()[0].isdigit())


def alfred(series: str, key: str) -> bytes:
    """Alle vintages (realtime-perioden) van een FRED-reeks, lang formaat, gepagineerd."""
    rows, offset = [], 0
    while True:
        url = ("https://api.stlouisfed.org/fred/series/observations?series_id=" + series +
               "&realtime_start=1776-07-04&realtime_end=9999-12-31&file_type=json"
               f"&limit=100000&offset={offset}&api_key={key}")
        data = json.loads(fetch(url)[0])
        obs = data.get("observations", [])
        rows.extend(obs)
        if len(obs) < 100000:
            break
        offset += 100000
    return json.dumps({"series": series, "observations": rows}, indent=0).encode("utf-8")


def record(name: str, url: str, blob: bytes, last_mod: str, extra: dict | None = None) -> dict:
    (RAW / name).write_bytes(blob)
    entry = {
        "file": f"data/sit1/raw/{name}",
        "url": url.split("&api_key=")[0],
        "downloaded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
        "last_modified": last_mod,
    }
    if extra:
        entry.update(extra)
    print(f"ok   {name}: {len(blob)} bytes, sha256 {entry['sha256'][:12]}")
    return entry


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    entries, fouten = [], []

    for name, url in SOURCES:
        try:
            blob, lm = fetch(url)
            extra = {"vintage_in_file": french_vintage(blob)} if name.startswith("ff_") else None
            entries.append(record(name, url, blob, lm, extra))
        except Exception as e:
            fouten.append(f"{name}: {type(e).__name__}: {e}")

    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        for s in ("BAA", "AAA"):
            try:
                entries.append(record(f"alfred_{s}_all_vintages.json",
                                      f"https://api.stlouisfed.org/fred/series/observations?series_id={s}",
                                      alfred(s, key), ""))
            except Exception as e:
                fouten.append(f"alfred_{s}: {type(e).__name__}: {str(e).split('api_key=')[0]}")
    else:
        fouten.append("ALFRED overgeslagen: FRED_API_KEY ontbreekt (vintage-controle §1 is verplicht)")

    try:
        import yfinance as yf
        df = yf.download("SPY", start="1993-01-01", auto_adjust=True, progress=False, actions=False)
        if df is None or df.empty:
            raise RuntimeError("lege SPY-download")
        blob = df.to_csv(lineterminator="\n").encode("utf-8")
        entries.append(record("yahoo_SPY_adjusted.csv", "yfinance download SPY auto_adjust=True",
                              blob, "", {"yfinance_version": getattr(yf, "__version__", "?")}))
    except Exception as e:
        fouten.append(f"SPY: {type(e).__name__}: {e}")

    MANIFEST.write_text(json.dumps({"stap": "SIT-1 stap 2 (data ophalen)",
                                    "prereg": "docs/PREREG_SIT1_20260926.md",
                                    "bestanden": entries, "fouten": fouten}, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
    for f in fouten:
        print(f"FOUT {f}")
    print("STAP 2 COMPLEET" if not fouten else f"STAP 2 NIET COMPLEET ({len(fouten)} fout(en))")
    return 0 if not fouten else 1


if __name__ == "__main__":
    sys.exit(main())
