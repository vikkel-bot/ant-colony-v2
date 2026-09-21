"""
ant_colony/strategies/mean_reversion_v2.py

Additive research strategy for crypto mean reversion.

This module does not place orders and is not wired into live routing. It exposes
a small signal-generation interface that the audit layer can exercise with
walk-forward backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MeanReversionV2Parameters:
    zscore_entry_threshold: float = 1.5
    zscore_exit_threshold: float = 0.0
    rsi_period: int = 14
    rsi_oversold: int = 35
    rsi_overbought: int = 65
    rolling_window: int = 20
    atr_stop_multiplier: float = 1.5
    max_holding_hours: int = 48
    volume_confirmation: bool = True


@dataclass(frozen=True)
class Signal:
    asset: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    timestamp: datetime
    z_score: float
    rsi_at_entry: float
    volume_ratio: float


class MeanReversionStrategy:
    """Generate mean-reversion signals from OHLCV candles."""

    def __init__(
        self,
        asset: str = "",
        *,
        parameters: MeanReversionV2Parameters | None = None,
        allow_short: bool = False,
    ) -> None:
        self.asset = asset
        self.parameters = parameters or MeanReversionV2Parameters()
        self.allow_short = allow_short

    def name(self) -> str:
        return "mean_reversion_v2"

    def generate_signals(self, candles: Any) -> list[Signal]:
        """
        Generate candidate signals from a pandas DataFrame.

        Required columns: timestamp, open, high, low, close, volume. The asset is
        read from `candles.attrs["asset"]` when present, otherwise from the
        strategy constructor.
        """

        try:
            import pandas as pd  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised only without pandas
            raise RuntimeError("pandas is required for MeanReversionStrategy.generate_signals") from exc

        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(candles.columns)
        if missing:
            raise ValueError(f"candles missing required columns: {sorted(missing)}")

        df = candles.copy().sort_values("timestamp").reset_index(drop=True)
        params = self.parameters
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        rolling_mean = close.rolling(params.rolling_window).mean()
        rolling_std = close.rolling(params.rolling_window).std(ddof=0)
        z_score = (close - rolling_mean) / rolling_std.replace(0, float("nan"))
        rsi = _rsi(close.tolist(), params.rsi_period)
        atr = _atr_abs(
            high.tolist(),
            low.tolist(),
            close.tolist(),
            params.rsi_period,
        )
        volume_avg = volume.rolling(params.rolling_window).mean()
        volume_ratio = volume / volume_avg.replace(0, float("nan"))

        asset = str(df.attrs.get("asset") or self.asset or "")
        signals: list[Signal] = []
        next_allowed_idx = 0
        for idx in range(len(df)):
            if idx < next_allowed_idx:
                continue
            z = float(z_score.iloc[idx]) if z_score.notna().iloc[idx] else None
            current_rsi = float(rsi[idx]) if rsi[idx] is not None else None
            current_atr = float(atr[idx]) if atr[idx] is not None else None
            v_ratio = float(volume_ratio.iloc[idx]) if volume_ratio.notna().iloc[idx] else None
            mean = float(rolling_mean.iloc[idx]) if rolling_mean.notna().iloc[idx] else None
            price = float(close.iloc[idx])
            if None in (z, current_rsi, current_atr, v_ratio, mean) or current_atr <= 0:
                continue
            if params.volume_confirmation and v_ratio <= 1.0:
                continue

            direction = ""
            if z < -params.zscore_entry_threshold and current_rsi < params.rsi_oversold:
                direction = "long"
            elif (
                self.allow_short
                and z > params.zscore_entry_threshold
                and current_rsi > params.rsi_overbought
            ):
                direction = "short"
            if not direction:
                continue

            if direction == "long":
                stop_loss = price - (params.atr_stop_multiplier * current_atr)
            else:
                stop_loss = price + (params.atr_stop_multiplier * current_atr)
            signals.append(
                Signal(
                    asset=asset,
                    direction=direction,
                    entry_price=price,
                    stop_loss=stop_loss,
                    take_profit=mean,
                    timestamp=df["timestamp"].iloc[idx],
                    z_score=z,
                    rsi_at_entry=current_rsi,
                    volume_ratio=v_ratio,
                )
            )
            next_allowed_idx = idx + max(1, params.max_holding_hours)
        return signals


def _rsi(closes: list[float], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(closes)
    for idx in range(period, len(closes)):
        window = closes[idx - period : idx + 1]
        gains = [max(0.0, window[i] - window[i - 1]) for i in range(1, len(window))]
        losses = [max(0.0, window[i - 1] - window[i]) for i in range(1, len(window))]
        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0:
            values[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            values[idx] = 100.0 - (100.0 / (1.0 + rs))
    return values


def _atr_abs(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(closes)
    true_ranges: list[float] = []
    for idx in range(1, len(closes)):
        tr = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )
        true_ranges.append(tr)
        if idx >= period:
            values[idx] = sum(true_ranges[-period:]) / period
    return values

