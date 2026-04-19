from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class AntType(str, Enum):
    SCOUT = "scout_ant"
    RESEARCH = "research_ant"
    PAPER = "paper_ant"
    EXECUTION = "execution_ant"
    AUDIT = "audit_ant"
    INGESTION = "ingestion_ant"
    STRATEGY = "strategy_ant"
    OPERATOR = "operator_ant"
    CLAUDE   = "claude_ant"
    # Equities biome
    SECTOR_SCOUT   = "sector_scout_ant"
    FUNDAMENTAL    = "fundamental_ant"
    DIVIDEND_SCOUT = "dividend_scout_ant"


class AntStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    TERMINATED_BY_TIMEOUT = "terminated_by_timeout"


class Ant(BaseModel):
    ant_id: str
    ant_type: AntType
    mission_id: str
    node_id: str
    status: AntStatus = AntStatus.IDLE
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl: int = Field(gt=0, description="Maximum lifetime in seconds, copied from Mission")
    budget_used: float = Field(default=0.0, ge=0)
