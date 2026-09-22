"""Point-in-time crypto-universum U(t) — Bitvavo EUR.

Bevroren volgens docs/PREREG_UNIVERSE_v2_20260922.md. Wijzigingen alleen via
een nieuwe pre-registratie.

U(t) gebruikt uitsluitend candles met timestamp < t. Geen wall-clock.
Candles in Bitvavo-formaat: [timestamp_ms, open, high, low, close, volume],
volume in de basismunt, oplopend gesorteerd, zonder de lopende (onvolledige) dag.
"""

from __future__ import annotations

import bisect
import statistics
from typing import Mapping, Sequence

DAY_MS = 86_400_000
MIN_HISTORY_DAYS = 180
LIQ_WINDOW_DAYS = 30
MIN_TURNOVER_EUR = 50_000
Q_DENOMINATOR = 5  # bovenste fractie q = 1/5
MIN_K = 8

EXCLUDED_BASES = frozenset({
    # stablecoins
    "USDT", "USDC", "EURC", "EURT", "DAI", "TUSD", "BUSD", "PYUSD", "FDUSD",
    "USDE", "USDS", "EURS", "EURR", "USDP", "GUSD", "LUSD", "FRAX", "USDD",
    "EURCV", "EUROP", "USDCV",
    # wrapped / liquid staking / gepegd aan iets buiten crypto
    "WBTC", "WETH", "STETH", "WSTETH", "CBETH", "RETH", "BETH", "PAXG", "XAUT",
})

Candle = Sequence  # [ts, o, h, l, c, v]


def base_of(market: str) -> str:
    return market.split("-", 1)[0].upper()


def eur_turnover(candle: Candle) -> float:
    return float(candle[5]) * float(candle[4])


def is_admitted(candles: Sequence[Candle], t_ms: int) -> bool:
    """Historie >= 180 dagen en mediane EUR-omzet over [t-30d, t) >= drempel."""
    ts = [int(c[0]) for c in candles]
    i = bisect.bisect_left(ts, t_ms)  # candles [0, i) liggen strikt vóór t
    if i == 0 or ts[0] > t_ms - MIN_HISTORY_DAYS * DAY_MS:
        return False
    j = bisect.bisect_left(ts, t_ms - LIQ_WINDOW_DAYS * DAY_MS)
    window = [eur_turnover(c) for c in candles[j:i]]
    return bool(window) and statistics.median(window) >= MIN_TURNOVER_EUR


def universe(candles_by_market: Mapping[str, Sequence[Candle]], t_ms: int) -> frozenset[str]:
    """U(t): verzameling toegelaten markten op tijdstip t."""
    return frozenset(
        m for m, c in candles_by_market.items()
        if c and base_of(m) not in EXCLUDED_BASES and is_admitted(c, t_ms)
    )


def top_k(n: int) -> int:
    """Grootte bovenste groep: max(8, n // 5). Integer-deling, geen floats."""
    return max(MIN_K, n // Q_DENOMINATOR)
