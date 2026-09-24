"""Bewaakt dat de Watchtower-state atomair wordt weggeschreven.

Achtergrond: de Queen heeft een achtergrondthread die dezelfde state wegschrijft
als flush_watchtower_state(). Met path.write_text() wordt het bestand eerst
afgekapt en daarna gevuld; een lezer die daartussen komt, krijgt een leeg
bestand. Dat brak CI op 24-09-2026 (JSONDecodeError op een lege string) terwijl
het lokaal groen was — klassieke timingafhankelijkheid.

Deze test leest honderden keren terwijl er continu geschreven wordt. Met
write_text faalt hij vrijwel altijd; met os.replace nooit.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

from ant_colony.queen.queen import Queen
from ant_colony.tools.atomic_io import read_text_with_retry


def _queen(tmp_path: Path) -> Queen:
    return Queen(capital_total=10_000.0, scheduler=MagicMock(), logs_root=tmp_path)


def _writer(queen: Queen, stop: threading.Event) -> None:
    n = 0
    while not stop.is_set():
        n += 1
        queen.register_watchtower_signal({
            "signal_id": f"sig-{n}",
            "asset": "AAPL",
            "asset_class": "equities",
            "entry_score": 0.5,
            "confidence": 0.5,
            "regime": "RISK_ON",
            "risk_flags": [],
            "queen_accepted": True,
        })
        queen.flush_watchtower_state()


def test_state_file_is_never_partially_written(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANT_LOGS", str(tmp_path))
    queen = _queen(tmp_path)
    queen.register_watchtower_signal({
        "signal_id": "sig-0", "asset": "AAPL", "asset_class": "equities",
        "entry_score": 0.5, "confidence": 0.5, "regime": "RISK_ON",
        "risk_flags": [], "queen_accepted": True,
    })
    queen.flush_watchtower_state()
    path = tmp_path / "queen" / "watchtower_state.json"
    assert path.exists()

    stop = threading.Event()
    t = threading.Thread(target=_writer, args=(queen, stop), daemon=True)
    t.start()
    try:
        for _ in range(500):
            text = read_text_with_retry(path)
            assert text != "", "lezer zag een leeg state-bestand (niet-atomaire write)"
            state = json.loads(text)          # mag nooit een JSONDecodeError geven
            assert state["last_asset"] == "AAPL"
    finally:
        stop.set()
        t.join(timeout=5)


def test_no_temp_file_is_left_behind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANT_LOGS", str(tmp_path))
    queen = _queen(tmp_path)
    queen.register_watchtower_signal({
        "signal_id": "sig-1", "asset": "BTC-EUR", "asset_class": "crypto",
        "entry_score": 0.6, "confidence": 0.6, "regime": "RISK_ON",
        "risk_flags": [], "queen_accepted": True,
    })
    queen.flush_watchtower_state()
    leftovers = list((tmp_path / "queen").glob("*.tmp"))
    assert not leftovers, f"tijdelijke bestanden blijven staan: {leftovers}"
