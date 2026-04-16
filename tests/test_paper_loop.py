"""
tests/test_paper_loop.py

Fase 2 — Entry + volledige loop, paper only.

Test-opzet:
  - TestEntrySignal           validatie van het signal schema
  - TestPaperBroker           acceptatie, afwijzing, positiegrootteberekening
  - TestPaperLedger           kapitaal, dagverlies, statistieken, logging
  - TestPaperLoopTick         tick-gedrag, entry/exit, stale prijs, prioriteiten
  - TestPaperLoopScenarios    multi-tick scenario's per exit-reden
  - TestPaperLoopShort        SHORT positie volledig doorlopen
  - Test50PaperTrades         gate-bewijs: 50+ trades volledig en correct afgehandeld
"""

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ant_colony.entry.entry_signal import EntrySignal, SignalSource
from ant_colony.exit_chain.exit_conditions import (
    DailyLossCondition,
    DrawdownCondition,
    StopLossCondition,
    TakeProfitCondition,
    TTLCondition,
)
from ant_colony.exit_chain.exit_evaluator import ExitEvaluator
from ant_colony.exit_chain.position import PositionStatus
from ant_colony.paper.paper_broker import PaperBroker, RejectionReason
from ant_colony.paper.paper_ledger import PaperLedger
from ant_colony.paper.paper_loop import PaperLoop
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(tz=timezone.utc)


def make_mission(
    capital_limit: float = 10_000.0,
    max_position_size: float = 500.0,
    daily_loss_limit: float = 9_999.0,
    ttl: int = 86_400,
    symbols: list[str] | None = None,
) -> Mission:
    return Mission(
        mission_id="m-test",
        ant_type="paper_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["paper_trade"],
        market_scope=MarketScope(
            biome="crypto", symbols=symbols or ["BTC-EUR", "ETH-EUR"]
        ),
        capital_limit=capital_limit,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=max_position_size,
            daily_loss_limit=daily_loss_limit,
        ),
        ttl=ttl,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="test"),
    )


def make_signal(
    entry: float = 30_000.0,
    side: str = "long",
    symbol: str = "BTC-EUR",
    sl_pct: float = 0.03,
    tp_pct: float = 0.06,
    expired: bool = False,
) -> EntrySignal:
    base = NOW - timedelta(minutes=10) if expired else NOW
    if side == "long":
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + sl_pct)
        tp = entry * (1 - tp_pct)
    return EntrySignal(
        symbol=symbol,
        biome="crypto",
        mission_id="m-test",
        ant_id="ant-001",
        source=SignalSource.SCOUT_ANT,
        side=side,
        entry_price=entry,
        stop_loss_price=sl,
        take_profit_price=tp,
        generated_at=base,
        valid_until=base + timedelta(minutes=5),
    )


def make_evaluator(
    daily_loss_limit: float = 9_999.0,
    max_drawdown_pct: float = 0.08,
) -> ExitEvaluator:
    return ExitEvaluator([
        DailyLossCondition(daily_loss_limit=daily_loss_limit),
        DrawdownCondition(max_drawdown_pct=max_drawdown_pct),
        StopLossCondition(),
        TTLCondition(),
        TakeProfitCondition(),
    ])


def make_loop(
    tmp: str,
    capital_limit: float = 10_000.0,
    max_position_size: float = 500.0,
    daily_loss_limit: float = 9_999.0,
    ttl: int = 86_400,
    symbols: list[str] | None = None,
) -> tuple[PaperLoop, PaperLedger, Mission]:
    mission = make_mission(
        capital_limit=capital_limit,
        max_position_size=max_position_size,
        daily_loss_limit=daily_loss_limit,
        ttl=ttl,
        symbols=symbols,
    )
    broker = PaperBroker(mission=mission)
    ledger = PaperLedger(mission=mission, logs_root=Path(tmp))
    evaluator = make_evaluator(daily_loss_limit=daily_loss_limit)
    loop = PaperLoop(mission, broker, ledger, evaluator, logs_root=Path(tmp))
    return loop, ledger, mission


# ---------------------------------------------------------------------------
# EntrySignal
# ---------------------------------------------------------------------------

class TestEntrySignal:
    def test_valid_long_signal(self):
        s = make_signal(side="long")
        assert s.side == "long"
        assert s.stop_loss_price < s.entry_price
        assert s.take_profit_price > s.entry_price

    def test_valid_short_signal(self):
        s = make_signal(side="short", entry=2_000.0, symbol="ETH-EUR")
        assert s.side == "short"
        assert s.stop_loss_price > s.entry_price
        assert s.take_profit_price < s.entry_price

    def test_signal_id_is_auto_generated(self):
        s1 = make_signal()
        s2 = make_signal()
        assert s1.signal_id != s2.signal_id

    def test_expired_signal(self):
        s = make_signal(expired=True)
        assert s.is_expired(NOW)

    def test_fresh_signal_not_expired(self):
        s = make_signal()
        assert not s.is_expired(NOW)

    def test_risk_reward_ratio_long(self):
        # risk=900, reward=1800 → rr=0.5
        s = make_signal(entry=30_000, sl_pct=0.03, tp_pct=0.06)
        assert s.risk_reward_ratio() == pytest.approx(0.5)

    def test_risk_reward_ratio_short(self):
        s = make_signal(side="short", entry=2_000, sl_pct=0.03, tp_pct=0.06)
        assert s.risk_reward_ratio() == pytest.approx(0.5)

    def test_long_stop_loss_above_entry_rejected(self):
        with pytest.raises(Exception, match="below entry_price"):
            make_signal(entry=30_000, sl_pct=-0.01)  # sl > entry

    def test_short_stop_loss_below_entry_rejected(self):
        with pytest.raises(Exception, match="above entry_price"):
            make_signal(side="short", entry=2_000, sl_pct=-0.01)

    def test_valid_until_before_generated_at_rejected(self):
        with pytest.raises(Exception, match="valid_until"):
            EntrySignal(
                symbol="BTC-EUR", biome="crypto",
                mission_id="m-test", ant_id="ant-001",
                source=SignalSource.SCOUT_ANT, side="long",
                entry_price=30_000.0, stop_loss_price=29_000.0,
                take_profit_price=32_000.0,
                generated_at=NOW,
                valid_until=NOW - timedelta(seconds=1),
            )


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------

class TestPaperBroker:
    def test_accepts_valid_long_signal(self):
        mission = make_mission()
        r = PaperBroker(mission).open_position(make_signal(), capital_available=5_000.0)
        assert r.accepted
        assert r.position is not None
        assert r.position.side.value == "long"

    def test_accepts_valid_short_signal(self):
        mission = make_mission()
        r = PaperBroker(mission).open_position(
            make_signal(side="short", entry=2_000.0, symbol="ETH-EUR"),
            capital_available=5_000.0,
        )
        assert r.accepted

    def test_rejects_expired_signal(self):
        mission = make_mission()
        r = PaperBroker(mission).open_position(
            make_signal(expired=True), capital_available=5_000.0
        )
        assert not r.accepted
        assert r.rejection_reason == RejectionReason.SIGNAL_EXPIRED

    def test_rejects_symbol_out_of_scope(self):
        mission = make_mission(symbols=["BTC-EUR"])
        r = PaperBroker(mission).open_position(
            make_signal(symbol="SOL-EUR"), capital_available=5_000.0
        )
        assert r.rejection_reason == RejectionReason.SYMBOL_NOT_IN_SCOPE

    def test_rejects_zero_capital(self):
        mission = make_mission()
        r = PaperBroker(mission).open_position(make_signal(), capital_available=0.0)
        assert r.rejection_reason == RejectionReason.INSUFFICIENT_CAPITAL

    def test_rejects_zero_capital_limit(self):
        # Mission staat capital_limit=0 toe (ge=0); broker weigert met CAPITAL_LIMIT_ZERO
        mission = make_mission(capital_limit=0.0)
        r = PaperBroker(mission).open_position(make_signal(), capital_available=5_000.0)
        assert r.rejection_reason == RejectionReason.CAPITAL_LIMIT_ZERO

    def test_quantity_capped_at_max_position_size(self):
        # max_position_size=500, entry=30000 → max_qty=500/30000=0.01666667
        mission = make_mission(max_position_size=500.0, capital_limit=10_000.0)
        r = PaperBroker(mission).open_position(make_signal(), capital_available=10_000.0)
        assert r.quantity == pytest.approx(round(500.0 / 30_000.0, 8))

    def test_quantity_capped_at_available_capital(self):
        # capital_available=300, entry=30000 → max_qty=300/30000=0.01
        mission = make_mission(max_position_size=500.0, capital_limit=10_000.0)
        r = PaperBroker(mission).open_position(make_signal(), capital_available=300.0)
        assert r.quantity == pytest.approx(round(300.0 / 30_000.0, 8))

    def test_suggested_quantity_respected_when_smaller(self):
        mission = make_mission()
        sig = make_signal().model_copy(update={"suggested_quantity": 0.005})
        r = PaperBroker(mission).open_position(sig, capital_available=10_000.0)
        assert r.quantity == 0.005

    def test_suggested_quantity_capped_at_max(self):
        # suggested=1.0, max_by_risk=500/30000=0.01667 → capped
        mission = make_mission(max_position_size=500.0)
        sig = make_signal().model_copy(update={"suggested_quantity": 1.0})
        r = PaperBroker(mission).open_position(sig, capital_available=10_000.0)
        assert r.quantity == pytest.approx(round(500.0 / 30_000.0, 8))

    def test_position_ttl_from_mission(self):
        mission = make_mission(ttl=3600)
        r = PaperBroker(mission).open_position(make_signal(), capital_available=5_000.0)
        assert r.position.ttl == 3600

    def test_position_peak_equals_entry_at_open(self):
        mission = make_mission()
        r = PaperBroker(mission).open_position(make_signal(), capital_available=5_000.0)
        assert r.position.peak_price == r.position.entry_price

    def test_broker_result_always_returned_no_exception(self):
        mission = make_mission()
        # Verlopen signal — geen exception, altijd BrokerResult
        r = PaperBroker(mission).open_position(
            make_signal(expired=True), capital_available=5_000.0
        )
        assert r is not None
        assert not r.accepted


# ---------------------------------------------------------------------------
# PaperLedger
# ---------------------------------------------------------------------------

class TestPaperLedger:
    def _make_closed_position(self, pnl_positive: bool = True):
        """Helper: maak een gesloten positie met bekende PnL."""
        mission = make_mission()
        broker = PaperBroker(mission)
        evaluator = ExitEvaluator([TakeProfitCondition() if pnl_positive else StopLossCondition()])
        r = broker.open_position(make_signal(), capital_available=5_000.0)
        price = r.position.take_profit_price if pnl_positive else r.position.stop_loss_price
        ev = evaluator.evaluate(r.position, new_price=price)
        return r.position, ev.position  # open, closed

    def test_capital_available_before_open(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        assert ledger.capital_available == 10_000.0

    def test_capital_in_use_after_open(self):
        mission = make_mission()
        ledger = PaperLedger(mission, logs_root=None)
        broker = PaperBroker(mission)
        r = broker.open_position(make_signal(), capital_available=5_000.0)
        ledger.record_opened(r.position)
        expected = r.position.entry_price * r.position.quantity
        assert ledger.capital_in_use == pytest.approx(expected)

    def test_capital_available_restored_after_close(self):
        mission = make_mission()
        ledger = PaperLedger(mission, logs_root=None)
        broker = PaperBroker(mission)
        r = broker.open_position(make_signal(), capital_available=10_000.0)
        ledger.record_opened(r.position)
        assert ledger.capital_available < 10_000.0

        ev = ExitEvaluator([StopLossCondition()])
        closed = ev.evaluate(r.position, new_price=r.position.stop_loss_price).position
        ledger.record_closed(closed)
        assert ledger.capital_available == pytest.approx(10_000.0)

    def test_trade_count_increments_on_close(self):
        mission = make_mission()
        ledger = PaperLedger(mission, logs_root=None)
        assert ledger.trade_count == 0
        _, closed = self._make_closed_position()
        ledger.record_closed(closed)
        assert ledger.trade_count == 1

    def test_win_rate_none_before_trades(self):
        assert PaperLedger(make_mission(), logs_root=None).win_rate is None

    def test_win_rate_after_winning_trade(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=True)
        ledger.record_closed(closed)
        assert ledger.win_rate == pytest.approx(1.0)

    def test_win_rate_after_losing_trade(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=False)
        ledger.record_closed(closed)
        assert ledger.win_rate == pytest.approx(0.0)

    def test_daily_loss_zero_before_trades(self):
        assert PaperLedger(make_mission(), logs_root=None).daily_loss_so_far() == 0.0

    def test_daily_loss_increases_after_losing_trade(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=False)
        ledger.record_closed(closed)
        assert ledger.daily_loss_so_far() > 0

    def test_daily_loss_unchanged_after_winning_trade(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=True)
        ledger.record_closed(closed)
        assert ledger.daily_loss_so_far() == 0.0

    def test_daily_loss_excludes_other_days(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=False)
        ledger.record_closed(closed)
        yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date()
        assert ledger.daily_loss_so_far(trading_day=yesterday) == 0.0

    def test_make_check_context(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position(pnl_positive=False)
        ledger.record_closed(closed)
        ctx = ledger.make_check_context()
        assert ctx.daily_loss_so_far == ledger.daily_loss_so_far()

    def test_record_closed_ignores_still_open_position(self):
        mission = make_mission()
        ledger = PaperLedger(mission, logs_root=None)
        broker = PaperBroker(mission)
        r = broker.open_position(make_signal(), capital_available=5_000.0)
        ledger.record_opened(r.position)
        ledger.record_closed(r.position)  # still open — should be ignored
        assert ledger.trade_count == 0

    def test_exit_breakdown(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed_tp = self._make_closed_position(pnl_positive=True)
        _, closed_sl = self._make_closed_position(pnl_positive=False)
        ledger.record_closed(closed_tp)
        ledger.record_closed(closed_sl)
        bd = ledger.exit_breakdown()
        assert bd.get("closed_take_profit", 0) == 1
        assert bd.get("closed_stop_loss", 0) == 1

    def test_trade_log_written(self, tmp_path):
        ledger = PaperLedger(make_mission(), logs_root=tmp_path)
        _, closed = self._make_closed_position(pnl_positive=True)
        ledger.record_closed(closed)
        log = tmp_path / "paper" / "m-test_trades.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["symbol"] == "BTC-EUR"
        assert record["realized_pnl"] is not None

    def test_no_log_when_logs_root_is_none(self):
        ledger = PaperLedger(make_mission(), logs_root=None)
        _, closed = self._make_closed_position()
        ledger.record_closed(closed)  # must not raise


# ---------------------------------------------------------------------------
# PaperLoop — tick-gedrag
# ---------------------------------------------------------------------------

class TestPaperLoopTick:
    def test_stale_price_skipped(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        r = loop.tick(price=0.0)
        assert r.stale_price
        assert not r.entry_occurred
        assert ledger.trade_count == 0

    def test_negative_price_skipped(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        r = loop.tick(price=-1.0)
        assert r.stale_price

    def test_tick_without_signal_no_action(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        r = loop.tick(price=30_000.0)
        assert not r.entry_occurred
        assert not r.exit_occurred

    def test_signal_opens_position(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        r = loop.tick(price=30_000.0, signal=make_signal())
        assert r.entry_occurred
        assert r.opened_position is not None
        assert len(ledger.open_positions) == 1

    def test_second_signal_ignored_while_open(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        loop.tick(price=30_000.0, signal=make_signal())
        r = loop.tick(price=30_500.0, signal=make_signal())
        assert not r.entry_occurred
        assert len(ledger.open_positions) == 1

    def test_exit_closes_position(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        loop.tick(price=30_000.0, signal=make_signal())
        tp_price = make_signal().take_profit_price
        r = loop.tick(price=tp_price)
        assert r.exit_occurred
        assert ledger.trade_count == 1
        assert len(ledger.open_positions) == 0

    def test_entry_possible_after_exit(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal()
        loop.tick(price=30_000.0, signal=sig)
        loop.tick(price=sig.take_profit_price)        # exit
        r = loop.tick(price=30_000.0, signal=make_signal())  # nieuwe entry
        assert r.entry_occurred

    def test_exit_and_entry_same_tick(self, tmp_path):
        """Exit in stap 2, daarna entry in stap 4 van dezelfde tick."""
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal()
        loop.tick(price=30_000.0, signal=sig)
        tp_price = sig.take_profit_price
        # Tick met exit-prijs én nieuw signaal → exit én entry in één tick
        r = loop.tick(price=tp_price, signal=make_signal())
        assert r.exit_occurred
        assert r.entry_occurred
        assert ledger.trade_count == 1
        assert len(ledger.open_positions) == 1

    def test_tick_number_increments(self, tmp_path):
        loop, _, _ = make_loop(str(tmp_path))
        loop.tick(price=30_000.0)
        loop.tick(price=30_000.0)
        assert loop.tick_number == 2

    def test_tick_log_written(self, tmp_path):
        loop, _, mission = make_loop(str(tmp_path))
        loop.tick(price=30_000.0)
        log = Path(tmp_path) / "paper" / f"{mission.mission_id}_ticks.jsonl"
        assert log.exists()
        record = json.loads(log.read_text().strip())
        assert record["tick"] == 1
        assert record["price"] == 30_000.0

    def test_no_tick_log_when_logs_root_none(self):
        mission = make_mission()
        loop = PaperLoop(
            mission, PaperBroker(mission),
            PaperLedger(mission, None),
            make_evaluator(), logs_root=None,
        )
        loop.tick(price=30_000.0)  # must not raise


# ---------------------------------------------------------------------------
# PaperLoop — scenario's per exit-reden
# ---------------------------------------------------------------------------

class TestPaperLoopScenarios:
    def test_take_profit_scenario(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal(entry=30_000.0, sl_pct=0.03, tp_pct=0.06)
        loop.tick(price=30_000.0, signal=sig)
        r = loop.tick(price=sig.take_profit_price)
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_TAKE_PROFIT
        assert r.closed_position.realized_pnl() > 0

    def test_stop_loss_scenario(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal(entry=30_000.0, sl_pct=0.03, tp_pct=0.06)
        loop.tick(price=30_000.0, signal=sig)
        r = loop.tick(price=sig.stop_loss_price)
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_STOP_LOSS
        assert r.closed_position.realized_pnl() < 0

    def test_ttl_scenario(self, tmp_path):
        # Gebruik now-injectie om aging te simuleren zonder time.sleep.
        # Open positie met now = 2 uur geleden; volgende tick ziet age > ttl.
        loop, ledger, _ = make_loop(str(tmp_path), ttl=3_600)
        two_hours_ago = NOW - timedelta(hours=2)
        loop.tick(price=30_000.0, signal=make_signal(), now=two_hours_ago)
        assert len(ledger.open_positions) == 1
        r = loop.tick(price=30_200.0)   # age ≈ 2h > ttl 1h
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_TTL

    def test_drawdown_scenario(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        evaluator = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.03)])
        mission = make_mission()
        loop2 = PaperLoop(
            mission, PaperBroker(mission),
            PaperLedger(mission, Path(tmp_path)),
            evaluator, logs_root=Path(tmp_path),
        )
        sig = make_signal(entry=30_000.0)
        loop2.tick(price=30_000.0, signal=sig)
        loop2.tick(price=31_000.0)          # peak = 31000
        r = loop2.tick(price=30_000.0)      # drawdown = 3.23% > 3%
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_RISK_BREACH

    def test_daily_loss_scenario(self, tmp_path):
        """Daily loss bereikt → nieuwe entry wordt geblokkeerd door DailyLossCondition."""
        loop, ledger, _ = make_loop(str(tmp_path), daily_loss_limit=5.0)
        sig = make_signal(entry=30_000.0, sl_pct=0.03)

        # Trade 1: stop-loss exit → verlies > 5 EUR
        loop.tick(price=30_000.0, signal=sig)
        r = loop.tick(price=sig.stop_loss_price)
        assert r.exit_occurred
        assert ledger.daily_loss_so_far() > 5.0

        # Trade 2: nieuw signaal → daily loss triggert onmiddellijk
        loop.tick(price=30_000.0, signal=make_signal())
        r = loop.tick(price=30_000.0)
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_RISK_BREACH


# ---------------------------------------------------------------------------
# PaperLoop — SHORT positie
# ---------------------------------------------------------------------------

class TestPaperLoopShort:
    def test_short_take_profit(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal(side="short", entry=2_000.0, symbol="ETH-EUR")
        loop.tick(price=2_000.0, signal=sig)
        r = loop.tick(price=sig.take_profit_price)
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_TAKE_PROFIT
        assert r.closed_position.realized_pnl() > 0

    def test_short_stop_loss(self, tmp_path):
        loop, ledger, _ = make_loop(str(tmp_path))
        sig = make_signal(side="short", entry=2_000.0, symbol="ETH-EUR")
        loop.tick(price=2_000.0, signal=sig)
        r = loop.tick(price=sig.stop_loss_price)
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_STOP_LOSS
        assert r.closed_position.realized_pnl() < 0

    def test_short_peak_trailing(self, tmp_path):
        """SHORT peak daalt mee; drawdown triggert als prijs terugstijgt."""
        mission = make_mission()
        evaluator = ExitEvaluator([DrawdownCondition(max_drawdown_pct=0.03)])
        loop = PaperLoop(
            mission, PaperBroker(mission),
            PaperLedger(mission, Path(tmp_path)),
            evaluator, logs_root=Path(tmp_path),
        )
        sig = make_signal(side="short", entry=2_000.0, symbol="ETH-EUR")
        loop.tick(price=2_000.0, signal=sig)
        loop.tick(price=1_900.0)   # peak SHORT = 1900
        r = loop.tick(price=1_959.0)  # drawdown = (1959-1900)/1900 = 3.1% > 3%
        assert r.exit_occurred
        assert r.closed_position.status == PositionStatus.CLOSED_RISK_BREACH


# ---------------------------------------------------------------------------
# 50 paper trades — gate-bewijs
# ---------------------------------------------------------------------------

class Test50PaperTrades:
    """
    Bewijst dat de loop 50+ trades volledig en correct kan afhandelen.

    Scenario: wisselende take-profit en stop-loss exits over 50+ trades.
    Na elke exit wordt direct een nieuw signaal ingediend.
    """

    def test_fifty_trades_completed(self, tmp_path):
        TARGET = 50
        loop, ledger, mission = make_loop(
            str(tmp_path),
            capital_limit=100_000.0,
            max_position_size=1_000.0,
            daily_loss_limit=99_999.0,
        )
        entry = 30_000.0
        trades_done = 0

        for i in range(TARGET * 3):  # ruim genoeg ticks
            if trades_done >= TARGET:
                break

            if len(ledger.open_positions) == 0:
                sig = make_signal(entry=entry)
                loop.tick(price=entry, signal=sig)
            else:
                pos = ledger.open_positions[0]
                # Wisselend: even trades → take-profit, oneven → stop-loss
                if trades_done % 2 == 0:
                    exit_price = pos.take_profit_price
                else:
                    exit_price = pos.stop_loss_price
                r = loop.tick(price=exit_price)
                if r.exit_occurred:
                    trades_done += 1

        assert ledger.trade_count >= TARGET, (
            f"Verwacht >= {TARGET} trades, got {ledger.trade_count}"
        )

    def test_fifty_trades_all_have_exit_fields(self, tmp_path):
        TARGET = 50
        loop, ledger, _ = make_loop(
            str(tmp_path),
            capital_limit=100_000.0,
            max_position_size=1_000.0,
            daily_loss_limit=99_999.0,
        )
        entry = 30_000.0
        trades_done = 0

        for _ in range(TARGET * 3):
            if trades_done >= TARGET:
                break
            if len(ledger.open_positions) == 0:
                loop.tick(price=entry, signal=make_signal(entry=entry))
            else:
                pos = ledger.open_positions[0]
                exit_price = pos.take_profit_price if trades_done % 2 == 0 else pos.stop_loss_price
                r = loop.tick(price=exit_price)
                if r.exit_occurred:
                    trades_done += 1

        for trade in ledger.closed_trades:
            assert trade.exit_price is not None, f"{trade.position_id} missing exit_price"
            assert trade.closed_at is not None, f"{trade.position_id} missing closed_at"
            assert trade.exit_reason is not None, f"{trade.position_id} missing exit_reason"
            assert trade.realized_pnl() is not None

    def test_fifty_trades_capital_always_non_negative(self, tmp_path):
        TARGET = 50
        loop, ledger, _ = make_loop(
            str(tmp_path),
            capital_limit=100_000.0,
            max_position_size=1_000.0,
            daily_loss_limit=99_999.0,
        )
        entry = 30_000.0
        trades_done = 0

        for _ in range(TARGET * 3):
            if trades_done >= TARGET:
                break
            assert ledger.capital_available >= 0, "Kapitaal negatief!"
            if len(ledger.open_positions) == 0:
                loop.tick(price=entry, signal=make_signal(entry=entry))
            else:
                pos = ledger.open_positions[0]
                exit_price = pos.take_profit_price if trades_done % 2 == 0 else pos.stop_loss_price
                r = loop.tick(price=exit_price)
                if r.exit_occurred:
                    trades_done += 1

    def test_fifty_trades_exit_breakdown_has_both_tp_and_sl(self, tmp_path):
        TARGET = 50
        loop, ledger, _ = make_loop(
            str(tmp_path),
            capital_limit=100_000.0,
            max_position_size=1_000.0,
            daily_loss_limit=99_999.0,
        )
        entry = 30_000.0
        trades_done = 0

        for _ in range(TARGET * 3):
            if trades_done >= TARGET:
                break
            if len(ledger.open_positions) == 0:
                loop.tick(price=entry, signal=make_signal(entry=entry))
            else:
                pos = ledger.open_positions[0]
                exit_price = pos.take_profit_price if trades_done % 2 == 0 else pos.stop_loss_price
                r = loop.tick(price=exit_price)
                if r.exit_occurred:
                    trades_done += 1

        bd = ledger.exit_breakdown()
        assert bd.get("closed_take_profit", 0) >= 1
        assert bd.get("closed_stop_loss", 0) >= 1

    def test_fifty_trades_log_complete(self, tmp_path):
        """Trade log bevat exact evenveel regels als er trades zijn."""
        TARGET = 50
        loop, ledger, mission = make_loop(
            str(tmp_path),
            capital_limit=100_000.0,
            max_position_size=1_000.0,
            daily_loss_limit=99_999.0,
        )
        entry = 30_000.0
        trades_done = 0

        for _ in range(TARGET * 3):
            if trades_done >= TARGET:
                break
            if len(ledger.open_positions) == 0:
                loop.tick(price=entry, signal=make_signal(entry=entry))
            else:
                pos = ledger.open_positions[0]
                exit_price = pos.take_profit_price if trades_done % 2 == 0 else pos.stop_loss_price
                r = loop.tick(price=exit_price)
                if r.exit_occurred:
                    trades_done += 1

        log = Path(tmp_path) / "paper" / f"{mission.mission_id}_trades.jsonl"
        assert log.exists()
        lines = [l for l in log.read_text().splitlines() if l.strip()]
        assert len(lines) == ledger.trade_count
        assert ledger.trade_count >= TARGET
