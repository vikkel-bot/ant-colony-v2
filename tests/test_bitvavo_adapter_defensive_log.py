"""
tests/test_bitvavo_adapter_defensive_log.py

Controleert dat BitvavoAdapter.get_market_data() voor niet-crypto symbolen
(geen "-" in de naam) logt op DEBUG in plaats van WARNING, zodat equities
ETF-symbolen (XLK, XLE, QQQ etc.) die per ongeluk binnenkomen geen spam
produceren.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

import ant_colony.biome.adapters.bitvavo_adapter as bitvavo_module
from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter


def _make_adapter(candles_return=None) -> BitvavoAdapter:
    """Maak een BitvavoAdapter met een gemockte Bitvavo-client."""
    adapter = BitvavoAdapter.__new__(BitvavoAdapter)
    client = MagicMock()
    client.candles.return_value = candles_return  # None of een dict = API-fout
    adapter._client = client
    adapter._paper_mode = True
    return adapter


class TestBitvavoAdapterDefensiveLog:
    def test_niet_crypto_symbool_geeft_debug_geen_warning(self, caplog):
        """XLK heeft geen '-' → API-fout logt op DEBUG, niet WARNING."""
        adapter = _make_adapter(candles_return={"errorCode": 205, "error": "market parameter is invalid."})

        with caplog.at_level(logging.DEBUG, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            result = adapter.get_market_data("XLK", "1m")

        assert result is None
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING
                           and "XLK" in r.message]
        assert len(warning_records) == 0, "Geen WARNING verwacht voor niet-crypto symbool XLK"

    def test_niet_crypto_symbool_logt_debug(self, caplog):
        """XLE → API-fout wordt wél gelogd, maar op DEBUG niveau."""
        adapter = _make_adapter(candles_return={"errorCode": 205, "error": "market parameter is invalid."})

        with caplog.at_level(logging.DEBUG, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            adapter.get_market_data("XLE", "1m")

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG
                         and "XLE" in r.message]
        assert len(debug_records) >= 1

    def test_crypto_symbool_api_fout_geeft_warning(self, caplog):
        """BTC-EUR heeft '-' → API-fout logt op WARNING (bestaand gedrag behouden)."""
        adapter = _make_adapter(candles_return={"errorCode": 999, "error": "some error"})

        with caplog.at_level(logging.WARNING, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            result = adapter.get_market_data("BTC-EUR", "1m")

        assert result is None
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING
                           and "BTC-EUR" in r.message]
        assert len(warning_records) == 1

    def test_qqq_geen_warning(self, caplog):
        """QQQ (geen '-') → geen WARNING spam."""
        adapter = _make_adapter(candles_return={"errorCode": 205, "error": "market parameter is invalid."})

        with caplog.at_level(logging.DEBUG, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            result = adapter.get_market_data("QQQ", "1d")

        assert result is None
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING
                           and "QQQ" in r.message]
        assert len(warning_records) == 0

    def test_lege_candles_lijst_niet_crypto_debug(self, caplog):
        """Lege candle-lijst voor niet-crypto symbool → DEBUG."""
        adapter = _make_adapter(candles_return=[])

        with caplog.at_level(logging.DEBUG, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            result = adapter.get_market_data("XLU", "1d")

        assert result is None
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING
                           and "XLU" in r.message]
        assert len(warning_records) == 0

    def test_trage_bitvavo_candle_call_timeout_returnt_none(self, monkeypatch, caplog):
        """Een hangende candles-call mag PaperAnt niet blokkeren."""
        adapter = _make_adapter()

        def slow_candles(*_args, **_kwargs):
            time.sleep(0.05)
            return [[0, "1", "1", "1", "1", "1"]]

        adapter._client.candles.side_effect = slow_candles
        monkeypatch.setattr(bitvavo_module, "_BITVAVO_API_TIMEOUT_SECONDS", 0.01)

        started = time.perf_counter()
        with caplog.at_level(logging.WARNING, logger="ant_colony.biome.adapters.bitvavo_adapter"):
            result = adapter.get_market_data("BTC-EUR", "1m")
        elapsed = time.perf_counter() - started

        assert result is None
        assert elapsed < 0.2
        assert any("Bitvavo API timeout" in record.message for record in caplog.records)
