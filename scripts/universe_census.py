"""Universe census — Bitvavo EUR-markten, point-in-time.

Meet (stap UNIVERSE DEFINITION, nog geen drempelkeuze):
  1. aantal EUR-markten nu beschikbaar, per status
  2. welke vooraf uitgesloten zijn (stable / wrapped / gepegd) + data-verdachten
  3. per maandmoment t: hoeveel assets >= MIN_HISTORY_DAYS historie hebben
  4. verdeling van de 30-daagse mediane dagomzet in EUR (volume x close)
  5. universumgrootte bij een vaste reeks kandidaat-drempels
  6. stabiliteit van die grootte door de tijd
  7. signalen van ontbrekende / gestopte markten

Point-in-time: op moment t tellen alleen candles met timestamp < t.
Geen now() in rekenlogica; de referentietijd is het laatste volledige candle.
Kiest GEEN drempel en gebruikt GEEN toekomstige rendementen.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.bitvavo.com/v2"
DAY_MS = 86_400_000
MIN_HISTORY_DAYS = 180
LIQ_WINDOW_DAYS = 30
THRESHOLDS_EUR = (0, 10_000, 50_000, 100_000, 250_000, 1_000_000)

# Vooraf bevroren uitsluitlijst (basismunt). Aanvullingen alleen via commit.
EXCLUDED_BASES = {
    # stablecoins
    "USDT", "USDC", "EURC", "EURT", "DAI", "TUSD", "BUSD", "PYUSD", "FDUSD",
    "USDE", "USDS", "EURS", "EURR", "USDP", "GUSD", "LUSD", "FRAX", "USDD", "EURCV", "EUROP", "USDCV",
    # wrapped / liquid staking / gepegd aan iets buiten crypto
    "WBTC", "WETH", "STETH", "WSTETH", "CBETH", "RETH", "BETH", "PAXG", "XAUT",
}
PEG_SUSPECT_CV = 0.01  # variatiecoefficient slotkoers over 30 dagen < 1% -> verdacht gepegd


def _get(path: str):
    req = urllib.request.Request(f"{API}{path}", headers={"User-Agent": "ant-colony-census/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_markets() -> list[dict]:
    return _get("/markets")


def fetch_daily_candles(market: str, pause: float = 0.15) -> list[list]:
    """Volledige dag-historie, oud -> nieuw. Pagineert terug in stappen van 1440."""
    rows: list[list] = []
    end = None
    while True:
        q = f"/{market}/candles?interval=1d&limit=1440" + (f"&end={end}" if end else "")
        batch = _get(q)
        time.sleep(pause)
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(r[0]) for r in batch)
        if len(batch) < 1440:
            break
        end = oldest - 1
    uniq = {int(r[0]): r for r in rows}
    return [uniq[k] for k in sorted(uniq)]


def load_or_fetch(market: str, cache: Path) -> list[list]:
    f = cache / f"{market}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    rows = fetch_daily_candles(market)
    f.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def eur_turnover(row: list) -> float:
    return float(row[5]) * float(row[4])  # volume (basismunt) x close (EUR)


def admitted(candles: list[list], t_ms: int) -> tuple[bool, float | None]:
    """(historie_ok, mediane EUR-omzet over [t-30d, t)) — alleen data vóór t."""
    past = [r for r in candles if int(r[0]) < t_ms]
    if not past:
        return False, None
    history_ok = int(past[0][0]) <= t_ms - MIN_HISTORY_DAYS * DAY_MS
    window = [eur_turnover(r) for r in past if int(r[0]) >= t_ms - LIQ_WINDOW_DAYS * DAY_MS]
    med = statistics.median(window) if window else None
    return history_ok, med


def peg_suspect(candles: list[list]) -> bool:
    closes = [float(r[4]) for r in candles[-LIQ_WINDOW_DAYS:]]
    if len(closes) < 10 or statistics.mean(closes) == 0:
        return False
    return statistics.pstdev(closes) / statistics.mean(closes) < PEG_SUSPECT_CV


def month_starts(first_ms: int, last_ms: int) -> list[int]:
    d = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    while True:
        d = (d + timedelta(days=32)).replace(day=1)
        ms = int(d.timestamp() * 1000)
        if ms > last_ms:
            return out
        out.append(ms)


def census(markets: list[dict], data: dict[str, list[list]]) -> dict:
    eur = [m for m in markets if m.get("quote") == "EUR"]
    status_count: dict[str, int] = {}
    for m in eur:
        status_count[m.get("status", "?")] = status_count.get(m.get("status", "?"), 0) + 1

    excluded = sorted(m["market"] for m in eur if m.get("base") in EXCLUDED_BASES)
    usable = {k: v for k, v in data.items() if v and k not in excluded}

    # referentie = laatste volledige candle over alle markten (onvolledige dag is al weggelaten)
    ref_ms = max(int(v[-1][0]) for v in usable.values()) + DAY_MS
    stale = sorted(k for k, v in usable.items() if int(v[-1][0]) < ref_ms - 8 * DAY_MS)
    suspects = sorted(k for k, v in usable.items() if peg_suspect(v))

    first_ms = min(int(v[0][0]) for v in usable.values())
    snapshots = []
    for t in month_starts(first_ms, ref_ms):
        row = {"t": datetime.fromtimestamp(t / 1000, tz=timezone.utc).date().isoformat(), "with_data": 0, "history_ok": 0}
        for thr in THRESHOLDS_EUR:
            row[f">={thr}"] = 0
        for cand in usable.values():
            if int(cand[0][0]) >= t:
                continue
            row["with_data"] += 1
            ok, med = admitted(cand, t)
            if not ok or med is None:
                continue
            row["history_ok"] += 1
            for thr in THRESHOLDS_EUR:
                if med >= thr:
                    row[f">={thr}"] += 1
        snapshots.append(row)

    latest = [admitted(v, ref_ms) for v in usable.values()]
    meds = sorted(m for ok, m in latest if ok and m is not None)

    def pct(p):
        return round(meds[min(len(meds) - 1, int(p / 100 * len(meds)))]) if meds else None

    stability = {}
    for thr in THRESHOLDS_EUR:
        sizes = [s[f">={thr}"] for s in snapshots]
        if sizes:
            stability[f">={thr}"] = {"min": min(sizes), "median": statistics.median(sizes), "max": max(sizes)}

    return {
        "reference_date": datetime.fromtimestamp(ref_ms / 1000, tz=timezone.utc).date().isoformat(),
        "eur_markets": len(eur),
        "status_count": status_count,
        "excluded_by_list": excluded,
        "peg_suspects_not_excluded": [s for s in suspects if s not in excluded],
        "stale_last_candle_gt_8d": stale,
        "latest_turnover_eur_percentiles": {p: pct(p) for p in (10, 25, 50, 75, 90)},
        "universe_size_stability": stability,
        "snapshots": snapshots,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=".cache/bitvavo_1d")
    ap.add_argument("--out", default="universe_census.json")
    args = ap.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)

    markets = fetch_markets()
    (cache / "_markets.json").write_text(json.dumps(markets), encoding="utf-8")
    eur = [m["market"] for m in markets if m.get("quote") == "EUR"]
    print(f"{len(eur)} EUR-markten; candles ophalen (gecachet in {cache})")

    data: dict[str, list[list]] = {}
    for i, mkt in enumerate(eur, 1):
        try:
            rows = load_or_fetch(mkt, cache)
            data[mkt] = rows[:-1]  # laatste candle is (mogelijk) onvolledig
        except Exception as exc:  # noqa: BLE001
            print(f"  {mkt}: FOUT {exc}")
        if i % 25 == 0:
            print(f"  {i}/{len(eur)}")

    btc = data.get("BTC-EUR") or []
    if btc:
        r = btc[-1]
        ts = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc)
        print(f"controle BTC-EUR laatste volledige candle: ts={ts.isoformat()} volume={r[5]} close={r[4]} "
              f"-> omzet EUR ~{eur_turnover(r):,.0f}")

    result = census(markets, data)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    summary = {k: v for k, v in result.items() if k != "snapshots"}
    print(json.dumps(summary, indent=2))
    print(f"\nlaatste 6 maandmomenten:")
    for s in result["snapshots"][-6:]:
        print("  " + json.dumps(s))
    print(f"\nvolledig: {args.out}")


if __name__ == "__main__":
    main()
