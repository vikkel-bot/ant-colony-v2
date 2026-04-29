"""
ant_colony/lab/watchtower_backtester.py

Event-driven replay backtester for Watchtower signals.

Watchtower and Colony stay separate:
  - Watchtower exports immutable signal events.
  - Colony replays those events against historical OHLCV data.
  - The backtest never calls live Watchtower endpoints.

The engine is deliberately conservative:
  - no same-bar entry; signals enter at the next available bar open
  - fees are charged on entry and exit
  - slippage is applied against the trade on both sides
  - if TP and SL are both touched in one bar, SL wins (pessimistic ordering)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ant_colony.lab.backtester import OHLCVBar


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchtowerSignal:
    """Normalized Watchtower signal used by the replay backtester."""

    signal_id: str
    symbol: str
    direction: str
    timestamp: datetime
    entry_score: float = 0.0
    confidence: float = 0.0
    asset_class: str = "unknown"
    source_field: str = "unknown"
    linked_assets: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatchtowerBacktestAssumptions:
    """Execution assumptions for realistic signal replay."""

    starting_equity: float = 10_000.0
    notional_per_trade: float = 500.0
    fee_pct_per_side: float = 0.0025
    slippage_pct: float = 0.0010
    default_take_profit_pct: float = 0.03
    default_stop_loss_pct: float = 0.015
    max_bars_held: int = 24
    max_concurrent_positions: int = 1
    allow_short: bool = False

    def __post_init__(self) -> None:
        if self.starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")
        if self.notional_per_trade <= 0:
            raise ValueError("notional_per_trade must be > 0")
        if self.fee_pct_per_side < 0:
            raise ValueError("fee_pct_per_side must be >= 0")
        if self.slippage_pct < 0:
            raise ValueError("slippage_pct must be >= 0")
        if self.default_take_profit_pct <= 0:
            raise ValueError("default_take_profit_pct must be > 0")
        if self.default_stop_loss_pct <= 0:
            raise ValueError("default_stop_loss_pct must be > 0")
        if self.max_bars_held < 1:
            raise ValueError("max_bars_held must be >= 1")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")


@dataclass(frozen=True)
class WatchtowerBacktestTrade:
    """One completed replay trade."""

    signal_id: str
    symbol: str
    direction: str
    asset_class: str
    source_field: str
    entry_timestamp: datetime
    exit_timestamp: datetime
    entry_price: float
    exit_price: float
    quantity: float
    exit_reason: str
    gross_pnl: float
    fee_cost: float
    net_pnl: float
    net_return_pct: float
    bars_held: int
    linked_assets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "asset_class": self.asset_class,
            "source_field": self.source_field,
            "entry_timestamp": self.entry_timestamp.isoformat(),
            "exit_timestamp": self.exit_timestamp.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "exit_reason": self.exit_reason,
            "gross_pnl": self.gross_pnl,
            "fee_cost": self.fee_cost,
            "net_pnl": self.net_pnl,
            "net_return_pct": self.net_return_pct,
            "bars_held": self.bars_held,
            "linked_assets": list(self.linked_assets),
        }


@dataclass(frozen=True)
class WatchtowerBacktestReport:
    """Summary report for one replay run."""

    generated_at: datetime
    assumptions: WatchtowerBacktestAssumptions
    total_signals: int
    total_trades: int
    skipped_signals: int
    skipped_by_reason: dict[str, int]
    total_net_pnl: float
    total_return_pct: float
    win_rate: float | None
    expectancy_pct: float | None
    avg_net_return_pct: float | None
    profit_factor: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float
    by_symbol: dict[str, dict[str, Any]]
    by_asset_class: dict[str, dict[str, Any]]
    cross_field: dict[str, Any]
    trades: list[WatchtowerBacktestTrade]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "assumptions": self.assumptions.__dict__,
            "total_signals": self.total_signals,
            "total_trades": self.total_trades,
            "skipped_signals": self.skipped_signals,
            "skipped_by_reason": self.skipped_by_reason,
            "total_net_pnl": self.total_net_pnl,
            "total_return_pct": self.total_return_pct,
            "win_rate": self.win_rate,
            "expectancy_pct": self.expectancy_pct,
            "avg_net_return_pct": self.avg_net_return_pct,
            "profit_factor": self.profit_factor,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown_pct": self.max_drawdown_pct,
            "by_symbol": self.by_symbol,
            "by_asset_class": self.by_asset_class,
            "cross_field": self.cross_field,
            "trades": [t.to_dict() for t in self.trades],
        }


# ---------------------------------------------------------------------------
# Signal loading
# ---------------------------------------------------------------------------

def load_watchtower_signals(
    logs_root: Path,
    include_neutral_as_long: bool = False,
) -> list[WatchtowerSignal]:
    """
    Load filtered signals from ANT_LOGS/watchtower/signals.jsonl.

    Each WatchtowerAnt line stores a poll snapshot with a `signals` list. The
    signal timestamp is preferred; the snapshot timestamp is used as fallback.
    Corrupt records are skipped.
    """
    path = Path(logs_root) / "watchtower" / "signals.jsonl"
    if not path.exists():
        return []

    signals: list[WatchtowerSignal] = []
    seen_ids: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                snapshot = json.loads(line)
            except json.JSONDecodeError:
                continue
            snapshot_ts = _parse_ts(snapshot.get("timestamp"))
            for raw in snapshot.get("signals") or []:
                signal = normalize_watchtower_signal(
                    raw,
                    fallback_timestamp=snapshot_ts,
                    include_neutral_as_long=include_neutral_as_long,
                )
                if signal is not None and signal.signal_id not in seen_ids:
                    seen_ids.add(signal.signal_id)
                    signals.append(signal)
    except OSError:
        return []

    signals.sort(key=lambda s: s.timestamp)
    return signals


def append_watchtower_export(logs_root: Path, export_packet: dict[str, Any]) -> Path:
    """
    Append a Watchtower /backtest/signals export as replay input.

    The file format intentionally matches WatchtowerAnt output so the same
    loader/backtester can replay both continuous polls and one-shot exports.
    """
    out_dir = Path(logs_root) / "watchtower"
    out_dir.mkdir(parents=True, exist_ok=True)
    signals = export_packet.get("signals") if isinstance(export_packet.get("signals"), list) else []
    generated_at = _parse_ts(export_packet.get("generated_at")) or datetime.now(timezone.utc)
    record = {
        "timestamp": generated_at.isoformat(),
        "source": "watchtower_backtest_export",
        "received": int(export_packet.get("count") or len(signals)),
        "passed_filter": len(signals),
        "filters": export_packet.get("filters", {}),
        "signals": signals,
    }
    log_path = out_dir / "signals.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return log_path


def normalize_watchtower_signal(
    raw: dict[str, Any],
    fallback_timestamp: datetime | None = None,
    include_neutral_as_long: bool = False,
) -> WatchtowerSignal | None:
    """Normalize one raw Watchtower signal dict."""
    raw_payload = dict(raw)
    symbol = str(raw_payload.get("asset") or raw_payload.get("symbol") or "").strip()
    if not symbol:
        return None

    ts = _parse_ts(raw_payload.get("timestamp")) or fallback_timestamp
    if ts is None:
        return None

    direction = str(raw_payload.get("direction") or "LONG").strip().lower()
    if direction == "neutral" and include_neutral_as_long:
        raw_payload["original_direction"] = "neutral"
        raw_payload["direction"] = "long"
        direction = "long"
    if direction not in ("long", "short"):
        return None

    asset_class = str(
        raw_payload.get("asset_class") or raw_payload.get("biome") or _infer_asset_class(symbol)
    ).strip().lower()
    source_field = str(
        raw_payload.get("source_field") or raw_payload.get("field") or asset_class
    ).strip().lower()
    linked = tuple(str(x) for x in (raw_payload.get("linked_assets") or []) if x)

    return WatchtowerSignal(
        signal_id=str(raw_payload.get("signal_id") or f"wt-{symbol}-{int(ts.timestamp())}"),
        symbol=symbol,
        direction=direction,
        timestamp=ts,
        entry_score=_safe_float(raw_payload.get("entry_score"), 0.0),
        confidence=_safe_float(raw_payload.get("confidence"), 0.0),
        asset_class=asset_class or "unknown",
        source_field=source_field or "unknown",
        linked_assets=linked,
        raw=raw_payload,
    )


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class WatchtowerSignalBacktester:
    """Replay Watchtower signals against historical OHLCV bars."""

    def run(
        self,
        signals: list[WatchtowerSignal],
        bars_by_symbol: dict[str, list[OHLCVBar]],
        assumptions: WatchtowerBacktestAssumptions | None = None,
    ) -> WatchtowerBacktestReport:
        assumptions = assumptions or WatchtowerBacktestAssumptions()

        normalized_bars = {
            symbol: sorted([b for b in bars if _bar_is_valid(b)], key=lambda b: b.timestamp)
            for symbol, bars in bars_by_symbol.items()
        }

        active_exit_times: list[datetime] = []
        trades: list[WatchtowerBacktestTrade] = []
        skipped_by_reason: dict[str, int] = {}

        for signal in sorted(signals, key=lambda s: s.timestamp):
            if signal.direction == "short" and not assumptions.allow_short:
                _add_skip(skipped_by_reason, "short_disabled")
                continue

            bars = normalized_bars.get(signal.symbol)
            if not bars:
                _add_skip(skipped_by_reason, "no_bars")
                continue

            entry_idx = _first_bar_after(bars, signal.timestamp)
            if entry_idx is None:
                _add_skip(skipped_by_reason, "no_next_bar_after_signal")
                continue

            entry_time = bars[entry_idx].timestamp
            active_exit_times = [t for t in active_exit_times if t > entry_time]
            if len(active_exit_times) >= assumptions.max_concurrent_positions:
                _add_skip(skipped_by_reason, "max_concurrent_positions")
                continue

            trade = self._simulate_trade(signal, bars, entry_idx, assumptions)
            if trade is None:
                _add_skip(skipped_by_reason, "simulation_failed")
                continue

            trades.append(trade)
            active_exit_times.append(trade.exit_timestamp)

        return self._build_report(signals, trades, skipped_by_reason, assumptions)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _simulate_trade(
        self,
        signal: WatchtowerSignal,
        bars: list[OHLCVBar],
        entry_idx: int,
        assumptions: WatchtowerBacktestAssumptions,
    ) -> WatchtowerBacktestTrade | None:
        raw_entry = bars[entry_idx].open if bars[entry_idx].open > 0 else bars[entry_idx].close
        if raw_entry <= 0:
            return None

        tp_pct = _signal_pct(
            signal,
            ("take_profit_pct", "tp_pct", "target_pct"),
            assumptions.default_take_profit_pct,
        )
        sl_pct = _signal_pct(
            signal,
            ("stop_loss_pct", "sl_pct", "risk_pct"),
            assumptions.default_stop_loss_pct,
        )

        entry_price = _apply_slippage(raw_entry, signal.direction, "entry", assumptions.slippage_pct)
        quantity = assumptions.notional_per_trade / entry_price

        if signal.direction == "long":
            tp_price = entry_price * (1.0 + tp_pct)
            sl_price = entry_price * (1.0 - sl_pct)
        else:
            tp_price = entry_price * (1.0 - tp_pct)
            sl_price = entry_price * (1.0 + sl_pct)

        exit_idx, raw_exit, reason = self._find_exit(
            signal.direction, bars, entry_idx, tp_price, sl_price, assumptions.max_bars_held
        )
        exit_price = _apply_slippage(raw_exit, signal.direction, "exit", assumptions.slippage_pct)

        if signal.direction == "long":
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity
        fee_cost = (entry_price + exit_price) * quantity * assumptions.fee_pct_per_side
        net_pnl = gross_pnl - fee_cost
        net_return_pct = net_pnl / assumptions.notional_per_trade

        return WatchtowerBacktestTrade(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            asset_class=signal.asset_class,
            source_field=signal.source_field,
            entry_timestamp=bars[entry_idx].timestamp,
            exit_timestamp=bars[exit_idx].timestamp,
            entry_price=round(entry_price, 8),
            exit_price=round(exit_price, 8),
            quantity=round(quantity, 12),
            exit_reason=reason,
            gross_pnl=round(gross_pnl, 6),
            fee_cost=round(fee_cost, 6),
            net_pnl=round(net_pnl, 6),
            net_return_pct=round(net_return_pct, 8),
            bars_held=exit_idx - entry_idx + 1,
            linked_assets=signal.linked_assets,
        )

    @staticmethod
    def _find_exit(
        direction: str,
        bars: list[OHLCVBar],
        entry_idx: int,
        tp_price: float,
        sl_price: float,
        max_bars_held: int,
    ) -> tuple[int, float, str]:
        end_idx = min(entry_idx + max_bars_held - 1, len(bars) - 1)

        for idx in range(entry_idx, end_idx + 1):
            bar = bars[idx]
            if direction == "long":
                # Pessimistic same-bar ordering: when both touched, SL wins.
                if bar.low <= sl_price:
                    return idx, sl_price, "stop_loss"
                if bar.high >= tp_price:
                    return idx, tp_price, "take_profit"
            else:
                if bar.high >= sl_price:
                    return idx, sl_price, "stop_loss"
                if bar.low <= tp_price:
                    return idx, tp_price, "take_profit"

        return end_idx, bars[end_idx].close, "ttl"

    @staticmethod
    def _build_report(
        signals: list[WatchtowerSignal],
        trades: list[WatchtowerBacktestTrade],
        skipped_by_reason: dict[str, int],
        assumptions: WatchtowerBacktestAssumptions,
    ) -> WatchtowerBacktestReport:
        returns = [t.net_return_pct for t in trades]
        pnls = [t.net_pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        total_net = sum(pnls)

        return WatchtowerBacktestReport(
            generated_at=datetime.now(tz=timezone.utc),
            assumptions=assumptions,
            total_signals=len(signals),
            total_trades=len(trades),
            skipped_signals=sum(skipped_by_reason.values()),
            skipped_by_reason=dict(sorted(skipped_by_reason.items())),
            total_net_pnl=round(total_net, 6),
            total_return_pct=round(total_net / assumptions.starting_equity, 8),
            win_rate=_win_rate(returns),
            expectancy_pct=round(sum(returns) / len(returns), 8) if returns else None,
            avg_net_return_pct=round(sum(returns) / len(returns), 8) if returns else None,
            profit_factor=round(sum(wins) / sum(losses), 6) if losses else (None if not wins else math.inf),
            sharpe_ratio=_sharpe(returns),
            max_drawdown_pct=_max_drawdown_pct(assumptions.starting_equity, trades),
            by_symbol=_group_stats(trades, lambda t: t.symbol),
            by_asset_class=_group_stats(trades, lambda t: t.asset_class),
            cross_field=_cross_field_stats(signals, trades),
            trades=sorted(trades, key=lambda t: t.exit_timestamp),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        ts = datetime.fromisoformat(text)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _add_skip(bucket: dict[str, int], reason: str) -> None:
    bucket[reason] = bucket.get(reason, 0) + 1


def _normalize_pct(value: Any, default: float) -> float:
    pct = _safe_float(value, default)
    if pct <= 0:
        return default
    return pct / 100.0 if pct > 1.0 else pct


def _signal_pct(signal: WatchtowerSignal, keys: tuple[str, ...], default: float) -> float:
    for key in keys:
        if key in signal.raw:
            return _normalize_pct(signal.raw.get(key), default)
    return default


def _infer_asset_class(symbol: str) -> str:
    if "-" in symbol:
        return "crypto"
    return "equities"


def _bar_is_valid(bar: OHLCVBar) -> bool:
    return bar.close > 0 and bar.high > 0 and bar.low > 0


def _first_bar_after(bars: list[OHLCVBar], timestamp: datetime) -> int | None:
    ts = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    for idx, bar in enumerate(bars):
        bar_ts = bar.timestamp if bar.timestamp.tzinfo else bar.timestamp.replace(tzinfo=timezone.utc)
        if bar_ts > ts:
            return idx
    return None


def _apply_slippage(price: float, direction: str, side: str, slippage_pct: float) -> float:
    if slippage_pct == 0:
        return price
    if direction == "long":
        return price * (1.0 + slippage_pct) if side == "entry" else price * (1.0 - slippage_pct)
    return price * (1.0 - slippage_pct) if side == "entry" else price * (1.0 + slippage_pct)


def _win_rate(returns: list[float]) -> float | None:
    if not returns:
        return None
    return round(sum(1 for r in returns if r > 0) / len(returns), 6)


def _sharpe(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return round(mean / std, 6)


def _max_drawdown_pct(starting_equity: float, trades: list[WatchtowerBacktestTrade]) -> float:
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: t.exit_timestamp):
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return round(max_dd, 8)


def _group_stats(
    trades: list[WatchtowerBacktestTrade],
    key_fn,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[WatchtowerBacktestTrade]] = {}
    for trade in trades:
        groups.setdefault(str(key_fn(trade)), []).append(trade)

    result: dict[str, dict[str, Any]] = {}
    for key, group in sorted(groups.items()):
        returns = [t.net_return_pct for t in group]
        total = sum(t.net_pnl for t in group)
        result[key] = {
            "trade_count": len(group),
            "total_net_pnl": round(total, 6),
            "avg_net_return_pct": round(sum(returns) / len(returns), 8) if returns else None,
            "win_rate": _win_rate(returns),
        }
    return result


def _cross_field_stats(
    signals: list[WatchtowerSignal],
    trades: list[WatchtowerBacktestTrade],
) -> dict[str, Any]:
    signal_cross_count = sum(1 for s in signals if s.linked_assets)
    cross_trades = [t for t in trades if t.linked_assets]
    returns = [t.net_return_pct for t in cross_trades]
    return {
        "signals_with_linked_assets": signal_cross_count,
        "trades_with_linked_assets": len(cross_trades),
        "avg_net_return_pct": round(sum(returns) / len(returns), 8) if returns else None,
        "win_rate": _win_rate(returns),
    }
