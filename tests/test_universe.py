"""Tests voor U(t). Kern: geen informatie van tijdstip >= t mag meetellen."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

from ant_colony.lab import universe as U

D = U.DAY_MS
T0 = 1_700_000_000_000 - (1_700_000_000_000 % D)


def _series(start_day: int, days: int, close: float = 10.0, turnover_eur: float = 100_000.0):
    return [[T0 + (start_day + i) * D, close, close, close, close, turnover_eur / close] for i in range(days)]


def test_future_candles_do_not_change_universe():
    t = T0 + 400 * D
    thin = _series(0, 400, turnover_eur=1_000)          # vóór t te dun
    future = _series(400, 60, turnover_eur=10_000_000)  # na t enorm liquide
    assert "X-EUR" not in U.universe({"X-EUR": thin}, t)
    assert "X-EUR" not in U.universe({"X-EUR": thin + future}, t)


def test_candle_exactly_at_t_is_ignored():
    t = T0 + 400 * D
    base = _series(0, 400, turnover_eur=1_000)
    at_t = [[t, 10.0, 10.0, 10.0, 10.0, 1e9]]
    assert not U.is_admitted(base + at_t, t)


def test_history_boundary_180_days():
    s = _series(0, 400)
    assert not U.is_admitted(s, T0 + 179 * D + 1)
    assert U.is_admitted(s, T0 + 180 * D)


def test_turnover_is_volume_times_close():
    # volume in basismunt: 10.000 stuks x EUR 5 = EUR 50.000 -> precies op de drempel
    s = [[T0 + i * D, 5.0, 5.0, 5.0, 5.0, 10_000.0] for i in range(300)]
    assert U.is_admitted(s, T0 + 300 * D)
    s_low = [[T0 + i * D, 4.99, 4.99, 4.99, 4.99, 10_000.0] for i in range(300)]
    assert not U.is_admitted(s_low, T0 + 300 * D)


def test_excluded_bases_never_admitted():
    s = _series(0, 400, turnover_eur=10_000_000)
    got = U.universe({"USDC-EUR": s, "EURCV-EUR": s, "WBTC-EUR": s, "BTC-EUR": s}, T0 + 400 * D)
    assert got == frozenset({"BTC-EUR"})


def test_top_k_integer_rule():
    assert [U.top_k(n) for n in (0, 30, 40, 44, 45, 49, 50, 112)] == [8, 8, 8, 8, 9, 9, 10, 22]
    assert all(U.top_k(n) == max(8, n // 5) for n in range(0, 2000))


def test_no_wall_clock_in_module():
    src = inspect.getsource(U)
    assert "now(" not in src and "time.time" not in src


def test_matches_census_admission():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import universe_census as uc

    series = {
        "A-EUR": _series(0, 500, turnover_eur=80_000),
        "B-EUR": _series(200, 300, turnover_eur=40_000),
        "C-EUR": _series(100, 150, turnover_eur=500_000),
    }
    for day in range(0, 520, 13):
        t = T0 + day * D
        census = frozenset(
            m for m, c in series.items()
            if (lambda ok_med: ok_med[0] and ok_med[1] is not None and ok_med[1] >= U.MIN_TURNOVER_EUR)(uc.admitted(c, t))
        )
        assert U.universe(series, t) == census, day
