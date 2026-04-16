"""
ant_colony/exit_chain/exit_evaluator.py

ExitEvaluator — combineert condities en evalueert een positie per tick.

Verantwoordelijkheden:
  1. Peak-price bijhouden (trailing, kant-bewust voor LONG/SHORT)
  2. Alle condities checken in opgegeven volgorde
  3. Bij trigger: positie sluiten en EvaluationResult teruggeven
  4. Geen side-effects buiten de teruggegeven PaperPosition

Ontwerp:
  - ExitEvaluator is stateless — staat leeft in PaperPosition
  - Condities worden bij constructie geïnjecteerd
  - Volgorde van condities = prioriteitsvolgorde (index 0 = hoogste prioriteit)
  - Aanbevolen volgorde: DailyLoss → Drawdown → StopLoss → TTL → TakeProfit
  - evaluate() retourneert altijd een nieuwe PaperPosition (immutable update)
  - Gesloten posities passeren evaluate() ongewijzigd (no-op)

Fail-closed:
  - Als new_price <= 0: evaluatie geblokkeerd, stale_price=True in result
  - Als positie al gesloten is: onmiddellijk teruggeven, geen checks
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ant_colony.exit_chain.exit_conditions import (
    CheckContext,
    ConditionResult,
    ExitCondition,
    ExitReason,
)
from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus


# ---------------------------------------------------------------------------
# Evaluatie-resultaat
# ---------------------------------------------------------------------------

@dataclass
class EvaluationResult:
    """
    Resultaat van één evaluate()-aanroep.

    position:       Bijgewerkte PaperPosition (nieuwe prijs, mogelijk gesloten)
    triggered:      Welke conditie triggererde, of None als geen
    peak_updated:   True als peak_price werd bijgewerkt in deze tick
    stale_price:    True als new_price ongeldig was — evaluatie geblokkeerd
    """
    position: PaperPosition
    triggered: ConditionResult | None = None
    peak_updated: bool = False
    stale_price: bool = False

    @property
    def exit_occurred(self) -> bool:
        return self.triggered is not None and self.triggered.triggered


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class ExitEvaluator:
    """
    Evalueert een PaperPosition per tick tegen een geordende lijst condities.

    Args:
        conditions: Condities in prioriteitsvolgorde (index 0 = eerste check).
                    Aanbevolen: [DailyLoss, Drawdown, StopLoss, TTL, TakeProfit]

    Usage::

        evaluator = ExitEvaluator(conditions=[
            DailyLossCondition(daily_loss_limit=200.0),
            DrawdownCondition(max_drawdown_pct=0.05),
            StopLossCondition(),
            TTLCondition(),
            TakeProfitCondition(),
        ])

        result = evaluator.evaluate(position, new_price=31500.0)
        position = result.position  # altijd de bijgewerkte versie gebruiken
    """

    def __init__(self, conditions: list[ExitCondition]) -> None:
        if not conditions:
            raise ValueError("ExitEvaluator vereist minimaal één conditie")
        self._conditions = conditions

    # ------------------------------------------------------------------
    # Publieke API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        position: PaperPosition,
        new_price: float,
        context: CheckContext | None = None,
    ) -> EvaluationResult:
        """
        Evalueer de positie op basis van de nieuwe marktprijs.

        Stappen:
          1. Valideer new_price
          2. Sla door als positie al gesloten is
          3. Update current_price en peak_price
          4. Check condities in volgorde
          5. Sluit positie als conditie triggert
        """
        # --- stap 1: valideer prijs ---
        if new_price <= 0:
            return EvaluationResult(position=position, stale_price=True)

        # --- stap 2: al gesloten ---
        if not position.is_open():
            return EvaluationResult(position=position)

        # --- stap 3: update prijs en peak ---
        new_peak, peak_updated = self._update_peak(position, new_price)
        position = position.model_copy(update={
            "current_price": new_price,
            "peak_price": new_peak,
        })

        # --- stap 4: check condities ---
        for condition in self._conditions:
            result = condition.check(position, context)
            if result.triggered:
                # --- stap 5: sluit positie ---
                position = self._close_position(position, result)
                return EvaluationResult(
                    position=position,
                    triggered=result,
                    peak_updated=peak_updated,
                )

        return EvaluationResult(position=position, peak_updated=peak_updated)

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _update_peak(
        self, position: PaperPosition, new_price: float
    ) -> tuple[float, bool]:
        """
        Bereken nieuwe peak_price en of hij werd bijgewerkt.

        LONG:  peak = hoogste prijs gezien
        SHORT: peak = laagste prijs gezien
        """
        old_peak = position.peak_price
        if position.side == PositionSide.LONG:
            new_peak = max(old_peak, new_price)
        else:
            new_peak = min(old_peak, new_price)
        return new_peak, new_peak != old_peak

    def _close_position(
        self, position: PaperPosition, result: ConditionResult
    ) -> PaperPosition:
        """
        Geef een gesloten kopie van de positie terug.

        exit_price = current_price op het moment van sluiten
        """
        return position.model_copy(update={
            "status": result.reason.to_position_status(),
            "exit_price": position.current_price,
            "closed_at": datetime.now(timezone.utc),
            "exit_reason": f"{result.reason.value}: {result.detail}".rstrip(": "),
        })
