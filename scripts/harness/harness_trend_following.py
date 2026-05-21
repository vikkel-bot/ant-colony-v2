"""
scripts/harness/harness_trend_following.py

Standalone trend-following harness voor Donchian Channel breakouts.

Wat het doet:
  1. Haalt 1h crypto candles op via BitvavoAdapter.get_candles().
  2. Simuleert long/short Donchian breakouts zonder overlappende posities.
  3. Print een compact rapport met Go/No-Go criteria voor paper-testfase.
  4. Schrijft hetzelfde rapport naar ANT_LOGS/harness/trend_following_YYYYMMDD_HHMM.txt.

Veiligheid:
  - Read-only: haalt alleen candles op, plaatst nooit orders.
  - Fail-closed: ontbrekende data wordt overgeslagen; netwerkfouten crashen de harness niet.

Gebruik:
  python scripts/harness/harness_trend_following.py
"""

from __future__ import annotations

import math
import os
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is optional for this read-only harness
    load_dotenv = None

from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter
from ant_colony.biome.biome_adapter import MarketData

SYMBOLS = ["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR"]
TIMEFRAME = "1h"
LOOKBACK_DAYS = 90
CANDLE_LIMIT = LOOKBACK_DAYS * 24

ENTRY_LOOKBACK = 20
EXIT_LOOKBACK = 10
STOP_LOSS_PCT = 0.05
MAX_BARS_HELD = 72


@dataclass(frozen=True)
class TrendTrade:
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    pnl_pct: float
    bars_held: int
    exit_reason: str


@dataclass(frozen=True)
class TrendMetrics:
    trades: int
    wins: int
    winrate: float
    avg_return: float
    total_return: float
    sharpe: float
    max_drawdown: float


def _load_env() -> None:
    if load_dotenv is None:
        return
    for env_path in (_REPO_ROOT / ".env", Path.cwd() / ".env"):
        try:
            if env_path.exists():
                load_dotenv(env_path, override=False)
        except OSError:
            continue


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    if direction == "short":
        return (entry_price - exit_price) / entry_price
    return (exit_price - entry_price) / entry_price


def _build_trade(
    *,
    symbol: str,
    direction: str,
    entry_bar: MarketData,
    exit_bar: MarketData,
    entry_index: int,
    exit_index: int,
    exit_reason: str,
) -> TrendTrade:
    return TrendTrade(
        symbol=symbol,
        direction=direction,
        entry_time=entry_bar.timestamp,
        exit_time=exit_bar.timestamp,
        entry_price=entry_bar.close,
        exit_price=exit_bar.close,
        pnl_pct=_pnl_pct(direction, entry_bar.close, exit_bar.close),
        bars_held=exit_index - entry_index,
        exit_reason=exit_reason,
    )


def simulate_symbol(symbol: str, candles: list[MarketData]) -> list[TrendTrade]:
    """Simuleer Donchian breakout trades voor één symbool."""
    candles = sorted(candles, key=lambda candle: candle.timestamp)
    if len(candles) < ENTRY_LOOKBACK + 2:
        return []

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    trades: list[TrendTrade] = []
    position: dict | None = None

    for index in range(ENTRY_LOOKBACK, len(candles)):
        bar = candles[index]
        exited_this_bar = False

        if position is not None:
            direction = position["direction"]
            entry_bar = position["entry_bar"]
            entry_index = position["entry_index"]
            entry_price = entry_bar.close
            bars_held = index - entry_index

            exit_reason: str | None = None
            if direction == "long":
                trailing_lows = lows[max(0, index - EXIT_LOOKBACK):index]
                if bar.close <= entry_price * (1.0 - STOP_LOSS_PCT):
                    exit_reason = "stop_loss"
                elif len(trailing_lows) >= EXIT_LOOKBACK and bar.close < min(trailing_lows):
                    exit_reason = "trailing_donchian_exit"
                elif bars_held >= MAX_BARS_HELD:
                    exit_reason = "max_bars"
            else:
                trailing_highs = highs[max(0, index - EXIT_LOOKBACK):index]
                if bar.close >= entry_price * (1.0 + STOP_LOSS_PCT):
                    exit_reason = "stop_loss"
                elif len(trailing_highs) >= EXIT_LOOKBACK and bar.close > max(trailing_highs):
                    exit_reason = "trailing_donchian_exit"
                elif bars_held >= MAX_BARS_HELD:
                    exit_reason = "max_bars"

            if exit_reason is not None:
                trades.append(
                    _build_trade(
                        symbol=symbol,
                        direction=direction,
                        entry_bar=entry_bar,
                        exit_bar=bar,
                        entry_index=entry_index,
                        exit_index=index,
                        exit_reason=exit_reason,
                    )
                )
                position = None
                exited_this_bar = True

        if position is not None or exited_this_bar:
            continue

        entry_high = max(highs[index - ENTRY_LOOKBACK:index])
        entry_low = min(lows[index - ENTRY_LOOKBACK:index])
        if bar.close > entry_high:
            position = {"direction": "long", "entry_bar": bar, "entry_index": index}
        elif bar.close < entry_low:
            position = {"direction": "short", "entry_bar": bar, "entry_index": index}

    if position is not None:
        trades.append(
            _build_trade(
                symbol=symbol,
                direction=position["direction"],
                entry_bar=position["entry_bar"],
                exit_bar=candles[-1],
                entry_index=position["entry_index"],
                exit_index=len(candles) - 1,
                exit_reason="end_of_data",
            )
        )

    return trades


def calculate_metrics(trades: Iterable[TrendTrade]) -> TrendMetrics:
    trade_list = list(trades)
    returns = [trade.pnl_pct for trade in trade_list]
    wins = sum(1 for value in returns if value > 0)
    winrate = wins / len(returns) if returns else 0.0
    avg_return = statistics.mean(returns) if returns else 0.0
    total_return = math.prod(1.0 + value for value in returns) - 1.0 if returns else 0.0

    if len(returns) < 2:
        sharpe = 0.0
    else:
        std = statistics.pstdev(returns)
        if std == 0:
            sharpe = float("inf") if avg_return > 0 else 0.0
        else:
            sharpe = (avg_return / std) * math.sqrt(len(returns))

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    return TrendMetrics(
        trades=len(returns),
        wins=wins,
        winrate=winrate,
        avg_return=avg_return,
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
    )


def build_report(
    trades_by_symbol: dict[str, list[TrendTrade]],
    candles_by_symbol: dict[str, list[MarketData]],
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(tz=timezone.utc)
    all_trades = [trade for trades in trades_by_symbol.values() for trade in trades]
    all_candles = [candle for candles in candles_by_symbol.values() for candle in candles]
    overall = calculate_metrics(all_trades)
    go = (
        overall.sharpe > 1.0
        and overall.max_drawdown < 0.10
        and overall.winrate > 0.55
    )

    if all_candles:
        start = min(candle.timestamp for candle in all_candles).isoformat()
        end = max(candle.timestamp for candle in all_candles).isoformat()
    else:
        start = end = "n/a"

    lines = [
        "=== TREND FOLLOWING HARNESS RAPPORT ===",
        f"Generated at: {generated_at.isoformat()}",
        f"Periode: {start} -> {end}",
        f"Symbolen: {', '.join(SYMBOLS)}",
        f"Timeframe: {TIMEFRAME}",
        f"Totaal trades: {overall.trades}",
        "",
        "--- OVERALL ---",
        f"Sharpe: {overall.sharpe:.3f}" if math.isfinite(overall.sharpe) else "Sharpe: inf",
        f"Max DD: {_fmt_pct(overall.max_drawdown)}",
        f"Winrate: {_fmt_pct(overall.winrate)} ({overall.wins}/{overall.trades})",
        f"Avg return/trade: {_fmt_pct(overall.avg_return)}",
        f"Compounded return: {_fmt_pct(overall.total_return)}",
        "",
        "--- PER SYMBOL ---",
    ]

    for symbol in SYMBOLS:
        metrics = calculate_metrics(trades_by_symbol.get(symbol, []))
        candle_count = len(candles_by_symbol.get(symbol, []))
        lines.append(
            f"{symbol}: trades={metrics.trades} candles={candle_count} "
            f"winrate={_fmt_pct(metrics.winrate)} sharpe={metrics.sharpe:.3f} "
            f"max_dd={_fmt_pct(metrics.max_drawdown)} return={_fmt_pct(metrics.total_return)}"
        )

    exit_counts = Counter(trade.exit_reason for trade in all_trades)
    lines.extend(["", "--- EXIT REDENEN ---"])
    if exit_counts:
        for reason, count in exit_counts.most_common():
            pct = count / overall.trades if overall.trades else 0.0
            lines.append(f"{reason}: {count} ({_fmt_pct(pct)})")
    else:
        lines.append("Geen trades.")

    direction_counts = Counter(trade.direction for trade in all_trades)
    lines.extend(["", "--- DIRECTION MIX ---"])
    if direction_counts:
        for direction, count in direction_counts.items():
            lines.append(f"{direction}: {count}")
    else:
        lines.append("Geen trades.")

    lines.extend([
        "",
        "--- GO/NO-GO ---",
        "Criteria: Sharpe > 1.0 EN max DD < 10% EN winrate > 55%",
        f"Verdict: {'GO' if go else 'NO-GO'}",
    ])

    if not all_trades:
        lines.append("Reden: geen trades gegenereerd uit de opgehaalde candles.")

    return "\n".join(lines) + "\n"


def write_report(report: str, logs_root: Path | None = None) -> Path:
    logs_root = logs_root or Path(os.environ.get("ANT_LOGS", r"C:\Trading\ANT_LOGS"))
    out_dir = logs_root / "harness"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"trend_following_{stamp}.txt"
    out_path.write_text(report, encoding="utf-8")
    return out_path


def fetch_candles(adapter: BitvavoAdapter) -> dict[str, list[MarketData]]:
    candles_by_symbol: dict[str, list[MarketData]] = {}
    for symbol in SYMBOLS:
        try:
            candles = adapter.get_candles(symbol, TIMEFRAME, CANDLE_LIMIT)
        except Exception:
            candles = []
        candles_by_symbol[symbol] = candles or []
    return candles_by_symbol


def run_harness(adapter: BitvavoAdapter | None = None) -> tuple[str, Path]:
    _load_env()
    adapter = adapter or BitvavoAdapter(paper_only=True)
    candles_by_symbol = fetch_candles(adapter)
    trades_by_symbol = {
        symbol: simulate_symbol(symbol, candles)
        for symbol, candles in candles_by_symbol.items()
    }
    report = build_report(trades_by_symbol, candles_by_symbol)
    out_path = write_report(report)
    return report, out_path


def main() -> None:
    try:
        report, out_path = run_harness()
        print(report)
        print(f"Rapport opgeslagen: {out_path}")
    except Exception as exc:
        print("Trend-following harness faalde fail-closed.")
        print(f"Fout: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
