"""
ant_colony/schemas/opportunity_signal.py

OpportunitySignal — observatierapport van een scout-ant.

Bevat een gedetecteerde marktkans (prijsbeweging of volume-spike).
Geen orders, geen kapitaal — puur informatief.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    PRICE_MOVE = "price_move"
    VOLUME_SPIKE = "volume_spike"


class OpportunitySignal(BaseModel):
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    detected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signal_type: SignalType
    current_price: float = Field(gt=0)
    change_pct: float = Field(description="Positief of negatief; abs() voor magnitude")
    confidence: float = Field(ge=0.0, le=1.0)
    biome: str
    mission_id: str
