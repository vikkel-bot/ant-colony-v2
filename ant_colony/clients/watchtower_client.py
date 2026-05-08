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
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data["signals"]
            _log.warning("Onverwacht signaalformaat van Watchtower")
            return []
        except Exception:
            self.last_get_succeeded = False
            _log.warning(
                "Watchtower get_signals mislukt (%s) — degrading gracefully",
                self.base_url,
            )
            return []

    def get_backtest_signals(
        self,
        limit: int = 1000,
        asset: str | None = None,
        asset_class: str | None = None,
        exchange: str | None = None,
        region: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> dict[str, Any]:
        """
        GET /backtest/signals for immutable replay input.

        Returns Watchtower's export packet when available. On any error, returns
        an empty packet and marks last_get_succeeded=False so callers can fail
        gracefully without breaking the colony.
        """
        params: dict[str, Any] = {"limit": limit}
        if asset:
            params["asset"] = asset
        if asset_class:
            params["asset_class"] = asset_class
        if exchange:
            params["exchange"] = exchange
        if region:
            params["region"] = region
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts

        try:
            resp = httpx.get(
                f"{self.base_url}/backtest/signals",
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self.last_get_succeeded = True
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data
            return self._empty_backtest_export(params)
        except Exception:
            self.last_get_succeeded = False
            _log.warning(
                "Watchtower get_backtest_signals mislukt (%s) — degrading gracefully",
                self.base_url,
            )
            return self._empty_backtest_export(params)

    def get_seed_signals(
        self,
        asset: str | None = None,
        from_dt: str | None = None,
        to_dt: str | None = None,
        min_entry_score: float = 0.5,
        limit: int = 250,
    ) -> list[dict]:
        """
        GET /backtest/signals/seed for historical seeded replay signals.

        Returns a list of signal dicts. On any error, returns [] and logs a
        warning so the colony can continue without Watchtower seed input.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "min_entry_score": min_entry_score,
        }
        if asset:
            params["asset"] = asset
        if from_dt:
            params["from_dt"] = from_dt
        if to_dt:
            params["to_dt"] = to_dt

        try:
            resp = httpx.get(
                f"{self.base_url}/backtest/signals/seed",
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            self.last_get_succeeded = True
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("signals"), list):
                return data["signals"]
            return []
        except Exception:
            self.last_get_succeeded = False
            _log.warning(
                "Watchtower get_seed_signals mislukt (%s) — degrading gracefully",
                self.base_url,
            )
            return []

    def post_outcome(self, outcome: dict) -> bool:
        """
        POST /colony/feedback

        Fire-and-forget feedback. Nooit blocken op het resultaat.

        Returns:
            True als succesvol, False bij fout (nooit exception).
        """
        try:
            resp = httpx.post(
                f"{self.base_url}/colony/feedback",
                json=outcome,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            _log.info(
                "Watchtower feedback response | status_code=%s signal_id=%s",
                resp.status_code,
                outcome.get("signal_id"),
            )
            return True
        except httpx.HTTPStatusError as exc:
            status_code = getattr(exc.response, "status_code", "unknown")
            _log.warning(
                "Watchtower post_outcome mislukt | status_code=%s signal_id=%s",
                status_code,
                outcome.get("signal_id"),
            )
            return False
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

    def _empty_backtest_export(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "source": "watchtower",
            "export_type": "signals",
            "generated_at": None,
            "count": 0,
            "filters": filters or {},
            "signals": [],
        }
