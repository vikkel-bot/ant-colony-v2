"""
ant_colony/entry/entry_signal.py

EntrySignal — voorstel van een agent om een positie te openen.

Een EntrySignal is geen order en geen positie. Het is een voorstel dat de
PaperBroker mag accepteren of weigeren op basis van beschikbaar kapitaal,
risicolimieten en andere constraints.

Regels (P3 — exit vóór entry):
  - stop_loss_price is altijd verplicht
  - take_profit_price is altijd verplicht
  - stop_loss en take_profit moeten aan de juiste kant van entry_price liggen
  - Een signal heeft een valid_until — verlopen signals worden genegeerd

Wat een EntrySignal NIET is:
  - Geen executie-opdracht
  - Geen garantie dat een positie wordt geopend
  - Geen live order
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SignalSource(str, Enum):
    SCOUT_ANT = "scout_ant"
    RESEARCH_ANT = "research_ant"
    MANUAL = "manual"          # operator-ingevoerd signaal (test/debug)
    BACKTEST = "backtest"      # gegenereerd tijdens backtest


class EntrySignal(BaseModel):
    # --- identiteit ---
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    biome: str
    mission_id: str
    ant_id: str
    source: SignalSource

    # --- richting ---
    side: str = Field(description="'long' or 'short'")

    # --- prijsniveaus (alle drie verplicht — P3) ---
    entry_price: float = Field(gt=0, description="Verwachte fill-prijs")
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)

    # --- optionele hints voor de broker ---
    suggested_quantity: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Suggestie voor positiegrootte. Broker mag negeren en "
            "zelf berekenen op basis van risicolimieten."
        ),
    )
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Optionele confidence score van de agent (0–1)",
    )

    # --- tijdstempels ---
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_until: datetime = Field(
        description="Signal vervalt na dit tijdstip en wordt genegeerd door de broker"
    )

    # --- metadata ---
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Validatie
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def side_must_be_long_or_short(self) -> EntrySignal:
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got '{self.side}'")
        return self

    @model_validator(mode="after")
    def exit_prices_must_be_on_correct_sides(self) -> EntrySignal:
        if self.side == "long":
            if self.stop_loss_price >= self.entry_price:
                raise ValueError(
                    f"LONG stop_loss_price ({self.stop_loss_price}) "
                    f"must be below entry_price ({self.entry_price})"
                )
            if self.take_profit_price <= self.entry_price:
                raise ValueError(
                    f"LONG take_profit_price ({self.take_profit_price}) "
                    f"must be above entry_price ({self.entry_price})"
                )
        else:  # short
            if self.stop_loss_price <= self.entry_price:
                raise ValueError(
                    f"SHORT stop_loss_price ({self.stop_loss_price}) "
                    f"must be above entry_price ({self.entry_price})"
                )
            if self.take_profit_price >= self.entry_price:
                raise ValueError(
                    f"SHORT take_profit_price ({self.take_profit_price}) "
                    f"must be below entry_price ({self.entry_price})"
                )
        return self

    @model_validator(mode="after")
    def valid_until_must_be_after_generated_at(self) -> EntrySignal:
        if self.valid_until <= self.generated_at:
            raise ValueError(
                f"valid_until ({self.valid_until}) must be after "
                f"generated_at ({self.generated_at})"
            )
        return self

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def is_expired(self, now: datetime | None = None) -> bool:
        """True als het signal zijn valid_until heeft overschreden."""
        if now is None:
            now = datetime.now(timezone.utc)
        return now >= self.valid_until

    def risk_reward_ratio(self) -> float:
        """
        Verhouding tussen potentieel verlies en potentiële winst.

        LONG:  (entry - stop_loss) / (take_profit - entry)
        SHORT: (stop_loss - entry) / (entry - take_profit)

        Een waarde < 1 betekent dat de potentiële winst groter is dan het risico.
        """
        if self.side == "long":
            risk = self.entry_price - self.stop_loss_price
            reward = self.take_profit_price - self.entry_price
        else:
            risk = self.stop_loss_price - self.entry_price
            reward = self.entry_price - self.take_profit_price
        return risk / reward
