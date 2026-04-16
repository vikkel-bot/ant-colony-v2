"""
tests/test_mission_validation.py

Mission schema validation: all governance rules that the Mission schema
enforces are tested here, including the rules that agents must check
when they receive a Mission (see COLONY_GOVERNANCE.md §3).
"""

import pytest
from datetime import datetime, timezone

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

def make_risk_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_drawdown_pct=0.10,
        max_position_size=500.0,
        daily_loss_limit=200.0,
        stop_loss_required=True,
    )
    return RiskLimits(**{**defaults, **overrides})


def make_market_scope(**overrides) -> MarketScope:
    defaults = dict(biome="crypto", symbols=["BTC-EUR"])
    return MarketScope(**{**defaults, **overrides})


def make_mission(**overrides) -> Mission:
    defaults = dict(
        mission_id="m-001",
        ant_type="scout_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["scan_market", "write_artifact"],
        market_scope=make_market_scope(),
        capital_limit=1000.0,
        risk_limits=make_risk_limits(),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="Found at least one candidate"),
        abort_conditions=AbortConditions(),
    )
    return Mission(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestMissionHappyPath:
    def test_valid_mission_constructs(self):
        m = make_mission()
        assert m.mission_id == "m-001"
        assert m.issued_by == "queen"

    def test_default_issued_by_is_queen(self):
        assert make_mission().issued_by == "queen"

    def test_abort_conditions_present_by_default(self):
        m = make_mission()
        assert m.abort_conditions.stale_heartbeat is True
        assert m.abort_conditions.capital_limit_breach is True
        assert m.abort_conditions.risk_limit_breach is True
        assert m.abort_conditions.ttl_expired is True
        assert m.abort_conditions.stale_market_data is True

    def test_capital_limit_can_be_zero_for_non_execution_missions(self):
        m = make_mission(capital_limit=0.0)
        assert m.capital_limit == 0.0

    def test_issued_at_defaults_to_now(self):
        m = make_mission()
        assert isinstance(m.issued_at, datetime)


# ---------------------------------------------------------------------------
# issued_by enforcement
# ---------------------------------------------------------------------------

class TestIssuedByEnforcement:
    def test_issued_by_queen_is_valid(self):
        m = make_mission(issued_by="queen")
        assert m.issued_by == "queen"

    def test_issued_by_other_is_rejected(self):
        with pytest.raises(Exception, match="issued_by"):
            make_mission(issued_by="rogue_agent")

    def test_issued_by_empty_is_rejected(self):
        with pytest.raises(Exception):
            make_mission(issued_by="")


# ---------------------------------------------------------------------------
# heartbeat_interval < ttl enforcement
# ---------------------------------------------------------------------------

class TestHeartbeatTtlRelationship:
    def test_heartbeat_shorter_than_ttl_is_valid(self):
        m = make_mission(ttl=3600, heartbeat_interval=30)
        assert m.heartbeat_interval < m.ttl

    def test_heartbeat_equal_to_ttl_is_rejected(self):
        with pytest.raises(Exception, match="heartbeat_interval"):
            make_mission(ttl=60, heartbeat_interval=60)

    def test_heartbeat_longer_than_ttl_is_rejected(self):
        with pytest.raises(Exception, match="heartbeat_interval"):
            make_mission(ttl=60, heartbeat_interval=90)

    def test_ttl_must_be_positive(self):
        with pytest.raises(Exception):
            make_mission(ttl=0)

    def test_heartbeat_interval_must_be_positive(self):
        with pytest.raises(Exception):
            make_mission(heartbeat_interval=0)


# ---------------------------------------------------------------------------
# allowed_actions enforcement
# ---------------------------------------------------------------------------

class TestAllowedActions:
    def test_allowed_actions_must_not_be_empty(self):
        with pytest.raises(Exception):
            make_mission(allowed_actions=[])

    def test_single_action_is_valid(self):
        m = make_mission(allowed_actions=["scan_market"])
        assert len(m.allowed_actions) == 1


# ---------------------------------------------------------------------------
# market_scope enforcement
# ---------------------------------------------------------------------------

class TestMarketScope:
    def test_symbols_must_not_be_empty(self):
        with pytest.raises(Exception):
            make_mission(market_scope=MarketScope(biome="crypto", symbols=[]))

    def test_multiple_symbols_are_valid(self):
        m = make_mission(
            market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR", "ETH-EUR"])
        )
        assert len(m.market_scope.symbols) == 2


# ---------------------------------------------------------------------------
# risk_limits enforcement
# ---------------------------------------------------------------------------

class TestRiskLimits:
    def test_max_drawdown_must_be_positive(self):
        with pytest.raises(Exception):
            make_mission(risk_limits=make_risk_limits(max_drawdown_pct=0.0))

    def test_max_drawdown_cannot_exceed_one(self):
        with pytest.raises(Exception):
            make_mission(risk_limits=make_risk_limits(max_drawdown_pct=1.5))

    def test_max_drawdown_of_one_is_valid(self):
        m = make_mission(risk_limits=make_risk_limits(max_drawdown_pct=1.0))
        assert m.risk_limits.max_drawdown_pct == 1.0

    def test_max_position_size_must_be_positive(self):
        with pytest.raises(Exception):
            make_mission(risk_limits=make_risk_limits(max_position_size=0.0))

    def test_daily_loss_limit_must_be_positive(self):
        with pytest.raises(Exception):
            make_mission(risk_limits=make_risk_limits(daily_loss_limit=0.0))

    def test_stop_loss_required_defaults_to_true(self):
        assert make_risk_limits().stop_loss_required is True


# ---------------------------------------------------------------------------
# capital_limit enforcement
# ---------------------------------------------------------------------------

class TestCapitalLimit:
    def test_capital_limit_cannot_be_negative(self):
        with pytest.raises(Exception):
            make_mission(capital_limit=-1.0)
