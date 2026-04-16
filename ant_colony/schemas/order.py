"""
ant_colony/schemas/order.py

LiveOrder en OrderResult — schema's voor live order-uitvoering.

LiveOrder is een gevalideerd order-voorstel dat een execution_ant naar de
LiveExecutionGate stuurt. De gate voert de veiligheidscontroles uit en
stuurt het order — indien alle checks groen zijn — via de adapter naar
de exchange.

OrderResult is de respons van de exchange (of van de gate bij afwijzing
vóór de exchange). Het bevat altijd voldoende informatie om een audit
event te schrijven.

Regels:
  - stop_loss_price is altijd verplicht (P3: exit vóór entry)
  - LiveOrder is immutable na constructie (Pydantic)
  - OrderResult.accepted == False betekent dat er niets op de exchange
    is terechtgekomen — geen positie geopend, geen kapitaal verbruikt
  - order_id is alleen aanwezig bij accepted == True (exchange-ID)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT  = "limit"


class OrderRejectionReason(str, Enum):
    COLONY_HALTED          = "colony_halted"
    MISSION_NOT_ACTIVE     = "mission_not_active"
    CAPITAL_LIMIT_BREACHED = "capital_limit_breached"
    MARKET_DATA_STALE      = "market_data_stale"
    ADAPTER_UNAVAILABLE    = "adapter_unavailable"
    INVALID_ORDER          = "invalid_order"
    EXCHANGE_REJECTED      = "exchange_rejected"


# ---------------------------------------------------------------------------
# LiveOrder
# ---------------------------------------------------------------------------

class LiveOrder(BaseModel):
    """
    Gevalideerd order-voorstel voor live execution.

    order_id:          Uniek ID gegenereerd door de execution_ant.
    mission_id:        Koppeling aan de actieve Mission.
    ant_id:            Agent die het order voordraagt.
    symbol:            Markt-identifier (bijv. "BTC-EUR").
    biome:             Biome waarop gehandeld wordt (bijv. "crypto").
    side:              BUY of SELL.
    order_type:        MARKET of LIMIT.
    quantity:          Aantal eenheden (> 0).
    limit_price:       Alleen verplicht bij LIMIT orders (> 0).
    stop_loss_price:   Altijd verplicht — exit vóór entry (P3).
    take_profit_price: Altijd verplicht — exit vóór entry (P3).
    created_at:        Tijdstip van aanmaak (UTC).

    Validaties:
      - LIMIT order vereist limit_price > 0
      - stop_loss en take_profit moeten aan de juiste kant van de
        referentieprijs liggen:
          BUY:  stop_loss < take_profit
          SELL: stop_loss > take_profit
    """

    order_id:          str      = Field(default_factory=lambda: str(uuid.uuid4()))
    mission_id:        str
    ant_id:            str
    symbol:            str
    biome:             str
    side:              OrderSide
    order_type:        OrderType
    quantity:          float    = Field(gt=0, description="Aantal eenheden (> 0)")
    limit_price:       float | None = Field(
        default=None, gt=0,
        description="Verplicht bij LIMIT orders",
    )
    stop_loss_price:   float    = Field(gt=0, description="Verplicht — P3")
    take_profit_price: float    = Field(gt=0, description="Verplicht — P3")
    created_at:        datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @model_validator(mode="after")
    def limit_order_requires_limit_price(self) -> LiveOrder:
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        return self

    @model_validator(mode="after")
    def stop_loss_and_take_profit_must_be_consistent(self) -> LiveOrder:
        if self.side == OrderSide.BUY:
            if self.stop_loss_price >= self.take_profit_price:
                raise ValueError(
                    f"BUY order: stop_loss_price ({self.stop_loss_price}) "
                    f"must be < take_profit_price ({self.take_profit_price})"
                )
        else:  # SELL
            if self.stop_loss_price <= self.take_profit_price:
                raise ValueError(
                    f"SELL order: stop_loss_price ({self.stop_loss_price}) "
                    f"must be > take_profit_price ({self.take_profit_price})"
                )
        return self


# ---------------------------------------------------------------------------
# OrderResult
# ---------------------------------------------------------------------------

class OrderResult(BaseModel):
    """
    Respons van de exchange of van de gate bij afwijzing.

    accepted:           True als het order op de exchange is geplaatst.
    order_id:           Overeenkomend order_id uit LiveOrder.
    exchange_order_id:  Exchange-toegewezen ID (alleen bij accepted=True).
    filled_quantity:    Gevulde hoeveelheid (0.0 bij afwijzing).
    avg_price:          Gemiddelde fill-prijs (None bij afwijzing).
    rejection_reason:   Reden van afwijzing (None bij acceptatie).
    rejection_detail:   Mensleesbare toelichting (leeg bij acceptatie).
    timestamp:          Tijdstip van de respons (UTC).
    """

    accepted:           bool
    order_id:           str
    exchange_order_id:  str | None  = None
    filled_quantity:    float       = 0.0
    avg_price:          float | None = None
    rejection_reason:   OrderRejectionReason | None = None
    rejection_detail:   str         = ""
    timestamp:          datetime    = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @classmethod
    def rejected(
        cls,
        order_id: str,
        reason: OrderRejectionReason,
        detail: str = "",
    ) -> OrderResult:
        """Factory voor afwijzingsresultaten."""
        return cls(
            accepted=False,
            order_id=order_id,
            rejection_reason=reason,
            rejection_detail=detail,
        )

    @classmethod
    def accepted_result(
        cls,
        order_id: str,
        exchange_order_id: str,
        filled_quantity: float,
        avg_price: float,
    ) -> OrderResult:
        """Factory voor succesvolle orderresultaten."""
        return cls(
            accepted=True,
            order_id=order_id,
            exchange_order_id=exchange_order_id,
            filled_quantity=filled_quantity,
            avg_price=avg_price,
        )
