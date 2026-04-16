from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class NodeStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    SUSPENDED = "suspended"
    UNTRUSTED = "untrusted"


class RuntimePaths(BaseModel):
    output: str = Field(description="General outputs, e.g. C:\\Trading\\ANT_OUT")
    live: str = Field(description="Live execution artifacts, e.g. C:\\Trading\\ANT_LIVE")
    logs: str = Field(description="Append-only logs, e.g. C:\\Trading\\ANT_LOGS")


class Node(BaseModel):
    node_id: str
    hostname: str
    allowed_biomes: list[str] = Field(min_length=1)
    allowed_ant_types: list[str] = Field(min_length=1)
    heartbeat_interval: int = Field(gt=0, description="Expected heartbeat interval in seconds")
    last_heartbeat: datetime | None = None
    status: NodeStatus = NodeStatus.ACTIVE
    runtime_paths: RuntimePaths
