from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class HeartbeatStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"


class Heartbeat(BaseModel):
    ant_id: str
    mission_id: str
    node_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: HeartbeatStatus = HeartbeatStatus.RUNNING
    budget_used: float = Field(ge=0)
    last_action: str = ""
    healthy: bool = True
    state: str = ""
    reason: str = ""
