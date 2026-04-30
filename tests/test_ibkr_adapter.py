"""
tests/test_ibkr_adapter.py

Tests voor IBKRAdapter.

ib_insync wordt nooit echt geïmporteerd: we patchen sys.modules zodat de
lazy-import inside de adapter de mock module ziet.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.biome.adapters.ibkr_adapter import (
    IBKRAdapter,
    _DEFAULT_CURRENCY,
    _DEFAULT_EXCHANGE,
    _INTERVAL_TO_BAR_SIZE,
    _PERIOD_TO_DURATION,
    _PORT_LIVE,
    _PORT_PAPER,
)
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.schemas.order import LiveOrder, OrderSide, OrderType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(
    close: float = 150.0,
    open_: float = 148.0,
    high: float = 152.0,
    low: float = 147.0,
    volume: float = 1_000_000.0,
    date_str: str = "20260420",
) -> MagicMock:
    bar = MagicMock()
    bar.close  = close
    bar.open   = open_
    bar.high   = high
    bar.low    = low
    bar.volume = volume
    bar.date   = date_str
    return bar


def _make_account_value(tag: str, value: str, currency: str = "USD") -> MagicMock:
    av = MagicMock()
    av.tag      = tag
    av.value    = value
    av.currency = currency
    return av


def _make_position(symbol: str, qty: float, avg_cost: float, exchange: str = "SMART") -> MagicMock:
    contract          = MagicMock()
    contract.symbol   = symbol
    contract.exchange = exchange
    contract.currency = "USD"
    pos               = MagicMock()
    pos.contract      = contract
    pos.position      = qty
    pos.avgCost       = avg_cost
    return pos


def _make_trade(order_id: int = 42) -> MagicMock:
    trade               = MagicMock()
    trade.order.orderId = order_id
    return trade


@contextmanager
def patched_ib(ib_instance: MagicMock):
    """Patch sys.modules zodat `from ib_insync import ...` de mock ziet."""
    mock_module = MagicMock()
    mock_module.IB.return_value = ib_instance

    # Zorg dat Stock / MarketOrder / LimitOrder constructors gewone mocks zijn
    mock_module.Stock         = MagicMock(return_value=MagicMock())
    mock_module.MarketOrder   = MagicMock(return_value=MagicMock())
    mock_module.LimitOrder    = MagicMock(return_value=MagicMock())

    with patch.dict(sys.modules, {"ib_insync": mock_module}):
        yield mock_module


def _connected_adapter(ib_instance: MagicMock, **kwargs) -> IBKRAdapter:
    """Maak een adapter aan met een reeds verbonden mock-IB."""
    adapter = IBKRAdapter(**kwargs)
    adapter._ib = ib_instance
    ib_instance.isConnected.return_value = True
    return adapter


# ---------------------------------------------------------------------------
# 1. Constructie & configuratie
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_paper_mode(self) -> None:
        with patch.dict("os.environ", {"IBKR_PAPER_MODE": "true"}, clear=False):
            adapter = IBKRAdapter()
        assert adapter._paper_mode is True

    def test_live_mode_via_env(self) -> None:
        with patch.dict("os.environ", {"IBKR_PAPER_MODE": "false"}, clear=False):
            adapter = IBKRAdapter()
        assert adapter._paper_mode is False

    def test_paper_mode_constructor_override(self) -> None:
        adapter = IBKRAdapter(paper_mode=False)
        assert adapter._paper_mode is False

    def test_paper_mode_default_port(self) -> None:
        adapter = IBKRAdapter(paper_mode=True)
        assert adapter._port == _PORT_PAPER

    def test_live_mode_default_port(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("IBKR_PORT", None)
            adapter = IBKRAdapter(paper_mode=False)
        assert adapter._port == _PORT_LIVE

    def test_port_constructor_override(self) -> None:
        adapter = IBKRAdapter(port=4002)
        assert adapter._port == 4002

    def test_biome_id_is_equities(self) -> None:
        assert IBKRAdapter().biome_id == "equities"

    def test_ib_initially_none(self) -> None:
        assert IBKRAdapter()._ib is None

    def test_host_from_env(self) -> None:
        with patch.dict("os.environ", {"IBKR_HOST": "192.168.1.10"}, clear=False):
            adapter = IBKRAdapter()
        assert adapter._host == "192.168.1.10"

    def test_client_id_default(self) -> None:
        adapter = IBKRAdapter()
        assert adapter._client_id == 1

    def test_client_id_override(self) -> None:
        adapter = IBKRAdapter(client_id=5)
        assert adapter._client_id == 5


# ---------------------------------------------------------------------------
# 2. is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_false_when_ib_insync_not_installed(self) -> None:
        with patch.dict(sys.modules, {"ib_insync": None}):
            adapter = IBKRAdapter()
            assert adapter.is_available() is False

    def test_false_when_no_connection(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = False
        with patched_ib(ib):
            adapter = IBKRAdapter()
            adapter._ib = ib
            assert adapter.is_available() is False

    def test_false_when_ib_is_none(self) -> None:
        with patched_ib(MagicMock()):
            adapter = IBKRAdapter()
            adapter._ib = None
            assert adapter.is_available() is False

    def test_true_when_connected(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        with patched_ib(ib):
            adapter = IBKRAdapter()
            adapter._ib = ib
            assert adapter.is_available() is True

    def test_false_when_is_connected_raises(self) -> None:
        ib = MagicMock()
        ib.isConnected.side_effect = RuntimeError("broken")
        with patched_ib(ib):
            adapter = IBKRAdapter()
            adapter._ib = ib
            assert adapter.is_available() is False


# ---------------------------------------------------------------------------
# 3. connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_success(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        with patched_ib(ib):
            adapter = IBKRAdapter(paper_mode=True)
            result  = adapter.connect()
        assert result is True
        assert adapter._ib is ib

    def test_connect_failure_returns_false(self, caplog) -> None:
        ib = MagicMock()
        ib.connect.side_effect = ConnectionRefusedError("TWS niet actief")
        with patched_ib(ib):
            adapter = IBKRAdapter()
            result  = adapter.connect()
        assert result is False
        assert adapter._ib is None
        assert "IBKR niet bereikbaar" in caplog.text

    def test_connect_uses_paper_port(self) -> None:
        ib = MagicMock()
        with patched_ib(ib) as mock_mod:
            adapter = IBKRAdapter(paper_mode=True)
            adapter.connect()
            _, kwargs = ib.connect.call_args
            assert kwargs.get("clientId") == adapter._client_id
        # port is passed positionally
        args, _ = ib.connect.call_args
        assert args[1] == _PORT_PAPER

    def test_connect_uses_live_port(self) -> None:
        ib = MagicMock()
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("IBKR_PORT", None)
            with patched_ib(ib):
                adapter = IBKRAdapter(paper_mode=False)
                adapter.connect()
        args, _ = ib.connect.call_args
        assert args[1] == _PORT_LIVE

    def test_connect_host_override(self) -> None:
        ib = MagicMock()
        with patched_ib(ib):
            adapter = IBKRAdapter()
            adapter.connect(host="10.0.0.5")
        args, _ = ib.connect.call_args
        assert args[0] == "10.0.0.5"

    def test_already_connected_returns_true_without_reconnect(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        with patched_ib(ib):
            adapter      = IBKRAdapter()
            adapter._ib  = ib
            result        = adapter.connect()
        assert result is True
        ib.connect.assert_not_called()

    def test_disconnect_clears_ib(self) -> None:
        ib = MagicMock()
        with patched_ib(ib):
            adapter     = IBKRAdapter()
            adapter._ib = ib
            adapter.disconnect()
        assert adapter._ib is None
        ib.disconnect.assert_called_once()

    def test_disconnect_when_not_connected_does_not_raise(self) -> None:
        adapter = IBKRAdapter()
        adapter.disconnect()  # _ib is None — must not raise


# ---------------------------------------------------------------------------
# 4. get_candles
# ---------------------------------------------------------------------------


class TestGetCandles:
    def test_returns_market_data_list(self) -> None:
        ib   = MagicMock()
        ib.isConnected.return_value = True
        ib.reqHistoricalData.return_value = [_make_bar(close=150.0)]
        with patched_ib(ib):
            adapter  = _connected_adapter(ib)
            candles  = adapter.get_candles("AAPL")
        assert len(candles) == 1
        assert candles[0].close == pytest.approx(150.0)
        assert candles[0].symbol == "AAPL"
        assert candles[0].biome_id == "equities"

    def test_empty_when_no_bars(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib):
            assert _connected_adapter(ib).get_candles("AAPL") == []

    def test_empty_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            result = adapter.get_candles("AAPL")
        assert result == []

    def test_skips_bars_with_zero_close(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.reqHistoricalData.return_value = [
            _make_bar(close=0.0),
            _make_bar(close=100.0),
        ]
        with patched_ib(ib):
            candles = _connected_adapter(ib).get_candles("SPY")
        assert len(candles) == 1
        assert candles[0].close == pytest.approx(100.0)

    def test_period_mapped_to_duration_string(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib):
            _connected_adapter(ib).get_candles("AAPL", period="6mo")
        _, kwargs = ib.reqHistoricalData.call_args
        assert kwargs["durationStr"] == "6 M"

    def test_interval_mapped_to_bar_size(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib):
            _connected_adapter(ib).get_candles("AAPL", interval="1wk")
        _, kwargs = ib.reqHistoricalData.call_args
        assert kwargs["barSizeSetting"] == "1 week"

    def test_unknown_period_defaults_to_3m(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib):
            _connected_adapter(ib).get_candles("AAPL", period="unknown")
        _, kwargs = ib.reqHistoricalData.call_args
        assert kwargs["durationStr"] == "3 M"

    def test_exception_returns_empty_list(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.reqHistoricalData.side_effect = RuntimeError("api error")
        with patched_ib(ib):
            result = _connected_adapter(ib).get_candles("AAPL")
        assert result == []

    def test_timeout_falls_back_to_yfinance(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.reqHistoricalData.side_effect = TimeoutError("historical data request timed out")
        fallback = [
            MarketData(
                symbol="XLK",
                timeframe="1d",
                timestamp=datetime.now(tz=timezone.utc),
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=1_000_000,
                biome_id="equities",
            )
        ]
        with patched_ib(ib):
            with patch(
                "ant_colony.biome.adapters.yahoo_finance_adapter.YahooFinanceAdapter.get_candles",
                return_value=fallback,
            ) as yf_get:
                result = _connected_adapter(ib).get_candles("XLK", period="3mo", interval="1d")
        assert result == fallback
        yf_get.assert_called_once_with("XLK", period="3mo", interval="1d")

    def test_exchange_and_currency_passed_to_stock(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib) as mock_mod:
            _connected_adapter(ib).get_candles("AEX", exchange="AEB", currency="EUR")
            mock_mod.Stock.assert_called_with("AEX", "AEB", "EUR")

    def test_timestamp_parsed_from_date_string(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.reqHistoricalData.return_value = [_make_bar(date_str="20260415")]
        with patched_ib(ib):
            candles = _connected_adapter(ib).get_candles("AAPL")
        assert candles[0].timestamp.year == 2026
        assert candles[0].timestamp.month == 4

    def test_get_candles_creates_event_loop_in_worker_thread(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True

        def req_historical_data(*args, **kwargs):
            asyncio.get_event_loop()
            return [_make_bar(close=151.0)]

        ib.reqHistoricalData.side_effect = req_historical_data
        result: dict[str, object] = {}

        def worker() -> None:
            with patched_ib(ib):
                result["candles"] = _connected_adapter(ib).get_candles("AAPL")

        thread = threading.Thread(target=worker, name="eq-sector-test")
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive()
        candles = result["candles"]
        assert len(candles) == 1
        assert candles[0].close == pytest.approx(151.0)


# ---------------------------------------------------------------------------
# 5. get_quote
# ---------------------------------------------------------------------------


class TestGetQuote:
    def test_returns_price_from_snapshot(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ticker      = MagicMock()
        ticker.last = 155.5
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            price = _connected_adapter(ib).get_quote("AAPL")
        assert price == pytest.approx(155.5)

    def test_falls_back_to_historical_when_snapshot_zero(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ticker      = MagicMock()
        ticker.last  = 0.0
        ticker.close = 0.0
        ib.reqMktData.return_value = ticker
        ib.reqHistoricalData.return_value = [_make_bar(close=148.0)]
        with patched_ib(ib):
            price = _connected_adapter(ib).get_quote("AAPL")
        assert price == pytest.approx(148.0)

    def test_returns_none_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            price = adapter.get_quote("AAPL")
        assert price is None

    def test_returns_none_on_exception(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.reqMktData.side_effect   = RuntimeError("error")
        ib.reqHistoricalData.side_effect = RuntimeError("error")
        with patched_ib(ib):
            price = _connected_adapter(ib).get_quote("AAPL")
        assert price is None

    def test_cancels_market_data_subscription(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ticker      = MagicMock()
        ticker.last = 100.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            _connected_adapter(ib).get_quote("AAPL")
        ib.cancelMktData.assert_called_once()


# ---------------------------------------------------------------------------
# 6. place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    def test_market_order_returns_order_id(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade(order_id=99)
        with patched_ib(ib):
            order_id = _connected_adapter(ib).place_order("AAPL", "BUY", 10)
        assert order_id == "99"

    def test_limit_order_returns_order_id(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade(order_id=100)
        with patched_ib(ib) as mock_mod:
            order_id = _connected_adapter(ib).place_order(
                "AAPL", "BUY", 5, order_type="limit", limit_price=145.0
            )
        assert order_id == "100"
        mock_mod.LimitOrder.assert_called_with("BUY", 5, 145.0)

    def test_limit_order_without_price_returns_none(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        with patched_ib(ib):
            result = _connected_adapter(ib).place_order(
                "AAPL", "BUY", 5, order_type="limit", limit_price=None
            )
        assert result is None
        ib.placeOrder.assert_not_called()

    def test_returns_none_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            result = adapter.place_order("AAPL", "SELL", 10)
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.side_effect   = RuntimeError("TWS error")
        with patched_ib(ib):
            result = _connected_adapter(ib).place_order("AAPL", "BUY", 10)
        assert result is None

    def test_action_uppercased(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade()
        with patched_ib(ib) as mock_mod:
            _connected_adapter(ib).place_order("AAPL", "buy", 10)
        mock_mod.MarketOrder.assert_called_with("BUY", 10)

    def test_sell_order(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade(order_id=77)
        with patched_ib(ib) as mock_mod:
            order_id = _connected_adapter(ib).place_order("SPY", "SELL", 20)
        assert order_id == "77"
        mock_mod.MarketOrder.assert_called_with("SELL", 20)

    def test_sleep_called_after_place(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade()
        with patched_ib(ib):
            _connected_adapter(ib).place_order("AAPL", "BUY", 1)
        ib.sleep.assert_called_with(0)


# ---------------------------------------------------------------------------
# 7. get_positions
# ---------------------------------------------------------------------------


class TestGetPositions:
    def test_returns_live_positions(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = [_make_position("AAPL", qty=100.0, avg_cost=150.0)]
        ticker      = MagicMock()
        ticker.last = 155.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions is not None
        assert len(positions) == 1
        assert positions[0].symbol   == "AAPL"
        assert positions[0].quantity == pytest.approx(100.0)
        assert positions[0].side     == "buy"

    def test_short_position_has_sell_side(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = [_make_position("SPY", qty=-50.0, avg_cost=400.0)]
        ticker = MagicMock()
        ticker.last = 395.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions is not None
        assert positions[0].side     == "sell"
        assert positions[0].quantity == pytest.approx(50.0)

    def test_skips_zero_quantity_positions(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = [_make_position("AAPL", qty=0.0, avg_cost=150.0)]
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions == []

    def test_returns_empty_list_when_no_positions(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = []
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions == []

    def test_returns_none_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            result = adapter.get_positions()
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.side_effect    = RuntimeError("error")
        with patched_ib(ib):
            result = _connected_adapter(ib).get_positions()
        assert result is None

    def test_position_id_starts_with_ibkr(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = [_make_position("AAPL", qty=10.0, avg_cost=140.0)]
        ticker = MagicMock()
        ticker.last = 142.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions[0].position_id.startswith("ibkr-aapl-")

    def test_biome_id_is_equities(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value   = [_make_position("AAPL", qty=5.0, avg_cost=150.0)]
        ticker = MagicMock()
        ticker.last = 151.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            positions = _connected_adapter(ib).get_positions()
        assert positions[0].biome_id == "equities"


# ---------------------------------------------------------------------------
# 8. get_account_state
# ---------------------------------------------------------------------------


class TestGetAccountState:
    def test_returns_account_state(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.accountValues.return_value = [
            _make_account_value("TotalCashValue", "50000.0", "USD"),
            _make_account_value("UnrealizedPnL",  "2500.0",  "USD"),
        ]
        with patched_ib(ib):
            state = _connected_adapter(ib).get_account_state()
        assert state is not None
        assert state.balance          == pytest.approx(50_000.0)
        assert state.positions_value  == pytest.approx(2_500.0)
        assert state.equity           == pytest.approx(52_500.0)
        assert state.biome_id         == "equities"

    def test_returns_none_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            result = adapter.get_account_state()
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value  = True
        ib.accountValues.side_effect = RuntimeError("error")
        with patched_ib(ib):
            result = _connected_adapter(ib).get_account_state()
        assert result is None

    def test_zero_values_when_no_matching_tags(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.accountValues.return_value = []
        with patched_ib(ib):
            state = _connected_adapter(ib).get_account_state()
        assert state is not None
        assert state.balance         == pytest.approx(0.0)
        assert state.positions_value == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 9. get_market_data (protocol wrapper)
# ---------------------------------------------------------------------------


class TestGetMarketData:
    def test_returns_last_candle(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = [
            _make_bar(close=140.0),
            _make_bar(close=145.0),
        ]
        with patched_ib(ib):
            data = _connected_adapter(ib).get_market_data("AAPL", "1d")
        assert data is not None
        assert data.close == pytest.approx(145.0)

    def test_returns_none_when_no_candles(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value   = True
        ib.reqHistoricalData.return_value = []
        with patched_ib(ib):
            assert _connected_adapter(ib).get_market_data("AAPL", "1d") is None


# ---------------------------------------------------------------------------
# 10. place_live_order (BiomeAdapter protocol interface)
# ---------------------------------------------------------------------------


class TestPlaceLiveOrder:
    def _make_live_order(self, symbol: str = "AAPL", side: OrderSide = OrderSide.BUY) -> LiveOrder:
        return LiveOrder(
            mission_id        = str(uuid.uuid4()),
            ant_id            = str(uuid.uuid4()),
            symbol            = symbol,
            biome             = "equities",
            side              = side,
            order_type        = OrderType.MARKET,
            quantity          = 10.0,
            stop_loss_price   = 130.0,
            take_profit_price = 170.0,
        )

    def test_accepted_result_on_success(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.return_value  = _make_trade(order_id=55)
        ticker = MagicMock()
        ticker.last = 150.0
        ib.reqMktData.return_value = ticker
        with patched_ib(ib):
            order  = self._make_live_order()
            result = _connected_adapter(ib).place_live_order(order)
        assert result is not None
        assert result.accepted            is True
        assert result.exchange_order_id   == "55"
        assert result.filled_quantity     == pytest.approx(10.0)

    def test_rejected_result_when_order_fails(self) -> None:
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.placeOrder.side_effect   = RuntimeError("TWS down")
        with patched_ib(ib):
            order  = self._make_live_order()
            result = _connected_adapter(ib).place_live_order(order)
        assert result is not None
        assert result.accepted is False

    def test_rejected_when_not_connected(self) -> None:
        adapter = IBKRAdapter()
        with patched_ib(MagicMock()) as mock_mod:
            mock_mod.IB.return_value.connect.side_effect = ConnectionRefusedError
            order  = self._make_live_order()
            result = adapter.place_live_order(order)
        assert result is not None
        assert result.accepted is False


# ---------------------------------------------------------------------------
# 11. Constanten
# ---------------------------------------------------------------------------


class TestConstants:
    def test_paper_port_is_7497(self) -> None:
        assert _PORT_PAPER == 7497

    def test_live_port_is_7496(self) -> None:
        assert _PORT_LIVE == 7496

    def test_period_map_contains_common_periods(self) -> None:
        for period in ("1d", "5d", "1mo", "3mo", "6mo", "1y"):
            assert period in _PERIOD_TO_DURATION

    def test_interval_map_contains_daily(self) -> None:
        assert _INTERVAL_TO_BAR_SIZE["1d"] == "1 day"
