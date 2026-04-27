"""
ant_colony/exit_chain/position.py

PaperPosition — een open positie in paper-mode.

Dit schema is het centrale object dat de exit-keten evalueert.
Het bevat alle informatie die nodig is om elk exit-pad te beoordelen:
  - stop-loss      → stop_loss_price vs current_price
  - take-profit    → take_profit_price vs current_price
  - TTL            → opened_at + ttl vs now
  - drawdown       → peak_price vs current_price (relatief)
  - daily loss     → wordt extern bijgehouden, doorgegeven bij evaluatie

Regels:
  - Alleen paper mode — geen live execution in deze fase
  - stop_loss_price en take_profit_price zijn verplicht (P3: exit vóór entry)
  - Zodra status niet meer OPEN is, wordt de positie niet meer geëvalueerd
  - peak_price wordt bijgehouden door de evaluator, niet door externe code
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED_STOP_LOSS = "closed_stop_loss"
    CLOSED_TAKE_PROFIT = "closed_take_profit"
    CLOSED_TTL = "closed_ttl"
    CLOSED_RISK_BREACH = "closed_risk_breach"
    CLOSED_MANUAL = "closed_manual"


class PaperPosition(BaseModel):
    # --- identiteit ---
    position_id: str
    symbol: str
    biome: str
    mission_id: str
    ant_id: str

    # --- positie definitie ---
    side: PositionSide
    entry_price: float = Field(gt=0)
    quantity: float = Field(gt=0)

    # --- exit-condities (altijd verplicht — P3) ---
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    ttl: int = Field(gt=0, description="Maximale levensduur in seconden")

    # --- live state (bijgehouden door evaluator) ---
    current_price: float = Field(gt=0)
    peak_price: float = Field(
        gt=0,
        description=(
            "Hoogste prijs gezien voor LONG (laagste voor SHORT). "
            "Gebruikt voor drawdown-berekening. Bijgehouden door evaluator."
        ),
    )

    # --- tijdstempels ---
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # --- status ---
    status: PositionStatus = PositionStatus.OPEN
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    # --- watchtower koppeling (optioneel) ---
    watchtower_signal_id: str | None = None

    # ------------------------------------------------------------------
    # Validatie
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def stop_loss_and_take_profit_must_be_on_correct_sides(self) -> PaperPosition:
        if self.side == PositionSide.LONG:
            if self.stop_loss_price >= self.entry_price:
                raise ValueError(
                    f"LONG stop_loss_price ({self.stop_loss_price}) must be below entry_price ({self.entry_price})"
                )
            if self.take_profit_price <= self.entry_price:
                raise ValueError(
                    f"LONG take_profit_price ({self.take_profit_price}) must be above entry_price ({self.entry_price})"
                )
        else:  # SHORT
            if self.stop_loss_price <= self.entry_price:
                raise ValueError(
                    f"SHORT stop_loss_price ({self.stop_loss_price}) must be above entry_price ({self.entry_price})"
                )
            if self.take_profit_price >= self.entry_price:
                raise ValueError(
                    f"SHORT take_profit_price ({self.take_profit_price}) must be below entry_price ({self.entry_price})"
                )
        return self

    @model_validator(mode="after")
    def closed_position_must_have_exit_fields(self) -> PaperPosition:
        if self.status != PositionStatus.OPEN:
            if self.exit_price is None:
                raise ValueError("Gesloten positie vereist exit_price")
            if self.closed_at is None:
                raise ValueError("Gesloten positie vereist closed_at")
            if self.exit_reason is None:
                raise ValueError("Gesloten positie vereist exit_reason")
        return self

    # ------------------------------------------------------------------
    # Berekeningen (pure functies — geen side-effects)
    # ------------------------------------------------------------------

    def unrealized_pnl(self) -> float:
        """Ongerealiseerde PnL in basismunt."""
        if self.side == PositionSide.LONG:
            return (self.current_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.current_price) * self.quantity

    def unrealized_pnl_pct(self) -> float:
        """Ongerealiseerde PnL als percentage van de entry waarde."""
        entry_value = self.entry_price * self.quantity
        return self.unrealized_pnl() / entry_value

    def drawdown_from_peak_pct(self) -> float:
        """
        Relatieve terugval van de peak.

        Voor LONG: (peak - current) / peak
        Voor SHORT: (current - peak) / peak  (peak is de laagste prijs)

        Altijd >= 0. Een hogere waarde is slechter.
        """
        if self.side == PositionSide.LONG:
            return max(0.0, (self.peak_price - self.current_price) / self.peak_price)
        else:
            return max(0.0, (self.current_price - self.peak_price) / self.peak_price)

    def age_seconds(self) -> float:
        """Aantal seconden since opening."""
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds()

    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN

    def realized_pnl(self) -> float | None:
        """Gerealiseerde PnL na sluiting. None als positie nog open is."""
        if self.exit_price is None:
            return None
        if self.side == PositionSide.LONG:
            return (self.exit_price - self.entry_price) * self.quantity
        else:
            return (self.entry_price - self.exit_price) * self.quantity
