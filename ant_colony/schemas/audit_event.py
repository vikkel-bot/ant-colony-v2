from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AuditEventType(str, Enum):
    MISSION_ISSUED = "mission_issued"
    MISSION_ACCEPTED = "mission_accepted"
    MISSION_REJECTED = "mission_rejected"
    MISSION_COMPLETED = "mission_completed"
    MISSION_ABORTED = "mission_aborted"
    HEARTBEAT = "heartbeat"
    ACTION_EXECUTED = "action_executed"
    RISK_BREACH = "risk_breach"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"
    STRATEGY_CANDIDATE_PROMOTED = "strategy_candidate_promoted"
    STRATEGY_CANDIDATE_REJECTED = "strategy_candidate_rejected"
    NODE_HEARTBEAT_STALE = "node_heartbeat_stale"
    COLONY_HALTED = "colony_halted"


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AuditEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = Field(description="ant_id | queen | scheduler | operator")
    mission_id: str | None = None
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int = Field(ge=0, description="Monotonically increasing per log file; gaps indicate anomaly")

    @field_validator("source")
    @classmethod
    def source_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source must not be empty")
        return v
