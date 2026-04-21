"""
ant_colony/biome/biome_adapter.py

BiomeAdapter — formeel contract voor alle biome-adapters.

De Queen en agents weten welk biome ze aanspreken, nooit welke exchange
er achter zit. Adapters zijn verwisselbaar binnen een biome zonder dat de
rest van het systeem dat merkt.

Vier datastructuren:
  MarketData    — snapshot van marktprijsdata voor één symbool/timeframe
  AccountState  — snapshot van de account bij de adapter
  LivePosition  — snapshot van één open positie bij de exchange
  BiomeAdapter  — Protocol dat elke concrete adapter moet implementeren

Regels:
  - Protocol is het enige contract — geen inheritance vereist (duck typing)
  - MarketData is stale als timestamp te oud is (fail-closed: P2)
  - AccountState equity = balance + positions_value (altijd berekend)
  - place_order() gooit nooit — retourneert OrderResult of None bij falen
  - get_positions() gooit nooit — retourneert lijst of None bij falen
  - Adapters zijn stateless t.o.v. interne state — state leeft in de gate
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from ant_colony.schemas.order import LiveOrder, OrderResult

_STALE_THRESHOLDS: dict[str, float] = {
    "1m":  120.0,
    "5m":  600.0,
    "15m": 1800.0,
    "1h":  7200.0,
    "4h":  28800.0,
    "1d":  172800.0,
}

# ---------------------------------------------------------------------------
# Marktdata snapshot
# ---------------------------------------------------------------------------

@dataclass
class MarketData:
    """
    Snapshot van marktprijsdata voor één symbool en timeframe.

    symbol:     Markt-identifier (bijv. "BTC-EUR", "AAPL", "XAU-USD").
    timeframe:  Tijdschaal (bijv. "1m", "5m", "1h", "1d").
    timestamp:  Tijdstip van de data — altijd timezone-aware UTC.
    open:       Openingsprijs van de bar.
    high:       Hoogste prijs van de bar.
    low:        Laagste prijs van de bar.
    close:      Sluitingsprijs — de primaire prijs voor strategiebeslissingen.
    volume:     Handelsvolume (0.0 als onbekend).
    biome_id:   Biome waaruit de data afkomstig is.
    """
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    biome_id: str

    def is_stale(self, max_age_seconds: float | None = None) -> bool:
        """
        True als de data ouder is dan max_age_seconds.

        Fail-closed: stale data blokkeert execution (P2).
        Zonder max_age_seconds: drempel afgeleid van self.timeframe (2× candle-duur).
        Met expliciete max_age_seconds: die waarde wordt gebruikt ongeacht timeframe.
        """
        if max_age_seconds is None:
            max_age_seconds = _STALE_THRESHOLDS.get(self.timeframe, 300.0)
        age = (datetime.now(tz=timezone.utc) - self.timestamp).total_seconds()
        return age > max_age_seconds

    @property
    def is_valid_price(self) -> bool:
        """True als close > 0 en alle prijsvelden positief zijn."""
        return self.close > 0 and self.open > 0 and self.high > 0 and self.low > 0


# ---------------------------------------------------------------------------
# Account snapshot
# ---------------------------------------------------------------------------

@dataclass
class AccountState:
    """
    Snapshot van de account bij de biome-adapter.

    biome_id:        Biome waaruit de state afkomstig is.
    balance:         Vrij beschikbare cash.
    positions_value: Huidige marktwaarde van open posities.
    timestamp:       Tijdstip van de snapshot — altijd timezone-aware UTC.
    currency:        Basisvaluta van de account (bijv. "EUR", "USD").
    """
    biome_id: str
    balance: float
    positions_value: float
    timestamp: datetime
    currency: str = "EUR"

    @property
    def equity(self) -> float:
        """Totale account equity: cash + open positiewaarde."""
        return self.balance + self.positions_value

    def is_stale(self, max_age_seconds: float = 60.0) -> bool:
        """
        True als de state ouder is dan max_age_seconds.

        Account state heeft een kortere TTL dan marktdata (standaard 60s).
        """
        age = (datetime.now(tz=timezone.utc) - self.timestamp).total_seconds()
        return age > max_age_seconds


# ---------------------------------------------------------------------------
# Live positie snapshot
# ---------------------------------------------------------------------------

@dataclass
class LivePosition:
    """
    Snapshot van één open positie bij de exchange.

    Wordt opgehaald via BiomeAdapter.get_positions() en gebruikt door de
    LiveExecutionGate om positiegrootte en blootstelling te bewaken.

    position_id:    Exchange-ID van de positie.
    symbol:         Markt-identifier (bijv. "BTC-EUR").
    biome_id:       Biome waaruit de positie afkomstig is.
    side:           "buy" of "sell".
    quantity:       Hoeveelheid eenheden (> 0).
    entry_price:    Gemiddelde openingsprijs.
    current_price:  Huidige marktprijs (voor unrealized PnL).
    timestamp:      Tijdstip van de snapshot — altijd timezone-aware UTC.
    """
    position_id:   str
    symbol:        str
    biome_id:      str
    side:          str          # "buy" | "sell"
    quantity:      float
    entry_price:   float
    current_price: float
    timestamp:     datetime

    @property
    def unrealized_pnl(self) -> float:
        """
        Ongerealiseerde PnL op basis van huidige prijs.

        LONG (buy):  (current - entry) * quantity
        SHORT (sell): (entry - current) * quantity
        """
        if self.side == "buy":
            return (self.current_price - self.entry_price) * self.quantity
        return (self.entry_price - self.current_price) * self.quantity

    @property
    def market_value(self) -> float:
        """Huidige marktwaarde van de positie: current_price * quantity."""
        return self.current_price * self.quantity


# ---------------------------------------------------------------------------
# BiomeAdapter Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BiomeAdapter(Protocol):
    """
    Contract dat elke biome-adapter moet implementeren.

    De Queen en agents spreken adapters aan via dit protocol.
    De onderliggende exchange is volledig verborgen.

    Implementaties:
      - CryptoAdapter    (Bitvavo, v2 target)
      - EquitiesAdapter  (Interactive Brokers, v2 target)
      - CommoditiesAdapter (Saxo Bank, v2 target)

    Regels:
      - `biome_id` is de primaire identifier — uniek per biome type
      - `is_available()` geeft False terug bij connectieproblemen (fail-closed)
      - `get_market_data()` gooit nooit — retourneert MarketData of None bij falen
      - `get_account_state()` gooit nooit — retourneert AccountState of None bij falen
      - `place_order()` gooit nooit — retourneert OrderResult of None bij falen
      - `get_positions()` gooit nooit — retourneert lijst of None bij falen
      - Adapters zijn stateless t.o.v. interne state — state leeft in de gate

    Usage::

        adapter: BiomeAdapter = CryptoAdapter(api_key=..., api_secret=...)
        if adapter.is_available():
            data = adapter.get_market_data("BTC-EUR", "1h")
            if data and not data.is_stale():
                result = adapter.place_order(order)  # alleen via LiveExecutionGate
    """

    @property
    def biome_id(self) -> str:
        """Unieke identifier van de biome (bijv. "crypto", "equities")."""
        ...

    def is_available(self) -> bool:
        """
        True als de adapter verbinding kan maken met de exchange.

        Fail-closed: bij twijfel False teruggeven.
        """
        ...

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        """
        Haal de meest recente marktdata op voor het opgegeven symbool.

        Args:
            symbol:    Markt-identifier (bijv. "BTC-EUR").
            timeframe: Tijdschaal (bijv. "1h").

        Returns:
            MarketData snapshot, of None bij een fout of onbeschikbaarheid.
        """
        ...

    def get_account_state(self) -> AccountState | None:
        """
        Haal de huidige account state op.

        Returns:
            AccountState snapshot, of None bij een fout of onbeschikbaarheid.
        """
        ...

    def place_order(self, order: LiveOrder) -> OrderResult | None:
        """
        Stuur een order naar de exchange.

        Mag alleen worden aangeroepen via LiveExecutionGate — nooit direct
        door een agent of de Queen.

        Args:
            order: Gevalideerd LiveOrder van de execution_ant.

        Returns:
            OrderResult bij succes of zakelijke weigering door de exchange.
            None bij een onverwachte fout (adapter-probleem).
        """
        ...

    def get_positions(self) -> list[LivePosition] | None:
        """
        Haal alle open posities op bij de exchange.

        Returns:
            Lijst van LivePosition snapshots, of None bij falen.
            Lege lijst betekent geen open posities (niet None).
        """
        ...
