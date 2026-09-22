"""Sensor T001: 3-weeks prijsmomentum (docs/PREREG_T001_MOMENTUM21_20260922.md).

Geïsoleerde functie. Gebruikt uitsluitend candles met timestamp < t.
"""

from __future__ import annotations

import bisect
from typing import Sequence

DAY_MS = 86_400_000
LOOKBACK_DAYS = 21


def _close_before(ts: list[int], candles: Sequence[Sequence], t_ms: int) -> float | None:
    i = bisect.bisect_left(ts, t_ms)
    return float(candles[i - 1][4]) if i else None


def mom21(candles: Sequence[Sequence], t_ms: int) -> float | None:
    """close(laatste candle < t) / close(laatste candle < t - 21d) - 1."""
    ts = [int(c[0]) for c in candles]
    latest = _close_before(ts, candles, t_ms)
    then = _close_before(ts, candles, t_ms - LOOKBACK_DAYS * DAY_MS)
    if latest is None or then is None or then <= 0:
        return None
    return latest / then - 1.0
