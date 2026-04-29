"""
ant_colony/ants/watchtower_ant.py

WatchtowerAnt — pollt Watchtower entry-intelligence en filtert bruikbare signalen.

Verantwoordelijkheden:
  1. Poll Watchtower elke WATCHTOWER_POLL_INTERVAL seconden (standaard 300s).
  2. Filter signalen: entry_score >= min, confidence >= min, geen HIGH_RISK.
  3. Schrijf gefilterde signalen naar ANT_LOGS/watchtower/signals.jsonl.
  4. Log hoeveel signalen ontvangen en hoeveel door filter gekomen.
  5. Log "Watchtower offline, degrading gracefully" elke poll als offline.

Regels:
  - Geen live orders, geen paper posities (pure observatie).
  - Gooit nooit een exception naar buiten.
  - Als Watchtower offline: colony draait gewoon door.
  - Één JSONL-regel per poll (append-only).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.clients.watchtower_client import WatchtowerClient
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Constanten (overschrijfbaar via env vars)
# ---------------------------------------------------------------------------

_POLL_INTERVAL  = int(os.getenv("WATCHTOWER_POLL_INTERVAL", "300"))
_MIN_ENTRY_SCORE = float(os.getenv("WATCHTOWER_MIN_ENTRY_SCORE", "0.6"))
_MIN_CONFIDENCE  = float(os.getenv("WATCHTOWER_MIN_CONFIDENCE", "0.5"))


class WatchtowerAnt:
    """
    Entry-intelligence monitor die Watchtower pollt en signalen filtert.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission (capital_limit=0, observatie-only).
        scheduler: ColonyScheduler voor heartbeats.
        logs_root: Pad naar ANT_LOGS root directory.
        client:    WatchtowerClient instantie.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None,
        client: WatchtowerClient,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root
        self._client   = client

        self._log = logging.getLogger(f"ant.watchtower.{ant_id[:8]}")
        self._out_dir = (Path(logs_root) / "watchtower") if logs_root else None

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Hoofdlus: poll elke WATCHTOWER_POLL_INTERVAL seconden. Blokkeert indefinitely."""
        if self._out_dir is not None:
            self._out_dir.mkdir(parents=True, exist_ok=True)

        self._log.info(
            "WatchtowerAnt gestart | ant_id=%s  interval=%ds  url=%s",
            self.ant_id, _POLL_INTERVAL, self._client.base_url,
        )
        while True:
            try:
                self._tick()
            except Exception:
                self._log.exception("WatchtowerAnt tick onverwachte fout — gaat door")
            time.sleep(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Interne methoden
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        signals = self._client.get_signals()

        # Offline detectie: get_signals() zet last_get_succeeded=False bij fout
        if not self._client.last_get_succeeded:
            self._log.warning(
                "Watchtower offline, degrading gracefully | url=%s",
                self._client.base_url,
            )
            return

        received = len(signals)
        filtered: list[dict] = []
        rejections: list[dict] = []
        for signal in signals:
            reasons: list[str] = []
            if signal.get("entry_score", 0.0) < _MIN_ENTRY_SCORE:
                reasons.append("score too low")
            if signal.get("confidence", 0.0) < _MIN_CONFIDENCE:
                reasons.append("confidence too low")
            if "HIGH_RISK" in signal.get("risk_flags", []):
                reasons.append("risk_flag HIGH_RISK")

            if reasons:
                rejections.append({
                    "signal_id": signal.get("signal_id") or signal.get("id"),
                    "asset": signal.get("asset") or signal.get("symbol"),
                    "direction": signal.get("direction"),
                    "timestamp": signal.get("timestamp") or signal.get("created_at"),
                    "entry_score": signal.get("entry_score"),
                    "confidence": signal.get("confidence"),
                    "risk_flags": signal.get("risk_flags", []),
                    "rejection_reason": "; ".join(reasons),
                })
            else:
                filtered.append(signal)

        self._log.info(
            "Watchtower poll | ontvangen=%d  door_filter=%d",
            received, len(filtered),
        )

        # altijd schrijven — ook bij 0 gefilterd (voor dashboard stats)
        self._write_snapshot(now, received, filtered, rejections)

    def _write_snapshot(
        self,
        now: datetime,
        total_received: int,
        filtered: list[dict],
        rejections: list[dict] | None = None,
    ) -> None:
        """Schrijf poll-resultaat naar ANT_LOGS/watchtower/signals.jsonl."""
        if self._out_dir is None:
            return

        self._out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp":     now.strftime("%Y-%m-%dT%H:%M:%S"),
            "received":      total_received,
            "passed_filter": len(filtered),
            "signals":       filtered,
            "rejections":    rejections or [],
        }
        log_path = self._out_dir / "signals.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._log.exception("Kon watchtower snapshot niet schrijven: %s", log_path)
