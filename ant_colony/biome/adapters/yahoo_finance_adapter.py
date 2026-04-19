"""
ant_colony/biome/adapters/yahoo_finance_adapter.py

YahooFinanceAdapter — leest marktdata en fundamentals via yfinance (gratis, geen auth).

Methoden:
  get_candles(symbol, period, interval) -> list[MarketData]
  get_fundamentals(symbol)              -> dict
  get_dividend_info(symbol)             -> dict

Fail-closed: elke methode vangt alle exceptions op en retourneert [] of {}.
Geen code wordt uitgevoerd bij import (P7).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from ant_colony.biome.biome_adapter import MarketData

log = logging.getLogger(__name__)

_BIOME_ID = "equities"


class YahooFinanceAdapter:
    """
    Adapter voor Yahoo Finance via de yfinance library.

    Alle methoden zijn read-only en fail-closed:
      - Retourneren [] of {} bij fouten, nooit een exception.
      - Logt waarschuwingen voor elk probleem.

    Geen authenticatie vereist.
    """

    # ------------------------------------------------------------------
    # OHLCV candles
    # ------------------------------------------------------------------

    def get_candles(
        self,
        symbol: str,
        period: str = "3mo",
        interval: str = "1d",
    ) -> list[MarketData]:
        """
        Haal OHLCV bars op voor symbool.

        Args:
            symbol:   Ticker-symbool (bijv. "AAPL", "XLK").
            period:   Historische periode: "1mo", "3mo", "6mo", "1y", "5y".
            interval: Bar-grootte: "1d", "1wk", "1mo".

        Returns:
            Lijst van MarketData (oudste eerst), leeg bij fout.
        """
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty:
                log.warning("Geen data voor %s (period=%s interval=%s)", symbol, period, interval)
                return []

            result: list[MarketData] = []
            for ts, row in df.iterrows():
                # yfinance levert timezone-aware timestamps
                if hasattr(ts, "to_pydatetime"):
                    dt = ts.to_pydatetime()
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = datetime.now(tz=timezone.utc)

                result.append(
                    MarketData(
                        symbol=symbol,
                        timeframe=interval,
                        timestamp=dt,
                        open=float(row.get("Open", 0.0)),
                        high=float(row.get("High", 0.0)),
                        low=float(row.get("Low", 0.0)),
                        close=float(row.get("Close", 0.0)),
                        volume=float(row.get("Volume", 0.0)),
                        biome_id=_BIOME_ID,
                    )
                )
            return result

        except Exception:
            log.exception("get_candles mislukt voor %s", symbol)
            return []

    # ------------------------------------------------------------------
    # Fundamentele data
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """
        Haal fundamentele ratio's op voor een aandeel.

        Returns:
            Dict met: pe_ratio, debt_to_equity, roa, roe, current_ratio,
            gross_margin, operating_cashflow, total_assets, net_income.
            Alle waarden zijn floats (0.0 als onbekend).
        """
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            def _f(key: str) -> float:
                v = info.get(key)
                try:
                    return float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    return 0.0

            # Cashflow voor operating cash flow
            ocf = 0.0
            try:
                cf = ticker.cashflow
                if cf is not None and not cf.empty:
                    for label in ("Operating Cash Flow", "Total Cash From Operating Activities"):
                        if label in cf.index:
                            ocf = float(cf.loc[label].iloc[0])
                            break
            except Exception:
                pass

            # Netto inkomen
            net_income = 0.0
            try:
                fin = ticker.financials
                if fin is not None and not fin.empty:
                    for label in ("Net Income", "Net Income Common Stockholders"):
                        if label in fin.index:
                            net_income = float(fin.loc[label].iloc[0])
                            break
            except Exception:
                pass

            # Total assets
            total_assets = _f("totalAssets")
            if total_assets == 0.0:
                try:
                    bs = ticker.balance_sheet
                    if bs is not None and not bs.empty:
                        for label in ("Total Assets",):
                            if label in bs.index:
                                total_assets = float(bs.loc[label].iloc[0])
                                break
                except Exception:
                    pass

            return {
                "pe_ratio":          _f("trailingPE"),
                "debt_to_equity":    _f("debtToEquity"),
                "roa":               _f("returnOnAssets"),
                "roe":               _f("returnOnEquity"),
                "current_ratio":     _f("currentRatio"),
                "gross_margin":      _f("grossMargins"),
                "operating_cashflow": ocf,
                "total_assets":      total_assets,
                "net_income":        net_income,
            }

        except Exception:
            log.exception("get_fundamentals mislukt voor %s", symbol)
            return {
                "pe_ratio": 0.0, "debt_to_equity": 0.0, "roa": 0.0,
                "roe": 0.0, "current_ratio": 0.0, "gross_margin": 0.0,
                "operating_cashflow": 0.0, "total_assets": 0.0, "net_income": 0.0,
            }

    # ------------------------------------------------------------------
    # Dividend data
    # ------------------------------------------------------------------

    def get_dividend_info(self, symbol: str) -> dict[str, Any]:
        """
        Haal dividendinformatie op voor een aandeel.

        Returns:
            Dict met: dividend_yield (fraction), consecutive_years (int),
            payout_ratio (fraction).
        """
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

            dividend_yield = float(info.get("dividendYield") or 0.0)
            payout_ratio   = float(info.get("payoutRatio")   or 0.0)

            # Tel opeenvolgende jaren met dividenduitkering
            consecutive_years = 0
            try:
                divs = ticker.dividends
                if divs is not None and not divs.empty:
                    years_with_divs = set(divs.index.year)
                    current_year = datetime.now(tz=timezone.utc).year
                    for yr in range(current_year, current_year - 60, -1):
                        if yr in years_with_divs:
                            consecutive_years += 1
                        else:
                            break
            except Exception:
                pass

            return {
                "dividend_yield":    dividend_yield,
                "consecutive_years": consecutive_years,
                "payout_ratio":      payout_ratio,
            }

        except Exception:
            log.exception("get_dividend_info mislukt voor %s", symbol)
            return {
                "dividend_yield": 0.0,
                "consecutive_years": 0,
                "payout_ratio": 0.0,
            }
