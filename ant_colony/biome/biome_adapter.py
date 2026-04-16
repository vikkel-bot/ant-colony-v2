"""
ant_colony/biome/biome_adapter.py

BiomeAdapter — formeel contract voor alle biome-adapters.

De Queen en agents weten welk biome ze aanspreken, nooit welke exchange
er achter zit. Adapters zijn verwisselbaar binnen een biome zonder dat de
rest van het systeem dat merkt.

Drie datastructuren:
  MarketData    — snapshot van marktprijsdata voor één symbool/timeframe
  AccountState  — snapshot van de account bij de adapter
  BiomeAdapter  — Protocol dat elke concrete adapter moet implementeren

Regels:
  - Protocol is het enige contract — geen inheritance vereist (duck typing)
  - MarketData is stale als timestamp te oud is (fail-closed: P2)
  - AccountState equity = balance + positions_value (altijd berekend)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


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

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        """
        True als de data ouder is dan max_age_seconds.

        Fail-closed: stale data blokkeert execution (P2).
        max_age_seconds=300 = 5 minuten (standaard voor live data).
        """
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
      - Adapters zijn stateless t.o.v. posities — state leeft in PaperLedger / live gate

    Usage::

        adapter: BiomeAdapter = CryptoAdapter(api_key=..., api_secret=...)
        if adapter.is_available():
            data = adapter.get_market_data("BTC-EUR", "1h")
            if data and not data.is_stale():
                # gebruik data voor strategie
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
