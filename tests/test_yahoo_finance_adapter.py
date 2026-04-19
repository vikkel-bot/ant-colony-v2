"""
tests/test_yahoo_finance_adapter.py

Tests voor YahooFinanceAdapter — alle yfinance-calls gemockt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_adapter import MarketData


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ohlcv_df(rows: list[tuple]) -> pd.DataFrame:
    """
    rows: list of (date_str, open, high, low, close, volume)
    """
    import pandas as pd
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


_GOOD_DF = make_ohlcv_df([
    ("2026-01-02", 100.0, 105.0, 99.0,  103.0, 1_000_000),
    ("2026-01-03", 103.0, 108.0, 102.0, 107.0, 1_100_000),
    ("2026-01-04", 107.0, 110.0, 105.0, 109.0, 900_000),
])


# ---------------------------------------------------------------------------
# 1. get_candles
# ---------------------------------------------------------------------------


class TestGetCandles:
    def _patch(self, ticker_mock):
        return patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker", return_value=ticker_mock)

    def test_returns_market_data_list(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with self._patch(ticker):
            result = adapter.get_candles("AAPL")
        assert len(result) == 3
        assert all(isinstance(r, MarketData) for r in result)

    def test_candle_fields_correct(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with self._patch(ticker):
            result = adapter.get_candles("AAPL")
        first = result[0]
        assert first.symbol == "AAPL"
        assert first.timeframe == "1d"
        assert first.open == pytest.approx(100.0)
        assert first.high == pytest.approx(105.0)
        assert first.low == pytest.approx(99.0)
        assert first.close == pytest.approx(103.0)
        assert first.volume == pytest.approx(1_000_000.0)
        assert first.biome_id == "equities"

    def test_timestamp_is_utc_aware(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with self._patch(ticker):
            result = adapter.get_candles("AAPL")
        assert result[0].timestamp.tzinfo is not None

    def test_empty_df_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=pd.DataFrame())
        with self._patch(ticker):
            result = adapter.get_candles("AAPL")
        assert result == []

    def test_exception_returns_empty_list(self) -> None:
        adapter = YahooFinanceAdapter()
        with patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker", side_effect=RuntimeError("net")):
            result = adapter.get_candles("AAPL")
        assert result == []

    def test_passes_period_and_interval(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with self._patch(ticker):
            adapter.get_candles("XLK", period="1y", interval="1wk")
        ticker.history.assert_called_once_with(period="1y", interval="1wk", auto_adjust=True)

    def test_candles_ordered_oldest_first(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(history_df=_GOOD_DF)
        with self._patch(ticker):
            result = adapter.get_candles("AAPL")
        closes = [r.close for r in result]
        assert closes == [103.0, 107.0, 109.0]

    def test_yfinance_import_error_returns_empty(self) -> None:
        adapter = YahooFinanceAdapter()
        with patch.dict("sys.modules", {"yfinance": None}):
            with patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker",
                       side_effect=Exception("import")):
                result = adapter.get_candles("AAPL")
        assert result == []


# ---------------------------------------------------------------------------
# 2. get_fundamentals
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

    def _patch(self, ticker_mock):
        return patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker", return_value=ticker_mock)

    def test_returns_all_required_keys(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info=self._make_info())
        with self._patch(ticker):
            result = adapter.get_fundamentals("AAPL")
        required = {"pe_ratio", "debt_to_equity", "roa", "roe", "current_ratio",
                    "gross_margin", "operating_cashflow", "total_assets", "net_income"}
        assert required.issubset(result.keys())

    def test_values_mapped_correctly(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info=self._make_info())
        with self._patch(ticker):
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
        with self._patch(ticker):
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
        with self._patch(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert result["operating_cashflow"] == pytest.approx(50_000_000.0)

    def test_exception_returns_zero_dict(self) -> None:
        adapter = YahooFinanceAdapter()
        with patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker",
                   side_effect=RuntimeError("net")):
            result = adapter.get_fundamentals("AAPL")
        assert result["pe_ratio"]   == 0.0
        assert result["roa"]        == 0.0
        assert result["gross_margin"] == 0.0

    def test_missing_info_key_returns_zero(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={})
        with self._patch(ticker):
            result = adapter.get_fundamentals("AAPL")
        assert all(isinstance(v, float) for v in result.values())


# ---------------------------------------------------------------------------
# 3. get_dividend_info
# ---------------------------------------------------------------------------


class TestGetDividendInfo:
    def _patch(self, ticker_mock):
        return patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker", return_value=ticker_mock)

    def _make_dividend_series(self, years: list[int]) -> pd.Series:
        import pandas as pd
        dates = [pd.Timestamp(f"{yr}-06-01", tz="UTC") for yr in years]
        return pd.Series([1.0] * len(dates), index=pd.DatetimeIndex(dates))

    def test_returns_required_keys(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": 0.03, "payoutRatio": 0.45})
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert {"dividend_yield", "consecutive_years", "payout_ratio"}.issubset(result.keys())

    def test_dividend_yield_and_payout(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": 0.035, "payoutRatio": 0.55})
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"] == pytest.approx(0.035)
        assert result["payout_ratio"]   == pytest.approx(0.55)

    def test_consecutive_years_counted(self) -> None:
        adapter = YahooFinanceAdapter()
        current_year = datetime.now(tz=timezone.utc).year
        years = list(range(current_year - 29, current_year + 1))  # 30 consecutive years
        divs = self._make_dividend_series(years)
        ticker = make_ticker_mock(
            info={"dividendYield": 0.03, "payoutRatio": 0.4},
            dividends=divs,
        )
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 30

    def test_gap_in_years_stops_count(self) -> None:
        adapter = YahooFinanceAdapter()
        current_year = datetime.now(tz=timezone.utc).year
        # 5 consecutive, then a gap
        years = list(range(current_year - 4, current_year + 1)) + [current_year - 10]
        divs = self._make_dividend_series(years)
        ticker = make_ticker_mock(
            info={"dividendYield": 0.03, "payoutRatio": 0.4},
            dividends=divs,
        )
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 5

    def test_empty_dividends_returns_zero_years(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(
            info={"dividendYield": 0.02, "payoutRatio": 0.3},
            dividends=pd.Series([], dtype=float),
        )
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["consecutive_years"] == 0

    def test_none_yield_becomes_zero(self) -> None:
        adapter = YahooFinanceAdapter()
        ticker = make_ticker_mock(info={"dividendYield": None, "payoutRatio": None})
        with self._patch(ticker):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"] == 0.0
        assert result["payout_ratio"]   == 0.0

    def test_exception_returns_zero_dict(self) -> None:
        adapter = YahooFinanceAdapter()
        with patch("ant_colony.biome.adapters.yahoo_finance_adapter.yf.Ticker",
                   side_effect=RuntimeError("net")):
            result = adapter.get_dividend_info("KO")
        assert result["dividend_yield"]   == 0.0
        assert result["consecutive_years"] == 0
        assert result["payout_ratio"]     == 0.0
