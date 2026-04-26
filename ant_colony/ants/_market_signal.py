"""
ant_colony/ants/_market_signal.py

Leest het meest recente gecombineerde markt-signaal uit ANT_LOGS/queen/market_signal.jsonl.

Formaat (append-only JSONL):
    {"timestamp": "...", "payload": {
        "action":               "market_signal",
        "regime":               "SIDEWAYS|TRENDING|VOLATILE",
        "news_sentiment":       "bullish|bearish|neutral",
        "combined_signal":      "normal|optimistic|cautious|cautious_trending|restrictive",
        "position_size_mult":   float,
        "sl_mult":              float,
    }}

Fail-open: als het bestand niet bestaat of geen geldig signaal bevat,
retourneert de functie None. Callers behandelen None als standaard (1.0 / 1.0).
"""

from __future__ import annotations

import json
from pathlib import Path

_VALID_SIGNALS = frozenset([
    "normal", "optimistic", "cautious", "cautious_trending", "restrictive",
])


def read_latest_market_signal(logs_root: Path) -> dict | None:
    """
    Lees het meest recente markt-signaal uit ANT_LOGS/queen/market_signal.jsonl.

    Returns:
        Dict met keys: combined_signal, position_size_mult, sl_mult, regime, news_sentiment.
        None als geen geldig signaal beschikbaar is.
    """
    signal_path = logs_root / "queen" / "market_signal.jsonl"
    if not signal_path.exists():
        return None
    try:
        last_line: str | None = None
        with signal_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if last_line is None:
            return None
        rec = json.loads(last_line)
        payload = rec.get("payload") or {}
        if payload.get("action") != "market_signal":
            return None
        return payload
    except (OSError, json.JSONDecodeError):
        return None
