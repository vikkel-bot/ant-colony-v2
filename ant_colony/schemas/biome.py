"""
ant_colony/schemas/biome.py

BiomeConfig — schema voor biome-configuratie.

Een BiomeConfig beschrijft één marktdomein: welke symbolen er verhandeld
worden, wat het risicoprofiel is en welke execution constraints gelden.
De Queen gebruikt BiomeConfig om per biome-kapitaallimieten en risicogrenzen
in te stellen.

Drie standaard biomes (uit COLONY_OBJECT_MODEL.md):
  crypto       — Bitvavo, 24/7, hoge precisie, fractele lots
  equities     — Interactive Brokers, beursuren, gehele aantallen
  commodities  — Saxo Bank, beperkte handelsvensters

Regels:
  - biome_id is altijd lowercase en bevat geen spaties
  - symbols is niet leeg — elke biome heeft minimaal één markt
  - max_position_pct is een fractie van het biome-kapitaal (0, 1]
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Risicoprofiel per biome
# ---------------------------------------------------------------------------

class BiomeRiskProfile(BaseModel):
    """
    Biome-specifieke risicoparameters.

    max_position_pct:  Maximale fractie van het biome-kapitaal per positie (0, 1].
    min_trade_size:    Minimale ordergrootte in basisvaluta (≥ 0).
    max_daily_trades:  Maximaal aantal trades per kalenderdag (≥ 1).
    """
    max_position_pct: float = Field(
        gt=0, le=1.0,
        description="Max fractie van biome-kapitaal per positie",
    )
    min_trade_size: float = Field(
        ge=0,
        description="Minimale ordergrootte in basisvaluta",
    )
    max_daily_trades: int = Field(
        ge=1,
        description="Maximaal aantal trades per kalenderdag",
    )


# ---------------------------------------------------------------------------
# Execution constraints per biome
# ---------------------------------------------------------------------------

class ExecutionConstraints(BaseModel):
    """
    Execution-beperkingen die gelden voor dit biome.

    trading_hours_utc:  Handelsvensters in UTC, formaat "HH:MM-HH:MM".
                        ["00:00-23:59"] = 24/7 (crypto).
    min_lot_size:       Minimale lot-grootte per order (≥ 0).
    price_precision:    Aantal decimalen voor prijzen (≥ 0).
    """
    trading_hours_utc: list[str] = Field(
        default_factory=lambda: ["00:00-23:59"],
        description='Handelsvensters in UTC, formaat "HH:MM-HH:59"',
    )
    min_lot_size: float = Field(
        ge=0,
        description="Minimale lot-grootte per order",
    )
    price_precision: int = Field(
        ge=0,
        description="Aantal decimalen voor prijzen",
    )


# ---------------------------------------------------------------------------
# BiomeConfig
# ---------------------------------------------------------------------------

class BiomeConfig(BaseModel):
    """
    Volledige configuratie voor één biome.

    biome_id:              Unieke identifier — altijd lowercase, geen spaties.
    display_name:          Leesbare naam (bijv. "Crypto (Bitvavo)").
    symbols:               Beschikbare symbolen in dit biome (minimaal 1).
    risk_profile:          Biome-specifiek risicoprofiel.
    execution_constraints: Handelsvensters en lot-beperkingen.
    is_active:             False = biome tijdelijk buiten gebruik.

    Usage::

        cfg = BiomeConfig.crypto()
        cfg = BiomeConfig.equities(symbols=["AAPL", "MSFT", "AMZN"])
    """
    biome_id: str
    display_name: str
    symbols: list[str] = Field(min_length=1)
    risk_profile: BiomeRiskProfile
    execution_constraints: ExecutionConstraints
    is_active: bool = True

    @field_validator("biome_id")
    @classmethod
    def biome_id_must_be_valid(cls, v: str) -> str:
        if not v:
            raise ValueError("biome_id must not be empty")
        if v != v.lower():
            raise ValueError(f"biome_id must be lowercase, got '{v}'")
        if " " in v:
            raise ValueError(f"biome_id must not contain spaces, got '{v}'")
        return v

    # ------------------------------------------------------------------
    # Standaard biome-configuraties
    # ------------------------------------------------------------------

    @classmethod
    def crypto(cls, symbols: list[str] | None = None) -> BiomeConfig:
        """
        Standaard crypto-configuratie (Bitvavo, 24/7).

        Kenmerken: hoge prijsprecisie (2 decimalen EUR), fractele lots,
        geen handelsvenster-beperking.
        """
        return cls(
            biome_id="crypto",
            display_name="Crypto (Bitvavo)",
            symbols=symbols or ["BTC-EUR", "ETH-EUR", "SOL-EUR"],
            risk_profile=BiomeRiskProfile(
                max_position_pct=0.20,
                min_trade_size=10.0,
                max_daily_trades=10,
            ),
            execution_constraints=ExecutionConstraints(
                trading_hours_utc=["00:00-23:59"],
                min_lot_size=0.0001,
                price_precision=2,
            ),
        )

    @classmethod
    def equities(cls, symbols: list[str] | None = None) -> BiomeConfig:
        """
        Standaard equities-configuratie (Interactive Brokers, beursuren).

        Kenmerken: NYSE/Nasdaq handelsuren (14:30-21:00 UTC), gehele aantallen,
        2 decimalen prijsprecisie.
        """
        return cls(
            biome_id="equities",
            display_name="Equities (Interactive Brokers)",
            symbols=symbols or ["AAPL", "MSFT", "AMZN", "NVDA"],
            risk_profile=BiomeRiskProfile(
                max_position_pct=0.15,
                min_trade_size=100.0,
                max_daily_trades=5,
            ),
            execution_constraints=ExecutionConstraints(
                trading_hours_utc=["14:30-21:00"],
                min_lot_size=1.0,
                price_precision=2,
            ),
        )

    @classmethod
    def commodities(cls, symbols: list[str] | None = None) -> BiomeConfig:
        """
        Standaard commodities-configuratie (Saxo Bank, beperkte vensters).

        Kenmerken: meerdere handelsvensters (metals, energy), fractele lots,
        3 decimalen prijsprecisie.
        """
        return cls(
            biome_id="commodities",
            display_name="Commodities (Saxo Bank)",
            symbols=symbols or ["XAU-USD", "XAG-USD", "CL-USD"],
            risk_profile=BiomeRiskProfile(
                max_position_pct=0.10,
                min_trade_size=50.0,
                max_daily_trades=3,
            ),
            execution_constraints=ExecutionConstraints(
                trading_hours_utc=["01:00-23:00"],
                min_lot_size=0.01,
                price_precision=3,
            ),
        )
