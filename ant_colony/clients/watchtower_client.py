"""
ant_colony/clients/watchtower_client.py

WatchtowerClient — HTTP client voor Watchtower entry-intelligence service.

Watchtower is een lokale FastAPI service (standaard http://127.0.0.1:8011).
Colony is altijd de beslisser/executor; Watchtower is alleen entry-intelligence.

Kritieke eigenschap: als Watchtower offline is, retourneert de client stille
defaults ([] of False) zonder exceptions te gooien. Colony draait altijd door.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

_log = logging.getLogger("watchtower.client")


class WatchtowerClient:
    """
    Sync HTTP client voor de Watchtower service.

    Thread-safe: alle methoden zijn sync en veilig vanuit meerdere threads.

    Args:
        base_url: Base URL van Watchtower. Default uit WATCHTOWER_URL env var.
        timeout:  Max wachttijd per request in seconden. Default uit WATCHTOWER_TIMEOUT.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
    ) -> None:
        self.base_url = (
            (base_url or os.getenv("WATCHTOWER_URL", "http://127.0.0.1:8011")).rstrip("/")
        )
        self.timeout = timeout if timeout is not None else int(
            os.getenv("WATCHTOWER_TIMEOUT", "5")
        )
        self.last_get_succeeded: bool = True

    def get_signals(self, limit: int = 50) -> list[dict]:
        """
        GET /colony/signals?limit=<limit>

        Returns:
            Lijst van signal-dicts. Lege lijst als Watchtower offline of bij fout.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/colony/signals",
                params={"limit": limit},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self.last_get_succeeded = True
            data = resp.json()
            if isinstance(data, list):
                return data
            return []
        except Exception:
            self.last_get_succeeded = False
            _log.warning(
                "Watchtower get_signals mislukt (%s) — degrading gracefully",
                self.base_url,
            )
            return []

    def post_outcome(self, outcome: dict) -> bool:
        """
        POST /outcomes/evaluate

        Fire-and-forget feedback. Nooit blocken op het resultaat.

        Returns:
            True als succesvol, False bij fout (nooit exception).
        """
        try:
            resp = httpx.post(
                f"{self.base_url}/outcomes/evaluate",
                json=outcome,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except Exception:
            _log.warning(
                "Watchtower post_outcome mislukt — outcome genegeerd (signal_id=%s)",
                outcome.get("signal_id"),
            )
            return False

    def is_healthy(self) -> bool:
        """
        GET /health — sync health check voor startup en offline-detectie.

        Returns:
            True als Watchtower bereikbaar en gezond is.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/health",
                timeout=self.timeout,
            )
            return resp.status_code == 200
        except Exception:
            return False
