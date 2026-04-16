"""
tests/test_crypto_adapter.py

Fase 10 — CryptoAdapter (Bitvavo).

Test-opzet:
  - TestCryptoAdapterInit          biome_id, paper_only default
  - TestCryptoAdapterIsAvailable   connectivity check, fail-closed
  - TestCryptoAdapterGetMarketData candle parsing, errors, timeframe guard
  - TestCryptoAdapterGetAccountState balance parsing, errors
  - TestCryptoAdapterPlaceOrder    paper_only guard, live path
  - TestCryptoAdapterGetPositions  spot exchange, lege lijst
  - TestCryptoAdapterProtocol      isinstance BiomeAdapter
  - TestCryptoAdapterFailClosed    geen enkele methode gooit
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from ant_colony.biome import BiomeAdapter
from ant_colony.biome.crypto_adapter import CryptoAdapter
from ant_colony.schemas.order import (
    LiveOrder,
    OrderRejectionReason,
    OrderSide,
    OrderType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _mock_client(**overrides) -> MagicMock:
    client = MagicMock()
    now_ms = _now_ms()
    client.time.return_value = {"time": now_ms}
    client.candles.return_value = [
        [now_ms, "30000.00", "31000.00", "29000.00", "30500.00", "100.0"]
    ]
    client.balance.return_value = [
        {"symbol": "EUR", "available": "5000.00", "inOrder": "250.00"},
        {"symbol": "BTC", "available": "0.1", "inOrder": "0.0"},
    ]
    for key, value in overrides.items():
        getattr(client, key).return_value = value
    return client


def _adapter(paper_only: bool = True, **client_overrides) -> CryptoAdapter:
    return CryptoAdapter(_client=_mock_client(**client_overrides), paper_only=paper_only)


def _make_order() -> LiveOrder:
    return LiveOrder(
        mission_id="mission-1",
        ant_id="ant-1",
        symbol="BTC-EUR",
        biome="crypto",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=0.001,
        stop_loss_price=29_000.0,
        take_profit_price=32_000.0,
    )


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

class TestCryptoAdapterInit:
    def test_biome_id_is_crypto(self):
        assert _adapter().biome_id == "crypto"

    def test_paper_only_default_is_true(self):
        adapter = CryptoAdapter(_client=_mock_client())
        assert adapter._paper_only is True

    def test_paper_only_can_be_disabled(self):
        adapter = CryptoAdapter(_client=_mock_client(), paper_only=False)
        assert adapter._paper_only is False


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------

class TestCryptoAdapterIsAvailable:
    def test_true_when_time_returns_correctly(self):
        assert _adapter().is_available() is True

    def test_false_when_time_raises(self):
        client = _mock_client()
        client.time.side_effect = ConnectionError("timeout")
        adapter = CryptoAdapter(_client=client)
        assert adapter.is_available() is False

    def test_false_when_time_returns_error_dict_without_time_key(self):
        adapter = _adapter(time={"errorCode": 403, "error": "unauthorized"})
        assert adapter.is_available() is False

    def test_false_when_time_returns_none(self):
        adapter = _adapter(time=None)
        assert adapter.is_available() is False


# ---------------------------------------------------------------------------
# get_market_data
# ---------------------------------------------------------------------------

class TestCryptoAdapterGetMarketData:
    def test_returns_market_data_for_valid_candle(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.symbol == "BTC-EUR"
        assert data.timeframe == "1h"
        assert data.biome_id == "crypto"

    def test_close_price_parsed_correctly(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.close == 30_500.0

    def test_open_high_low_parsed(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.open == 30_000.0
        assert data.high == 31_000.0
        assert data.low == 29_000.0

    def test_volume_parsed(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.volume == 100.0

    def test_timestamp_is_utc_aware(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.timestamp.tzinfo is not None
        assert data.timestamp.tzinfo == timezone.utc

    def test_fresh_data_is_not_stale(self):
        data = _adapter().get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.is_stale() is False

    def test_returns_none_for_unknown_timeframe(self):
        assert _adapter().get_market_data("BTC-EUR", "99x") is None

    def test_returns_none_when_candles_raises(self):
        client = _mock_client()
        client.candles.side_effect = RuntimeError("API fout")
        adapter = CryptoAdapter(_client=client)
        assert adapter.get_market_data("BTC-EUR", "1h") is None

    def test_returns_none_when_candles_returns_error_dict(self):
        adapter = _adapter(candles={"errorCode": 400, "error": "invalid market"})
        assert adapter.get_market_data("BTC-EUR", "1h") is None

    def test_returns_none_when_candles_returns_empty_list(self):
        adapter = _adapter(candles=[])
        assert adapter.get_market_data("BTC-EUR", "1h") is None

    def test_all_supported_timeframes(self):
        timeframes = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]
        for tf in timeframes:
            data = _adapter().get_market_data("BTC-EUR", tf)
            assert data is not None, f"timeframe {tf!r} leverde None op"


# ---------------------------------------------------------------------------
# get_account_state
# ---------------------------------------------------------------------------

class TestCryptoAdapterGetAccountState:
    def test_returns_account_state(self):
        state = _adapter().get_account_state()
        assert state is not None

    def test_balance_is_eur_available(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.balance == 5_000.0

    def test_positions_value_is_eur_in_order(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.positions_value == 250.0

    def test_currency_is_eur(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.currency == "EUR"

    def test_biome_id_is_crypto(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.biome_id == "crypto"

    def test_timestamp_is_utc_aware(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.timestamp.tzinfo == timezone.utc

    def test_equity_is_balance_plus_positions(self):
        state = _adapter().get_account_state()
        assert state is not None
        assert state.equity == pytest.approx(5_250.0)

    def test_returns_none_when_balance_raises(self):
        client = _mock_client()
        client.balance.side_effect = RuntimeError("netwerk fout")
        adapter = CryptoAdapter(_client=client)
        assert adapter.get_account_state() is None

    def test_returns_none_when_balance_returns_error_dict(self):
        adapter = _adapter(balance={"errorCode": 401, "error": "unauthorized"})
        assert adapter.get_account_state() is None

    def test_zero_balance_when_no_eur_in_response(self):
        client = _mock_client()
        client.balance.return_value = [
            {"symbol": "BTC", "available": "0.5", "inOrder": "0.0"},
        ]
        adapter = CryptoAdapter(_client=client)
        state = adapter.get_account_state()
        assert state is not None
        assert state.balance == 0.0
        assert state.positions_value == 0.0


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------

class TestCryptoAdapterPlaceOrder:
    def test_paper_only_returns_rejected_result(self):
        order = _make_order()
        result = _adapter(paper_only=True).place_order(order)
        assert result is not None
        assert result.accepted is False

    def test_paper_only_rejection_reason_is_exchange_rejected(self):
        order = _make_order()
        result = _adapter(paper_only=True).place_order(order)
        assert result is not None
        assert result.rejection_reason == OrderRejectionReason.EXCHANGE_REJECTED

    def test_paper_only_order_id_preserved(self):
        order = _make_order()
        result = _adapter(paper_only=True).place_order(order)
        assert result is not None
        assert result.order_id == order.order_id

    def test_paper_only_rejection_detail_mentions_fase_10(self):
        order = _make_order()
        result = _adapter(paper_only=True).place_order(order)
        assert result is not None
        assert "fase 10" in result.rejection_detail.lower()

    def test_live_path_returns_none(self):
        order = _make_order()
        result = _adapter(paper_only=False).place_order(order)
        assert result is None


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------

class TestCryptoAdapterGetPositions:
    def test_returns_empty_list(self):
        positions = _adapter().get_positions()
        assert positions == []

    def test_returns_list_not_none(self):
        assert _adapter().get_positions() is not None


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestCryptoAdapterProtocol:
    def test_isinstance_biome_adapter(self):
        assert isinstance(_adapter(), BiomeAdapter)


# ---------------------------------------------------------------------------
# Fail-closed — geen enkele methode gooit
# ---------------------------------------------------------------------------

class TestCryptoAdapterFailClosed:
    def test_is_available_never_raises(self):
        client = _mock_client()
        client.time.side_effect = Exception("onbekende fout")
        adapter = CryptoAdapter(_client=client)
        result = adapter.is_available()
        assert result is False

    def test_get_market_data_never_raises(self):
        client = _mock_client()
        client.candles.side_effect = Exception("onbekende fout")
        adapter = CryptoAdapter(_client=client)
        result = adapter.get_market_data("BTC-EUR", "1h")
        assert result is None

    def test_get_account_state_never_raises(self):
        client = _mock_client()
        client.balance.side_effect = Exception("onbekende fout")
        adapter = CryptoAdapter(_client=client)
        result = adapter.get_account_state()
        assert result is None

    def test_place_order_never_raises_in_paper_mode(self):
        order = _make_order()
        result = _adapter(paper_only=True).place_order(order)
        assert result is not None

    def test_get_positions_never_raises(self):
        result = _adapter().get_positions()
        assert result is not None
