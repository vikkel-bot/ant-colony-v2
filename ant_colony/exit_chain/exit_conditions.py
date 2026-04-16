"""
ant_colony/exit_chain/exit_conditions.py

Exit-conditie types voor de exit-keten.

Elke conditie heeft één verantwoordelijkheid: bepalen of een positie
gesloten moet worden op basis van één specifiek criterium.

Condities:
  StopLossCondition     — prijs raakt stop-loss niveau
  TakeProfitCondition   — prijs raakt take-profit niveau
  TTLCondition          — positie overschrijdt maximale levensduur
  DrawdownCondition     — terugval van peak overschrijdt drempel
  DailyLossCondition    — dagelijks verlies overschrijdt limiet

Ontwerp:
  - Elke conditie is een dataclass met een check(position, context) methode
  - check() retourneert True als de conditie triggert (positie moet sluiten)
  - CheckContext draagt externe state mee (daily_loss_so_far)
  - Condities hebben geen side-effects — ze lezen alleen
  - De evaluator (deliverable 3) combineert condities en handelt het sluiten af
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus


# ---------------------------------------------------------------------------
# Exit-reden — spiegelt PositionStatus, maar als zelfstandige waarde
# ---------------------------------------------------------------------------

class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TTL = "ttl"
    DRAWDOWN_BREACH = "drawdown_breach"
    DAILY_LOSS_BREACH = "daily_loss_breach"

    def to_position_status(self) -> PositionStatus:
        mapping = {
            ExitReason.STOP_LOSS: PositionStatus.CLOSED_STOP_LOSS,
            ExitReason.TAKE_PROFIT: PositionStatus.CLOSED_TAKE_PROFIT,
            ExitReason.TTL: PositionStatus.CLOSED_TTL,
            ExitReason.DRAWDOWN_BREACH: PositionStatus.CLOSED_RISK_BREACH,
            ExitReason.DAILY_LOSS_BREACH: PositionStatus.CLOSED_RISK_BREACH,
        }
        return mapping[self]


# ---------------------------------------------------------------------------
# Context — externe state die condities nodig hebben maar niet zelf bijhouden
# ---------------------------------------------------------------------------

@dataclass
class CheckContext:
    """
    Externe state die wordt meegegeven bij elke conditie-check.

    daily_loss_so_far:
        Totaal gerealiseerd verlies op de huidige handelsdag (positief getal).
        Wordt extern bijgehouden door de paper_ant of een audit_ant.
        Alleen relevant voor DailyLossCondition.
    """
    daily_loss_so_far: float = 0.0


# ---------------------------------------------------------------------------
# Resultaat van een conditie-check
# ---------------------------------------------------------------------------

@dataclass
class ConditionResult:
    triggered: bool
    reason: ExitReason
    detail: str = ""

    @classmethod
    def not_triggered(cls, reason: ExitReason) -> ConditionResult:
        return cls(triggered=False, reason=reason)


# ---------------------------------------------------------------------------
# Basis-interface (geen abstracte klasse — Protocol zou ook kunnen,
# maar dataclass inheritance houdt het simpel)
# ---------------------------------------------------------------------------

class ExitCondition:
    """Basis voor alle exit-condities. Subclasses implementeren check()."""

    reason: ExitReason

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. Stop-loss
# ---------------------------------------------------------------------------

@dataclass
class StopLossCondition(ExitCondition):
    """
    Triggert wanneer de prijs het stop-loss niveau raakt of doorschiet.

    LONG:  current_price <= stop_loss_price
    SHORT: current_price >= stop_loss_price
    """

    reason: ExitReason = field(default=ExitReason.STOP_LOSS, init=False)

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        if position.side == PositionSide.LONG:
            triggered = position.current_price <= position.stop_loss_price
        else:
            triggered = position.current_price >= position.stop_loss_price

        detail = (
            f"current={position.current_price} stop_loss={position.stop_loss_price}"
            if triggered else ""
        )
        return ConditionResult(triggered=triggered, reason=self.reason, detail=detail)


# ---------------------------------------------------------------------------
# 2. Take-profit
# ---------------------------------------------------------------------------

@dataclass
class TakeProfitCondition(ExitCondition):
    """
    Triggert wanneer de prijs het take-profit niveau raakt of overschrijdt.

    LONG:  current_price >= take_profit_price
    SHORT: current_price <= take_profit_price
    """

    reason: ExitReason = field(default=ExitReason.TAKE_PROFIT, init=False)

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        if position.side == PositionSide.LONG:
            triggered = position.current_price >= position.take_profit_price
        else:
            triggered = position.current_price <= position.take_profit_price

        detail = (
            f"current={position.current_price} take_profit={position.take_profit_price}"
            if triggered else ""
        )
        return ConditionResult(triggered=triggered, reason=self.reason, detail=detail)


# ---------------------------------------------------------------------------
# 3. TTL
# ---------------------------------------------------------------------------

@dataclass
class TTLCondition(ExitCondition):
    """
    Triggert wanneer de positie zijn maximale levensduur heeft overschreden.

    Gebruikt position.ttl (in seconden) en position.opened_at.
    """

    reason: ExitReason = field(default=ExitReason.TTL, init=False)

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        age = position.age_seconds()
        triggered = age >= position.ttl

        detail = (
            f"age={age:.1f}s ttl={position.ttl}s"
            if triggered else ""
        )
        return ConditionResult(triggered=triggered, reason=self.reason, detail=detail)


# ---------------------------------------------------------------------------
# 4. Drawdown-breach
# ---------------------------------------------------------------------------

@dataclass
class DrawdownCondition(ExitCondition):
    """
    Triggert wanneer de terugval van de peak de drempel overschrijdt.

    max_drawdown_pct: drempelwaarde (0–1), bijv. 0.05 = 5% terugval van peak.

    Gebruikt position.drawdown_from_peak_pct() — peak wordt door de
    evaluator bijgehouden in position.peak_price.
    """

    max_drawdown_pct: float

    reason: ExitReason = field(default=ExitReason.DRAWDOWN_BREACH, init=False)

    def __post_init__(self) -> None:
        if not (0 < self.max_drawdown_pct <= 1):
            raise ValueError(
                f"max_drawdown_pct must be in (0, 1], got {self.max_drawdown_pct}"
            )

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        drawdown = position.drawdown_from_peak_pct()
        triggered = drawdown > self.max_drawdown_pct

        detail = (
            f"drawdown={drawdown:.4f} max_drawdown={self.max_drawdown_pct}"
            if triggered else ""
        )
        return ConditionResult(triggered=triggered, reason=self.reason, detail=detail)


# ---------------------------------------------------------------------------
# 5. Daily-loss-breach
# ---------------------------------------------------------------------------

@dataclass
class DailyLossCondition(ExitCondition):
    """
    Triggert wanneer het totale dagverlies de limiet overschrijdt.

    daily_loss_limit: maximaal toegestaan verlies op één handelsdag
                      (positief getal, in basismunt).

    Leest context.daily_loss_so_far — extern bijgehouden.
    Als context None is, wordt de conditie niet getriggerd.
    """

    daily_loss_limit: float

    reason: ExitReason = field(default=ExitReason.DAILY_LOSS_BREACH, init=False)

    def __post_init__(self) -> None:
        if self.daily_loss_limit <= 0:
            raise ValueError(
                f"daily_loss_limit must be > 0, got {self.daily_loss_limit}"
            )

    def check(self, position: PaperPosition, context: CheckContext | None = None) -> ConditionResult:
        if context is None:
            return ConditionResult.not_triggered(self.reason)

        triggered = context.daily_loss_so_far >= self.daily_loss_limit

        detail = (
            f"daily_loss={context.daily_loss_so_far:.2f} limit={self.daily_loss_limit:.2f}"
            if triggered else ""
        )
        return ConditionResult(triggered=triggered, reason=self.reason, detail=detail)
