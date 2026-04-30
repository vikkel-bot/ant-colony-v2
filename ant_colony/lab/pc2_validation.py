"""
Read-only PC2 validation tooling for equity canary deployment.

The functions in this module inspect broker candles, yfinance candles, fee
assumptions, paper logs and signal-flow logs. They deliberately do not change
Colony state, thresholds, routing, positions or configuration.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


AUDIT_FEE_PER_SIDE = 0.0010
AUDIT_SLIPPAGE_PER_SIDE = 0.0010
DEFAULT_RISK_PER_TRADE = 0.01
BENCHMARK_CAGR = 0.106

POSITIVE_EDGE_ASSETS = ("JNJ", "GLD", "XLK", "AAPL", "XLY", "QQQ", "XLF", "XLI")
VALIDATION_ASSETS = (
    "JNJ",
    "GLD",
    "XLK",
    "AAPL",
    "XLY",
    "QQQ",
    "XLF",
    "XLI",
    "XLE",
    "MSFT",
    "XLB",
    "XLP",
    "XLRE",
    "XLU",
)

BACKTEST_EXPECTANCY_R = {
    "JNJ:momentum long": 0.2738,
    "GLD:momentum long": 0.2575,
    "XLK:momentum long": 0.2476,
    "AAPL:momentum long": 0.2346,
    "XLY:momentum long": 0.2069,
    "QQQ:momentum long": 0.2003,
    "XLF:bb_lower_approach": 0.1708,
    "XLI:momentum long": 0.1176,
}

POSITIVE_EDGE_DETAILS = {
    "JNJ": {"combo_id": "JNJ:momentum long", "expectancy_r": 0.2738, "trades_per_year": 11, "audit_cagr": 0.030},
    "GLD": {"combo_id": "GLD:momentum long", "expectancy_r": 0.2575, "trades_per_year": 12, "audit_cagr": 0.031},
    "XLK": {"combo_id": "XLK:momentum long", "expectancy_r": 0.2476, "trades_per_year": 15, "audit_cagr": 0.037},
    "AAPL": {"combo_id": "AAPL:momentum long", "expectancy_r": 0.2346, "trades_per_year": 14, "audit_cagr": 0.033},
    "XLY": {"combo_id": "XLY:momentum long", "expectancy_r": 0.2069, "trades_per_year": 12, "audit_cagr": 0.025},
    "QQQ": {"combo_id": "QQQ:momentum long", "expectancy_r": 0.2003, "trades_per_year": 13, "audit_cagr": 0.026},
    "XLF": {"combo_id": "XLF:bb_lower_approach", "expectancy_r": 0.1708, "trades_per_year": 11, "audit_cagr": 0.018},
    "XLI": {"combo_id": "XLI:momentum long", "expectancy_r": 0.1176, "trades_per_year": 11, "audit_cagr": 0.013},
}

POSITIVE_EDGE_STRATEGY_BY_ASSET = {
    "JNJ": "momentum long",
    "GLD": "momentum long",
    "XLK": "momentum long",
    "AAPL": "momentum long",
    "XLY": "momentum long",
    "QQQ": "momentum long",
    "XLF": "bb_lower_approach",
    "XLI": "momentum long",
}


@dataclass(frozen=True)
class Candle:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    source: str = ""


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        for suffix in ("Z", "z"):
            if text.endswith(suffix):
                text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d %H:%M:%S", "%Y%m%d"):
                try:
                    dt = datetime.strptime(text.split(".")[0], fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _date_key(dt: datetime) -> str:
    return parse_dt(dt).date().isoformat()  # type: ignore[union-attr]


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.upper()).strip("_")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    except OSError:
        return []
    return records


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        merged = dict(payload)
        for key in ("timestamp", "event_type", "source", "mission_id", "node_id"):
            if key in record and key not in merged:
                merged[key] = record[key]
        return merged
    return record


def _in_range(dt: datetime, from_dt: datetime, to_dt: datetime) -> bool:
    utc = parse_dt(dt)
    return bool(utc and from_dt <= utc <= to_dt)


def default_logs_root() -> Path:
    env = os.getenv("ANT_LOGS")
    if env:
        return Path(env)
    pc2_default = Path(r"C:\Trading\ANT_LOGS")
    if pc2_default.exists():
        return pc2_default
    return Path("logs")


def default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("logs") / "pc2_validation" / stamp


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()}) or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Price data validation
# ---------------------------------------------------------------------------


def load_broker_candles(
    asset: str,
    from_dt: datetime,
    to_dt: datetime,
    *,
    logs_root: Path,
    broker_cache_dir: Path | None = None,
    use_broker_api: bool = True,
) -> list[Candle]:
    """Load broker candles from cache first, then IBKR historical API if allowed."""

    cache_roots = _broker_cache_roots(logs_root, broker_cache_dir)
    for root in cache_roots:
        candles = _read_cached_candles(asset, root)
        filtered = [c for c in candles if _in_range(c.timestamp, from_dt, to_dt)]
        if filtered:
            return filtered

    if not use_broker_api:
        return []

    try:
        from ant_colony.biome.adapters.ibkr_adapter import IBKRAdapter

        adapter = IBKRAdapter()
        raw = adapter.get_candles(asset, period="5y", interval="1d")
    except Exception:
        raw = []

    candles: list[Candle] = []
    for item in raw or []:
        dt = parse_dt(getattr(item, "timestamp", None))
        if dt is None or not (from_dt <= dt <= to_dt):
            continue
        close = _float(getattr(item, "close", None))
        if close <= 0:
            continue
        candles.append(
            Candle(
                symbol=asset,
                timestamp=dt,
                open=_float(getattr(item, "open", None)),
                high=_float(getattr(item, "high", None)),
                low=_float(getattr(item, "low", None)),
                close=close,
                volume=_float(getattr(item, "volume", None)),
                source="broker_api",
            )
        )
    return sorted(candles, key=lambda c: c.timestamp)


def load_yfinance_candles(asset: str, from_dt: datetime, to_dt: datetime) -> list[Candle]:
    try:
        _configure_yfinance_cache()
        from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter

        years = (to_dt - from_dt).total_seconds() / (365.25 * 86400)
        period = "1y" if years <= 1 else "2y" if years <= 2 else "5y"
        raw = YahooFinanceAdapter().get_candles(asset, period=period, interval="1d")
    except Exception:
        raw = []

    candles: list[Candle] = []
    for item in raw or []:
        dt = parse_dt(getattr(item, "timestamp", None))
        if dt is None or not (from_dt <= dt <= to_dt):
            continue
        close = _float(getattr(item, "close", None))
        if close <= 0:
            continue
        candles.append(
            Candle(
                symbol=asset,
                timestamp=dt,
                open=_float(getattr(item, "open", None)),
                high=_float(getattr(item, "high", None)),
                low=_float(getattr(item, "low", None)),
                close=close,
                volume=_float(getattr(item, "volume", None)),
                source="yfinance",
            )
        )
    return sorted(candles, key=lambda c: c.timestamp)


def _configure_yfinance_cache() -> None:
    try:
        import yfinance as yf

        location = Path(os.getenv("YFINANCE_CACHE_DIR", ".yfinance_cache"))
        location.mkdir(parents=True, exist_ok=True)
        if hasattr(yf, "set_tz_cache_location"):
            yf.set_tz_cache_location(str(location.resolve()))
    except Exception:
        return


def _broker_cache_roots(logs_root: Path, explicit: Path | None) -> list[Path]:
    roots = []
    if explicit is not None:
        roots.append(explicit)
    roots.extend(
        [
            Path("data") / "candles",
            Path("logs") / "candles",
            Path("logs") / "market_data",
            logs_root / "candles",
            logs_root / "market_data",
            logs_root / "broker",
        ]
    )
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        resolved = root.resolve() if root.exists() else root
        if resolved not in seen:
            seen.add(resolved)
            result.append(root)
    return result


def _read_cached_candles(asset: str, root: Path) -> list[Candle]:
    if not root.exists():
        return []
    safe = _safe_name(asset)
    candidates = [
        root / f"{safe}_1D.csv",
        root / f"{safe}_1d.csv",
        root / f"{asset}_1d.csv",
        root / f"{safe}.csv",
        root / f"{asset}.csv",
        root / f"{safe}_1D.jsonl",
        root / f"{safe}_1d.jsonl",
        root / f"{asset}_1d.jsonl",
        root / f"{safe}.jsonl",
        root / f"{asset}.jsonl",
    ]
    matches = [p for p in candidates if p.exists()]
    if not matches:
        try:
            matches = [
                p
                for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in {".csv", ".jsonl"} and safe in _safe_name(p.stem)
            ][:20]
        except OSError:
            matches = []

    candles: list[Candle] = []
    for path in matches:
        if path.suffix.lower() == ".csv":
            candles.extend(_read_candle_csv(asset, path))
        elif path.suffix.lower() == ".jsonl":
            candles.extend(_read_candle_jsonl(asset, path))
    dedup = {_date_key(c.timestamp): c for c in candles if c.close > 0}
    return sorted(dedup.values(), key=lambda c: c.timestamp)


def _read_candle_csv(asset: str, path: Path) -> list[Candle]:
    rows: list[Candle] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                candle = _candle_from_mapping(asset, row, source=str(path))
                if candle:
                    rows.append(candle)
    except OSError:
        return []
    return rows


def _read_candle_jsonl(asset: str, path: Path) -> list[Candle]:
    rows = []
    for rec in _read_jsonl(path):
        candle = _candle_from_mapping(asset, _record_payload(rec), source=str(path))
        if candle:
            rows.append(candle)
    return rows


def _candle_from_mapping(asset: str, row: dict[str, Any], *, source: str) -> Candle | None:
    symbol = str(row.get("symbol") or row.get("asset") or asset)
    if symbol.upper() != asset.upper():
        return None
    dt = parse_dt(row.get("timestamp") or row.get("date") or row.get("datetime") or row.get("time"))
    close = _float(row.get("close") or row.get("Close"))
    if dt is None or close <= 0:
        return None
    return Candle(
        symbol=asset,
        timestamp=dt,
        open=_float(row.get("open") or row.get("Open") or close),
        high=_float(row.get("high") or row.get("High") or close),
        low=_float(row.get("low") or row.get("Low") or close),
        close=close,
        volume=_float(row.get("volume") or row.get("Volume")),
        source=source,
    )


def compare_price_series(asset: str, broker: list[Candle], yfinance: list[Candle]) -> dict[str, Any]:
    broker_by_day = {_date_key(c.timestamp): c for c in broker}
    yf_by_day = {_date_key(c.timestamp): c for c in yfinance}
    broker_days = set(broker_by_day)
    yf_days = set(yf_by_day)
    common = sorted(broker_days & yf_days)

    if not broker_by_day:
        verdict = "NO_BROKER_DATA"
        canary_blocked = asset in POSITIVE_EDGE_ASSETS
        return {
            "asset": asset,
            "broker_days": 0,
            "yfinance_days": len(yf_by_day),
            "common_days": 0,
            "mape_close_pct": None,
            "max_close_discrepancy_pct": None,
            "missing_in_yfinance": 0,
            "missing_in_broker": len(yf_by_day),
            "return_correlation": None,
            "verdict": verdict,
            "canary_blocked": canary_blocked,
        }

    errors: list[float] = []
    for day in common:
        b = broker_by_day[day].close
        y = yf_by_day[day].close
        if b > 0:
            errors.append(abs(y - b) / b)
    mape = statistics.fmean(errors) * 100 if errors else None
    max_disc = max(errors) * 100 if errors else None
    corr = _return_correlation([broker_by_day[d] for d in common], [yf_by_day[d] for d in common])
    verdict = price_verdict(mape, corr)
    canary_blocked = asset in POSITIVE_EDGE_ASSETS and verdict in {"NO_BROKER_DATA", "DATA_SIGNIFICANT_DRIFT"}
    return {
        "asset": asset,
        "broker_days": len(broker_by_day),
        "yfinance_days": len(yf_by_day),
        "common_days": len(common),
        "mape_close_pct": round(mape, 6) if mape is not None else None,
        "max_close_discrepancy_pct": round(max_disc, 6) if max_disc is not None else None,
        "missing_in_yfinance": len(broker_days - yf_days),
        "missing_in_broker": len(yf_days - broker_days),
        "return_correlation": round(corr, 8) if corr is not None else None,
        "verdict": verdict,
        "canary_blocked": canary_blocked,
    }


def _return_correlation(broker: list[Candle], yfinance: list[Candle]) -> float | None:
    if len(broker) < 3 or len(yfinance) < 3:
        return None
    b_rets: list[float] = []
    y_rets: list[float] = []
    for idx in range(1, min(len(broker), len(yfinance))):
        b_prev = broker[idx - 1].close
        y_prev = yfinance[idx - 1].close
        if b_prev <= 0 or y_prev <= 0:
            continue
        b_rets.append((broker[idx].close - b_prev) / b_prev)
        y_rets.append((yfinance[idx].close - y_prev) / y_prev)
    if len(b_rets) < 2:
        return None
    mean_b = statistics.fmean(b_rets)
    mean_y = statistics.fmean(y_rets)
    cov = sum((b - mean_b) * (y - mean_y) for b, y in zip(b_rets, y_rets))
    var_b = sum((b - mean_b) ** 2 for b in b_rets)
    var_y = sum((y - mean_y) ** 2 for y in y_rets)
    denom = math.sqrt(var_b * var_y)
    return cov / denom if denom > 0 else None


def price_verdict(mape_pct: float | None, corr: float | None) -> str:
    if mape_pct is None or corr is None:
        return "DATA_SIGNIFICANT_DRIFT"
    if mape_pct < 0.5 and corr > 0.995:
        return "DATA_MATCH"
    if mape_pct > 2.0 or corr < 0.98:
        return "DATA_SIGNIFICANT_DRIFT"
    return "DATA_MINOR_DRIFT"


def run_price_validation(
    *,
    output_dir: Path,
    logs_root: Path,
    from_dt: datetime,
    to_dt: datetime,
    assets: Iterable[str] = VALIDATION_ASSETS,
    broker_cache_dir: Path | None = None,
    use_broker_api: bool = True,
    yfinance_loader: Any | None = None,
    broker_loader: Any | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    yf_loader = yfinance_loader or load_yfinance_candles
    br_loader = broker_loader or load_broker_candles
    for asset in assets:
        broker = br_loader(
            asset,
            from_dt,
            to_dt,
            logs_root=logs_root,
            broker_cache_dir=broker_cache_dir,
            use_broker_api=use_broker_api,
        )
        yf = yf_loader(asset, from_dt, to_dt)
        rows.append(compare_price_series(asset, broker, yf))

    _write_csv(output_dir / "price_validation.csv", rows)
    summary = build_price_summary(rows)
    _write_text(output_dir / "price_validation_summary.md", summary)
    return {"rows": rows, "summary": summary}


def build_price_summary(rows: list[dict[str, Any]]) -> str:
    blocked = [r for r in rows if r.get("canary_blocked")]
    matches = sum(1 for r in rows if r.get("verdict") == "DATA_MATCH")
    minor = sum(1 for r in rows if r.get("verdict") == "DATA_MINOR_DRIFT")
    significant = sum(1 for r in rows if r.get("verdict") == "DATA_SIGNIFICANT_DRIFT")
    no_broker = sum(1 for r in rows if r.get("verdict") == "NO_BROKER_DATA")
    lines = [
        "# Price Validation Summary",
        "",
        f"- DATA_MATCH: `{matches}`",
        f"- DATA_MINOR_DRIFT: `{minor}`",
        f"- DATA_SIGNIFICANT_DRIFT: `{significant}`",
        f"- NO_BROKER_DATA: `{no_broker}`",
        f"- CANARY_BLOCKED assets: `{len(blocked)}`",
    ]
    if blocked:
        lines.append("")
        lines.append("## Blocking Assets")
        for row in blocked:
            lines.append(f"- {row['asset']}: {row['verdict']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Fee and slippage validation
# ---------------------------------------------------------------------------


def read_config_fee_schedule(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    found: dict[str, Any] = {
        "source": "ant_colony.paper.paper_ledger",
        "maker_fee": None,
        "taker_fee": None,
        "fee_per_side": None,
        "crypto_fee_per_side": None,
        "equity_fee_per_side": None,
        "minimum_fee": None,
        "notes": [],
    }
    try:
        from ant_colony.paper.paper_ledger import BROKER_FEE_PCT, EQUITY_FEE_PCT

        found["crypto_fee_per_side"] = float(BROKER_FEE_PCT)
        found["equity_fee_per_side"] = float(EQUITY_FEE_PCT)
        found["fee_per_side"] = float(EQUITY_FEE_PCT)
        found["maker_fee"] = float(EQUITY_FEE_PCT)
        found["taker_fee"] = float(EQUITY_FEE_PCT)
    except Exception:
        found["notes"].append("BROKER_FEE_PCT/EQUITY_FEE_PCT not importable")

    for name in ("broker_config.json", "colony_config.json"):
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("maker_fee", "taker_fee", "fee_per_side", "equity_fee_per_side", "crypto_fee_per_side", "minimum_fee"):
            if key in data:
                found[key] = _float(data[key], found.get(key) or 0.0)
        found["source"] = str(path)
        found["notes"].append(f"loaded {path.name}")
    return found


def load_equity_trades(logs_root: Path) -> list[dict[str, Any]]:
    if not logs_root.exists():
        return []
    paper_dir = logs_root / "paper"
    if not paper_dir.exists():
        return []

    opened: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    direct_closed: list[dict[str, Any]] = []

    for path in sorted(paper_dir.rglob("*.jsonl")):
        for rec in _read_jsonl(path):
            payload = _record_payload(rec)
            action = str(payload.get("action") or "").lower()
            symbol = str(payload.get("symbol") or "")
            if symbol and symbol not in VALIDATION_ASSETS:
                continue
            pos_id = str(payload.get("position_id") or payload.get("id") or "")

            if action == "trade_opened" and str(payload.get("biome") or "equities") == "equities":
                if pos_id:
                    opened[pos_id] = payload
                continue

            if action == "trade_closed":
                merged = dict(opened.get(pos_id, {}))
                merged.update(payload)
                if "opened_at" not in merged:
                    merged["opened_at"] = opened.get(pos_id, {}).get("timestamp")
                if "closed_at" not in merged:
                    merged["closed_at"] = payload.get("timestamp")
                closed.append(normalize_trade_record(merged))
                continue

            if payload.get("closed_at") or path.name.endswith("_trades.jsonl"):
                direct_closed.append(normalize_trade_record(payload))

    # Prefer event-paired records when available because they include source.
    rows = closed or direct_closed
    return [r for r in rows if r.get("asset") in VALIDATION_ASSETS and r.get("entry_price")]


def normalize_trade_record(payload: dict[str, Any]) -> dict[str, Any]:
    asset = str(payload.get("symbol") or payload.get("asset") or "")
    entry = _float(payload.get("entry_price") or payload.get("fill_price") or payload.get("actual_entry_price"))
    exit_price = _float(payload.get("exit_price"))
    qty = _float(payload.get("quantity"), 1.0)
    fee = _float(payload.get("broker_fee_cost") or payload.get("fee_charged") or payload.get("fees"))
    pnl_eur = _float(payload.get("realized_pnl") or payload.get("pnl_eur"))
    pnl_pct = _float(payload.get("pnl_pct"))
    if pnl_pct == 0.0 and entry > 0 and exit_price > 0:
        pnl_pct = ((exit_price - entry) / entry) * 100
    stop_loss = _float(payload.get("stop_loss") or payload.get("stop_loss_price"))
    risk_per_unit = max(entry - stop_loss, 0.0)
    pnl_r = _float(payload.get("pnl_r"), math.nan)
    if math.isnan(pnl_r):
        pnl_r = ((exit_price - entry) / risk_per_unit) if risk_per_unit > 0 and exit_price > 0 else 0.0
    notional = entry * qty
    fee_rate_per_side = fee / ((entry + exit_price) * qty) if fee > 0 and entry > 0 and exit_price > 0 and qty > 0 else None
    intended = _float(payload.get("intended_entry_price") or payload.get("signal_entry_price"), entry)
    slippage = (entry - intended) / intended if intended > 0 and entry > 0 else None
    strategy = infer_strategy(payload)
    return {
        "asset": asset,
        "strategy": strategy,
        "combo_id": f"{asset}:{strategy}" if asset and strategy else asset,
        "direction": str(payload.get("side") or payload.get("direction") or "long").lower(),
        "entry_price": entry,
        "entry_time": payload.get("opened_at") or payload.get("entry_time") or payload.get("timestamp"),
        "exit_price": exit_price,
        "exit_time": payload.get("closed_at") or payload.get("exit_time") or payload.get("timestamp"),
        "exit_reason": payload.get("exit_reason") or payload.get("exit_type") or payload.get("status"),
        "quantity": qty,
        "notional": notional,
        "fee_charged": fee,
        "fee_rate_per_side": fee_rate_per_side,
        "slippage_per_side": slippage,
        "pnl_r": pnl_r,
        "pnl_eur": pnl_eur,
        "pnl_pct": pnl_pct,
        "stop_loss": stop_loss,
        "take_profit": _float(payload.get("take_profit") or payload.get("take_profit_price")),
    }


def infer_strategy(payload: dict[str, Any]) -> str:
    explicit = payload.get("strategy") or payload.get("strategy_type")
    if explicit:
        return str(explicit)
    source = str(payload.get("source") or "").lower()
    if "momentum" in source or "sector" in source or "rotation" in source:
        return "momentum long"
    if "breakout" in source:
        return "momentum long"
    if "bb_lower" in source or "bollinger" in source:
        return "bb_lower_approach"
    asset = str(payload.get("symbol") or payload.get("asset") or "")
    return POSITIVE_EDGE_STRATEGY_BY_ASSET.get(asset, "unknown")


def run_fee_validation(
    *,
    output_dir: Path,
    logs_root: Path,
    repo_root: Path | None = None,
    equity_fee_per_side: float | None = None,
    slippage_per_side: float = AUDIT_SLIPPAGE_PER_SIDE,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    capital: float = 150_000.0,
) -> dict[str, Any]:
    schedule = read_config_fee_schedule(repo_root)
    trades = load_equity_trades(logs_root)
    fee_rates = [t["fee_rate_per_side"] for t in trades if t.get("fee_rate_per_side") is not None]
    slippages = [abs(t["slippage_per_side"]) for t in trades if t.get("slippage_per_side") is not None]
    configured_equity_fee = (
        equity_fee_per_side
        if equity_fee_per_side is not None
        else schedule.get("equity_fee_per_side")
        if schedule.get("equity_fee_per_side") is not None
        else schedule.get("fee_per_side")
    )
    actual_fee = statistics.fmean(fee_rates) if fee_rates else configured_equity_fee
    actual_slippage = statistics.fmean(slippages) if slippages else slippage_per_side
    fee_invalid = actual_fee is not None and abs(float(actual_fee) - AUDIT_FEE_PER_SIDE) > 0.0005
    slippage_invalid = actual_slippage is not None and abs(float(actual_slippage) - AUDIT_SLIPPAGE_PER_SIDE) > 0.0005

    adjusted_rows = adjusted_expectancy_rows(
        actual_fee_per_side=float(actual_fee if actual_fee is not None else AUDIT_FEE_PER_SIDE),
        actual_slippage_per_side=float(actual_slippage if actual_slippage is not None else AUDIT_SLIPPAGE_PER_SIDE),
        risk_per_trade=risk_per_trade,
    )
    blocked = [r for r in adjusted_rows if r["canary_blocked"]]
    summary = build_fee_summary(
        schedule,
        trades,
        actual_fee,
        actual_slippage,
        fee_invalid,
        slippage_invalid,
        adjusted_rows,
        configured_equity_fee=configured_equity_fee,
        risk_per_trade=risk_per_trade,
        capital=capital,
    )
    _write_text(output_dir / "fee_validation.md", summary)
    _write_csv(output_dir / "fee_adjusted_expectancy.csv", adjusted_rows)
    return {
        "schedule": schedule,
        "trade_count": len(trades),
        "actual_fee_per_side": actual_fee,
        "actual_slippage_per_side": actual_slippage,
        "configured_equity_fee_per_side": configured_equity_fee,
        "capital": capital,
        "risk_per_trade": risk_per_trade,
        "fee_invalid": fee_invalid,
        "slippage_invalid": slippage_invalid,
        "adjusted_rows": adjusted_rows,
        "canary_blocked": blocked,
        "summary": summary,
    }


def adjusted_expectancy_rows(
    *,
    actual_fee_per_side: float,
    actual_slippage_per_side: float,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
) -> list[dict[str, Any]]:
    cost_delta = 2 * ((actual_fee_per_side - AUDIT_FEE_PER_SIDE) + (actual_slippage_per_side - AUDIT_SLIPPAGE_PER_SIDE))
    r_delta = cost_delta / risk_per_trade if risk_per_trade > 0 else 0.0
    rows: list[dict[str, Any]] = []
    for combo, expectancy in BACKTEST_EXPECTANCY_R.items():
        adjusted = expectancy - r_delta
        if adjusted <= 0:
            status = "NEGATIVE_EDGE"
        elif adjusted <= 0.05:
            status = "MARGINAL"
        else:
            status = "POSITIVE_EDGE"
        rows.append(
            {
                "combo_id": combo,
                "backtest_expectancy_r": round(expectancy, 6),
                "actual_fee_per_side": round(actual_fee_per_side, 8),
                "actual_slippage_per_side": round(actual_slippage_per_side, 8),
                "adjusted_expectancy_r": round(adjusted, 6),
                "status": status,
                "canary_blocked": status != "POSITIVE_EDGE",
            }
        )
    return rows


def build_fee_summary(
    schedule: dict[str, Any],
    trades: list[dict[str, Any]],
    actual_fee: float | None,
    actual_slippage: float | None,
    fee_invalid: bool,
    slippage_invalid: bool,
    adjusted_rows: list[dict[str, Any]],
    *,
    configured_equity_fee: float | None,
    risk_per_trade: float,
    capital: float,
) -> str:
    blocked = [r for r in adjusted_rows if r["canary_blocked"]]
    risk_amount = capital * risk_per_trade
    notional_at_four_pct_stop = risk_amount / 0.04 if risk_amount > 0 else 0.0
    lines = [
        "# Fee Validation",
        "",
        f"- Config source: `{schedule.get('source')}`",
        f"- Crypto fee_per_side found: `{schedule.get('crypto_fee_per_side')}`",
        f"- Equity fee_per_side found: `{schedule.get('equity_fee_per_side')}`",
        f"- Equity fee_per_side used for validation: `{configured_equity_fee}`",
        f"- Equity paper trades inspected: `{len(trades)}`",
        f"- Capital base: `EUR {capital:.2f}`",
        f"- Risk per trade: `{risk_per_trade:.4f}` (`EUR {risk_amount:.2f}`)",
        f"- Approx notional at 4% stop: `EUR {notional_at_four_pct_stop:.2f}`",
        f"- Audit fee_per_side: `{AUDIT_FEE_PER_SIDE}`",
        f"- Audit slippage_per_side: `{AUDIT_SLIPPAGE_PER_SIDE}`",
        f"- Actual/observed fee_per_side: `{actual_fee}`",
        f"- Actual/observed slippage_per_side: `{actual_slippage}`",
        f"- FEE_ASSUMPTION_INVALID: `{fee_invalid}`",
        f"- SLIPPAGE_ASSUMPTION_INVALID: `{slippage_invalid}`",
        f"- CANARY_BLOCKED after fee/slippage adjustment: `{len(blocked)}`",
    ]
    if blocked:
        lines.append("")
        lines.append("## Blocking Combinations")
        for row in blocked:
            lines.append(f"- {row['combo_id']}: adjusted={row['adjusted_expectancy_r']} status={row['status']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live paper trade validation
# ---------------------------------------------------------------------------


def summarize_live_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        asset = str(trade.get("asset") or "")
        strategy = str(trade.get("strategy") or POSITIVE_EDGE_STRATEGY_BY_ASSET.get(asset, "unknown"))
        combo = f"{asset}:{strategy}"
        groups.setdefault(combo, []).append(trade)

    rows: list[dict[str, Any]] = []
    for combo, items in sorted(groups.items()):
        pnls = [_float(t.get("pnl_r")) for t in items]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        live_expectancy = statistics.fmean(pnls) if pnls else 0.0
        backtest = BACKTEST_EXPECTANCY_R.get(combo)
        if len(items) < 5:
            verdict = "INSUFFICIENT_LIVE_DATA"
        elif backtest is None:
            verdict = "NO_BACKTEST_REFERENCE"
        else:
            diff = live_expectancy - backtest
            if abs(diff) < 0.05:
                verdict = "LIVE_CONFIRMS"
            elif diff > 0.05:
                verdict = "LIVE_DIVERGES_POSITIVE"
            else:
                verdict = "LIVE_DIVERGES_NEGATIVE"
        rows.append(
            {
                "combo_id": combo,
                "asset": combo.split(":", 1)[0],
                "strategy": combo.split(":", 1)[1] if ":" in combo else "",
                "trade_count": len(items),
                "observed_expectancy_r": round(live_expectancy, 6),
                "backtest_expectancy_r": backtest,
                "expectancy_difference_r": round(live_expectancy - backtest, 6) if backtest is not None else None,
                "win_rate": round(len(wins) / len(items), 6) if items else None,
                "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
                "total_pnl_eur": round(sum(_float(t.get("pnl_eur")) for t in items), 6),
                "verdict": verdict,
            }
        )
    return rows


def run_live_trade_validation(*, output_dir: Path, logs_root: Path) -> dict[str, Any]:
    trades = load_equity_trades(logs_root)
    rows = summarize_live_trades(trades)
    _write_csv(output_dir / "live_trade_validation.csv", rows)
    summary = build_live_trade_summary(rows, trades, logs_root)
    _write_text(output_dir / "live_trade_validation_summary.md", summary)
    return {"trades": trades, "rows": rows, "summary": summary}


def build_live_trade_summary(rows: list[dict[str, Any]], trades: list[dict[str, Any]], logs_root: Path) -> str:
    confirms = [r for r in rows if r["verdict"] == "LIVE_CONFIRMS"]
    negative = [r for r in rows if r["verdict"] == "LIVE_DIVERGES_NEGATIVE"]
    insufficient = [r for r in rows if r["verdict"] == "INSUFFICIENT_LIVE_DATA"]
    rejected = count_rejected_candidates(logs_root)
    lines = [
        "# Live Trade Validation",
        "",
        f"- Equity paper trades inspected: `{len(trades)}`",
        f"- LIVE_CONFIRMS groups: `{len(confirms)}`",
        f"- LIVE_DIVERGES_NEGATIVE groups: `{len(negative)}`",
        f"- INSUFFICIENT_LIVE_DATA groups: `{len(insufficient)}`",
        f"- Rejected/blocked candidate records found: `{rejected}`",
        "",
        "Rejected-trade opportunity cost cannot be simulated unless rejected signals include timestamp, asset, strategy, and entry price in logs.",
    ]
    return "\n".join(lines) + "\n"


def count_rejected_candidates(logs_root: Path) -> int:
    count = 0
    for subdir in ("strategy", "paper_execution", "execution"):
        path = logs_root / subdir
        if not path.exists():
            continue
        for file in path.rglob("*.jsonl"):
            for rec in _read_jsonl(file):
                payload = _record_payload(rec)
                text = json.dumps(payload).lower()
                if "reject" in text or "rejected" in text:
                    count += 1
    return count


# ---------------------------------------------------------------------------
# Signal generation validation
# ---------------------------------------------------------------------------


def run_signal_generation_validation(*, output_dir: Path, logs_root: Path, assets: Iterable[str] = POSITIVE_EDGE_ASSETS) -> dict[str, Any]:
    rows = [signal_flow_for_asset(logs_root, asset) for asset in assets]
    _write_csv(output_dir / "signal_generation.csv", rows)
    summary = build_signal_generation_summary(rows)
    _write_text(output_dir / "signal_generation_summary.md", summary)
    return {"rows": rows, "summary": summary}


def signal_flow_for_asset(logs_root: Path, asset: str, *, lookback_days: int = 30) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    research_dir = logs_root / "research"
    paper_dir = logs_root / "paper"
    strategy_dir = logs_root / "strategy"
    paper_exec_dir = logs_root / "paper_execution"

    if not logs_root.exists():
        return _signal_row(asset, "NO_LOG_DATA")
    if not research_dir.exists() and not paper_dir.exists():
        return _signal_row(asset, "NO_LOG_DATA")

    generated = _count_research_candidates(research_dir, asset, since)
    research_rejected = _count_text_research_rejections(logs_root, asset, since)
    paper_opens = _count_paper_actions(paper_dir, asset, "trade_opened", since)
    paper_rejections, reasons = _count_paper_rejections(paper_exec_dir, strategy_dir, asset, since)

    if generated == 0 and research_rejected > 0:
        verdict = "BLOCKED_AT_RESEARCH"
    elif generated == 0:
        verdict = "NO_SIGNAL_GENERATED"
    elif paper_opens > 0:
        verdict = "SIGNAL_FLOWING"
    elif paper_rejections > 0:
        verdict = "BLOCKED_AT_PAPER"
    else:
        verdict = "BLOCKED_AT_PAPER"

    return {
        "asset": asset,
        "signals_generated": generated,
        "signals_rejected": research_rejected + paper_rejections,
        "research_rejections": research_rejected,
        "paper_rejections": paper_rejections,
        "paper_opens": paper_opens,
        "rejection_reasons": ";".join(f"{k}={v}" for k, v in sorted(reasons.items())),
        "most_common_rejection_reason": max(reasons, key=reasons.get) if reasons else "",
        "verdict": verdict,
    }


def _signal_row(asset: str, verdict: str) -> dict[str, Any]:
    return {
        "asset": asset,
        "signals_generated": 0,
        "signals_rejected": 0,
        "research_rejections": 0,
        "paper_rejections": 0,
        "paper_opens": 0,
        "rejection_reasons": "",
        "most_common_rejection_reason": "",
        "verdict": verdict,
    }


def _count_research_candidates(research_dir: Path, asset: str, since: datetime) -> int:
    if not research_dir.exists():
        return 0
    count = 0
    for file in research_dir.rglob("*.jsonl"):
        for rec in _read_jsonl(file):
            payload = _record_payload(rec)
            dt = parse_dt(payload.get("timestamp"))
            if dt and dt < since:
                continue
            if payload.get("action") == "candidate_accepted" and str(payload.get("symbol")) == asset:
                strategy = str(payload.get("strategy_type") or payload.get("signal_type") or "").lower()
                if "momentum" in strategy or asset == "XLF":
                    count += 1
    return count


def _count_text_research_rejections(logs_root: Path, asset: str, since: datetime) -> int:
    count = 0
    for file in logs_root.rglob("*.log"):
        try:
            lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            if asset in line and "REJECTED" in line and "Kandidaat" in line:
                count += 1
    return count


def _count_paper_actions(paper_dir: Path, asset: str, action: str, since: datetime) -> int:
    if not paper_dir.exists():
        return 0
    count = 0
    for file in paper_dir.rglob("*.jsonl"):
        for rec in _read_jsonl(file):
            payload = _record_payload(rec)
            dt = parse_dt(payload.get("timestamp"))
            if dt and dt < since:
                continue
            if payload.get("action") == action and str(payload.get("symbol")) == asset:
                count += 1
    return count


def _count_paper_rejections(paper_exec_dir: Path, strategy_dir: Path, asset: str, since: datetime) -> tuple[int, dict[str, int]]:
    count = 0
    reasons: dict[str, int] = {}
    for root in (paper_exec_dir, strategy_dir):
        if not root.exists():
            continue
        for file in root.rglob("*.jsonl"):
            for rec in _read_jsonl(file):
                payload = _record_payload(rec)
                dt = parse_dt(payload.get("timestamp"))
                if dt and dt < since:
                    continue
                if str(payload.get("symbol") or payload.get("asset") or "") != asset:
                    continue
                outcome = str(payload.get("outcome") or payload.get("status") or "").lower()
                if "reject" not in outcome and "reject" not in json.dumps(payload).lower():
                    continue
                reason = str(payload.get("rejection_reason") or payload.get("reason") or "unknown")
                reasons[reason] = reasons.get(reason, 0) + 1
                count += 1
    return count, reasons


def build_signal_generation_summary(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["verdict"])] = counts.get(str(row["verdict"]), 0) + 1
    lines = [
        "# Signal Generation Summary",
        "",
        *[f"- {key}: `{value}`" for key, value in sorted(counts.items())],
    ]
    blocked = [r for r in rows if r["verdict"] != "SIGNAL_FLOWING"]
    if blocked:
        lines.append("")
        lines.append("## Blocked Or Unknown Assets")
        for row in blocked:
            lines.append(f"- {row['asset']}: {row['verdict']} ({row.get('most_common_rejection_reason') or 'no reason'})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Final validation report
# ---------------------------------------------------------------------------


def run_full_pc2_validation(
    *,
    output_dir: Path,
    logs_root: Path,
    from_dt: datetime,
    to_dt: datetime,
    broker_cache_dir: Path | None = None,
    use_broker_api: bool = True,
    capital: float = 150_000.0,
    equity_fee_per_side: float | None = AUDIT_FEE_PER_SIDE,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
) -> dict[str, Any]:
    failures: list[str] = []
    try:
        price = run_price_validation(
            output_dir=output_dir,
            logs_root=logs_root,
            from_dt=from_dt,
            to_dt=to_dt,
            broker_cache_dir=broker_cache_dir,
            use_broker_api=use_broker_api,
        )
    except Exception as exc:
        failures.append(f"PRICE DATA failed: {exc}")
        price = _failed_price_result(str(exc))
    try:
        fees = run_fee_validation(
            output_dir=output_dir,
            logs_root=logs_root,
            equity_fee_per_side=equity_fee_per_side,
            risk_per_trade=risk_per_trade,
            capital=capital,
        )
    except Exception as exc:
        failures.append(f"FEE VALIDATION failed: {exc}")
        fees = _failed_fee_result(str(exc))
    try:
        live = run_live_trade_validation(output_dir=output_dir, logs_root=logs_root)
    except Exception as exc:
        failures.append(f"LIVE TRADES failed: {exc}")
        live = _failed_live_result(str(exc))
    try:
        signals = run_signal_generation_validation(output_dir=output_dir, logs_root=logs_root)
    except Exception as exc:
        failures.append(f"SIGNAL FLOW failed: {exc}")
        signals = _failed_signal_result(str(exc))
    report = build_validation_report(
        price=price,
        fees=fees,
        live=live,
        signals=signals,
        logs_root=logs_root,
        capital=capital,
        risk_per_trade=risk_per_trade,
    )
    if failures:
        report += "\nTask failures:\n" + "\n".join(f"- {failure}" for failure in failures) + "\n"
    _write_text(output_dir / "validation_report.md", report)
    return {"price": price, "fees": fees, "live": live, "signals": signals, "failures": failures, "report": report}


def _failed_price_result(error: str) -> dict[str, Any]:
    rows = [
        {
            "asset": asset,
            "broker_days": 0,
            "yfinance_days": 0,
            "common_days": 0,
            "mape_close_pct": None,
            "max_close_discrepancy_pct": None,
            "missing_in_yfinance": 0,
            "missing_in_broker": 0,
            "return_correlation": None,
            "verdict": "NO_BROKER_DATA",
            "canary_blocked": asset in POSITIVE_EDGE_ASSETS,
            "error": error,
        }
        for asset in VALIDATION_ASSETS
    ]
    return {"rows": rows, "summary": f"PRICE DATA failed: {error}\n"}


def _failed_fee_result(error: str) -> dict[str, Any]:
    rows = [
        {
            "combo_id": combo,
            "backtest_expectancy_r": expectancy,
            "adjusted_expectancy_r": None,
            "status": "VALIDATION_FAILED",
            "canary_blocked": True,
            "error": error,
        }
        for combo, expectancy in BACKTEST_EXPECTANCY_R.items()
    ]
    return {
        "schedule": {},
        "trade_count": 0,
        "actual_fee_per_side": None,
        "actual_slippage_per_side": None,
        "configured_equity_fee_per_side": None,
        "capital": 150_000.0,
        "risk_per_trade": DEFAULT_RISK_PER_TRADE,
        "fee_invalid": True,
        "slippage_invalid": True,
        "adjusted_rows": rows,
        "canary_blocked": rows,
        "summary": f"FEE VALIDATION failed: {error}\n",
    }


def _failed_live_result(error: str) -> dict[str, Any]:
    return {"trades": [], "rows": [], "summary": f"LIVE TRADES failed: {error}\n"}


def _failed_signal_result(error: str) -> dict[str, Any]:
    rows = [_signal_row(asset, "NO_LOG_DATA") | {"error": error} for asset in POSITIVE_EDGE_ASSETS]
    return {"rows": rows, "summary": f"SIGNAL FLOW failed: {error}\n"}


def build_validation_report(
    *,
    price: dict[str, Any],
    fees: dict[str, Any],
    live: dict[str, Any],
    signals: dict[str, Any],
    logs_root: Path,
    capital: float = 150_000.0,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
) -> str:
    price_rows = price["rows"]
    price_blocked = [r for r in price_rows if r.get("canary_blocked")]
    price_pass = "FAIL" if price_blocked else "PARTIAL" if any(r["verdict"] == "DATA_MINOR_DRIFT" for r in price_rows) else "PASS"

    fee_blocked = fees.get("canary_blocked") or []
    if fee_blocked:
        fee_status = "FAIL"
    elif fees.get("fee_invalid") or fees.get("slippage_invalid"):
        fee_status = "ADJUSTED"
    else:
        fee_status = "PASS"

    live_rows = live["rows"]
    if any(r["verdict"] == "LIVE_DIVERGES_NEGATIVE" for r in live_rows):
        live_status = "DIVERGES"
    elif any(r["verdict"] == "LIVE_CONFIRMS" for r in live_rows):
        live_status = "CONFIRMED"
    else:
        live_status = "INSUFFICIENT_DATA"

    signal_rows = signals["rows"]
    if all(r["verdict"] == "SIGNAL_FLOWING" for r in signal_rows):
        signal_status = "FLOWING"
    else:
        non_flow = [str(r["verdict"]) for r in signal_rows if r["verdict"] != "SIGNAL_FLOWING"]
        signal_status = sorted(non_flow, key=non_flow.count, reverse=True)[0] if non_flow else "FLOWING"

    final = final_verdict(price_pass, fee_status, live_status, signal_status)
    blocked_reasons = blocking_reasons(price_rows, fees, signals)
    recommended = recommend_canary_assets(price_rows, fees, signals, capital=capital, risk_per_trade=risk_per_trade)
    risk_amount = capital * risk_per_trade
    notional_at_four_pct_stop = risk_amount / 0.04 if risk_amount > 0 else 0.0
    lines = [
        "VALIDATION SUMMARY",
        "==================",
        f"Date: {datetime.now(timezone.utc).date().isoformat()}",
        f"PC2 hostname/identifier: {socket.gethostname()}",
        f"Logs root: {logs_root}",
        f"Capital base: EUR {capital:.2f}",
        f"Risk per trade: EUR {risk_amount:.2f} ({risk_per_trade:.2%})",
        f"Approx notional at 4% stop: EUR {notional_at_four_pct_stop:.2f}",
        "",
        f"PRICE DATA:      {price_pass} - {sum(1 for r in price_rows if r['verdict'] == 'DATA_MATCH')} assets DATA_MATCH, {len(price_blocked)} CANARY_BLOCKED",
        f"FEE VALIDATION:  {fee_status} - actual fees/slippage vs assumptions",
        f"LIVE TRADES:     {live_status}",
        f"SIGNAL FLOW:     {signal_status}",
        "",
        "CANARY DEPLOYMENT VERDICT:",
        final,
        "",
        "Blocking reasons:",
    ]
    lines.extend([f"- {reason}" for reason in blocked_reasons] or ["- none"])
    lines.extend(["", "Adjusted expectancy after real fees:"])
    fee_rows = fees.get("adjusted_rows") or []
    lines.extend(_markdown_table(fee_rows, ["combo_id", "backtest_expectancy_r", "adjusted_expectancy_r", "status"]))
    lines.extend(["", "Recommended first canary assets:"])
    if recommended:
        for idx, item in enumerate(recommended[:3], start=1):
            lines.append(
                f"{idx}. {item['asset']} - expectancy {item['expectancy_r']}R, "
                f"~{item['trades_per_year']} trades/yr, est. CAGR {item['estimated_cagr_pct']}% at EUR {capital:.0f}; {item['reason']}"
            )
    else:
        lines.append("1. none - validation did not clear any asset")
    lines.extend([
        "",
        f"Recommended canary position size: EUR {risk_amount:.2f} risk per trade at {risk_per_trade:.2%} of EUR {capital:.2f}",
        "Recommended canary duration: 60 days before scaling",
    ])
    lines.extend(["", "Next step:", next_step(final)])
    return "\n".join(lines) + "\n"


def final_verdict(price_status: str, fee_status: str, live_status: str, signal_status: str) -> str:
    if price_status == "FAIL" or fee_status == "FAIL" or live_status == "DIVERGES":
        return "DO_NOT_DEPLOY"
    if signal_status != "FLOWING":
        return "BLOCKED_PENDING_SIGNAL_FLOW"
    if live_status == "INSUFFICIENT_DATA":
        return "BLOCKED_PENDING_LIVE_DATA"
    if price_status in {"PASS", "PARTIAL"} and fee_status in {"PASS", "ADJUSTED"}:
        return "CLEAR_TO_DEPLOY"
    return "BLOCKED_PENDING_VALIDATION"


def blocking_reasons(price_rows: list[dict[str, Any]], fees: dict[str, Any], signals: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for row in price_rows:
        if row.get("canary_blocked"):
            reasons.append(f"{row['asset']} price data {row['verdict']}")
    for row in fees.get("canary_blocked") or []:
        reasons.append(f"{row['combo_id']} fee-adjusted status {row['status']}")
    for row in signals.get("rows") or []:
        if row["verdict"] != "SIGNAL_FLOWING":
            reasons.append(f"{row['asset']} signal flow {row['verdict']}")
    return reasons


def recommend_canary_assets(
    price_rows: list[dict[str, Any]],
    fees: dict[str, Any],
    signals: dict[str, Any],
    *,
    capital: float = 150_000.0,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
) -> list[dict[str, str]]:
    price_ok = {r["asset"] for r in price_rows if not r.get("canary_blocked")}
    fee_ok = {r["combo_id"].split(":", 1)[0] for r in fees.get("adjusted_rows") or [] if not r.get("canary_blocked")}
    signal_ok = {r["asset"] for r in signals.get("rows") or [] if r["verdict"] == "SIGNAL_FLOWING"}
    candidates = [a for a in POSITIVE_EDGE_ASSETS if a in price_ok and a in fee_ok and a in signal_ok]
    preference = ["JNJ", "GLD", "XLI", "XLK", "AAPL", "XLY", "QQQ", "XLF"]
    result: list[dict[str, str]] = []
    for asset in preference:
        if asset in candidates:
            detail = POSITIVE_EDGE_DETAILS.get(asset, {})
            expectancy = float(detail.get("expectancy_r") or 0.0)
            trades_per_year = float(detail.get("trades_per_year") or 0.0)
            est_cagr = trades_per_year * risk_per_trade * expectancy
            result.append(
                {
                    "asset": asset,
                    "expectancy_r": f"{expectancy:.4f}",
                    "trades_per_year": f"{trades_per_year:.0f}",
                    "estimated_cagr_pct": f"{est_cagr * 100:.2f}",
                    "capital": f"{capital:.2f}",
                    "reason": "validated data, fee-adjusted positive edge, and signal flow present",
                }
            )
    return result


def next_step(verdict: str) -> str:
    if verdict == "CLEAR_TO_DEPLOY":
        return "Start een kleine paper-to-canary dry run met de drie aanbevolen assets en log elke fill voor feedback."
    if verdict == "DO_NOT_DEPLOY":
        return "Los de blokkerende data-, fee- of live-divergentie eerst op voordat kapitaal wordt toegewezen."
    return "Verzamel of herstel de ontbrekende PC2 validatiedata en draai deze validatie opnieuw."


def _markdown_table(rows: list[dict[str, Any]], fields: list[str]) -> list[str]:
    if not rows:
        return ["n/a"]
    lines = ["|" + "|".join(fields) + "|", "|" + "|".join("---" for _ in fields) + "|"]
    for row in rows:
        lines.append("|" + "|".join(str(row.get(f, "")) for f in fields) + "|")
    return lines
