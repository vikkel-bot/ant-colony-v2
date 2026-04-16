"""
ant_colony/biome/crypto_adapter.py

CryptoAdapter — Bitvavo implementatie van BiomeAdapter.

Wraps de Bitvavo REST API achter het BiomeAdapter Protocol.
Fase 10: paper_only=True (standaard) blokkeert altijd live execution.
Live orders vereisen expliciete Queen promotie naar paper_only=False.

Bitvavo API-bibliotheek: python-bitvavo-api
Import is lazy (binnen _build_client) zodat import nooit faalt zonder library.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ant_colony.biome.biome_adapter import AccountState, LivePosition, MarketData
from ant_colony.schemas.order import LiveOrder, OrderRejectionReason, OrderResult

log = logging.getLogger(__name__)

_TIMEFRAME_MAP: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h",
    "8h": "8h", "12h": "12h", "1d": "1d",
}


class CryptoAdapter:
    """
    Bitvavo implementatie van BiomeAdapter.

    paper_only=True (standaard): place_order() wordt altijd geblokkeerd.
    Elke methode gooit nooit — retourneert None bij onverwachte fout (fail-closed).

    Args:
        api_key:    Bitvavo API key (leeg bij paper-only gebruik zonder account data).
        api_secret: Bitvavo API secret.
        paper_only: Als True, blokkeert place_order() altijd. Standaard True.
        _client:    Optioneel pre-built client-object (voor tests).
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper_only: bool = True,
        _client: Any = None,
    ) -> None:
        self._paper_only = paper_only
        self._client: Any = _client if _client is not None else self._build_client(api_key, api_secret)

    def _build_client(self, api_key: str, api_secret: str) -> Any:
        from python_bitvavo_api.bitvavo import Bitvavo  # lazy import
        return Bitvavo({"APIKEY": api_key, "APISECRET": api_secret})

    # -----------------------------------------------------------------------
    # BiomeAdapter protocol
    # -----------------------------------------------------------------------

    @property
    def biome_id(self) -> str:
        return "crypto"

    def is_available(self) -> bool:
        """
        True als Bitvavo bereikbaar is.

        Fail-closed: bij elke twijfel False.
        """
        try:
            result = self._client.time()
            return isinstance(result, dict) and "time" in result
        except Exception:
            log.exception("CryptoAdapter.is_available: connectiefout")
            return False

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        """
        Haal de meest recente gesloten candle op voor symbol/timeframe.

        Bitvavo candle-formaat: [timestamp_ms, open, high, low, close, volume]
        Fout of onbekend timeframe → None.
        """
        interval = _TIMEFRAME_MAP.get(timeframe)
        if not interval:
            log.warning("CryptoAdapter.get_market_data: onbekend timeframe %r", timeframe)
            return None
        try:
            candles = self._client.candles(symbol, interval, {"limit": 1})
            if not candles or isinstance(candles, dict):
                return None
            ts_ms, open_, high, low, close, volume = candles[0]
            return MarketData(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc),
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
                biome_id=self.biome_id,
            )
        except Exception:
            log.exception("CryptoAdapter.get_market_data: fout voor %s/%s", symbol, timeframe)
            return None

    def get_account_state(self) -> AccountState | None:
        """
        Haal EUR balance en locked-in-orders op als AccountState.

        balance         = EUR beschikbaar
        positions_value = EUR vergrendeld in open orders (inOrder)
        """
        try:
            balances = self._client.balance({})
            if isinstance(balances, dict):
                return None
            eur_available = 0.0
            eur_in_order = 0.0
            for entry in balances:
                if entry.get("symbol") == "EUR":
                    eur_available = float(entry.get("available", 0))
                    eur_in_order = float(entry.get("inOrder", 0))
                    break
            return AccountState(
                biome_id=self.biome_id,
                balance=eur_available,
                positions_value=eur_in_order,
                timestamp=datetime.now(tz=timezone.utc),
                currency="EUR",
            )
        except Exception:
            log.exception("CryptoAdapter.get_account_state: fout")
            return None

    def place_order(self, order: LiveOrder) -> OrderResult | None:
        """
        Fase 10: altijd geblokkeerd wanneer paper_only=True.

        Live execution (paper_only=False) is gereserveerd voor fase 11+
        na expliciete Queen promotie.
        """
        if self._paper_only:
            return OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.EXCHANGE_REJECTED,
                detail="paper_only=True — live execution niet toegestaan (fase 10)",
            )
        return None

    def get_positions(self) -> list[LivePosition] | None:
        """
        Bitvavo is een spot-exchange — geen margin posities.

        Retourneert altijd een lege lijst (geen open posities in de
        traditionele zin). Fase 11+ kan dit uitbreiden met eigen-vermogen
        tracking op basis van balances.
        """
        return []
