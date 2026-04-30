"""
Zet Colony paper trade logs om naar een Watchtower replay payload.

Gebruik:
  python ant_colony/tools/replay_extractor.py \\
    --source C:\\Trading\\ANT_LOGS\\paper_trades.jsonl \\
    --post http://127.0.0.1:8011/colony/feedback/replay \\
    --dry-run

Leest trade_closed events uit de JSONL log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


EXIT_REASON_MAP = {
    "stop_loss": "SL",
    "take_profit": "TP",
    "trailing_stop": "TRAILING",
    "ttl_expired": "TTL",
    "manual": "MANUAL",
}


def _data_quality(trade: dict) -> str:
    has_prices = trade.get("entry_price") is not None and trade.get("exit_price") is not None
    has_pnl = trade.get("pnl_pct") is not None
    has_regime = trade.get("regime") is not None or trade.get("colony_regime") is not None
    if has_prices and has_pnl and has_regime:
        return "HIGH"
    if has_prices and has_pnl:
        return "MEDIUM"
    return "LOW"


def _exit_reason(raw: str | None) -> str | None:
    if raw is None:
        return None
    return EXIT_REASON_MAP.get(raw.lower(), raw.upper())


def _biome(trade: dict, source_path: str) -> str:
    if "biome" in trade:
        return trade["biome"].upper()
    p = source_path.lower()
    if "equit" in p:
        return "EQUITIES"
    return "CRYPTO"


def _replay_id(source_path: str) -> str:
    path = Path(source_path)
    mtime = str(path.stat().st_mtime) if path.exists() else "0"
    return hashlib.sha256((source_path + mtime).encode()).hexdigest()[:16]


def _load_trade_closed(source_path: str) -> list[dict]:
    trades = []
    with open(source_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = record.get("event_type") or record.get("type") or record.get("event")
            if event_type == "trade_closed":
                trades.append(record)
    return trades


def _build_outcome(trade: dict, source_path: str) -> dict:
    return {
        "feedback_type": "TRADE_OUTCOME",
        "signal_id": trade.get("signal_id"),
        "asset": trade.get("asset") or trade.get("symbol") or "UNKNOWN",
        "biome": _biome(trade, source_path),
        "timestamp": trade.get("timestamp") or trade.get("closed_at") or trade.get("exit_time") or "",
        "direction": (trade.get("direction") or "LONG").upper(),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time") or trade.get("closed_at"),
        "duration_hours": trade.get("duration_hours"),
        "pnl_pct": trade.get("pnl_pct"),
        "pnl_eur": trade.get("pnl_eur"),
        "exit_reason": _exit_reason(trade.get("exit_reason")),
        "peak_pnl_pct": trade.get("peak_pnl_pct"),
        "max_drawdown_pct": trade.get("max_drawdown_pct"),
        "open_positions_count": trade.get("open_positions_count", 0),
        "portfolio_heat": trade.get("portfolio_heat", 0.0),
        "data_quality": _data_quality(trade),
        "is_replay": True,
        "replay_id": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Zet Colony paper trade logs om naar Watchtower replay payload.")
    parser.add_argument("--source", required=True, help="Pad naar JSONL logbestand")
    parser.add_argument("--post", default=None, help="URL om replay payload naar te posten")
    parser.add_argument("--dry-run", action="store_true", help="Toon wat verstuurd zou worden, stuur niets")
    args = parser.parse_args()

    if not Path(args.source).exists():
        print(f"FOUT: bestand niet gevonden: {args.source}", file=sys.stderr)
        sys.exit(1)

    raw_trades = _load_trade_closed(args.source)
    trades = [_build_outcome(t, args.source) for t in raw_trades]
    rid = _replay_id(args.source)

    quality_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for t in trades:
        quality_counts[t["data_quality"]] += 1

    print(
        f"Gevonden: {len(trades)} trades | "
        f"HIGH: {quality_counts['HIGH']} | "
        f"MEDIUM: {quality_counts['MEDIUM']} | "
        f"LOW: {quality_counts['LOW']}"
    )
    print(f"replay_id zou worden: {rid}")

    if args.dry_run:
        print("Geen data verstuurd (dry-run)")
        return

    if not args.post:
        print("Geen --post URL opgegeven, niets verstuurd.", file=sys.stderr)
        sys.exit(1)

    for t in trades:
        t["replay_id"] = rid

    payload = {
        "source": args.source,
        "replay_id": rid,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "trades": trades,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        args.post,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            print(
                f"Geaccepteerd: {result.get('accepted')} | "
                f"Overgeslagen: {result.get('skipped_duplicate', 0)}"
            )
    except Exception as exc:
        print(f"FOUT bij versturen: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
