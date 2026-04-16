"""
ant_colony/lab/promotion_criteria.py

PromotionCriteria — drempelwaarden voor Queen-promotie van StrategyCandidate.

Werking:
  - PromotionCriteria bevat drie drempelwaarden: min_sharpe, max_drawdown_pct, min_trades
  - assess(candidate) kiest automatisch de juiste resultaten op basis van status:
      RESEARCH → backtest_results getoetst
      PAPER    → paper_results getoetst
  - Retourneert AssessmentResult(passed, reason) — nooit een exception

Gebruik in de promotieketen (COLONY_GOVERNANCE.md §4):
  Research → Paper:   backtest sharpe ≥ drempel, drawdown ≤ drempel, trades ≥ min
  Paper → Approved:   paper sharpe ≥ drempel, drawdown ≤ drempel, trades ≥ min

De criteria zijn door de Queen configureerbaar per biome of strategie-type.
Geen code wordt uitgevoerd bij import (P7).
"""

from __future__ import annotations

from dataclasses import dataclass

from ant_colony.schemas.strategy_candidate import (
    BacktestResults,
    CandidateStatus,
    PaperResults,
    StrategyCandidate,
)


# ---------------------------------------------------------------------------
# Assessment resultaat
# ---------------------------------------------------------------------------

@dataclass
class AssessmentResult:
    """
    Resultaat van een PromotionCriteria.assess()-aanroep.

    passed:  True als de candidate aan alle criteria voldoet.
    reason:  Mensleesbare toelichting — bij falen: welk criterium faalde en waarom.
    """
    passed: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# PromotionCriteria
# ---------------------------------------------------------------------------

@dataclass
class PromotionCriteria:
    """
    Drempelwaarden voor Queen-promotie van een StrategyCandidate.

    Args:
        min_sharpe:        Minimale Sharpe ratio (mag negatief zijn als drempel).
        max_drawdown_pct:  Maximale toegestane drawdown als fractie (0, 1].
        min_trades:        Minimaal aantal voltooide trades (≥ 1).

    Usage::

        criteria = PromotionCriteria(min_sharpe=0.5, max_drawdown_pct=0.20, min_trades=30)
        result = criteria.assess(candidate)
        if result.passed:
            queen.promote_candidate(candidate, CandidateStatus.PAPER)
        else:
            print("Afgewezen:", result.reason)
    """
    min_sharpe: float
    max_drawdown_pct: float
    min_trades: int

    def __post_init__(self) -> None:
        if not (0 < self.max_drawdown_pct <= 1.0):
            raise ValueError(
                f"max_drawdown_pct must be in (0, 1], got {self.max_drawdown_pct}"
            )
        if self.min_trades < 1:
            raise ValueError(
                f"min_trades must be >= 1, got {self.min_trades}"
            )

    # ------------------------------------------------------------------
    # Publieke API
    # ------------------------------------------------------------------

    def assess(self, candidate: StrategyCandidate) -> AssessmentResult:
        """
        Toets een candidate aan de criteria.

        Kiest automatisch de juiste resultaten op basis van candidate.status:
          - RESEARCH → backtest_results
          - PAPER    → paper_results
          - Overig   → altijd failed (geen relevante resultaten beschikbaar)

        Alle drie de criteria worden gecontroleerd; alle faalredenen worden
        gecombineerd in één reason-string.

        Args:
            candidate: De te toetsen StrategyCandidate.

        Returns:
            AssessmentResult — nooit een exception.
        """
        results = self._select_results(candidate)

        if results is None:
            return AssessmentResult(
                passed=False,
                reason=(
                    f"no results available for status='{candidate.status.value}' — "
                    f"expected backtest_results (RESEARCH) or paper_results (PAPER)"
                ),
            )

        failures: list[str] = []

        # --- sharpe ---
        if results.sharpe_ratio is None:
            failures.append(f"sharpe=None (required >= {self.min_sharpe})")
        elif results.sharpe_ratio < self.min_sharpe:
            failures.append(
                f"sharpe={results.sharpe_ratio:.4f} < min={self.min_sharpe}"
            )

        # --- drawdown ---
        if results.max_drawdown_pct is None:
            failures.append(f"max_drawdown_pct=None (required <= {self.max_drawdown_pct})")
        elif results.max_drawdown_pct > self.max_drawdown_pct:
            failures.append(
                f"max_drawdown_pct={results.max_drawdown_pct:.4f} > max={self.max_drawdown_pct}"
            )

        # --- trade count ---
        if results.total_trades is None:
            failures.append(f"total_trades=None (required >= {self.min_trades})")
        elif results.total_trades < self.min_trades:
            failures.append(
                f"total_trades={results.total_trades} < min={self.min_trades}"
            )

        if failures:
            return AssessmentResult(passed=False, reason="; ".join(failures))

        return AssessmentResult(passed=True, reason="all criteria met")

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    @staticmethod
    def _select_results(
        candidate: StrategyCandidate,
    ) -> BacktestResults | PaperResults | None:
        """
        Kies de relevante resultaten op basis van candidate.status.

        RESEARCH → backtest_results (bewijs van backtest-fase)
        PAPER    → paper_results    (bewijs van paper-trading fase)
        Overig   → None             (geen relevante resultaten)
        """
        if candidate.status == CandidateStatus.RESEARCH:
            return candidate.backtest_results
        if candidate.status == CandidateStatus.PAPER:
            return candidate.paper_results
        return None
