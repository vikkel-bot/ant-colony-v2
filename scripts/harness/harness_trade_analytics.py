"""
scripts/harness/harness_trade_analytics.py

Kleine read-only harness voor gesloten paper trades.

Leest eerst expliciete paper-ledger bestanden als die bestaan. Als die ontbreken,
valt het script terug op de bestaande append-only paper logs:
ANT_LOGS\\paper\\*.jsonl.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOGS_ROOT = Path(os.environ.get("ANT_LOGS", r"C:\Trading\ANT_LOGS"))
MIN_CLOSED_TRADES = 5
MAX_TRADES = 30


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_biome(record: dict[str, Any]) -> str:
    biome = str(_first(record, "biome", "asset_class", "source_field") or "").strip()
    if biome:
        return biome.lower()
    symbol = str(_first(record, "symbol", "asset") or "").upper()
    if symbol.endswith("-EUR") or symbol.endswith("-USD"):
        return "crypto"
    if symbol in {"BRENT", "NATGAS", "COPPER", "SILVER", "GOLD"}:
        return "commodities"
    return "unknown"


def _normalise_strategy(record: dict[str, Any]) -> str:
    return str(
        _first(
            record,
            "strategy_type",
            "strategy",
            "signal_type",
            "source",
            "entry_source",
        )
        or "unknown"
    )


def _normalise_pnl(record: dict[str, Any]) -> float:
    for key in (
        "realized_pnl",
        "realized_pnl_eur",
        "pnl_eur",
        "net_pnl",
        "profit",
        "pnl",
    ):
        value = _float_or_none(record.get(key))
        if value is not None:
            return value
    return 0.0


def _duration_hours(record: dict[str, Any], closed_at: datetime) -> float | None:
    direct = _float_or_none(_first(record, "duration_hours", "holding_hours"))
    if direct is not None:
        return direct
    seconds = _float_or_none(_first(record, "trading_seconds", "duration_seconds"))
    if seconds is not None:
        return seconds / 3600.0
    opened_at = _parse_ts(
        _first(record, "opened_at", "entry_time", "entry_timestamp", "open_time")
    )
    if opened_at is None:
        return None
    return max(0.0, (closed_at - opened_at).total_seconds() / 3600.0)


def _candidate_sources(logs_root: Path, repo_root: Path) -> list[Path]:
    explicit = [
        logs_root / "paper_ledger.json",
        logs_root / "paper_ledger.jsonl",
        logs_root / "paper" / "paper_ledger.json",
        logs_root / "paper" / "paper_ledger.jsonl",
        repo_root / "data" / "paper_ledger.json",
        repo_root / "data" / "paper_ledger.jsonl",
    ]
    existing_explicit = [path for path in explicit if path.exists()]
    if existing_explicit:
        return existing_explicit

    paper_dir = logs_root / "paper"
    if not paper_dir.exists():
        return []
    return sorted(paper_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def _iter_json_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("closed_trades", "trades", "positions", "records"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            return [data]
        return []

    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    records.append(row)
    except OSError:
        return []
    return records


def _flatten_record(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        merged = dict(record)
        merged.update(payload)
        if "event_timestamp" not in merged:
            merged["event_timestamp"] = record.get("timestamp")
        return merged
    return dict(record)


def _closed_trade_from_record(
    record: dict[str, Any],
    opened_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    row = _flatten_record(record)
    action = str(row.get("action") or "").lower()
    position_id = str(row.get("position_id") or row.get("id") or "").strip()

    if action in {"trade_opened", "position_opened"} and position_id:
        row.setdefault("opened_at", row.get("event_timestamp") or row.get("timestamp"))
        opened_by_id[position_id] = row
        return None

    has_close_marker = bool(_first(row, "closed_at", "exit_time"))
    is_close_action = action in {"trade_closed", "position_closed"}
    if not has_close_marker and not is_close_action:
        return None

    base = opened_by_id.get(position_id, {}) if position_id else {}
    combined = dict(base)
    combined.update(row)

    closed_at = _parse_ts(
        _first(combined, "closed_at", "exit_time", "closed_time")
        or (combined.get("event_timestamp") if is_close_action else None)
        or (combined.get("timestamp") if is_close_action else None)
    )
    if closed_at is None:
        return None

    return {
        "position_id": position_id,
        "symbol": str(_first(combined, "symbol", "asset") or "unknown"),
        "strategy_type": _normalise_strategy(combined),
        "biome": _normalise_biome(combined),
        "exit_reason": str(_first(combined, "exit_reason", "exit_type", "status") or "unknown"),
        "pnl": _normalise_pnl(combined),
        "closed_at": closed_at,
        "duration_hours": _duration_hours(combined, closed_at),
    }


def load_closed_trades(
    logs_root: Path = DEFAULT_LOGS_ROOT,
    repo_root: Path = REPO_ROOT,
) -> tuple[list[dict[str, Any]], list[Path]]:
    opened_by_id: dict[str, dict[str, Any]] = {}
    trades_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    sources = _candidate_sources(logs_root, repo_root)

    for path in sources:
        for record in _iter_json_records(path):
            trade = _closed_trade_from_record(record, opened_by_id)
            if trade is None:
                continue
            key = (
                trade.get("position_id") or "",
                trade["symbol"],
                trade["closed_at"].isoformat(),
            )
            trades_by_key[key] = trade

    trades = sorted(trades_by_key.values(), key=lambda t: t["closed_at"])
    return trades[-MAX_TRADES:], sources


def _money(value: float) -> str:
    return f"€{value:.2f}".replace(".", ",")


def build_report(trades: list[dict[str, Any]]) -> str:
    oldest = trades[0]["closed_at"]
    newest = trades[-1]["closed_at"]
    lines: list[str] = [
        "=== TRADE ANALYTICS RAPPORT ===",
        f"Periode: {oldest.strftime('%Y-%m-%d %H:%M')} → {newest.strftime('%Y-%m-%d %H:%M')}",
        f"Totaal gesloten trades geanalyseerd: {len(trades)}",
        "",
        "--- WIN RATE PER STRATEGY_TYPE ---",
    ]

    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_biome: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exit_counts: Counter[str] = Counter()

    for trade in trades:
        by_strategy[str(trade["strategy_type"])].append(trade)
        by_biome[str(trade["biome"])].append(trade)
        exit_counts[str(trade["exit_reason"])] += 1

    for strategy in sorted(by_strategy):
        rows = by_strategy[strategy]
        wins = sum(1 for row in rows if float(row["pnl"]) > 0)
        avg_pnl = sum(float(row["pnl"]) for row in rows) / len(rows)
        win_pct = wins / len(rows) * 100.0
        lines.append(
            f"{strategy}: {wins} wins / {len(rows)} trades = {win_pct:.1f}%  "
            f"(gem PnL: {_money(avg_pnl)})"
        )

    lines.extend(["", "--- GEM. PnL PER BIOME ---"])
    for biome in sorted(by_biome):
        rows = by_biome[biome]
        avg_pnl = sum(float(row["pnl"]) for row in rows) / len(rows)
        lines.append(f"{biome}: {_money(avg_pnl)}  ({len(rows)} trades)")

    lines.extend(["", "--- TOP-3 EXIT-REDENEN ---"])
    for idx, (reason, count) in enumerate(exit_counts.most_common(3), start=1):
        pct = count / len(trades) * 100.0
        lines.append(f"{idx}. {reason}: {count} keer ({pct:.1f}%)")

    lines.extend(["", "--- GEM. TRADE DURATION PER STRATEGY_TYPE ---"])
    for strategy in sorted(by_strategy):
        durations = [
            float(row["duration_hours"])
            for row in by_strategy[strategy]
            if row.get("duration_hours") is not None
        ]
        if durations:
            avg_duration = sum(durations) / len(durations)
            lines.append(f"{strategy}: {avg_duration:.1f} uur gemiddeld")
        else:
            lines.append(f"{strategy}: onbekend")

    return "\n".join(lines)


def write_report(report: str, logs_root: Path = DEFAULT_LOGS_ROOT) -> Path:
    out_dir = logs_root / "analytics"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"trade_report_{stamp}.txt"
    out_path.write_text(report + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    trades, sources = load_closed_trades()
    if len(trades) < MIN_CLOSED_TRADES:
        source_msg = ", ".join(str(path) for path in sources) if sources else "geen ledger/logbron gevonden"
        print(
            "WAARSCHUWING: minder dan 5 gesloten trades gevonden "
            f"({len(trades)}). Geen rapport geschreven. Bronnen: {source_msg}"
        )
        return 0

    report = build_report(trades)
    out_path = write_report(report)
    print(report)
    print("")
    print(f"Rapport opgeslagen: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
