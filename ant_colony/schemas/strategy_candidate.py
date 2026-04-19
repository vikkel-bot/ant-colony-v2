from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class CandidateStatus(str, Enum):
    INGESTED = "ingested"   # discovered from external source, not yet analysed
    RESEARCH = "research"
    PAPER = "paper"
    APPROVED = "approved"
    REJECTED = "rejected"
    LIVE = "live"


# Status may only move forward along this chain (or to rejected from any state).
_STATUS_ORDER = [
    CandidateStatus.INGESTED,
    CandidateStatus.RESEARCH,
    CandidateStatus.PAPER,
    CandidateStatus.APPROVED,
    CandidateStatus.LIVE,
]


class ProvenanceEntry(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str = Field(description="ant_id | queen | operator")
    action: str
    details: dict[str, Any] = Field(default_factory=dict)


class BacktestResults(BaseModel):
    sharpe_ratio: float | None = None
    max_drawdown_pct: float | None = None
    total_trades: int | None = None
    win_rate: float | None = None
    avg_win: float | None = None        # gemiddeld rendement per winnende trade
    avg_loss: float | None = None       # gemiddeld verlies per verliezende trade (positief getal)
    best_streak: int | None = None      # langste reeks winstgevende trades
    regime_stats: dict[str, Any] | None = None   # {bull/bear/sideways: {win_rate, sharpe, trade_count}}
    best_regime: str | None = None      # regime met de beste sharpe (min 3 trades)
    extra: dict[str, Any] = Field(default_factory=dict)


class PaperResults(BaseModel):
    sharpe_ratio: float | None = None
    max_drawdown_pct: float | None = None
    total_trades: int | None = None
    win_rate: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class StrategyCandidate(BaseModel):
    candidate_id: str
    name: str
    source: str = Field(description="github | paper | internal | mutation")
    source_url: str | None = None
    biome: str
    market_scope: dict[str, Any] = Field(default_factory=dict)
    logic_summary: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    entry_conditions: dict[str, Any] = Field(default_factory=dict)
    exit_conditions: dict[str, Any] = Field(
        description="Required. No candidate without exit logic."
    )
    backtest_results: BacktestResults | None = None
    paper_results: PaperResults | None = None
    fitness_score: float | None = None
    status: CandidateStatus = CandidateStatus.RESEARCH
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    proposed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    approved_by: str | None = None

    @model_validator(mode="after")
    def exit_conditions_must_not_be_empty(self) -> StrategyCandidate:
        if not self.exit_conditions:
            raise ValueError("exit_conditions must not be empty — no candidate without exit logic")
        return self

    @model_validator(mode="after")
    def approved_by_must_be_queen(self) -> StrategyCandidate:
        if self.approved_by is not None and self.approved_by != "queen":
            raise ValueError(f"approved_by must be 'queen', got '{self.approved_by}'")
        return self

    @model_validator(mode="after")
    def live_status_requires_approval(self) -> StrategyCandidate:
        if self.status == CandidateStatus.LIVE and self.approved_by != "queen":
            raise ValueError("status='live' requires approved_by='queen'")
        return self
