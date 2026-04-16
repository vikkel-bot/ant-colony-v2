"""
tests/test_order_schema.py

Tests voor LiveOrder, OrderResult, OrderSide, OrderType en
OrderRejectionReason (ant_colony/schemas/order.py).

Dekt:
  - LiveOrder constructie en defaults
  - LIMIT order vereist limit_price
  - BUY: stop_loss < take_profit
  - SELL: stop_loss > take_profit
  - OrderResult.rejected() factory
  - OrderResult.accepted_result() factory
  - OrderResult defaults
  - OrderRejectionReason waarden
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ant_colony.schemas.order import (
    LiveOrder,
    OrderRejectionReason,
    OrderResult,
    OrderSide,
    OrderType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _buy_market(**overrides) -> LiveOrder:
    defaults = dict(
        mission_id="m-1",
        ant_id="a-1",
        symbol="BTC-EUR",
        biome="crypto",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=0.5,
        stop_loss_price=28_000.0,
        take_profit_price=35_000.0,
    )
    defaults.update(overrides)
    return LiveOrder(**defaults)


def _sell_market(**overrides) -> LiveOrder:
    defaults = dict(
        mission_id="m-1",
        ant_id="a-1",
        symbol="BTC-EUR",
        biome="crypto",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=0.5,
        stop_loss_price=35_000.0,
        take_profit_price=28_000.0,
    )
    defaults.update(overrides)
    return LiveOrder(**defaults)


# ---------------------------------------------------------------------------
# TestLiveOrderDefaults
# ---------------------------------------------------------------------------

class TestLiveOrderDefaults:

    def test_order_id_generated(self):
        order = _buy_market()
        assert isinstance(order.order_id, str)
        assert len(order.order_id) == 36  # UUID4

    def test_two_orders_have_different_ids(self):
        a = _buy_market()
        b = _buy_market()
        assert a.order_id != b.order_id

    def test_created_at_is_utc(self):
        from datetime import timezone
        order = _buy_market()
        assert order.created_at.tzinfo is not None
        assert order.created_at.utcoffset().total_seconds() == 0

    def test_explicit_order_id_preserved(self):
        order = _buy_market(order_id="explicit-id")
        assert order.order_id == "explicit-id"

    def test_all_fields_accessible(self):
        order = _buy_market()
        assert order.mission_id == "m-1"
        assert order.ant_id == "a-1"
        assert order.symbol == "BTC-EUR"
        assert order.biome == "crypto"
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.quantity == 0.5
        assert order.stop_loss_price == 28_000.0
        assert order.take_profit_price == 35_000.0
        assert order.limit_price is None


# ---------------------------------------------------------------------------
# TestLiveOrderValidation
# ---------------------------------------------------------------------------

class TestLiveOrderValidation:

    def test_quantity_must_be_positive(self):
        with pytest.raises(ValidationError):
            _buy_market(quantity=0)

    def test_quantity_negative_rejected(self):
        with pytest.raises(ValidationError):
            _buy_market(quantity=-1.0)

    def test_stop_loss_must_be_positive(self):
        with pytest.raises(ValidationError):
            _buy_market(stop_loss_price=0)

    def test_take_profit_must_be_positive(self):
        with pytest.raises(ValidationError):
            _buy_market(take_profit_price=0)

    def test_limit_order_requires_limit_price(self):
        with pytest.raises(ValidationError, match="limit_price is required for LIMIT orders"):
            _buy_market(order_type=OrderType.LIMIT, limit_price=None)

    def test_limit_order_with_limit_price_accepted(self):
        order = _buy_market(order_type=OrderType.LIMIT, limit_price=29_000.0)
        assert order.limit_price == 29_000.0

    def test_limit_price_must_be_positive(self):
        with pytest.raises(ValidationError):
            _buy_market(order_type=OrderType.LIMIT, limit_price=0.0)

    def test_market_order_no_limit_price_accepted(self):
        order = _buy_market(order_type=OrderType.MARKET)
        assert order.limit_price is None


# ---------------------------------------------------------------------------
# TestLiveOrderBuyDirectionality
# ---------------------------------------------------------------------------

class TestLiveOrderBuyDirectionality:

    def test_buy_stop_loss_below_take_profit_accepted(self):
        order = _buy_market(stop_loss_price=28_000.0, take_profit_price=35_000.0)
        assert order.side == OrderSide.BUY

    def test_buy_stop_loss_equal_take_profit_rejected(self):
        with pytest.raises(ValidationError, match="stop_loss_price"):
            _buy_market(stop_loss_price=30_000.0, take_profit_price=30_000.0)

    def test_buy_stop_loss_above_take_profit_rejected(self):
        with pytest.raises(ValidationError, match="stop_loss_price"):
            _buy_market(stop_loss_price=35_000.0, take_profit_price=28_000.0)


# ---------------------------------------------------------------------------
# TestLiveOrderSellDirectionality
# ---------------------------------------------------------------------------

class TestLiveOrderSellDirectionality:

    def test_sell_stop_loss_above_take_profit_accepted(self):
        order = _sell_market(stop_loss_price=35_000.0, take_profit_price=28_000.0)
        assert order.side == OrderSide.SELL

    def test_sell_stop_loss_equal_take_profit_rejected(self):
        with pytest.raises(ValidationError, match="stop_loss_price"):
            _sell_market(stop_loss_price=30_000.0, take_profit_price=30_000.0)

    def test_sell_stop_loss_below_take_profit_rejected(self):
        with pytest.raises(ValidationError, match="stop_loss_price"):
            _sell_market(stop_loss_price=28_000.0, take_profit_price=35_000.0)


# ---------------------------------------------------------------------------
# TestOrderResult
# ---------------------------------------------------------------------------

class TestOrderResult:

    def test_rejected_factory(self):
        result = OrderResult.rejected(
            order_id="o-1",
            reason=OrderRejectionReason.COLONY_HALTED,
            detail="colony is halted",
        )
        assert result.accepted is False
        assert result.order_id == "o-1"
        assert result.rejection_reason == OrderRejectionReason.COLONY_HALTED
        assert result.rejection_detail == "colony is halted"
        assert result.exchange_order_id is None
        assert result.filled_quantity == 0.0
        assert result.avg_price is None

    def test_rejected_factory_no_detail(self):
        result = OrderResult.rejected("o-2", OrderRejectionReason.MARKET_DATA_STALE)
        assert result.rejection_detail == ""

    def test_accepted_result_factory(self):
        result = OrderResult.accepted_result(
            order_id="o-3",
            exchange_order_id="exch-999",
            filled_quantity=0.5,
            avg_price=30_000.0,
        )
        assert result.accepted is True
        assert result.order_id == "o-3"
        assert result.exchange_order_id == "exch-999"
        assert result.filled_quantity == 0.5
        assert result.avg_price == 30_000.0
        assert result.rejection_reason is None
        assert result.rejection_detail == ""

    def test_timestamp_is_utc(self):
        from datetime import timezone
        result = OrderResult.rejected("o-4", OrderRejectionReason.ADAPTER_UNAVAILABLE)
        assert result.timestamp.tzinfo is not None
        assert result.timestamp.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# TestOrderRejectionReason
# ---------------------------------------------------------------------------

class TestOrderRejectionReason:

    def test_all_reasons_exist(self):
        reasons = {r.value for r in OrderRejectionReason}
        assert "colony_halted" in reasons
        assert "mission_not_active" in reasons
        assert "capital_limit_breached" in reasons
        assert "market_data_stale" in reasons
        assert "adapter_unavailable" in reasons
        assert "invalid_order" in reasons
        assert "exchange_rejected" in reasons

    def test_reason_count(self):
        assert len(OrderRejectionReason) == 7
