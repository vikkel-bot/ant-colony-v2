"""
tests/test_yahoo_finance_adapter.py

Tests voor YahooFinanceAdapter — alle yfinance-calls gemockt via sys.modules.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ohlcv_df(rows: list[tuple]) -> pd.DataFrame:
    """rows: list of (date_str, open, high, low, close, volume)"""
    index = pd.DatetimeIndex(
        [pd.Timestamp(r[0], tz="UTC") for r in rows],
        name="Date",
    )
    data = {
        "Open":   [r[1] for r in rows],
        "High":   [r[2] for r in rows],
        "Low":    [r[3] for r in rows],
        "Close":  [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    }
    return pd.DataFrame(data, index=index)


def make_ticker_mock(
    info: dict | None = None,
    history_df: pd.DataFrame | None = None,
    cashflow_df: pd.DataFrame | None = None,
    financials_df: pd.DataFrame | None = None,
    balance_sheet_df: pd.DataFrame | None = None,
    dividends: pd.Series | None = None,
) -> MagicMock:
    ticker = MagicMock()
    ticker.info = info or {}
    ticker.history.return_value = history_df if history_df is not None else pd.DataFrame()
    ticker.cashflow = cashflow_df if cashflow_df is not None else pd.DataFrame()
    ticker.financials = financials_df if financials_df is not None else pd.DataFrame()
    ticker.balance_sheet = balance_sheet_df if balance_sheet_df is not None else pd.DataFrame()
    ticker.dividends = dividends if dividends is not None else pd.Series([], dtype=float)
    return ticker


def patch_yfinance(ticker_mock: MagicMock):
    """Context manager that injects a mock yfinance module into sys.modules."""
    mock_yf = MagicMock()
    mock_yf.Ticker.return_value = ticker_mock
    return patch.dict(sys.modules, {"yfinance": mock_yf})


_GOOD_DF = make_ohlcv_df([
    ("2026-01-02", 100.0, 105.0, 99.0,  103.0, 1_000_000),
    ("2026-01-03", 103.0, 108.0, 102.0, 107.0, 1_100_000),
    ("2026-01-04", 107.0, 110.0, 105.0, 109.0, 900_000),
])


# ---------------------------------------------------------------------------
# 1. BiomeAdapter protocol
# ---------------------------------------------------------------------------


class TestBiomeAdapterProtocol:
    def test_biome_id_default(self) -> None:
        adapter = YahooFinanceAdapter()
        assert adapter.biome_id == "equities"

    def test_biome_id_custom(self) -> None:
        adapter = YahooFinanceAdapter(biome_id="etf")
        assert adapter.biome_id == "etf"

    def test_is_available_when_yfinance_importable(self) -> None:
        mock_yf = MagicMock()
        adapter = YahooFinanceAdapter()
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            assert adapter.is_available() is True

    def test_is_available_false_when_not_installed(self) -> None:
        adapter = YahooFinanceAdapter()
        with patch.dict(sys.modules, {"yfinance": None}):
            assert adapter.is_available() is False

    def test_get_account_state_returns_none(self) -> None:
        assert YahooFinanceAdapter().get_account_state() is None

    def test_place_order_returns_none(self) -> None:
        assert YahooFinanceAdapter().place_order(object()) is None

    def test_get_positions_returns_none(self) -> None:
        assert YahooFinanceAdapter().get_positions() is None

    def test_get_market_data_returns_last_candle(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            md = adapter.get_market_data("AAPL", "1d")
        assert md is not None
        assert isinstance(md, MarketData)
        assert md.close == pytest.approx(109.0)

    def test_get_market_data_empty_returns_none(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=pd.DataFrame())
        with patch_yfinance(ticker):
            md = adapter.get_market_data("AAPL", "1d")
        assert md is None


# ---------------------------------------------------------------------------
# 2. get_candles
# ---------------------------------------------------------------------------


class TestGetCandles:
    def test_returns_market_data_list(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        assert len(result) == 3
        assert all(isinstance(r, MarketData) for r in result)

    def test_candle_fields_correct(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        first = result[0]
        assert first.symbol    == "AAPL"
        assert first.timeframe == "1d"
        assert first.open   == pytest.approx(100.0)
        assert first.high   == pytest.approx(105.0)
        assert first.low    == pytest.approx(99.0)
        assert first.close  == pytest.approx(103.0)
        assert first.volume == pytest.approx(1_000_000.0)
        assert first.biome_id == "equities"

    def test_timestamp_is_utc_aware(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        assert result[0].timestamp.tzinfo is not None

    def test_empty_df_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=pd.DataFrame())
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        assert result == []

    def test_exception_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("network")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            result = adapter.get_candles("AAPL")
        assert result == []

    def test_passes_period_and_interval(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            adapter.get_candles("XLK", period="1y", interval="1wk")
        ticker.history.assert_called_once_with(period="1y", interval="1wk", auto_adjust=True)

    def test_candles_ordered_oldest_first(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        closes = [r.close for r in result]
        assert closes == [103.0, 107.0, 109.0]

    def test_zero_close_rows_skipped(self) -> None:
        df = make_ohlcv_df([
            ("2026-01-02", 100.0, 105.0, 99.0, 0.0,   1_000),  # close=0 → skipped
            ("2026-01-03", 103.0, 108.0, 102.0, 107.0, 1_100),
        ])
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=df)
        with patch_yfinance(ticker):
            result = adapter.get_candles("AAPL")
        assert len(result) == 1
        assert result[0].close == pytest.approx(107.0)

    def test_result_cached_on_second_call(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with patch_yfinance(ticker):
            adapter.get_candles("AAPL")
            adapter.get_candles("AAPL")
        assert ticker.history.call_count == 1


# ---------------------------------------------------------------------------
# 3. get_fundamentals
# ---------------------------------------------------------------------------


class TestGetFundamentals:
    def _make_info(self, **overrides) -> dict:
        base = {
            "trailingPE":     18.5,
            "debtToEquity":   0.45,
            "returnOnAssets": 0.12,
            "returnOnEquity": 0.25,
            "currentRatio":   2.1,
            "grossMargins":   0.40,
            "totalAssets":    100_000_000.0,
        }
        base.update(overrides)
        return base

    def test_returns_all_required_keys(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info=self._make_info())
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        required = {"pe_ratio", "debt_to_equity", "roa", "roe", "current_ratio",
                    "gross_margin", "operating_cashflow", "total_assets", "net_income"}
        assert required.issubset(result.keys())

    def test_values_mapped_correctly(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info=self._make_info())
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert result["pe_ratio"]       == pytest.approx(18.5)
        assert result["debt_to_equity"] == pytest.approx(0.45)
        assert result["roa"]            == pytest.approx(0.12)
        assert result["roe"]            == pytest.approx(0.25)
        assert result["current_ratio"]  == pytest.approx(2.1)
        assert result["gross_margin"]   == pytest.approx(0.40)

    def test_none_values_become_zero(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"trailingPE": None, "debtToEquity": None})
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert result["pe_ratio"]       == 0.0
        assert result["debt_to_equity"] == 0.0

    def test_operating_cashflow_from_cashflow_df(self) -> None:
        adapter = YahooFinanceAdapter()
        cf_df = pd.DataFrame(
            {"2025": [50_000_000.0]},
            index=["Operating Cash Flow"],
        )
        ticker = make_ticker_mock(info=self._make_info(), cashflow_df=cf_df)
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert result["operating_cashflow"] == pytest.approx(50_000_000.0)

    def test_net_income_from_financials_df(self) -> None:
        adapter = YahooFinanceAdapter()
        fin_df = pd.DataFrame(
            {"2025": [12_000_000.0]},
            index=["Net Income"],
        )
        ticker = make_ticker_mock(info=self._make_info(), financials_df=fin_df)
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert result["net_income"] == pytest.approx(12_000_000.0)

    def test_exception_returns_zero_dict(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("network")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            result = adapter.get_fundamentals("AAPL")
        assert result["pe_ratio"]     == 0.0
        assert result["roa"]          == 0.0
        assert result["gross_margin"] == 0.0

    def test_missing_info_key_returns_zero(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={})
        with patch_yfinance(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert all(isinstance(v, float) for v in result.values())

    def test_result_cached_on_second_call(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_yf = MagicMock()
        mock_yf.Ticker.return_value = make_ticker_mock(info=self._make_info())
        with patch.dict("sys.modules", {"yfinance": mock_yf}):
            adapter.get_fundamentals("AAPL")
            adapter.get_fundamentals("AAPL")
        assert mock_yf.Ticker.call_count == 1


# ---------------------------------------------------------------------------
# 4. get_dividend_info
# ---------------------------------------------------------------------------


class TestGetDividendInfo:
    def _make_dividend_series(self, years: list[int]) -> pd.Series:
        dates = [pd.Timestamp(f"{yr}-06-01", tz="UTC") for yr in years]
        return pd.Series([1.0] * len(dates), index=pd.DatetimeIndex(dates))

    def test_returns_required_keys(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": 0.03, "payoutRatio": 0.45})
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert {"dividend_yield", "consecutive_years", "payout_ratio"}.issubset(result.keys())

    def test_dividend_yield_and_payout(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": 0.035, "payoutRatio": 0.55})
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"] == pytest.approx(0.035)
        assert result["payout_ratio"]   == pytest.approx(0.55)

    def test_consecutive_years_counted(self) -> None:
        adapter = YahooFinanceAdapter()
        current_year = datetime.now(tz=timezone.utc).year
        years = list(range(current_year - 29, current_year + 1))
        divs = self._make_dividend_series(years)
        ticker = make_ticker_mock(
            info={"dividendYield": 0.03, "payoutRatio": 0.4},
            dividends=divs,
        )
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 30

    def test_gap_in_years_stops_count(self) -> None:
        adapter = YahooFinanceAdapter()
        current_year = datetime.now(tz=timezone.utc).year
        years = list(range(current_year - 4, current_year + 1)) + [current_year - 10]
        divs = self._make_dividend_series(years)
        ticker = make_ticker_mock(
            info={"dividendYield": 0.03, "payoutRatio": 0.4},
            dividends=divs,
        )
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 5

    def test_empty_dividends_returns_zero_years(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(
            info={"dividendYield": 0.02, "payoutRatio": 0.3},
            dividends=pd.Series([], dtype=float),
        )
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 0

    def test_none_yield_becomes_zero(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": None, "payoutRatio": None})
        with patch_yfinance(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"] == 0.0
        assert result["payout_ratio"]   == 0.0

    def test_exception_returns_zero_dict(self) -> None:
        adapter = YahooFinanceAdapter()
        mock_yf = MagicMock()
        mock_yf.Ticker.side_effect = RuntimeError("network")
        with patch.dict(sys.modules, {"yfinance": mock_yf}):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"]    == 0.0
        assert result["consecutive_years"] == 0
        assert result["payout_ratio"]      == 0.0
