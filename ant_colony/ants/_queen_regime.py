"""
ant_colony/ants/_queen_regime.py

Leest het meest recente Queen regime-signaal uit ANT_LOGS/queen/regime.jsonl.

Formaat (append-only JSONL):
    {"timestamp": "...", "payload": {"action": "regime_signal",
                                     "regime": "SIDEWAYS|TRENDING|VOLATILE", ...}}

Fail-open: als het bestand niet bestaat of geen geldig signaal bevat,
retourneert de functie None. PaperAnt behandelt None als TRENDING (alle
entries toegestaan).
"""

from __future__ import annotations

import json
from pathlib import Path

_VALID_REGIMES = frozenset(["SIDEWAYS", "TRENDING", "VOLATILE", "RISK_ON"])


def read_latest_queen_regime(logs_root: Path) -> str | None:
    """
    Lees het meest recente regime-signaal uit ANT_LOGS/queen/regime.jsonl.

    Returns:
        "SIDEWAYS", "TRENDING", "VOLATILE", of None als geen signaal.
        None → fail-open (PaperAnt behandelt als TRENDING).
    """
    regime_path = logs_root / "queen" / "regime.jsonl"
    if not regime_path.exists():
        return None
    try:
        last_line: str | None = None
        with regime_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if last_line is None:
            return None
        rec = json.loads(last_line)
        payload = rec.get("payload") or {}
        if payload.get("action") != "regime_signal":
            return None
        regime = str(payload.get("regime", "")).upper()
        return regime if regime in _VALID_REGIMES else None
    except (OSError, json.JSONDecodeError):
        return None
