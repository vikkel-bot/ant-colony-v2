"""
ant_colony/biome/adapters/ibkr_adapter.py

IBKRAdapter — BiomeAdapter implementatie voor Interactive Brokers via ib_insync.

Verbindingsmodi:
  Paper mode (IBKR_PAPER_MODE=true, standaard):
    Verbindt met TWS paper trading port 7497.
    Orders worden ingediend bij de IBKR paper trading simulatie (server-side).
    Veilig voor testen zonder risico op echte uitvoering.

  Live mode (IBKR_PAPER_MODE=false):
    Verbindt met TWS live port 7496.
    Orders worden echt geplaatst via de live rekening.

Vereisten:
  - TWS of IB Gateway moet draaien op PC2
  - "Enable ActiveX and Socket Clients" ingeschakeld in TWS API-instellingen
  - ib_insync >= 0.9.70 geïnstalleerd

Fail-closed regels:
  - Alle publieke methoden gooien nooit
  - is_available() geeft False als niet verbonden of library ontbreekt
  - Geen data, geen order: return None / [] bij iedere twijfel
  - Geen code uitvoeren bij import (P7)

Omgevingsvariabelen:
  IBKR_HOST        Host van TWS/IB Gateway (standaard: 127.0.0.1)
  IBKR_PORT        Override poort (standaard: 7497 paper / 7496 live)
  IBKR_CLIENT_ID   Client-ID voor IB API-sessie (standaard: 1)
  IBKR_PAPER_MODE  "true" = paper trading port (standaard), "false" = live port

Installatie: pip install "ib_insync>=0.9.70"
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from ant_colony.biome.biome_adapter import AccountState, LivePosition, MarketData
from ant_colony.schemas.order import (
    LiveOrder,
    OrderRejectionReason,
    OrderResult,
)

log = logging.getLogger(__name__)

_PORT_PAPER = 7497
_PORT_LIVE  = 7496

# Mapping van yfinance/pandas-achtige periode-strings naar IB durationStr
_PERIOD_TO_DURATION: dict[str, str] = {
    "1d":  "1 D",
    "2d":  "2 D",
    "5d":  "5 D",
    "1mo": "1 M",
    "2mo": "2 M",
    "3mo": "3 M",
    "6mo": "6 M",
    "1y":  "1 Y",
    "2y":  "2 Y",
    "5y":  "5 Y",
}

# Mapping van interval-strings naar IB barSizeSetting
_INTERVAL_TO_BAR_SIZE: dict[str, str] = {
    "1m":  "1 min",
    "5m":  "5 mins",
    "15m": "15 mins",
    "30m": "30 mins",
    "1h":  "1 hour",
    "4h":  "4 hours",
    "1d":  "1 day",
    "1wk": "1 week",
    "1mo": "1 month",
}

_DEFAULT_EXCHANGE = "SMART"
_DEFAULT_CURRENCY = "USD"
_CANDLE_LOCK_WAIT_SECONDS = 0.25


def _is_connection_refused(exc: BaseException) -> bool:
    """Herken lokale TWS/IB Gateway connectiefouten zonder stacktrace-ruis."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    winerror = getattr(exc, "winerror", None)
    if winerror == 10061:
        return True
    if isinstance(exc, OSError):
        text = str(exc).lower()
        return (
            "connection refused" in text
            or "connect call failed" in text
            or "actively refused" in text
        )
    return False


def _is_historical_data_fallback_error(exc: BaseException) -> bool:
    """Herken IBKR historical-data fouten waarbij yfinance een veilige fallback is."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    text = str(exc).lower()
    return (
        "timeout" in text
        or "timed out" in text
        or "market data subscription" in text
        or "market data permissions" in text
        or "no market data permissions" in text
        or "requested market data is not subscribed" in text
        or "error 162" in text
    )


class IBKRAdapter:
    """
    BiomeAdapter voor Interactive Brokers via ib_insync.

    Verbindt met TWS of IB Gateway op de geconfigureerde host/poort.
    Lazy import van ib_insync: de module is bruikbaar zonder installatie
    (is_available() geeft dan False).

    Lazy verbinding: de eerste API-aanroep probeert automatisch te verbinden
    als er nog geen actieve sessie is. Gebruik connect() voor expliciete
    verbinding bij opstarten.

    Args:
        host:       Override voor IBKR_HOST env var.
        port:       Override voor IBKR_PORT env var.
        client_id:  Override voor IBKR_CLIENT_ID env var.
        paper_mode: Override voor IBKR_PAPER_MODE env var.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        paper_mode: bool | None = None,
    ) -> None:
        paper = (
            paper_mode
            if paper_mode is not None
            else (os.getenv("IBKR_PAPER_MODE", "true").lower().strip() != "false")
        )
        self._paper_mode = paper
        self._host       = host or os.getenv("IBKR_HOST", "127.0.0.1")
        default_port     = _PORT_PAPER if paper else _PORT_LIVE
        self._port       = port or int(os.getenv("IBKR_PORT", str(default_port)))
        self._client_id  = client_id or int(os.getenv("IBKR_CLIENT_ID", "1"))
        self._ib: Any    = None  # ib_insync.IB instance, lazy init
        self._ib_lock    = threading.RLock()
        self._log        = logging.getLogger(
            f"adapter.ibkr.{'paper' if paper else 'live'}"
        )

    # ------------------------------------------------------------------
    # BiomeAdapter protocol
    # ------------------------------------------------------------------

    @property
    def biome_id(self) -> str:
        return "equities"

    def is_available(self) -> bool:
        """
        True als ib_insync geïnstalleerd is én er een actieve verbinding is.

        Probeert NIET automatisch te verbinden — gebruik connect() of laat
        een API-aanroep de verbinding triggeren via _ensure_connected().
        """
        try:
            import ib_insync  # noqa: F401
        except ImportError:
            self._log.warning("ib_insync niet geïnstalleerd — adapter niet beschikbaar")
            return False
        if self._ib is None:
            return False
        try:
            return bool(self._ib.isConnected())
        except Exception:
            return False

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        """Meest recente bar voor symbool (BiomeAdapter protocol)."""
        candles = self.get_candles(symbol, period="5d", interval=timeframe or "1d")
        return candles[-1] if candles else None

    def get_account_state(self) -> AccountState | None:
        """
        Haal account state op via IBKR accountValues.
        Retourneert None bij fout of geen verbinding.
        """
        try:
            if not self._ensure_connected():
                return None

            values          = self._ib.accountValues()
            balance         = 0.0
            positions_value = 0.0
            currency        = _DEFAULT_CURRENCY

            for av in values:
                tag = getattr(av, "tag", "")
                val = getattr(av, "value", None)
                cur = getattr(av, "currency", "")
                if cur not in ("USD", "BASE", "EUR", ""):
                    continue
                try:
                    fval = float(val or 0.0)
                except (TypeError, ValueError):
                    continue
                if tag == "TotalCashValue":
                    balance = fval
                elif tag == "UnrealizedPnL":
                    positions_value = fval
                elif tag == "Currency" and val:
                    currency = str(val)

            return AccountState(
                biome_id=self.biome_id,
                balance=balance,
                positions_value=positions_value,
                timestamp=datetime.now(tz=timezone.utc),
                currency=currency,
            )
        except Exception:
            self._log.exception("get_account_state mislukt")
            return None

    def place_order(  # type: ignore[override]
        self,
        symbol: str,
        action: str,
        quantity: float,
        order_type: str = "market",
        exchange: str = _DEFAULT_EXCHANGE,
        currency: str = _DEFAULT_CURRENCY,
        limit_price: float | None = None,
    ) -> str | None:
        """
        Stuur een order naar IBKR via TWS. Retourneert order_id of None bij fout.

        Paper mode (IBKR_PAPER_MODE=true, standaard):
          Verbindt met TWS paper trading port 7497.
          TWS simuleert de fill in de paper rekening.

        Live mode (IBKR_PAPER_MODE=false):
          Verbindt met TWS live port 7496.
          Order wordt echt uitgevoerd op de exchange.

        Args:
            symbol:      Ticker-symbool (bijv. "AAPL", "SPY").
            action:      "BUY" of "SELL" (hoofdletterongevoelig).
            quantity:    Aantal aandelen (> 0).
            order_type:  "market" of "limit" (standaard: "market").
            exchange:    IB exchange-code (standaard: "SMART" = auto-routing).
            currency:    Valuta (standaard: "USD").
            limit_price: Verplicht bij order_type="limit".

        Returns:
            IB order_id als string, of None bij fout of geen verbinding.
        """
        return self._submit_order(
            symbol, exchange, currency,
            action.upper(), quantity, order_type.lower(), limit_price,
        )

    def get_positions(self) -> list[LivePosition] | None:
        """
        Haal alle open posities op bij IBKR.

        Returns:
            Lijst van LivePosition snapshots, of None bij fout.
            Lege lijst betekent geen open posities.
        """
        try:
            if not self._ensure_connected():
                return None

            positions = self._ib.positions()
            result: list[LivePosition] = []

            for pos in positions:
                try:
                    contract = pos.contract
                    qty      = float(pos.position)
                    if qty == 0.0:
                        continue

                    symbol   = str(contract.symbol)
                    exc      = getattr(contract, "exchange", _DEFAULT_EXCHANGE) or _DEFAULT_EXCHANGE
                    cur      = getattr(contract, "currency", _DEFAULT_CURRENCY) or _DEFAULT_CURRENCY
                    avg_cost = float(pos.avgCost or 0.0)

                    current_price = self.get_quote(symbol, exc, cur) or avg_cost

                    result.append(LivePosition(
                        position_id   = f"ibkr-{symbol.lower()}-{uuid.uuid4().hex[:8]}",
                        symbol        = symbol,
                        biome_id      = self.biome_id,
                        side          = "buy" if qty > 0 else "sell",
                        quantity      = abs(qty),
                        entry_price   = avg_cost,
                        current_price = current_price,
                        timestamp     = datetime.now(tz=timezone.utc),
                    ))
                except Exception:
                    self._log.exception("Positie overgeslagen")
                    continue

            return result

        except Exception:
            self._log.exception("get_positions mislukt")
            return None

    # ------------------------------------------------------------------
    # Verbindingsbeheer
    # ------------------------------------------------------------------

    def connect(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
    ) -> bool:
        """
        Verbindt expliciet met TWS of IB Gateway.

        Args:
            host:      Override host (standaard: geconfigureerde host).
            port:      Override poort (standaard: geconfigureerde poort).
            client_id: Override client-ID (standaard: geconfigureerde client-ID).

        Returns:
            True bij succesvolle verbinding, False bij fout.
        """
        try:
            self._ensure_thread_event_loop()

            from ib_insync import IB

            host_      = host      or self._host
            port_      = port      or self._port
            client_id_ = client_id or self._client_id

            if self._ib is not None:
                try:
                    if self._ib.isConnected():
                        return True
                except Exception:
                    pass
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
                self._ib = None

            ib = IB()
            try:
                ib.connect(host_, port_, clientId=client_id_, timeout=10, readonly=False)
            except Exception as exc:
                if not _is_connection_refused(exc):
                    raise
                self._log.warning(
                    "IBKR niet bereikbaar | %s:%d clientId=%d paper=%s | %s",
                    host_, port_, client_id_, self._paper_mode, exc,
                )
                try:
                    ib.disconnect()
                except Exception:
                    pass
                self._ib = None
                return False
            self._ib = ib
            self._log.info(
                "IBKR verbonden | %s:%d clientId=%d paper=%s",
                host_, port_, client_id_, self._paper_mode,
            )
            return True

        except Exception:
            self._log.exception(
                "IBKR verbinding mislukt | %s:%d", host or self._host, port or self._port
            )
            self._ib = None
            return False

    def disconnect(self) -> None:
        """Verbreek de verbinding met TWS."""
        if self._ib is not None:
            try:
                self._ib.disconnect()
                self._log.info("IBKR verbinding verbroken")
            except Exception:
                pass
            finally:
                self._ib = None

    # ------------------------------------------------------------------
    # OHLCV candles
    # ------------------------------------------------------------------

    def get_candles(
        self,
        symbol: str,
        exchange: str = _DEFAULT_EXCHANGE,
        currency: str = _DEFAULT_CURRENCY,
        period: str = "3mo",
        interval: str = "1d",
    ) -> list[MarketData]:
        """
        Haal OHLCV bars op via IBKR historical data API.

        Args:
            symbol:   Ticker-symbool (bijv. "AAPL", "SPY").
            exchange: IB exchange-code (standaard "SMART").
            currency: Valuta (standaard "USD").
            period:   Historische periode ("1d", "5d", "1mo", "3mo", "6mo", "1y", "5y").
            interval: Bar-grootte ("1d", "1wk", "1mo", "1h", "4h", "15m", "5m", "1m").

        Returns:
            Lijst van MarketData (oudste eerst), leeg bij fout of geen data.
        """
        try:
            self._ensure_thread_event_loop()

            bars = []
            fallback_reason: str | None = None
            lock_acquired = self._ib_lock.acquire(timeout=_CANDLE_LOCK_WAIT_SECONDS)
            if not lock_acquired:
                fallback_reason = "omdat IBKR candle lock bezet is"
            else:
                try:
                    if not self._ensure_connected():
                        return []

                    from ib_insync import Stock

                    contract  = Stock(symbol, exchange, currency)
                    duration  = _PERIOD_TO_DURATION.get(period, "3 M")
                    bar_size  = _INTERVAL_TO_BAR_SIZE.get(interval, "1 day")

                    try:
                        bars = self._ib.reqHistoricalData(
                            contract,
                            endDateTime="",
                            durationStr=duration,
                            barSizeSetting=bar_size,
                            whatToShow="MIDPOINT",
                            useRTH=True,
                            formatDate=1,
                            timeout=8,  # ib_insync default is 60s; keep equity ticks responsive.
                        )
                    except Exception as exc:
                        if not _is_historical_data_fallback_error(exc):
                            raise
                        fallback_reason = "na IBKR timeout/fout"
                finally:
                    self._ib_lock.release()

            if fallback_reason:
                return self._get_yfinance_fallback_candles(
                    symbol,
                    period=period,
                    interval=interval,
                    reason=fallback_reason,
                )

            result: list[MarketData] = []
            for bar in bars:
                try:
                    close = float(bar.close)
                    if close <= 0:
                        continue

                    raw_date = bar.date
                    if hasattr(raw_date, "astimezone"):
                        dt = raw_date.astimezone(timezone.utc)
                    elif isinstance(raw_date, str):
                        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
                            try:
                                dt = datetime.strptime(raw_date.split()[0], "%Y%m%d").replace(
                                    tzinfo=timezone.utc
                                )
                                break
                            except ValueError:
                                continue
                        else:
                            dt = datetime.now(tz=timezone.utc)
                    else:
                        dt = datetime.now(tz=timezone.utc)

                    result.append(MarketData(
                        symbol    = symbol,
                        timeframe = interval,
                        timestamp = dt,
                        open      = float(bar.open),
                        high      = float(bar.high),
                        low       = float(bar.low),
                        close     = close,
                        volume    = float(bar.volume),
                        biome_id  = self.biome_id,
                    ))
                except Exception:
                    continue

            return result

        except Exception:
            self._log.exception("get_candles mislukt voor %s", symbol)
            return []

    # ------------------------------------------------------------------
    # Huidige prijs
    # ------------------------------------------------------------------

    def get_quote(
        self,
        symbol: str,
        exchange: str = _DEFAULT_EXCHANGE,
        currency: str = _DEFAULT_CURRENCY,
    ) -> float | None:
        """
        Haal actuele marktprijs op voor symbool.

        Probeert eerst een snapshot market data request. Valt terug op de
        slotkoers van de meest recente historische bar als snapshot mislukt
        (bijv. buiten beurstijden of zonder dataabonnement).

        Returns:
            Huidige prijs als float, of None bij fout of geen data.
        """
        try:
            if not self._ensure_connected():
                return None

            from ib_insync import Stock

            contract = Stock(symbol, exchange, currency)

            # Snapshot market data (werkt binnen beurstijden)
            try:
                ticker = self._ib.reqMktData(contract, "", False, True)
                self._ib.sleep(2)
                price = getattr(ticker, "last", None) or getattr(ticker, "close", None)
                self._ib.cancelMktData(contract)
                if price and float(price) > 0:
                    return float(price)
            except Exception:
                pass

            # Fallback: slotkoers laatste historische bar
            bars = self.get_candles(symbol, exchange, currency, period="5d")
            return float(bars[-1].close) if bars else None

        except Exception:
            self._log.exception("get_quote mislukt voor %s", symbol)
            return None

    # ------------------------------------------------------------------
    # Order plaatsing (intern)
    # ------------------------------------------------------------------

    def _submit_order(
        self,
        symbol: str,
        exchange: str,
        currency: str,
        action: str,
        quantity: float,
        order_type: str,
        limit_price: float | None,
    ) -> str | None:
        """
        Dien een order in bij TWS. Gebruikt door place_order().

        Paper port (7497): TWS voert de simulatie server-side uit.
        Live port (7496):  Echte uitvoering op de exchange.
        """
        try:
            if not self._ensure_connected():
                self._log.warning("Order geweigerd: geen IBKR-verbinding")
                return None

            from ib_insync import LimitOrder, MarketOrder, Stock

            contract = Stock(symbol, exchange, currency)

            if order_type == "limit":
                if limit_price is None or limit_price <= 0:
                    self._log.error(
                        "Limit order voor %s vereist een geldige limit_price", symbol
                    )
                    return None
                ib_order = LimitOrder(action, quantity, limit_price)
            else:
                ib_order = MarketOrder(action, quantity)

            trade    = self._ib.placeOrder(contract, ib_order)
            self._ib.sleep(0)  # verwerk lopende events

            order_id = str(trade.order.orderId)
            self._log.info(
                "ORDER INGEDIEND | %s %s %s x%.2f @ %s id=%s paper=%s",
                symbol, action, order_type, quantity,
                f"{limit_price:.4f}" if limit_price else "market",
                order_id, self._paper_mode,
            )
            return order_id

        except Exception:
            self._log.exception("Order mislukt voor %s", symbol)
            return None

    # ------------------------------------------------------------------
    # Protocol-compatibele order interface (LiveExecutionGate)
    # ------------------------------------------------------------------

    def place_live_order(self, order: LiveOrder) -> OrderResult | None:
        """
        BiomeAdapter-compatibele order interface voor de LiveExecutionGate.

        Vertaalt een LiveOrder naar een IBKR order en retourneert een
        OrderResult. Gebruik place_order() voor directe IBKR-aanroepen.

        Args:
            order: Gevalideerd LiveOrder schema.

        Returns:
            OrderResult bij succes of afwijzing, None bij onverwachte fout.
        """
        try:
            order_id = self._submit_order(
                symbol      = order.symbol,
                exchange    = _DEFAULT_EXCHANGE,
                currency    = _DEFAULT_CURRENCY,
                action      = order.side.value.upper(),
                quantity    = order.quantity,
                order_type  = order.order_type.value,
                limit_price = order.limit_price,
            )
            if order_id is None:
                return OrderResult.rejected(
                    order.order_id,
                    OrderRejectionReason.ADAPTER_UNAVAILABLE,
                    "IBKR order mislukt — zie logs",
                )

            current_price = self.get_quote(order.symbol) or 0.0
            return OrderResult.accepted_result(
                order_id          = order.order_id,
                exchange_order_id = order_id,
                filled_quantity   = order.quantity,
                avg_price         = current_price,
            )
        except Exception:
            self._log.exception("place_live_order mislukt voor order %s", order.order_id)
            return None

    # ------------------------------------------------------------------
    # Intern hulpgedrag
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> bool:
        """
        Verbindt indien nodig. Retourneert True als er een actieve sessie is.

        Probeert eenmalig te verbinden als de verbinding verbroken of
        nooit opgezet is. Bij herhaalde fouten is het de verantwoordelijkheid
        van de aanroeper om opnieuw te proberen.
        """
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    return True
            except Exception:
                pass
            self._ib = None  # sessie verloren, opnieuw verbinden

        return self.connect()

    def _get_yfinance_candles(
        self,
        symbol: str,
        period: str,
        interval: str,
    ) -> list[MarketData]:
        """Read-only fallback voor ontbrekende IBKR market-data subscriptions."""
        try:
            from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter

            return YahooFinanceAdapter(biome_id=self.biome_id).get_candles(
                symbol,
                period=period,
                interval=interval,
            )
        except Exception:
            self._log.exception("yfinance fallback mislukt voor %s", symbol)
            return []

    def _get_yfinance_fallback_candles(
        self,
        symbol: str,
        period: str,
        interval: str,
        reason: str,
    ) -> list[MarketData]:
        self._log.info("yfinance fallback voor %s %s", symbol, reason)
        fallback_bars = self._get_yfinance_candles(symbol, period=period, interval=interval)
        if fallback_bars:
            self._log.info(
                "yfinance fallback geslaagd voor %s — %d bars",
                symbol,
                len(fallback_bars),
            )
        else:
            self._log.warning(
                "yfinance fallback ook mislukt voor %s — geen candles",
                symbol,
            )
        return fallback_bars

    def _ensure_thread_event_loop(self) -> asyncio.AbstractEventLoop:
        """
        Zorg dat de huidige thread een open asyncio event loop heeft.

        ib_insync gebruikt asyncio ook in zijn synchrone API. Equity ants
        draaien in worker threads, en Python maakt daar geen impliciete loop
        aan. Deze helper is thread-local via asyncio.set_event_loop().
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("loop is closed")
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    def __del__(self) -> None:
        """Verbreek verbinding bij garbage collection."""
        try:
            self.disconnect()
        except Exception:
            pass
