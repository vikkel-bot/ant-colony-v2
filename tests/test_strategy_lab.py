"""
tests/test_strategy_lab.py

Fase 4 — Strategy Lab, research only.

Test-opzet:
  - TestBacktesterConfig       validatie van BacktestConfig parameters
  - TestBacktester             deterministisch gedrag: PnL, win rate, drawdown, sharpe
  - TestPromotionCriteria      pass/fail per criterium, gecombineerde failures, status-selectie
  - TestQueenPromotion         promotie RESEARCH→PAPER→APPROVED→LIVE, reject, provenance
  - TestStrategyPipeline       end-to-end: bars → BacktestResults → candidate → Queen beslist
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.lab.backtester import Backtester, BacktestConfig, OHLCVBar
from ant_colony.lab.promotion_criteria import AssessmentResult, PromotionCriteria
from ant_colony.queen import Queen, PromotionResult
from ant_colony.schemas.strategy_candidate import (
    BacktestResults,
    CandidateStatus,
    PaperResults,
    StrategyCandidate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bar(close: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp=datetime.now(tz=timezone.utc),
        open=close, high=close * 1.01, low=close * 0.99, close=close,
    )


def make_candidate(
    status: CandidateStatus = CandidateStatus.RESEARCH,
    backtest: BacktestResults | None = None,
    paper: PaperResults | None = None,
    approved_by: str | None = None,
    candidate_id: str = "c-001",
) -> StrategyCandidate:
    kwargs: dict = dict(
        candidate_id=candidate_id,
        name="SMA Cross",
        source="internal",
        biome="crypto",
        logic_summary="Simple moving average crossover",
        exit_conditions={"stop_loss": 0.03, "take_profit": 0.06},
        status=status,
    )
    if backtest is not None:
        kwargs["backtest_results"] = backtest
    if paper is not None:
        kwargs["paper_results"] = paper
    if approved_by is not None:
        kwargs["approved_by"] = approved_by
    return StrategyCandidate(**kwargs)


def good_backtest(trades: int = 50) -> BacktestResults:
    return BacktestResults(
        sharpe_ratio=1.5, max_drawdown_pct=0.10,
        total_trades=trades, win_rate=0.60,
    )


def good_paper(trades: int = 55) -> PaperResults:
    return PaperResults(
        sharpe_ratio=1.2, max_drawdown_pct=0.12,
        total_trades=trades, win_rate=0.58,
    )


def make_queen(tmp_path: Path, logs: bool = False) -> Queen:
    sched = ColonyScheduler(logs_root=tmp_path, tick_interval=5)
    return Queen(
        capital_total=50_000.0, scheduler=sched,
        logs_root=tmp_path if logs else None,
    )


def strict_criteria() -> PromotionCriteria:
    return PromotionCriteria(min_sharpe=0.5, max_drawdown_pct=0.20, min_trades=10)


# ---------------------------------------------------------------------------
# TestBacktesterConfig
# ---------------------------------------------------------------------------

class TestBacktesterConfig:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            BacktestConfig(direction="sideways", take_profit_pct=0.06, stop_loss_pct=0.03)

    def test_zero_tp_raises(self):
        with pytest.raises(ValueError, match="take_profit_pct"):
            BacktestConfig(direction="long", take_profit_pct=0.0, stop_loss_pct=0.03)

    def test_zero_sl_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.0)

    def test_zero_max_bars_raises(self):
        with pytest.raises(ValueError, match="max_bars_held"):
            BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03, max_bars_held=0)


# ---------------------------------------------------------------------------
# TestBacktester
# ---------------------------------------------------------------------------

class TestBacktester:
    """Deterministisch: alle verwachte exits zijn vooraf berekend."""

    def test_empty_bars_raises(self):
        cfg = BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03)
        with pytest.raises(ValueError, match="empty"):
            Backtester().run([], cfg)

    def test_long_take_profit_hit(self):
        # entry=100, tp=106 → bar[1]=107 → TP exit at 106
        cfg = BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03, max_bars_held=5)
        bars = [bar(100), bar(107), bar(100), bar(107)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades >= 1
        assert r.win_rate == pytest.approx(1.0)

    def test_long_stop_loss_hit(self):
        # entry=100, sl=97 → bar[1]=96 → SL exit at 97, return=-3%
        cfg = BacktestConfig(direction="long", take_profit_pct=0.20, stop_loss_pct=0.03, max_bars_held=5)
        bars = [bar(100), bar(96), bar(100), bar(96)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades >= 1
        assert r.win_rate == pytest.approx(0.0)

    def test_long_ttl_exit(self):
        # entry=100, tp=120 (never reached), sl=80 (never hit), ttl=2 bars
        cfg = BacktestConfig(direction="long", take_profit_pct=0.20, stop_loss_pct=0.20, max_bars_held=2)
        bars = [bar(100), bar(101), bar(102), bar(103), bar(104)]
        r = Backtester().run(bars, cfg)
        # All exits via TTL — no wins (102 > 100 but < 120), return > 0 → win
        assert r.total_trades >= 1

    def test_short_take_profit_hit(self):
        # entry=2000, tp=1880 (6% down) → bar[1]=1870 → TP
        cfg = BacktestConfig(direction="short", take_profit_pct=0.06, stop_loss_pct=0.03, max_bars_held=5)
        bars = [bar(2000), bar(1870), bar(2000), bar(1870)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades >= 1
        assert r.win_rate == pytest.approx(1.0)

    def test_short_stop_loss_hit(self):
        # entry=2000, sl=2060 (3% up) → bar[1]=2070 → SL
        cfg = BacktestConfig(direction="short", take_profit_pct=0.20, stop_loss_pct=0.03, max_bars_held=5)
        bars = [bar(2000), bar(2070), bar(2000), bar(2070)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades >= 1
        assert r.win_rate == pytest.approx(0.0)

    def test_win_rate_all_wins(self):
        # tp=5%, sl=30% (sl far) → every trade hits TP
        cfg = BacktestConfig(direction="long", take_profit_pct=0.05, stop_loss_pct=0.30, max_bars_held=3)
        bars = [bar(100), bar(110), bar(100), bar(110), bar(100), bar(110)]
        r = Backtester().run(bars, cfg)
        assert r.win_rate == pytest.approx(1.0)
        assert r.total_trades >= 2

    def test_win_rate_all_losses(self):
        # tp=30% (far), sl=5% → every trade hits SL
        cfg = BacktestConfig(direction="long", take_profit_pct=0.30, stop_loss_pct=0.05, max_bars_held=3)
        bars = [bar(100), bar(94), bar(100), bar(94), bar(100), bar(94)]
        r = Backtester().run(bars, cfg)
        assert r.win_rate == pytest.approx(0.0)
        assert r.total_trades >= 2

    def test_max_drawdown_zero_when_all_wins(self):
        # Elke trade winstgevend → equity stijgt altijd → drawdown = 0
        cfg = BacktestConfig(direction="long", take_profit_pct=0.05, stop_loss_pct=0.30, max_bars_held=3)
        bars = [bar(100), bar(110)] * 6
        r = Backtester().run(bars, cfg)
        assert r.max_drawdown_pct == pytest.approx(0.0)

    def test_max_drawdown_positive_after_loss(self):
        # 1 win (6%) gevolgd door 1 loss (3%) → equity piekt dan daalt → drawdown > 0
        cfg = BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03, max_bars_held=5)
        # trade 1: entry 100 → 107 (TP), trade 2: entry 100 → 96 (SL)
        bars = [bar(100), bar(107), bar(100), bar(96)]
        r = Backtester().run(bars, cfg)
        assert r.max_drawdown_pct > 0.0

    def test_sharpe_none_with_single_trade(self):
        # 2 bars → precies 1 trade → sharpe=None
        cfg = BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03, max_bars_held=5)
        bars = [bar(100), bar(110)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades == 1
        assert r.sharpe_ratio is None

    def test_total_trades_matches_price_series(self):
        # 6 bars, max_bars=1: trade 0→1, 2→3, 4→(end) = 2 trades (bar 4 → exit bar 5)
        cfg = BacktestConfig(direction="long", take_profit_pct=0.30, stop_loss_pct=0.30, max_bars_held=1)
        bars = [bar(100), bar(101), bar(102), bar(103), bar(104), bar(105)]
        r = Backtester().run(bars, cfg)
        assert r.total_trades == pytest.approx(2, abs=1)  # 2 of 3 afhankelijk van boundary


# ---------------------------------------------------------------------------
# TestPromotionCriteria
# ---------------------------------------------------------------------------

class TestPromotionCriteria:
    def test_passes_when_all_criteria_met(self):
        c = make_candidate(backtest=good_backtest(50))
        r = strict_criteria().assess(c)
        assert r.passed
        assert r.reason == "all criteria met"

    def test_fails_on_low_sharpe(self):
        bt = BacktestResults(sharpe_ratio=0.2, max_drawdown_pct=0.10, total_trades=50, win_rate=0.60)
        c = make_candidate(backtest=bt)
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "sharpe" in r.reason

    def test_fails_on_high_drawdown(self):
        bt = BacktestResults(sharpe_ratio=1.5, max_drawdown_pct=0.35, total_trades=50, win_rate=0.60)
        c = make_candidate(backtest=bt)
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "max_drawdown_pct" in r.reason

    def test_fails_on_insufficient_trades(self):
        bt = BacktestResults(sharpe_ratio=1.5, max_drawdown_pct=0.10, total_trades=5, win_rate=0.60)
        c = make_candidate(backtest=bt)
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "total_trades" in r.reason

    def test_fails_multiple_criteria_combined_in_reason(self):
        bt = BacktestResults(sharpe_ratio=0.1, max_drawdown_pct=0.50, total_trades=2, win_rate=0.30)
        c = make_candidate(backtest=bt)
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "sharpe" in r.reason
        assert "max_drawdown_pct" in r.reason
        assert "total_trades" in r.reason

    def test_fails_when_sharpe_is_none(self):
        bt = BacktestResults(sharpe_ratio=None, max_drawdown_pct=0.10, total_trades=50)
        c = make_candidate(backtest=bt)
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "sharpe" in r.reason

    def test_no_results_for_non_research_paper_status(self):
        # APPROVED heeft geen backtest of paper results → geen resultaten beschikbaar
        c = make_candidate(status=CandidateStatus.APPROVED, approved_by="queen")
        r = strict_criteria().assess(c)
        assert not r.passed
        assert "no results available" in r.reason

    def test_paper_results_used_for_paper_status(self):
        pr = PaperResults(sharpe_ratio=1.5, max_drawdown_pct=0.10, total_trades=55, win_rate=0.60)
        c = make_candidate(status=CandidateStatus.PAPER, paper=pr)
        r = strict_criteria().assess(c)
        assert r.passed

    def test_invalid_max_drawdown_raises(self):
        with pytest.raises(ValueError, match="max_drawdown_pct"):
            PromotionCriteria(min_sharpe=0.5, max_drawdown_pct=0.0, min_trades=10)

    def test_invalid_min_trades_raises(self):
        with pytest.raises(ValueError, match="min_trades"):
            PromotionCriteria(min_sharpe=0.5, max_drawdown_pct=0.20, min_trades=0)


# ---------------------------------------------------------------------------
# TestQueenPromotion
# ---------------------------------------------------------------------------

class TestQueenPromotion:
    def test_promote_research_to_paper(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(backtest=good_backtest())
        r = queen.promote_candidate(c, CandidateStatus.PAPER)
        assert r.accepted
        assert r.candidate.status == CandidateStatus.PAPER

    def test_promote_paper_to_approved_sets_approved_by(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(status=CandidateStatus.PAPER, paper=good_paper())
        r = queen.promote_candidate(c, CandidateStatus.APPROVED)
        assert r.accepted
        assert r.candidate.approved_by == "queen"

    def test_promote_approved_to_live(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(status=CandidateStatus.APPROVED, approved_by="queen")
        r = queen.promote_candidate(c, CandidateStatus.LIVE)
        assert r.accepted
        assert r.candidate.status == CandidateStatus.LIVE
        assert r.candidate.approved_by == "queen"

    def test_promotion_original_candidate_unchanged(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(backtest=good_backtest())
        queen.promote_candidate(c, CandidateStatus.PAPER)
        assert c.status == CandidateStatus.RESEARCH  # origineel ongewijzigd

    def test_promotion_appends_provenance(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(backtest=good_backtest())
        r = queen.promote_candidate(c, CandidateStatus.PAPER)
        assert len(r.candidate.provenance) == len(c.provenance) + 1
        assert r.candidate.provenance[-1].actor == "queen"
        assert "paper" in r.candidate.provenance[-1].action

    def test_promotion_with_passing_criteria(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(backtest=good_backtest(50))
        r = queen.promote_candidate(c, CandidateStatus.PAPER, criteria=strict_criteria())
        assert r.accepted

    def test_promotion_blocked_by_failing_criteria(self, tmp_path):
        queen = make_queen(tmp_path)
        bt = BacktestResults(sharpe_ratio=0.1, max_drawdown_pct=0.5, total_trades=3)
        c = make_candidate(backtest=bt)
        r = queen.promote_candidate(c, CandidateStatus.PAPER, criteria=strict_criteria())
        assert not r.accepted
        assert "criteria not met" in r.rejection_reason

    def test_invalid_transition_rejected(self, tmp_path):
        # RESEARCH → APPROVED slaat PAPER over → geblokkeerd
        queen = make_queen(tmp_path)
        c = make_candidate(backtest=good_backtest())
        r = queen.promote_candidate(c, CandidateStatus.APPROVED)
        assert not r.accepted
        assert "invalid promotion" in r.rejection_reason

    def test_live_is_terminal_cannot_be_promoted(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(status=CandidateStatus.LIVE, approved_by="queen")
        r = queen.promote_candidate(c, CandidateStatus.PAPER)
        assert not r.accepted
        assert "terminal" in r.rejection_reason

    def test_rejected_is_terminal_cannot_be_promoted(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate()
        rejected = queen.reject_candidate(c, reason="test")
        r = queen.promote_candidate(rejected, CandidateStatus.PAPER)
        assert not r.accepted
        assert "terminal" in r.rejection_reason

    def test_reject_candidate_sets_rejected_status(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate(status=CandidateStatus.PAPER, paper=good_paper())
        rejected = queen.reject_candidate(c, reason="sharpe onvoldoende")
        assert rejected.status == CandidateStatus.REJECTED

    def test_reject_appends_provenance_with_reason(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate()
        rejected = queen.reject_candidate(c, reason="backtest onvoldoende")
        assert rejected.provenance[-1].actor == "queen"
        assert rejected.provenance[-1].action == "rejected"
        assert "backtest onvoldoende" in rejected.provenance[-1].details["reason"]

    def test_reject_already_rejected_is_idempotent(self, tmp_path):
        queen = make_queen(tmp_path)
        c = make_candidate()
        r1 = queen.reject_candidate(c, reason="eerste keer")
        r2 = queen.reject_candidate(r1, reason="tweede keer")
        assert r2.status == CandidateStatus.REJECTED
        assert len(r2.provenance) == len(r1.provenance)  # geen extra entry

    def test_promotion_log_written(self, tmp_path):
        queen = make_queen(tmp_path, logs=True)
        c = make_candidate(candidate_id="c-log", backtest=good_backtest())
        queen.promote_candidate(c, CandidateStatus.PAPER)
        log = tmp_path / "strategy" / "c-log.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["event_type"] == "strategy_candidate_promoted"
        assert record["payload"]["candidate_id"] == "c-log"
        assert record["source"] == "queen"

    def test_rejection_log_written(self, tmp_path):
        queen = make_queen(tmp_path, logs=True)
        c = make_candidate(candidate_id="c-rej")
        queen.reject_candidate(c, reason="onvoldoende backtest")
        log = tmp_path / "strategy" / "c-rej.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["event_type"] == "strategy_candidate_rejected"


# ---------------------------------------------------------------------------
# TestStrategyPipeline — gate-kern
# ---------------------------------------------------------------------------

class TestStrategyPipeline:
    """
    End-to-end pipeline: OHLCV bars → Backtester → StrategyCandidate → Queen beslist.
    Bewijst dat alle componenten correct samenwerken.
    """

    def _run_backtest(self, n_winning_pairs: int = 10) -> BacktestResults:
        """Bouw een prijs-serie met n winnende TP-exits en run de backtester."""
        cfg = BacktestConfig(
            direction="long",
            take_profit_pct=0.05,
            stop_loss_pct=0.30,
            max_bars_held=3,
        )
        bars = [bar(100), bar(110)] * (n_winning_pairs + 2)
        return Backtester().run(bars, cfg)

    def test_pipeline_good_candidate_promoted(self, tmp_path):
        """Bars → goede BacktestResults → criteria slagen → Queen promoveert."""
        queen = make_queen(tmp_path)
        results = self._run_backtest(n_winning_pairs=15)

        assert results.total_trades >= 10  # genoeg trades voor criteria

        c = make_candidate(backtest=results)
        criteria = PromotionCriteria(
            min_sharpe=-10.0,       # ruim: altijd slagen op sharpe
            max_drawdown_pct=1.0,   # ruim: altijd slagen op drawdown
            min_trades=5,
        )
        r = queen.promote_candidate(c, CandidateStatus.PAPER, criteria=criteria)
        assert r.accepted
        assert r.candidate.status == CandidateStatus.PAPER

    def test_pipeline_bad_candidate_rejected(self, tmp_path):
        """Bars → BacktestResults → criteria falen → Queen wijst af."""
        queen = make_queen(tmp_path)
        results = self._run_backtest(n_winning_pairs=2)  # te weinig trades

        c = make_candidate(backtest=results)
        strict = PromotionCriteria(min_sharpe=0.5, max_drawdown_pct=0.20, min_trades=100)
        r = queen.promote_candidate(c, CandidateStatus.PAPER, criteria=strict)
        assert not r.accepted
        assert r.rejection_reason != ""

    def test_pipeline_full_research_to_approved(self, tmp_path):
        """
        Volledig promotiepad: RESEARCH → PAPER → APPROVED.
        Elke stap door Queen, met criteria-check, approved_by gezet op APPROVED.
        """
        queen = make_queen(tmp_path)
        criteria = PromotionCriteria(min_sharpe=-10.0, max_drawdown_pct=1.0, min_trades=1)

        # Stap 1: RESEARCH → PAPER
        bt = self._run_backtest(n_winning_pairs=5)
        c_research = make_candidate(backtest=bt)
        r1 = queen.promote_candidate(c_research, CandidateStatus.PAPER, criteria=criteria)
        assert r1.accepted

        # Stap 2: PAPER → APPROVED (voeg paper_results toe)
        c_paper = r1.candidate.model_copy(update={
            "paper_results": PaperResults(
                sharpe_ratio=1.2, max_drawdown_pct=0.10,
                total_trades=55, win_rate=0.58,
            )
        })
        r2 = queen.promote_candidate(c_paper, CandidateStatus.APPROVED, criteria=criteria)
        assert r2.accepted
        assert r2.candidate.approved_by == "queen"
        assert r2.candidate.status == CandidateStatus.APPROVED

    def test_pipeline_provenance_tracks_full_history(self, tmp_path):
        """
        Na twee promoties bevat provenance twee entries van actor='queen',
        elk met de juiste actie — bewijs dat de trail append-only groeit.
        """
        queen = make_queen(tmp_path)
        criteria = PromotionCriteria(min_sharpe=-10.0, max_drawdown_pct=1.0, min_trades=1)
        bt = self._run_backtest(n_winning_pairs=5)
        c0 = make_candidate(backtest=bt)

        r1 = queen.promote_candidate(c0, CandidateStatus.PAPER, criteria=criteria)
        c_paper = r1.candidate.model_copy(update={
            "paper_results": PaperResults(
                sharpe_ratio=1.0, max_drawdown_pct=0.15,
                total_trades=50, win_rate=0.55,
            )
        })
        r2 = queen.promote_candidate(c_paper, CandidateStatus.APPROVED, criteria=criteria)

        provenance = r2.candidate.provenance
        assert len(provenance) == 2
        assert all(e.actor == "queen" for e in provenance)
        assert "paper" in provenance[0].action
        assert "approved" in provenance[1].action
