"""
ant_colony/ants/_heartbeat.py

HeartbeatThread — stuurt heartbeats onafhankelijk van tick-duur.

Probleem: als _tick() langer duurt dan heartbeat_interval (bijv. 121s bij
interval=60s) wordt de agent door de scheduler als stale beschouwd en afgebroken.

Oplossing: een daemon-thread die elke heartbeat_interval seconden
_send_heartbeat() aanroept, ongeacht wat de tick-loop doet.

Gebruik:
    _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
    _hb.start()
    try:
        ...tick-loop...
    finally:
        _hb.stop()
        self._send_heartbeat()   # finale heartbeat

Regels:
    - Daemon thread — sluit automatisch als het hoofdproces eindigt
    - Fail-closed (P2) — exceptions in _send_heartbeat worden genegeerd
    - Stop-event zorgt voor nette afsluiting zonder race conditions
    - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from ant_colony.schemas.ant import AntStatus


@runtime_checkable
class _AntProtocol(Protocol):
    """Minimaal interface dat HeartbeatThread verwacht van een ant."""
    ant_id: str
    _status: AntStatus

    def _send_heartbeat(self) -> None: ...


class HeartbeatThread(threading.Thread):
    """
    Achtergrond-thread die heartbeats stuurt ongeacht tick-duur.

    Args:
        ant:      De ant-instantie. Moet `ant_id`, `_status` en
                  `_send_heartbeat()` hebben.
        interval: Seconden tussen heartbeats (= mission.heartbeat_interval).
    """

    def __init__(self, ant: _AntProtocol, interval: float) -> None:
        super().__init__(
            daemon=True,
            name=f"hb-{ant.ant_id[:8]}",
        )
        self._ant        = ant
        self._interval   = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Heartbeat-loop: wacht interval seconden, stuur heartbeat, herhaal."""
        while not self._stop_event.wait(self._interval):
            if self._ant._status != AntStatus.RUNNING:
                break
            try:
                self._ant._send_heartbeat()
            except Exception:
                pass  # fail-closed P2 — heartbeat mag nooit de thread crashen

    def stop(self) -> None:
        """Signaleer stop en wacht tot thread klaar is (max interval + 1s)."""
        self._stop_event.set()
        if self is not threading.current_thread() and self._started.is_set():
            self.join(timeout=self._interval + 1.0)
