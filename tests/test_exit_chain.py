"""
tests/test_exit_chain.py

Exit-keten validatie harness.

Bewijst dat elk exit-pad correct en betrouwbaar triggert voor zowel
LONG als SHORT posities, inclusief prioriteitsordening, peak-trailing,
multi-tick scenario's en fail-closed gedrag.

Gate: EXIT_KETEN_VOLLEDIG_CORRECT
  → alle tests in dit bestand groen
"""

import pytest
from datetime import datetime, timedelta, timezone

from ant_colony.exit_chain.exit_conditions import (
    CheckContext,
    ConditionResult,
    DailyLossCondition,
    DrawdownCondition,
    ExitReason,
    StopLossCondition,
    TakeProfitCondition,
    TTLCondition,
)
from ant_colony.exit_chain.exit_evaluator import EvaluationResult, ExitEvaluator
from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_long(
    entry_price: float = 30_000.0,
    current_price: float = 30_000.0,
    peak_price: float | None = None,
    stop_loss_price: float = 29_000.0,
    take_profit_price: float = 32_000.0,
    ttl: int = 3600,
    opened_seconds_ago: int = 0,
    quantity: float = 0.1,
) -> PaperPosition:
    return PaperPosition(
        position_id="pos-long",
        symbol="BTC-EUR",
        biome="crypto",
        mission_id="m-001",
        ant_id="ant-001",
        side=PositionSide.LONG,
        entry_price=entry_price,
        quantity=quantity,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        ttl=ttl,
        current_price=current_price,
        peak_price=peak_price if peak_price is not None else current_price,
        opened_at=datetime.now(tz=timezone.utc) - timedelta(seconds=opened_seconds_ago),
    )


def make_short(
    entry_price: float = 2_000.0,
    current_price: float = 2_000.0,
    peak_price: float | None = None,
    stop_loss_price: float = 2_100.0,
    take_profit_price: float = 1_800.0,
    ttl: int = 3600,
    opened_seconds_ago: int = 0,
    quantity: float = 1.0,
) -> PaperPosition:
    return PaperPosition(
        position_id="pos-short",
        symbol="ETH-EUR",
        biome="crypto",
        mission_id="m-001",
        ant_id="ant-001",
        side=PositionSide.SHORT,
        entry_price=entry_price,
        quantity=quantity,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        ttl=ttl,
        current_price=current_price,
        peak_price=peak_price if peak_price is not None else current_price,
        opened_at=datetime.now(tz=timezone.utc) - timedelta(seconds=opened_seconds_ago),
    )


def default_evaluator() -> ExitEvaluator:
    """Aanbevolen prioriteitsvolgorde."""
    return ExitEvaluator([
        DailyLossCondition(daily_loss_limit=200.0),
        DrawdownCondition(max_drawdown_pct=0.05),
        StopLossCondition(),
        TTLCondition(),
        TakeProfitCondition(),
    ])


# ---------------------------------------------------------------------------
# PaperPosition — exit-keten specifieke validatie
# ---------------------------------------------------------------------------

class TestPaperPositionValidation:
    def test_long_stop_loss_below_entry_is_valid(self):
        p = make_long(entry_price=30_000, stop_loss_price=29_000)
        assert p.stop_loss_price < p.entry_price

    def test_long_stop_loss_at_or_above_entry_is_rejected(self):
        with pytest.raises(Exception, match="below entry_price"):
            make_long(entry_price=30_000, stop_loss_price=30_000)
        with pytest.raises(Exception, match="below entry_price"):
            make_long(entry_price=30_000, stop_loss_price=30_001)

    def test_long_take_profit_above_entry_is_valid(self):
        p = make_long(entry_price=30_000, take_profit_price=32_000)
        assert p.take_profit_price > p.entry_price

    def test_long_take_profit_at_or_below_entry_is_rejected(self):
        with pytest.raises(Exception, match="above entry_price"):
            make_long(entry_price=30_000, take_profit_price=30_000)
        with pytest.raises(Exception, match="above entry_price"):
            make_long(entry_price=30_000, take_profit_price=29_999)

    def test_short_stop_loss_above_entry_is_valid(self):
        p = make_short(entry_price=2_000, stop_loss_price=2_100)
        assert p.stop_loss_price > p.entry_price

    def test_short_stop_loss_at_or_below_entry_is_rejected(self):
        with pytest.raises(Exception, match="above entry_price"):
            make_short(entry_price=2_000, stop_loss_price=2_000)
        with pytest.raises(Exception, match="above entry_price"):
            make_short(entry_price=2_000, stop_loss_price=1_999)

    def test_short_take_profit_below_entry_is_valid(self):
        p = make_short(entry_price=2_000, take_profit_price=1_800)
        assert p.take_profit_price < p.entry_price

    def test_short_take_profit_at_or_above_entry_is_rejected(self):
        with pytest.raises(Exception, match="below entry_price"):
            make_short(entry_price=2_000, take_profit_price=2_000)
        with pytest.raises(Exception, match="below entry_price"):
            make_short(entry_price=2_000, take_profit_price=2_001)

    def test_closed_position_without_exit_fields_is_rejected(self):
        # Pydantic v2 model_copy() bypasses validators by design.
        # Validatie wordt afgedwongen bij constructie — test dat pad.
        with pytest.raises(Exception, match="exit_price"):
            PaperPosition(
                position_id="pos-closed",
                symbol="BTC-EUR",
                biome="crypto",
                mission_id="m-001",
                ant_id="ant-001",
                side=PositionSide.LONG,
                entry_price=30_000.0,
                quantity=0.1,
                stop_loss_price=29_000.0,
                take_profit_price=32_000.0,
                ttl=3600,
                current_price=28_999.0,
                peak_price=30_000.0,
                status=PositionStatus.CLOSED_STOP_LOSS,
                exit_price=None,
                closed_at=None,
                exit_reason=None,
            )

    def test_realized_pnl_long_profit(self):
        p = make_long(entry_price=30_000, quantity=0.1)
        closed = p.model_copy(update={
            "status": PositionStatus.CLOSED_TAKE_PROFIT,
            "exit_price": 32_000.0,
            "closed_at": datetime.now(tz=timezone.utc),
            "exit_reason": "take_profit",
        })
        assert closed.realized_pnl() == pytest.approx(200.0)

    def test_realized_pnl_long_loss(self):
        p = make_long(entry_price=30_000, quantity=0.1)
        closed = p.model_copy(update={
            "status": PositionStatus.CLOSED_STOP_LOSS,
            "exit_price": 29_000.0,
            "closed_at": datetime.now(tz=timezone.utc),
            "exit_reason": "stop_loss",
        })
        assert closed.realized_pnl() == pytest.approx(-100.0)

    def test_realized_pnl_short_profit(self):
        p = make_short(entry_price=2_000, quantity=1.0)
        closed = p.model_copy(update={
            "status": PositionStatus.CLOSED_TAKE_PROFIT,
            "exit_price": 1_800.0,
            "closed_at": datetime.now(tz=timezone.utc),
            "exit_reason": "take_profit",
        })
        assert closed.realized_pnl() == pytest.approx(200.0)

    def test_realized_pnl_is_none_when_open(self):
        assert make_long().realized_pnl() is None

    def test_drawdown_from_peak_long(self):
        p = make_long(current_price=28_500, peak_price=30_000)
        # (30000 - 28500) / 30000 = 0.05
        assert p.drawdown_from_peak_pct() == pytest.approx(0.05)

    def test_drawdown_from_peak_short(self):
        # SHORT: peak = laagste prijs gezien
        p = make_short(current_price=2_100, peak_price=2_000)
        # (2100 - 2000) / 2000 = 0.05
        assert p.drawdown_from_peak_pct() == pytest.approx(0.05)

    def test_drawdown_is_zero_when_at_peak(self):
        p = make_long(current_price=30_000, peak_price=30_000)
        assert p.drawdown_from_peak_pct() == 0.0


# ---------------------------------------------------------------------------
# StopLossCondition
# ---------------------------------------------------------------------------

class TestStopLossCondition:
    def test_long_triggers_at_stop_loss_price(self):
        r = StopLossCondition().check(make_long(current_price=29_000))
        assert r.triggered
        assert r.reason == ExitReason.STOP_LOSS

    def test_long_triggers_below_stop_loss_price(self):
        r = StopLossCondition().check(make_long(current_price=28_000))
        assert r.triggered

    def test_long_does_not_trigger_above_stop_loss_price(self):
        r = StopLossCondition().check(make_long(current_price=29_001))
        assert not r.triggered

    def test_short_triggers_at_stop_loss_price(self):
        r = StopLossCondition().check(make_short(current_price=2_100))
        assert r.triggered
        assert r.reason == ExitReason.STOP_LOSS

    def test_short_triggers_above_stop_loss_price(self):
        r = StopLossCondition().check(make_short(current_price=2_200))
        assert r.triggered

    def test_short_does_not_trigger_below_stop_loss_price(self):
        r = StopLossCondition().check(make_short(current_price=2_099))
        assert not r.triggered

    def test_result_contains_detail_when_triggered(self):
        r = StopLossCondition().check(make_long(current_price=28_000))
        assert r.detail != ""

    def test_result_has_no_detail_when_not_triggered(self):
        r = StopLossCondition().check(make_long(current_price=30_500))
        assert r.detail == ""


# ---------------------------------------------------------------------------
# TakeProfitCondition
# ---------------------------------------------------------------------------

class TestTakeProfitCondition:
    def test_long_triggers_at_take_profit_price(self):
        r = TakeProfitCondition().check(make_long(current_price=32_000))
        assert r.triggered
        assert r.reason == ExitReason.TAKE_PROFIT

    def test_long_triggers_above_take_profit_price(self):
        r = TakeProfitCondition().check(make_long(current_price=33_000))
        assert r.triggered

    def test_long_does_not_trigger_below_take_profit_price(self):
        r = TakeProfitCondition().check(make_long(current_price=31_999))
        assert not r.triggered

    def test_short_triggers_at_take_profit_price(self):
        r = TakeProfitCondition().check(make_short(current_price=1_800))
        assert r.triggered
        assert r.reason == ExitReason.TAKE_PROFIT

    def test_short_triggers_below_take_profit_price(self):
        r = TakeProfitCondition().check(make_short(current_price=1_700))
        assert r.triggered

    def test_short_does_not_trigger_above_take_profit_price(self):
        r = TakeProfitCondition().check(make_short(current_price=1_801))
        assert not r.triggered


# ---------------------------------------------------------------------------
# TTLCondition
# ---------------------------------------------------------------------------

class TestTTLCondition:
    def test_triggers_when_age_equals_ttl(self):
        r = TTLCondition().check(make_long(ttl=3600, opened_seconds_ago=3600))
        assert r.triggered
        assert r.reason == ExitReason.TTL

    def test_triggers_when_age_exceeds_ttl(self):
        r = TTLCondition().check(make_long(ttl=3600, opened_seconds_ago=3700))
        assert r.triggered

    def test_does_not_trigger_before_ttl(self):
        r = TTLCondition().check(make_long(ttl=3600, opened_seconds_ago=3599))
        assert not r.triggered

    def test_does_not_trigger_for_fresh_position(self):
        r = TTLCondition().check(make_long(ttl=3600, opened_seconds_ago=0))
        assert not r.triggered

    def test_result_detail_contains_age_and_ttl(self):
        r = TTLCondition().check(make_long(ttl=60, opened_seconds_ago=120))
        assert "ttl=60" in r.detail


# ---------------------------------------------------------------------------
# DrawdownCondition
# ---------------------------------------------------------------------------

class TestDrawdownCondition:
    def test_long_triggers_when_drawdown_exceeds_threshold(self):
        # peak=30000, current=28000 → drawdown = 6.67% > 5%
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_long(current_price=28_000, peak_price=30_000)
        )
        assert r.triggered
        assert r.reason == ExitReason.DRAWDOWN_BREACH

    def test_long_does_not_trigger_at_exact_threshold(self):
        # peak=30000, current=28500 → drawdown = 5.0%, not strictly >
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_long(current_price=28_500, peak_price=30_000)
        )
        assert not r.triggered

    def test_long_does_not_trigger_below_threshold(self):
        # peak=30000, current=29000 → drawdown = 3.33% < 5%
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_long(current_price=29_000, peak_price=30_000)
        )
        assert not r.triggered

    def test_short_triggers_when_drawdown_exceeds_threshold(self):
        # SHORT peak=1900 (laagste), current=2000 → drawdown = 5.26% > 5%
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_short(current_price=2_000, peak_price=1_900)
        )
        assert r.triggered

    def test_short_does_not_trigger_below_threshold(self):
        # SHORT peak=2000, current=2090 → drawdown = 4.5% < 5%
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_short(current_price=2_090, peak_price=2_000)
        )
        assert not r.triggered

    def test_no_drawdown_when_at_peak(self):
        r = DrawdownCondition(max_drawdown_pct=0.05).check(
            make_long(current_price=30_000, peak_price=30_000)
        )
        assert not r.triggered

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            DrawdownCondition(max_drawdown_pct=0.0)
        with pytest.raises(ValueError):
            DrawdownCondition(max_drawdown_pct=1.1)


# ---------------------------------------------------------------------------
# DailyLossCondition
# ---------------------------------------------------------------------------

class TestDailyLossCondition:
    def test_triggers_at_limit(self):
        r = DailyLossCondition(daily_loss_limit=200.0).check(
            make_long(), CheckContext(daily_loss_so_far=200.0)
        )
        assert r.triggered
        assert r.reason == ExitReason.DAILY_LOSS_BREACH

    def test_triggers_above_limit(self):
        r = DailyLossCondition(daily_loss_limit=200.0).check(
            make_long(), CheckContext(daily_loss_so_far=201.0)
        )
        assert r.triggered

    def test_does_not_trigger_below_limit(self):
        r = DailyLossCondition(daily_loss_limit=200.0).check(
            make_long(), CheckContext(daily_loss_so_far=199.99)
        )
        assert not r.triggered

    def test_does_not_trigger_without_context(self):
        r = DailyLossCondition(daily_loss_limit=200.0).check(make_long(), context=None)
        assert not r.triggered

    def test_invalid_limit_raises(self):
        with pytest.raises(ValueError):
            DailyLossCondition(daily_loss_limit=0.0)
        with pytest.raises(ValueError):
            DailyLossCondition(daily_loss_limit=-50.0)


# ---------------------------------------------------------------------------
# ExitEvaluator — exit-paden (één conditie per keer)
# ---------------------------------------------------------------------------

class TestExitEvaluatorExitPaths:
    def test_stop_loss_long(self):
        ev = ExitEvaluator([StopLossCondition()])
        r = ev.evaluate(make_long(), new_price=28_999.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.STOP_LOSS
        assert r.position.status == PositionStatus.CLOSED_STOP_LOSS
        assert r.position.exit_price == 28_999.0
        assert r.position.closed_at is not None
        assert r.position.exit_reason is not None

    def test_stop_loss_short(self):
        ev = ExitEvaluator([StopLossCondition()])
        r = ev.evaluate(make_short(), new_price=2_101.0)
        assert r.exit_occurred
        assert r.position.status == PositionStatus.CLOSED_STOP_LOSS

    def test_take_profit_long(self):
        ev = ExitEvaluator([TakeProfitCondition()])
        r = ev.evaluate(make_long(), new_price=32_001.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.TAKE_PROFIT
        assert r.position.status == PositionStatus.CLOSED_TAKE_PROFIT

    def test_take_profit_short(self):
        ev = ExitEvaluator([TakeProfitCondition()])
        r = ev.evaluate(make_short(), new_price=1_799.0)
        assert r.exit_occurred
        assert r.position.status == PositionStatus.CLOSED_TAKE_PROFIT

    def test_ttl_exit(self):
        ev = ExitEvaluator([TTLCondition()])
        r = ev.evaluate(make_long(opened_seconds_ago=3601), new_price=30_500.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.TTL
        assert r.position.status == PositionStatus.CLOSED_TTL

    def test_drawdown_exit_long(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        # peak=32000, current=30000 → drawdown = 6.25% > 5%
        r = ev.evaluate(make_long(peak_price=32_000), new_price=30_000.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.DRAWDOWN_BREACH
        assert r.position.status == PositionStatus.CLOSED_RISK_BREACH

    def test_drawdown_exit_short(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        # SHORT: peak=1800 (laagste), current=1908 → drawdown = 6% > 5%
        r = ev.evaluate(make_short(peak_price=1_800), new_price=1_908.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.DRAWDOWN_BREACH

    def test_daily_loss_exit(self):
        ev = ExitEvaluator([DailyLossCondition(daily_loss_limit=200.0)])
        ctx = CheckContext(daily_loss_so_far=200.0)
        r = ev.evaluate(make_long(), new_price=30_500.0, context=ctx)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.DAILY_LOSS_BREACH
        assert r.position.status == PositionStatus.CLOSED_RISK_BREACH


# ---------------------------------------------------------------------------
# ExitEvaluator — prioriteitsordening
# ---------------------------------------------------------------------------

class TestExitEvaluatorPriority:
    def test_daily_loss_beats_stop_loss(self):
        ev = default_evaluator()
        # stop-loss én daily-loss triggeren tegelijk
        ctx = CheckContext(daily_loss_so_far=201.0)
        r = ev.evaluate(make_long(), new_price=28_999.0, context=ctx)
        assert r.triggered.reason == ExitReason.DAILY_LOSS_BREACH

    def test_daily_loss_beats_take_profit(self):
        ev = default_evaluator()
        ctx = CheckContext(daily_loss_so_far=201.0)
        r = ev.evaluate(make_long(), new_price=32_001.0, context=ctx)
        assert r.triggered.reason == ExitReason.DAILY_LOSS_BREACH

    def test_drawdown_beats_stop_loss(self):
        ev = default_evaluator()
        # drawdown > 5%: peak=32000, new_price=29999 → 6.25%
        # stop-loss ook getriggerd bij 29999 < 29000? Nee, 29999 > 29000
        # Maar stop-loss bij 28999: dan ook drawdown? (32000-28999)/32000 = 9.4% > 5% ja
        # Gebruik prijs die drawdown triggert maar stop-loss NIET: 30000 < 28500 nee...
        # peak=32000, drawdown threshold 5%: triggert bij current < 30400
        # stop-loss triggert bij current <= 29000
        # dus: current=29999 → drawdown=(32000-29999)/32000=6.25%>5%, stop-loss niet
        r = ev.evaluate(make_long(peak_price=32_000), new_price=29_999.0)
        assert r.triggered.reason == ExitReason.DRAWDOWN_BREACH

    def test_stop_loss_beats_ttl(self):
        # stop-loss heeft hogere prioriteit dan TTL in default evaluator
        ev = default_evaluator()
        r = ev.evaluate(make_long(opened_seconds_ago=3601), new_price=28_999.0)
        assert r.triggered.reason == ExitReason.STOP_LOSS

    def test_stop_loss_beats_take_profit(self):
        # Kan niet tegelijk triggeren bij correcte positie —
        # dit test dat stop-loss eerder in de lijst staat dan take-profit
        ev = ExitEvaluator([StopLossCondition(), TakeProfitCondition()])
        r = ev.evaluate(make_long(), new_price=28_999.0)
        assert r.triggered.reason == ExitReason.STOP_LOSS
        assert r.position.status == PositionStatus.CLOSED_STOP_LOSS


# ---------------------------------------------------------------------------
# ExitEvaluator — peak trailing
# ---------------------------------------------------------------------------

class TestExitEvaluatorPeakTrailing:
    def test_long_peak_moves_up_with_price(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        pos = make_long(current_price=30_000, peak_price=30_000)
        r = ev.evaluate(pos, new_price=31_000.0)
        assert r.position.peak_price == 31_000.0
        assert r.peak_updated is True

    def test_long_peak_does_not_move_down(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        pos = make_long(current_price=31_000, peak_price=31_000)
        r = ev.evaluate(pos, new_price=30_500.0)
        assert r.position.peak_price == 31_000.0
        assert r.peak_updated is False

    def test_short_peak_moves_down_with_price(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        pos = make_short(current_price=2_000, peak_price=2_000)
        r = ev.evaluate(pos, new_price=1_900.0)
        assert r.position.peak_price == 1_900.0
        assert r.peak_updated is True

    def test_short_peak_does_not_move_up(self):
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        pos = make_short(current_price=1_900, peak_price=1_900)
        r = ev.evaluate(pos, new_price=1_950.0)
        assert r.position.peak_price == 1_900.0
        assert r.peak_updated is False

    def test_drawdown_triggers_only_after_peak_then_reversal(self):
        """
        Scenario: prijs stijgt naar peak, daalt dan genoeg voor drawdown exit.
        Bewijst dat peak correct meestijgt en daarna drawdown correct berekent.
        """
        ev = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.05)])
        pos = make_long(current_price=30_000, peak_price=30_000)

        # Tick 1: prijs stijgt — geen exit, peak omhoog
        r = ev.evaluate(pos, new_price=33_000.0)
        assert not r.exit_occurred
        assert r.position.peak_price == 33_000.0

        # Tick 2: kleine daling — drawdown 1.5%, geen exit
        r = ev.evaluate(r.position, new_price=32_505.0)
        assert not r.exit_occurred

        # Tick 3: daling voorbij 5% van peak (33000 * 0.95 = 31350)
        r = ev.evaluate(r.position, new_price=31_300.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.DRAWDOWN_BREACH


# ---------------------------------------------------------------------------
# ExitEvaluator — fail-closed gedrag
# ---------------------------------------------------------------------------

class TestExitEvaluatorFailClosed:
    def test_stale_price_zero_is_blocked(self):
        ev = default_evaluator()
        r = ev.evaluate(make_long(), new_price=0.0)
        assert r.stale_price is True
        assert r.exit_occurred is False
        assert r.position.is_open()

    def test_stale_price_negative_is_blocked(self):
        ev = default_evaluator()
        r = ev.evaluate(make_long(), new_price=-100.0)
        assert r.stale_price is True
        assert r.position.is_open()

    def test_closed_position_passes_through_unchanged(self):
        ev = default_evaluator()
        # Sluit eerst via stop-loss (28999 → geen drawdown bij peak=30000)
        r1 = ev.evaluate(make_long(), new_price=28_999.0)
        assert r1.exit_occurred

        # Tweede evaluate met lagere prijs — positie al gesloten
        r2 = ev.evaluate(r1.position, new_price=20_000.0)
        assert r2.triggered is None
        assert r2.position.status == PositionStatus.CLOSED_STOP_LOSS
        assert r2.position.exit_price == 28_999.0  # ongewijzigd

    def test_empty_conditions_list_raises(self):
        with pytest.raises(ValueError):
            ExitEvaluator(conditions=[])

    def test_position_unchanged_when_no_exit(self):
        ev = default_evaluator()
        pos = make_long()
        r = ev.evaluate(pos, new_price=30_500.0)
        assert not r.exit_occurred
        assert r.position.is_open()
        assert r.position.current_price == 30_500.0


# ---------------------------------------------------------------------------
# Multi-tick scenario's
# ---------------------------------------------------------------------------

class TestMultiTickScenarios:
    def test_long_profitable_journey_then_take_profit(self):
        """Prijs loopt op richting take-profit over meerdere ticks."""
        ev = ExitEvaluator([
            DrawdownCondition(max_drawdown_pct=0.05),
            StopLossCondition(),
            TakeProfitCondition(),
        ])
        pos = make_long(current_price=30_000, peak_price=30_000)
        prices = [30_500, 31_000, 31_500, 32_000]

        for price in prices[:-1]:
            r = ev.evaluate(pos, new_price=float(price))
            assert not r.exit_occurred, f"Onverwachte exit bij prijs {price}"
            pos = r.position

        r = ev.evaluate(pos, new_price=32_000.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.TAKE_PROFIT

    def test_long_stop_loss_after_drawdown_within_threshold(self):
        """
        Drawdown blijft nét onder drempel, daarna raakt prijs de stop-loss.
        """
        ev = ExitEvaluator([
            DrawdownCondition(max_drawdown_pct=0.10),
            StopLossCondition(),
        ])
        pos = make_long(
            current_price=30_000,
            peak_price=30_000,
            stop_loss_price=28_000,
            take_profit_price=35_000,
        )

        # Prijs daalt naar 27500 — onder stop-loss
        # Drawdown bij peak=30000: (30000-27500)/30000 = 8.3% < 10% → drawdown triggert niet
        # Stop-loss bij 27500 < 28000 → stop-loss triggert
        r = ev.evaluate(pos, new_price=27_500.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.STOP_LOSS

    def test_daily_loss_halts_position_mid_journey(self):
        """Daily loss limit bereikt terwijl positie nog winstgevend is."""
        ev = default_evaluator()
        pos = make_long(current_price=30_000, peak_price=30_000)

        # Tick 1: prijs stijgt, positie winstgevend, maar dagverlies elders opgebouwd
        ctx = CheckContext(daily_loss_so_far=199.0)
        r = ev.evaluate(pos, new_price=31_000.0, context=ctx)
        assert not r.exit_occurred

        # Tick 2: dagverlies overschreden — positie gesloten ook al is prijs goed
        ctx.daily_loss_so_far = 201.0
        r = ev.evaluate(r.position, new_price=31_500.0, context=ctx)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.DAILY_LOSS_BREACH

    def test_short_full_lifecycle(self):
        """SHORT positie van open tot take-profit over meerdere ticks."""
        ev = ExitEvaluator([
            DrawdownCondition(max_drawdown_pct=0.05),
            StopLossCondition(),
            TakeProfitCondition(),
        ])
        pos = make_short(
            entry_price=2_000,
            current_price=2_000,
            peak_price=2_000,
            stop_loss_price=2_100,
            take_profit_price=1_800,
        )

        # Prijs daalt gestaag richting take-profit
        for price in [1_950.0, 1_900.0, 1_850.0]:
            r = ev.evaluate(pos, new_price=price)
            assert not r.exit_occurred
            pos = r.position

        r = ev.evaluate(pos, new_price=1_800.0)
        assert r.exit_occurred
        assert r.triggered.reason == ExitReason.TAKE_PROFIT
        assert r.position.realized_pnl() == pytest.approx(200.0)
