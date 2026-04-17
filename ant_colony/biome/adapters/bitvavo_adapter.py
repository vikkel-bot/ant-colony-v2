"""
ant_colony/biome/adapters/bitvavo_adapter.py

BitvavoAdapter — productie-implementatie van BiomeAdapter voor Bitvavo.

Leest configuratie uitsluitend via environment variabelen:
  BITVAVO_API_KEY     — Bitvavo REST API key
  BITVAVO_API_SECRET  — Bitvavo REST API secret
  BITVAVO_PAPER_MODE  — "true" = paper mode (default), "false" = live execution

Paper mode gedrag:
  - get_market_data() en get_account_state() raken de echte API (read-only).
  - place_order() simuleert een fill op de huidige mid-price — geen echte order.
  - get_positions() werkt normaal (berekent spot-holdings).

Live mode (BITVAVO_PAPER_MODE=false):
  - place_order() plaatst echte orders via Bitvavo REST.
  - Vereist BITVAVO_API_KEY en BITVAVO_API_SECRET.

Fail-closed regels:
  - Elke methode gooit nooit — vang alles op, log, return None / [].
  - is_available() geeft False bij elke twijfel.
  - Bij ontbrekende of lege API keys → log warning, client wordt aangemaakt
    maar authenticated calls falen gracefully.

Bitvavo candle formaat: [timestamp_ms, open, high, low, close, volume]
Bitvavo order response: {"orderId": str, "filledAmount": str, "price": str, ...}
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from ant_colony.biome.biome_adapter import AccountState, LivePosition, MarketData
from ant_colony.schemas.order import (
    LiveOrder,
    OrderRejectionReason,
    OrderResult,
    OrderSide,
    OrderType,
)

log = logging.getLogger(__name__)

# Bitvavo ondersteunt deze intervals natively
_TIMEFRAME_MAP: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "6h":  "6h",
    "8h":  "8h",
    "12h": "12h",
    "1d":  "1d",
}

# Minimale EUR-waarde om een balance als "positie" te tellen (stofdeeltjes negeren)
_MIN_POSITION_EUR = 1.0


def _env_paper_mode() -> bool:
    """Lees BITVAVO_PAPER_MODE; standaard True (fail-safe)."""
    val = os.getenv("BITVAVO_PAPER_MODE", "true").strip().lower()
    return val != "false"


class BitvavoAdapter:
    """
    Productie BiomeAdapter voor Bitvavo crypto exchange.

    Gebruik via environment variabelen — nooit API keys doorgeven als argument
    behalve voor tests (via _client inject).

    Args:
        paper_only: Overschrijft BITVAVO_PAPER_MODE als expliciet meegegeven.
                    None = lees uit environment (standaard gedrag).
        _client:    Injecteerbaar Bitvavo-client object voor unit tests.
                    Als None, wordt client gebouwd via _build_client().
    """

    def __init__(
        self,
        paper_only: bool | None = None,
        _client: Any = None,
    ) -> None:
        self._paper_only: bool = paper_only if paper_only is not None else _env_paper_mode()

        if self._paper_only:
            log.info("BitvavoAdapter gestart in PAPER MODE — orders worden gesimuleerd")
        else:
            log.warning(
                "BitvavoAdapter gestart in LIVE MODE — echte orders worden geplaatst"
            )

        self._client: Any = _client if _client is not None else self._build_client()

    # -----------------------------------------------------------------------
    # Client constructie
    # -----------------------------------------------------------------------

    def _build_client(self) -> Any:
        """Lazy import van python-bitvavo-api; lees keys uit environment."""
        api_key = os.getenv("BITVAVO_API_KEY", "")
        api_secret = os.getenv("BITVAVO_API_SECRET", "")

        if not api_key or not api_secret:
            log.warning(
                "BITVAVO_API_KEY of BITVAVO_API_SECRET niet gezet — "
                "authenticated calls (balance, orders) zullen falen"
            )

        from python_bitvavo_api.bitvavo import Bitvavo  # noqa: PLC0415
        return Bitvavo({"APIKEY": api_key, "APISECRET": api_secret})

    # -----------------------------------------------------------------------
    # BiomeAdapter protocol
    # -----------------------------------------------------------------------

    @property
    def biome_id(self) -> str:
        return "crypto"

    def is_available(self) -> bool:
        """
        True als Bitvavo REST API bereikbaar is.

        Gebruikt het unauthenticated /time endpoint — werkt zonder API keys.
        Fail-closed: False bij elke twijfel.
        """
        try:
            result = self._client.time()
            available = isinstance(result, dict) and "time" in result
            if not available:
                log.warning("BitvavoAdapter.is_available: onverwacht antwoord: %r", result)
            return available
        except Exception:
            log.exception("BitvavoAdapter.is_available: connectiefout")
            return False

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        """
        Haal de meest recente afgesloten candle op voor symbol/timeframe.

        Bitvavo candle: [timestamp_ms, open, high, low, close, volume]
        Onbekend timeframe of API-fout → None.
        """
        interval = _TIMEFRAME_MAP.get(timeframe)
        if not interval:
            log.warning(
                "BitvavoAdapter.get_market_data: onbekend timeframe %r voor %s",
                timeframe, symbol,
            )
            return None

        try:
            candles = self._client.candles(symbol, interval, {"limit": 1})

            # Bitvavo geeft een dict terug bij een API-fout ({"errorCode": ..., "error": ...})
            if not candles or isinstance(candles, dict):
                log.warning(
                    "BitvavoAdapter.get_market_data: geen data voor %s/%s — %r",
                    symbol, timeframe, candles,
                )
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
            log.exception(
                "BitvavoAdapter.get_market_data: fout voor %s/%s", symbol, timeframe
            )
            return None

    def get_account_state(self) -> AccountState | None:
        """
        Haal EUR balance en in-order waarde op als AccountState.

        balance         = EUR beschikbaar (vrij te besteden)
        positions_value = EUR vergrendeld in open orders (inOrder)

        Vereist geldige API keys. Fout of ontbrekende keys → None.
        """
        try:
            balances = self._client.balance({})

            if isinstance(balances, dict):
                # Bitvavo stuurt dict terug bij auth-fout of rate-limit
                log.warning(
                    "BitvavoAdapter.get_account_state: auth-fout of rate-limit — %r",
                    balances,
                )
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
            log.exception("BitvavoAdapter.get_account_state: fout")
            return None

    def place_order(self, order: LiveOrder) -> OrderResult | None:
        """
        Plaatst een order via Bitvavo of simuleert het in paper mode.

        Paper mode: simuleert een volledige fill op de huidige marktprijs.
          - Haalt de actuele close-prijs op via get_market_data().
          - Geeft OrderResult.accepted_result() terug zonder echte order.
          - Faalt graceful als marktdata niet beschikbaar is.

        Live mode: plaatst echte order via Bitvavo REST.
          - MARKET order: direct geplaatst op marktprijs.
          - LIMIT order: geplaatst op limit_price.
          - Exchange-afwijzing → OrderResult.rejected() met EXCHANGE_REJECTED.
          - Onverwachte fout → None (fail-closed).
        """
        if self._paper_only:
            return self._simulate_order(order)
        return self._place_live_order(order)

    def get_positions(self) -> list[LivePosition] | None:
        """
        Berekent open spot-posities op basis van niet-EUR balances.

        Bitvavo is een spot-exchange zonder margin. "Posities" zijn crypto-
        holdings met een EUR-waarde boven _MIN_POSITION_EUR.

        Voor elk non-EUR asset:
          1. Haal balance op.
          2. Haal huidige EUR-prijs op via get_market_data("{ASSET}-EUR", "1m").
          3. Sla assets over waarvan de EUR-waarde < _MIN_POSITION_EUR.
          4. Construeer LivePosition met entry_price = current_price
             (spot heeft geen gekende entry — huidige prijs als benadering).

        Fout → None. Lege holdings → [].
        """
        try:
            balances = self._client.balance({})
            if isinstance(balances, dict):
                log.warning(
                    "BitvavoAdapter.get_positions: auth-fout — %r", balances
                )
                return None

            positions: list[LivePosition] = []
            now = datetime.now(tz=timezone.utc)

            for entry in balances:
                symbol_asset = entry.get("symbol", "")
                if symbol_asset == "EUR":
                    continue

                total = float(entry.get("available", 0)) + float(entry.get("inOrder", 0))
                if total <= 0:
                    continue

                market_symbol = f"{symbol_asset}-EUR"
                market_data = self.get_market_data(market_symbol, "1m")
                if not market_data or not market_data.is_valid_price:
                    log.debug(
                        "BitvavoAdapter.get_positions: geen prijs voor %s — overgeslagen",
                        market_symbol,
                    )
                    continue

                eur_value = total * market_data.close
                if eur_value < _MIN_POSITION_EUR:
                    continue

                positions.append(
                    LivePosition(
                        position_id=f"spot-{symbol_asset.lower()}",
                        symbol=market_symbol,
                        biome_id=self.biome_id,
                        side="buy",          # spot holdings zijn altijd long
                        quantity=total,
                        entry_price=market_data.close,   # benadering: geen historische entry
                        current_price=market_data.close,
                        timestamp=now,
                    )
                )

            return positions
        except Exception:
            log.exception("BitvavoAdapter.get_positions: fout")
            return None

    # -----------------------------------------------------------------------
    # Interne helpers
    # -----------------------------------------------------------------------

    def _simulate_order(self, order: LiveOrder) -> OrderResult | None:
        """
        Simuleert een fill op de huidige marktprijs (paper mode).

        Haalt de meest recente close-prijs op. Als de data stale is of
        ontbreekt, wordt de order geweigerd zodat paper-runs realistisch blijven.
        """
        market_data = self.get_market_data(order.symbol, "1m")

        if not market_data:
            log.warning(
                "BitvavoAdapter._simulate_order: geen marktdata voor %s — order geweigerd",
                order.symbol,
            )
            return OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.MARKET_DATA_STALE,
                detail=f"Paper mode: geen marktdata beschikbaar voor {order.symbol}",
            )

        if market_data.is_stale(max_age_seconds=60.0):
            log.warning(
                "BitvavoAdapter._simulate_order: stale marktdata voor %s — order geweigerd",
                order.symbol,
            )
            return OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.MARKET_DATA_STALE,
                detail=f"Paper mode: marktdata te oud (>60s) voor {order.symbol}",
            )

        fill_price = (
            order.limit_price
            if order.order_type == OrderType.LIMIT and order.limit_price
            else market_data.close
        )

        simulated_exchange_id = f"paper-{uuid.uuid4().hex[:12]}"
        log.info(
            "BitvavoAdapter [PAPER] %s %s %.6f %s @ %.4f EUR — id=%s",
            order.side.value.upper(),
            order.symbol,
            order.quantity,
            order.order_type.value,
            fill_price,
            simulated_exchange_id,
        )
        return OrderResult.accepted_result(
            order_id=order.order_id,
            exchange_order_id=simulated_exchange_id,
            filled_quantity=order.quantity,
            avg_price=fill_price,
        )

    def _place_live_order(self, order: LiveOrder) -> OrderResult | None:
        """
        Plaatst een echte order via Bitvavo REST (live mode).

        Bitvavo placeOrder body:
          market:     "BTC-EUR"
          side:       "buy" | "sell"
          orderType:  "market" | "limit"
          amount:     str  (market order)
          price:      str  (limit order)

        Succesvolle response bevat "orderId".
        Fout-response is een dict met "errorCode" en "error".
        """
        try:
            body: dict[str, Any] = {
                "market":    order.symbol,
                "side":      order.side.value,
                "orderType": order.order_type.value,
                "amount":    str(order.quantity),
            }
            if order.order_type == OrderType.LIMIT and order.limit_price:
                body["price"] = str(order.limit_price)

            log.info(
                "BitvavoAdapter [LIVE] placing %s %s %.6f @ %s",
                order.side.value.upper(),
                order.symbol,
                order.quantity,
                order.limit_price or "MARKET",
            )

            response = self._client.placeOrder(
                order.symbol,
                order.side.value,
                order.order_type.value,
                body,
            )

            if isinstance(response, dict) and "orderId" in response:
                filled_qty = float(response.get("filledAmount") or response.get("amount", order.quantity))
                avg_price = float(response.get("price") or response.get("filledAmountQuote", 0) or 0)
                # avg_price van 0 bij market orders — gebruik filledAmountQuote / filledAmount
                if avg_price == 0 and filled_qty > 0:
                    filled_quote = float(response.get("filledAmountQuote", 0))
                    avg_price = filled_quote / filled_qty if filled_quote else 0.0

                log.info(
                    "BitvavoAdapter [LIVE] order geplaatst — exchange_id=%s filled=%.6f avg=%.4f",
                    response["orderId"], filled_qty, avg_price,
                )
                return OrderResult.accepted_result(
                    order_id=order.order_id,
                    exchange_order_id=response["orderId"],
                    filled_quantity=filled_qty,
                    avg_price=avg_price,
                )

            # Exchange heeft het order zakelijk geweigerd
            error_msg = ""
            if isinstance(response, dict):
                error_msg = response.get("error", str(response))
            log.warning(
                "BitvavoAdapter [LIVE] order geweigerd door exchange: %s", error_msg
            )
            return OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.EXCHANGE_REJECTED,
                detail=f"Bitvavo: {error_msg}",
            )

        except Exception:
            log.exception(
                "BitvavoAdapter._place_live_order: onverwachte fout voor order %s",
                order.order_id,
            )
            return None
