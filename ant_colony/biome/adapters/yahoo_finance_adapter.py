"""
ant_colony/biome/adapters/yahoo_finance_adapter.py

YahooFinanceAdapter — BiomeAdapter implementatie voor Yahoo Finance (yfinance).

Verantwoordelijkheden:
  1. BiomeAdapter protocol implementeren voor equities / ETF biome
  2. Historische OHLCV candles leveren via get_candles() (ResearchAnt)
  3. Snapshot fundamentele ratio's leveren via get_fundamentals() (FundamentalAnt)
  4. Dividendinformatie leveren via get_dividend_info() (DividendScoutAnt)
  5. In-memory caching om API-calls te beperken

Regels:
  - yfinance lazy-import (binnen methoden): module is bruikbaar ook zonder installatie
    → is_available() geeft False als yfinance niet geïnstalleerd is
  - Alle publieke methoden gooien nooit (fail-closed P2)
  - place_order() en get_positions() retourneren altijd None (read-only)
  - Geen code uitvoeren bij import (P7)

Installatie: pip install yfinance
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ant_colony.biome.biome_adapter import AccountState, LivePosition, MarketData

log = logging.getLogger(__name__)

_BIOME_ID_DEFAULT    = "equities"
_CACHE_TTL_SECS      = 300.0    # 5 minuten marktdata cache
_FUND_CACHE_TTL_SECS = 86_400.0 # 24 uur fundamentals cache
_DIVIDEND_HISTORY_PERIOD = "5y"


class YahooFinanceAdapter:
    """
    Read-only BiomeAdapter voor Yahoo Finance (yfinance).

    Implementeert het BiomeAdapter protocol (duck typing) en voegt
    equities-specifieke methoden toe die door FundamentalAnt en
    DividendScoutAnt worden gebruikt.

    biome_id: configureert welke biome deze adapter bedient ("equities" of "etf").
    """

    def __init__(self, biome_id: str = _BIOME_ID_DEFAULT) -> None:
        self._biome_id = biome_id
        self._log      = logging.getLogger(f"adapter.yahoo.{biome_id}")

        # In-memory caches: key → (timestamp_monotonic, data)
        self._candle_cache: dict[str, tuple[float, list[MarketData]]] = {}
        self._fund_cache:   dict[str, tuple[float, dict]]             = {}
        self._div_cache:    dict[str, tuple[float, dict]]             = {}

    # ------------------------------------------------------------------
    # BiomeAdapter protocol
    # ------------------------------------------------------------------

    @property
    def biome_id(self) -> str:
        return self._biome_id

    def is_available(self) -> bool:
        """True als yfinance geïnstalleerd en importeerbaar is."""
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            self._log.warning("yfinance niet geïnstalleerd — adapter niet beschikbaar")
            return False

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        """Retourneert de meest recente bar als MarketData, of None bij fout."""
        candles = self.get_candles(symbol, period="5d", interval=timeframe or "1d")
        return candles[-1] if candles else None

    def get_account_state(self) -> AccountState | None:
        """Niet van toepassing — Yahoo Finance heeft geen account."""
        return None

    def place_order(self, order: Any) -> None:
        """Niet van toepassing — read-only adapter."""
        return None

    def get_positions(self) -> list[LivePosition] | None:
        """Niet van toepassing — Yahoo Finance heeft geen positiebeheer."""
        return None

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
            Lijst van MarketData (oudste eerst), leeg bij fout of geen data.
        """
        cache_key = f"{symbol}:{period}:{interval}"
        cached = self._candle_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECS:
            return cached[1]

        try:
            import yfinance as yf

            df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=True)
            if df is None or df.empty:
                self._log.warning("Geen data voor %s (period=%s interval=%s)", symbol, period, interval)
                return []

            result: list[MarketData] = []
            for ts, row in df.iterrows():
                try:
                    if hasattr(ts, "to_pydatetime"):
                        dt = ts.to_pydatetime()
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = datetime.now(tz=timezone.utc)

                    close = float(row.get("Close") or 0.0)
                    if close <= 0:
                        continue

                    result.append(MarketData(
                        symbol=symbol,
                        timeframe=interval,
                        timestamp=dt,
                        open=float(row.get("Open") or 0.0),
                        high=float(row.get("High") or 0.0),
                        low=float(row.get("Low")  or 0.0),
                        close=close,
                        volume=float(row.get("Volume") or 0.0),
                        biome_id=self._biome_id,
                    ))
                except Exception:
                    continue

            self._candle_cache[cache_key] = (time.monotonic(), result)
            return result

        except Exception:
            self._log.exception("get_candles mislukt voor %s", symbol)
            return []

    # ------------------------------------------------------------------
    # Fundamentele data (snapshot-ratio's voor Piotroski)
    # ------------------------------------------------------------------

    def get_fundamentals(self, symbol: str) -> dict[str, Any]:
        """
        Haal fundamentele ratio's op voor een aandeel.

        Returns:
            Dict met: pe_ratio, debt_to_equity, roa, roe, current_ratio,
            gross_margin, operating_cashflow, total_assets, net_income.
            Alle waarden zijn floats (0.0 als onbekend).
        """
        cached = self._fund_cache.get(symbol)
        if cached is not None and (time.monotonic() - cached[0]) < _FUND_CACHE_TTL_SECS:
            return cached[1]

        default = {
            "pe_ratio": 0.0, "debt_to_equity": 0.0, "roa": 0.0,
            "roe": 0.0, "current_ratio": 0.0, "gross_margin": 0.0,
            "operating_cashflow": 0.0, "total_assets": 0.0, "net_income": 0.0,
        }

        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
            info   = ticker.info or {}

            def _f(key: str) -> float:
                v = info.get(key)
                try:
                    return float(v) if v is not None else 0.0
                except (TypeError, ValueError):
                    return 0.0

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

            total_assets = _f("totalAssets")
            if total_assets == 0.0:
                try:
                    bs = ticker.balance_sheet
                    if bs is not None and not bs.empty and "Total Assets" in bs.index:
                        total_assets = float(bs.loc["Total Assets"].iloc[0])
                except Exception:
                    pass

            result = {
                "pe_ratio":           _f("trailingPE"),
                "debt_to_equity":     _f("debtToEquity"),
                "roa":                _f("returnOnAssets"),
                "roe":                _f("returnOnEquity"),
                "current_ratio":      _f("currentRatio"),
                "gross_margin":       _f("grossMargins"),
                "operating_cashflow": ocf,
                "total_assets":       total_assets,
                "net_income":         net_income,
            }
            self._fund_cache[symbol] = (time.monotonic(), result)
            return result

        except Exception:
            self._log.exception("get_fundamentals mislukt voor %s", symbol)
            return default

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
        cached = self._div_cache.get(symbol)
        if cached is not None and (time.monotonic() - cached[0]) < _FUND_CACHE_TTL_SECS:
            return cached[1]

        default = {"dividend_yield": 0.0, "consecutive_years": 0, "payout_ratio": 0.0}

        try:
            import yfinance as yf

            ticker = yf.Ticker(symbol)
            info   = ticker.info or {}

            # yfinance dividendYield is inconsistent: sometimes fraction (0.0237),
            # sometimes percentage (2.37). Use dividendRate/price as ground truth.
            div_rate = float(
                info.get("dividendRate") or
                info.get("trailingAnnualDividendRate") or 0.0
            )
            current_price = float(
                info.get("currentPrice") or
                info.get("regularMarketPrice") or
                info.get("previousClose") or 0.0
            )
            if div_rate > 0 and current_price > 0:
                dividend_yield = div_rate / current_price
            else:
                raw = float(
                    info.get("dividendYield") or
                    info.get("trailingAnnualDividendYield") or 0.0
                )
                # If raw > 1.0 it was returned as percentage — normalise to fraction
                dividend_yield = raw / 100.0 if raw > 1.0 else raw

            payout_ratio   = float(info.get("payoutRatio")   or 0.0)

            consecutive_years = 0
            try:
                # Gebruik geen ticker.dividends: yfinance haalt daarvoor impliciet
                # volledige historie op, soms met startdatums rond 1927.
                history = ticker.history(
                    period=_DIVIDEND_HISTORY_PERIOD,
                    interval="1d",
                    actions=True,
                    auto_adjust=False,
                )
                if history is not None and not history.empty and "Dividends" in history.columns:
                    divs = history[history["Dividends"].fillna(0.0) > 0.0]
                    years_with_divs = set(divs.index.year)
                    current_year    = datetime.now(tz=timezone.utc).year
                    for yr in range(current_year, current_year - 5, -1):
                        if yr in years_with_divs:
                            consecutive_years += 1
                        else:
                            break
            except Exception:
                pass

            result = {
                "dividend_yield":    dividend_yield,
                "consecutive_years": consecutive_years,
                "payout_ratio":      payout_ratio,
            }
            self._div_cache[symbol] = (time.monotonic(), result)
            return result

        except Exception:
            self._log.exception("get_dividend_info mislukt voor %s", symbol)
            return default
