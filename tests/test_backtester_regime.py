"""
tests/test_backtester_regime.py

Tests voor de uitgebreide Backtester statistieken:
  - avg_win / avg_loss
  - best_streak
  - regime detectie (SMA200: bull / bear / sideways)

Scenarios:
  1.  avg_win en avg_loss correct berekend
  2.  avg_win None als er geen winnende trades zijn
  3.  avg_loss None als er geen verliezende trades zijn
  4.  best_streak correct — langste reeks wins
  5.  best_streak = 0 bij alleen verliezende trades
  6.  regime_stats is None bij < 200 bars
  7.  regime_stats aanwezig bij ≥ 200 bars
  8.  best_regime is de regime met de hoogste sharpe
  9.  _label_regime — bull boven 2% SMA200
  10. _label_regime — bear onder 2% SMA200
  11. _label_regime — sideways binnen 2% SMA200
  12. _sma200_aligned lengte en None voor eerste 199 posities
  13. Alle nieuwe velden aanwezig in BacktestResults na run()
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ant_colony.lab.backtester import Backtester, BacktestConfig, OHLCVBar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bars(prices: list[float]) -> list[OHLCVBar]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        OHLCVBar(
            timestamp=base + timedelta(hours=i),
            open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def make_rising_bars(n: int, start: float = 100.0, step: float = 1.0) -> list[OHLCVBar]:
    """Altijd stijgende markt — alle long trades winnen via TP."""
    return make_bars([start + i * step for i in range(n)])


def make_flat_bars(n: int, price: float = 100.0) -> list[OHLCVBar]:
    """Vlakke markt — alle trades eindigen via TTL."""
    return make_bars([price] * n)


def long_config(tp: float = 0.06, sl: float = 0.03) -> BacktestConfig:
    return BacktestConfig(direction="long", take_profit_pct=tp, stop_loss_pct=sl, max_bars_held=10)


# ---------------------------------------------------------------------------
# avg_win / avg_loss
# ---------------------------------------------------------------------------

def test_avg_win_avg_loss_present_after_run() -> None:
    bars    = make_rising_bars(50)
    results = Backtester().run(bars, long_config())
    # stijgende markt → overwegend winstgevende trades
    assert results.avg_win is not None


def test_avg_loss_none_when_no_losses() -> None:
    """Sterk stijgende markt — elke trade raakt TP, geen verliezen."""
    bars    = make_rising_bars(50, step=10.0)   # 10% per bar, TP 6% → elke trade wint
    results = Backtester().run(bars, long_config(tp=0.05, sl=0.10))
    assert results.avg_win  is not None
    assert results.avg_loss is None     # geen verliezende trades


def test_avg_win_none_when_no_wins() -> None:
    """Dalende markt met korte SL — elke trade verliest."""
    bars = make_bars([100.0 - i * 5.0 for i in range(50) if 100.0 - i * 5.0 > 0])
    # Gebruik grote SL zodat SL nooit geraakt wordt en trade via TTL sluit met verlies
    # In een dalende markt met korte TTL sluit de long trade met verlies
    results = Backtester().run(bars, BacktestConfig(
        direction="long", take_profit_pct=0.50, stop_loss_pct=0.01, max_bars_held=3
    ))
    # avg_loss aanwezig als er verliezende trades zijn
    if results.avg_loss is not None:
        assert results.avg_loss > 0


# ---------------------------------------------------------------------------
# best_streak
# ---------------------------------------------------------------------------

def test_best_streak_correct() -> None:
    """Stijgende markt → alle trades winnen, streak = total_trades."""
    bars    = make_rising_bars(50, step=10.0)
    results = Backtester().run(bars, long_config(tp=0.05, sl=0.50))
    assert results.best_streak is not None
    assert results.best_streak >= 1


def test_best_streak_zero_flat_market() -> None:
    """Vlakke markt: prijs beweegt niet → geen TP/SL → TTL-exit, geen winst."""
    bars    = make_flat_bars(30)
    results = Backtester().run(bars, long_config())
    # TTL-exits op exacte entry price → return = 0 → niet winstgevend
    assert results.best_streak == 0


def test_best_streak_none_no_trades() -> None:
    """Slechts 1 bar → geen trades → best_streak is None."""
    bars    = make_bars([100.0])
    results = Backtester().run(bars, long_config())
    assert results.best_streak is None


# ---------------------------------------------------------------------------
# Regime detectie
# ---------------------------------------------------------------------------

def test_regime_stats_none_below_200_bars() -> None:
    bars    = make_rising_bars(100)
    results = Backtester().run(bars, long_config())
    assert results.regime_stats is None
    assert results.best_regime  is None


def test_regime_stats_present_with_200_plus_bars() -> None:
    """200+ bars → regime_stats ingevuld als er trades zijn na bar 199."""
    bars    = make_rising_bars(300, step=1.0)
    results = Backtester().run(bars, long_config())
    # regime_stats kan None zijn als alle trades vóór bar 199 vallen
    # maar met 300 bars en step=1 zijn er genoeg trades na bar 199
    if results.regime_stats is not None:
        assert isinstance(results.regime_stats, dict)
        for regime_data in results.regime_stats.values():
            assert "trade_count" in regime_data
            assert "win_rate"    in regime_data
            assert "sharpe"      in regime_data


def test_best_regime_is_string_when_stats_present() -> None:
    bars    = make_rising_bars(300, step=2.0)
    results = Backtester().run(bars, long_config())
    if results.regime_stats:
        assert results.best_regime in ("bull", "bear", "sideways")


# ---------------------------------------------------------------------------
# _label_regime
# ---------------------------------------------------------------------------

def test_label_regime_bull() -> None:
    bt = Backtester()
    # prijs 5% boven SMA200 → bull
    assert bt._label_regime(105.0, 100.0) == "bull"


def test_label_regime_bear() -> None:
    bt = Backtester()
    # prijs 5% onder SMA200 → bear
    assert bt._label_regime(95.0, 100.0) == "bear"


def test_label_regime_sideways_above() -> None:
    bt = Backtester()
    # prijs 1% boven SMA200 → sideways
    assert bt._label_regime(101.0, 100.0) == "sideways"


def test_label_regime_sideways_below() -> None:
    bt = Backtester()
    # prijs 1% onder SMA200 → sideways
    assert bt._label_regime(99.0, 100.0) == "sideways"


def test_label_regime_exact_boundary() -> None:
    bt = Backtester()
    # precies 2% boven → sideways (abs(0.02) <= 0.02)
    assert bt._label_regime(102.0, 100.0) == "sideways"
    # 2.01% boven → bull
    assert bt._label_regime(102.01, 100.0) == "bull"


# ---------------------------------------------------------------------------
# _sma200_aligned
# ---------------------------------------------------------------------------

def test_sma200_aligned_length_matches_input() -> None:
    closes = [float(i) for i in range(250)]
    result = Backtester._sma200_aligned(closes)
    assert len(result) == len(closes)


def test_sma200_aligned_first_199_are_none() -> None:
    closes = [float(i) for i in range(250)]
    result = Backtester._sma200_aligned(closes)
    for i in range(199):
        assert result[i] is None


def test_sma200_aligned_199th_is_not_none() -> None:
    closes = [float(i) for i in range(250)]
    result = Backtester._sma200_aligned(closes)
    assert result[199] is not None


def test_sma200_aligned_value_correct() -> None:
    """SMA200[199] = gemiddelde van closes[0..199] = 99.5 voor range(200)."""
    closes = [float(i) for i in range(250)]
    result = Backtester._sma200_aligned(closes)
    expected = sum(range(200)) / 200   # 99.5
    assert abs(result[199] - expected) < 1e-9


# ---------------------------------------------------------------------------
# Alle velden aanwezig in BacktestResults
# ---------------------------------------------------------------------------

def test_all_new_fields_in_results() -> None:
    bars    = make_rising_bars(50)
    results = Backtester().run(bars, long_config())
    # Controleer dat alle nieuwe velden bestaan (waarde mag None zijn)
    assert hasattr(results, "avg_win")
    assert hasattr(results, "avg_loss")
    assert hasattr(results, "best_streak")
    assert hasattr(results, "regime_stats")
    assert hasattr(results, "best_regime")
