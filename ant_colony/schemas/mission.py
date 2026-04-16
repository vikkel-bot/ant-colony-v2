from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, model_validator


class MarketScope(BaseModel):
    biome: str
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(default_factory=list)


class RiskLimits(BaseModel):
    max_drawdown_pct: float = Field(gt=0, le=1)
    max_position_size: float = Field(gt=0)
    daily_loss_limit: float = Field(gt=0)
    stop_loss_required: bool = True


class AbortConditions(BaseModel):
    stale_heartbeat: bool = True
    capital_limit_breach: bool = True
    risk_limit_breach: bool = True
    ttl_expired: bool = True
    stale_market_data: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)


class SuccessConditions(BaseModel):
    description: str
    criteria: dict[str, Any] = Field(default_factory=dict)


class Mission(BaseModel):
    mission_id: str
    ant_type: str
    allowed_node: str
    allowed_actions: list[str] = Field(min_length=1)
    market_scope: MarketScope
    capital_limit: float = Field(ge=0)
    risk_limits: RiskLimits
    ttl: int = Field(gt=0, description="Maximum agent lifetime in seconds")
    heartbeat_interval: int = Field(gt=0, description="Expected heartbeat interval in seconds")
    success_conditions: SuccessConditions
    abort_conditions: AbortConditions = Field(default_factory=AbortConditions)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    issued_by: str = Field(default="queen")

    @model_validator(mode="after")
    def heartbeat_must_be_shorter_than_ttl(self) -> Mission:
        if self.heartbeat_interval >= self.ttl:
            raise ValueError(
                f"heartbeat_interval ({self.heartbeat_interval}s) must be less than ttl ({self.ttl}s)"
            )
        return self

    @model_validator(mode="after")
    def issued_by_must_be_queen(self) -> Mission:
        if self.issued_by != "queen":
            raise ValueError(f"issued_by must be 'queen', got '{self.issued_by}'")
        return self
