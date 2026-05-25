"""
ant_colony/lab/backtester.py

Backtester — deterministisch in-memory backtester voor StrategyCandidate validatie.

Werking:
  - Neemt een reeks OHLCVBar's en een BacktestConfig
  - Opent een positie op de sluitingsprijs van elke bar zonder open positie
  - Sluit via TP, SL of TTL (max_bars_held)
  - Berekent BacktestResults: sharpe, max drawdown, trade count, win rate,
    avg win/loss, best streak, en SMA200-gebaseerde regime statistieken

Regels:
  - Volledig deterministisch — geen random elementen
  - Alleen sluitingsprijzen gebruikt voor entry en exit beslissingen
  - Fee en slippage worden toegepast op effectieve entry/exit-prijzen
  - Minimaal 2 bars vereist voor minstens één volledige trade
  - Stale bars (close ≤ 0) worden overgeslagen
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ant_colony.schemas.strategy_candidate import BacktestResults

_log = logging.getLogger(__name__)

# Minimum bars voor een betrouwbare backtest (logt warning, geen harde fout)
_MIN_RELIABLE_BARS = 200
# Minimum trades bij strategy-specifieke entry logica; minder → sharpe=None
_MIN_TRADES = 5

# Indicator warmup periodes
_RSI_PERIOD = 14
_BB_PERIOD  = 20
_SMA_FAST   = 20
_SMA_SLOW   = 50
_MOM_PERIOD = 3
_MR_PERIOD  = 20


# ---------------------------------------------------------------------------
# Datastructuren
# ---------------------------------------------------------------------------

@dataclass
class OHLCVBar:
    """
    Één OHLCV price bar.

    timestamp:  Tijdstip van de bar (timezone-aware).
    open:       Openingsprijs.
    high:       Hoogste prijs.
    low:        Laagste prijs.
    close:      Sluitingsprijs — gebruikt voor entry en exit evaluatie.
    volume:     Handelsvolume (optioneel, 0.0 als onbekend).
    """
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class BacktestConfig:
    """
    Configuratie voor één backtestrun.

    direction:        "long" of "short"
    take_profit_pct:  TP-afstand van entry als fractie (0.06 = 6%)
    stop_loss_pct:    SL-afstand van entry als fractie (0.03 = 3%)
    max_bars_held:    Maximaal aantal bars in positie voor TTL-exit (≥ 1)
    strategy_type:    Optioneel — bepaalt entry-logica (sma_crossover, rsi_based,
                      bollinger_bands, momentum, mean_reversion). None = elke bar.
    fee_pct:          Fee per kant als fractie (0.0025 = 0.25%)
    slippage_pct:     Slippage per kant als fractie (0.001 = 0.1%)
    """
    direction: str
    take_profit_pct: float
    stop_loss_pct: float
    max_bars_held: int = 10
    strategy_type: str | None = None
    fee_pct: float = 0.0025
    slippage_pct: float = 0.001

    def __post_init__(self) -> None:
        if self.direction not in ("long", "short"):
            raise ValueError(
                f"direction must be 'long' or 'short', got '{self.direction}'"
            )
        if self.take_profit_pct <= 0:
            raise ValueError(
                f"take_profit_pct must be > 0, got {self.take_profit_pct}"
            )
        if self.stop_loss_pct <= 0:
            raise ValueError(
                f"stop_loss_pct must be > 0, got {self.stop_loss_pct}"
            )
        if self.max_bars_held < 1:
            raise ValueError(
                f"max_bars_held must be >= 1, got {self.max_bars_held}"
            )
        if self.fee_pct < 0:
            raise ValueError(f"fee_pct must be >= 0, got {self.fee_pct}")
        if self.slippage_pct < 0:
            raise ValueError(
                f"slippage_pct must be >= 0, got {self.slippage_pct}"
            )
        if self.fee_pct + self.slippage_pct >= 1.0:
            raise ValueError(
                "fee_pct + slippage_pct must be < 1.0 for positive effective prices"
            )


# ---------------------------------------------------------------------------
# Backtester
# ---------------------------------------------------------------------------

class Backtester:
    """
    Deterministisch in-memory backtester.

    Strategie:
      - Elke bar zonder open positie: open een positie op de sluitingsprijs
      - Exit zodra TP, SL of TTL bereikt is (geëvalueerd op sluitingsprijzen)
      - Na exit: direct herentry op de volgende bar

    PnL berekening gebruikt effectieve prijzen na fee en slippage:
      - LONG entry hoger, exit lager
      - SHORT entry lager, exit hoger

    Statistieken:
      - sharpe_ratio:      mean(returns) / std(returns)  — None bij < 2 trades
      - max_drawdown_pct:  max relatieve drawdown van de equity curve
      - total_trades:      aantal voltooide trades
      - win_rate:          fractie winstgevende trades (return > 0)
      - avg_win:           gemiddeld rendement van winnende trades
      - avg_loss:          gemiddeld verlies van verliezende trades (positief)
      - best_streak:       langste opeenvolgende reeks winnende trades
      - regime_stats:      bull/bear/sideways stats o.b.v. SMA200 (None bij < 200 bars)
      - best_regime:       regime met hoogste sharpe (min 3 trades)

    Usage::

        config = BacktestConfig(direction="long", take_profit_pct=0.06, stop_loss_pct=0.03)
        bars = [OHLCVBar(...), ...]
        results = Backtester().run(bars, config)
        print(results.win_rate, results.sharpe_ratio, results.best_regime)
    """

    def run(self, bars: list[OHLCVBar], config: BacktestConfig) -> BacktestResults:
        """
        Voer een backtest uit.

        Args:
            bars:    Tijdgesorteerde OHLCV bars. Minimaal 2 nodig voor een trade.
            config:  Strategie-parameters (inclusief optioneel strategy_type).

        Returns:
            BacktestResults met alle statistieken ingevuld.
            sharpe_ratio is None als: minder dan 2 trades (altijd), of minder dan
            _MIN_TRADES trades bij strategy-specifieke entry logica.

        Raises:
            ValueError: als bars leeg is.
        """
        if not bars:
            raise ValueError("bars must not be empty")

        st = config.strategy_type
        if len(bars) < _MIN_RELIABLE_BARS:
            _log.warning(
                "Backtest heeft slechts %d bars (aanbevolen ≥ %d) | strategy_type=%s",
                len(bars), _MIN_RELIABLE_BARS, st,
            )

        closes: list[float] = [b.close for b in bars]
        trade_returns: list[float] = []
        gross_trade_returns: list[float] = []
        trade_entry_indices: list[int] = []
        equity: list[float] = [1.0]
        gross_equity: list[float] = [1.0]
        total_fee_drag = 0.0
        signal_count = 0

        i = 0
        while i < len(bars) - 1:
            entry_close = bars[i].close
            if entry_close <= 0:
                i += 1
                continue

            if st is not None and not self._has_entry_signal(closes, i, st, config.direction):
                i += 1
                continue

            signal_count += 1
            exit_price, exit_idx = self._find_exit(bars, i, entry_close, config)
            effective_entry, effective_exit = self._effective_prices(
                entry_close, exit_price, config
            )
            gross_ret = self._trade_return(entry_close, exit_price, config.direction)
            ret = self._trade_return(effective_entry, effective_exit, config.direction)
            trade_returns.append(ret)
            gross_trade_returns.append(gross_ret)
            total_fee_drag += max(0.0, gross_ret - ret)
            trade_entry_indices.append(i)
            equity.append(equity[-1] * (1.0 + ret))
            gross_equity.append(gross_equity[-1] * (1.0 + gross_ret))
            i = exit_idx + 1

        if st is not None:
            _log.debug(
                "Backtest klaar | strategy_type=%s bars=%d signals=%d trades=%d",
                st, len(bars), signal_count, len(trade_returns),
            )

        sharpe = self._sharpe(trade_returns)
        if st is not None and len(trade_returns) < _MIN_TRADES:
            _log.warning(
                "Backtest onbetrouwbaar | strategy_type=%s trades=%d < %d "
                "— te weinig trades, sharpe=None",
                st, len(trade_returns), _MIN_TRADES,
            )
            sharpe = None

        avg_win, avg_loss = self._avg_win_loss(trade_returns)
        best_streak = self._best_streak(trade_returns)
        regime_stats, best_regime = self._regime_analysis(
            bars, trade_returns, trade_entry_indices
        )

        return BacktestResults(
            sharpe_ratio=sharpe,
            max_drawdown_pct=self._max_drawdown(equity),
            total_trades=len(trade_returns),
            win_rate=self._win_rate(trade_returns),
            total_fees_pct=total_fee_drag,
            avg_win=avg_win,
            avg_loss=avg_loss,
            best_streak=best_streak,
            regime_stats=regime_stats,
            best_regime=best_regime,
            extra={
                "total_return_pct": equity[-1] - 1.0,
                "gross_total_return_pct": gross_equity[-1] - 1.0,
                "fee_pct_per_side": config.fee_pct,
                "slippage_pct_per_side": config.slippage_pct,
            },
        )

    # ------------------------------------------------------------------
    # Intern — exit logica
    # ------------------------------------------------------------------

    def _find_exit(
        self,
        bars: list[OHLCVBar],
        entry_idx: int,
        entry_price: float,
        config: BacktestConfig,
    ) -> tuple[float, int]:
        """
        Zoek de exitbar en exitprijs.

        Scant forward vanaf entry_idx + 1. Eerste bar waarop TP of SL is bereikt
        bepaalt de exit. Als geen TP/SL binnen max_bars_held: TTL-exit op sluitings-
        prijs van de laatste bar in het window.

        Returns:
            (exit_price, exit_bar_idx)
        """
        tp_price, sl_price = self._tp_sl_prices(entry_price, config)
        end_idx = min(entry_idx + config.max_bars_held, len(bars) - 1)

        for j in range(entry_idx + 1, end_idx + 1):
            close = bars[j].close
            if close <= 0:
                continue
            if config.direction == "long":
                if close >= tp_price:
                    return tp_price, j
                if close <= sl_price:
                    return sl_price, j
            else:
                if close <= tp_price:
                    return tp_price, j
                if close >= sl_price:
                    return sl_price, j

        # TTL: exit op sluitingsprijs van de laatste bar in het window
        return bars[end_idx].close, end_idx

    @staticmethod
    def _tp_sl_prices(
        entry_price: float, config: BacktestConfig
    ) -> tuple[float, float]:
        """Bereken absolute TP- en SL-prijzen vanuit entry."""
        if config.direction == "long":
            return (
                entry_price * (1.0 + config.take_profit_pct),
                entry_price * (1.0 - config.stop_loss_pct),
            )
        else:
            return (
                entry_price * (1.0 - config.take_profit_pct),
                entry_price * (1.0 + config.stop_loss_pct),
            )

    @staticmethod
    def _effective_prices(
        entry_price: float,
        exit_price: float,
        config: BacktestConfig,
    ) -> tuple[float, float]:
        """Pas fee en slippage per kant toe op entry en exit."""
        cost = config.fee_pct + config.slippage_pct
        if config.direction == "long":
            return (
                entry_price * (1.0 + cost),
                exit_price * (1.0 - cost),
            )
        return (
            entry_price * (1.0 - cost),
            exit_price * (1.0 + cost),
        )

    # ------------------------------------------------------------------
    # Intern — strategy-specifieke entry logica
    # ------------------------------------------------------------------

    def _has_entry_signal(
        self,
        closes: list[float],
        i: int,
        strategy_type: str,
        direction: str,
    ) -> bool:
        """
        True als bar i een valide entry-signaal geeft voor de opgegeven strategy_type.

        Elke strategie heeft een eigen warmup-periode; bars vóór de warmup
        geven altijd False terug. Onbekend/hybrid type → True (elke bar).
        """
        if strategy_type == "sma_crossover":
            if i < _SMA_SLOW:
                return False
            fast_now  = sum(closes[i - _SMA_FAST + 1 : i + 1]) / _SMA_FAST
            slow_now  = sum(closes[i - _SMA_SLOW + 1 : i + 1]) / _SMA_SLOW
            fast_prev = sum(closes[i - _SMA_FAST     : i    ]) / _SMA_FAST
            slow_prev = sum(closes[i - _SMA_SLOW     : i    ]) / _SMA_SLOW
            if direction == "long":
                return fast_prev <= slow_prev and fast_now > slow_now
            return fast_prev >= slow_prev and fast_now < slow_now

        if strategy_type == "rsi_based":
            if i < _RSI_PERIOD:
                return False
            rsi_val = self._rsi(closes, i)
            return rsi_val < 30 if direction == "long" else rsi_val > 70

        if strategy_type == "bollinger_bands":
            if i < _BB_PERIOD:
                return False
            window  = closes[i - _BB_PERIOD + 1 : i + 1]
            mean_bb = sum(window) / _BB_PERIOD
            std_bb  = math.sqrt(sum((c - mean_bb) ** 2 for c in window) / _BB_PERIOD)
            upper   = mean_bb + 2.0 * std_bb
            lower   = mean_bb - 2.0 * std_bb
            return closes[i] <= lower if direction == "long" else closes[i] >= upper

        if strategy_type == "momentum":
            if i < _MOM_PERIOD:
                return False
            mom = (closes[i] - closes[i - _MOM_PERIOD]) / closes[i - _MOM_PERIOD]
            return mom > 0.02 if direction == "long" else mom < -0.02

        if strategy_type == "mean_reversion":
            if i < _MR_PERIOD:
                return False
            window  = closes[i - _MR_PERIOD + 1 : i + 1]
            mean_mr = sum(window) / _MR_PERIOD
            var     = sum((c - mean_mr) ** 2 for c in window) / _MR_PERIOD
            std_mr  = math.sqrt(var)
            if std_mr == 0.0:
                return False
            z = (closes[i] - mean_mr) / std_mr
            return z < -2.0 if direction == "long" else z > 2.0

        # Onbekend / hybrid strategy type → elke bar (veilig fallback)
        return True

    @staticmethod
    def _rsi(closes: list[float], i: int) -> float:
        """
        Vereenvoudigde RSI op basis van simple average gains/losses.

        Gebruikt closes[i-_RSI_PERIOD : i+1]. Retourneert 50.0 bij onvoldoende data.
        """
        start  = max(0, i - _RSI_PERIOD)
        window = closes[start : i + 1]
        if len(window) < 2:
            return 50.0
        gains  = [max(0.0, window[j] - window[j - 1]) for j in range(1, len(window))]
        losses = [max(0.0, window[j - 1] - window[j]) for j in range(1, len(window))]
        avg_gain = sum(gains)  / len(gains)
        avg_loss = sum(losses) / len(losses)
        if avg_loss == 0.0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    # ------------------------------------------------------------------
    # Intern — statistieken
    # ------------------------------------------------------------------

    @staticmethod
    def _trade_return(entry: float, exit_price: float, direction: str) -> float:
        """PnL als fractie van de entryprijs."""
        if direction == "long":
            return (exit_price - entry) / entry
        else:
            return (entry - exit_price) / entry

    @staticmethod
    def _win_rate(returns: list[float]) -> float | None:
        """Fractie winstgevende trades. None als er geen trades zijn."""
        if not returns:
            return None
        return sum(1 for r in returns if r > 0) / len(returns)

    @staticmethod
    def _sharpe(returns: list[float]) -> float | None:
        """
        Vereenvoudigde Sharpe ratio: mean / std over trade returns.

        Niet geannualiseerd — bedoeld voor vergelijking tussen configuraties,
        niet als absolute maatstaf.
        None bij minder dan 2 trades; 0.0 bij nul-variantie.
        """
        if len(returns) < 2:
            return None
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(variance)
        if std == 0.0:
            return 0.0
        return mean / std

    @staticmethod
    def _max_drawdown(equity: list[float]) -> float:
        """
        Maximale relatieve drawdown van de equity curve.

        0.0 als de equity nooit daalt of maar één punt heeft.
        """
        if len(equity) < 2:
            return 0.0
        peak = equity[0]
        max_dd = 0.0
        for val in equity[1:]:
            if val > peak:
                peak = val
            dd = (peak - val) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _avg_win_loss(returns: list[float]) -> tuple[float | None, float | None]:
        """Gemiddeld rendement winnende trades en gemiddeld verlies verliezende trades."""
        wins   = [r for r in returns if r > 0]
        losses = [abs(r) for r in returns if r < 0]
        avg_win  = round(sum(wins)   / len(wins),   4) if wins   else None
        avg_loss = round(sum(losses) / len(losses), 4) if losses else None
        return avg_win, avg_loss

    @staticmethod
    def _best_streak(returns: list[float]) -> int | None:
        """Langste opeenvolgende reeks van winstgevende trades."""
        if not returns:
            return None
        best = current = 0
        for r in returns:
            if r > 0:
                current += 1
                if current > best:
                    best = current
            else:
                current = 0
        return best

    # ------------------------------------------------------------------
    # Intern — regime detectie (SMA200)
    # ------------------------------------------------------------------

    @staticmethod
    def _sma200_aligned(closes: list[float]) -> list[float | None]:
        """
        SMA200 uitgelijnde reeks — None voor de eerste 199 bars.

        sma200_aligned[i] is de SMA200 berekend over closes[i-199 : i+1].
        """
        period = 200
        result: list[float | None] = [None] * len(closes)
        for i in range(period - 1, len(closes)):
            result[i] = sum(closes[i - period + 1 : i + 1]) / period
        return result

    @staticmethod
    def _label_regime(close: float, sma200: float) -> str:
        """
        Label een bar als bull / sideways / bear op basis van positie t.o.v. SMA200.

        sideways: prijs binnen 2% van SMA200
        bull:     prijs > 2% boven SMA200
        bear:     prijs > 2% onder SMA200
        """
        if sma200 <= 0:
            return "sideways"
        pct_diff = (close - sma200) / sma200
        if abs(pct_diff) <= 0.02:
            return "sideways"
        return "bull" if pct_diff > 0 else "bear"

    def _regime_analysis(
        self,
        bars: list[OHLCVBar],
        trade_returns: list[float],
        trade_entry_indices: list[int],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """
        Berekent per-regime statistieken op basis van SMA200.

        Vereist minimaal 200 bars. Trades waarvoor SMA200 nog niet beschikbaar
        is (entry_idx < 199) worden genegeerd.

        Returns:
            (regime_stats, best_regime) of (None, None) bij onvoldoende data.
        """
        if len(bars) < 200 or not trade_returns:
            return None, None

        closes  = [b.close for b in bars]
        sma200  = self._sma200_aligned(closes)

        regime_returns: dict[str, list[float]] = {
            "bull": [], "bear": [], "sideways": []
        }

        for idx, ret in zip(trade_entry_indices, trade_returns):
            sma_val = sma200[idx]
            if sma_val is None:
                continue
            regime = self._label_regime(bars[idx].close, sma_val)
            regime_returns[regime].append(ret)

        stats: dict[str, Any] = {}
        for regime, returns in regime_returns.items():
            if not returns:
                continue
            stats[regime] = {
                "trade_count": len(returns),
                "win_rate":    round(
                    sum(1 for r in returns if r > 0) / len(returns), 3
                ),
                "sharpe":      round(self._sharpe(returns) or 0.0, 3),
            }

        if not stats:
            return None, None

        # Best regime: min 3 trades voor betrouwbaarheid
        viable = {k: v for k, v in stats.items() if v["trade_count"] >= 3}
        pool   = viable if viable else stats
        best   = max(pool, key=lambda k: pool[k]["sharpe"])
        return stats, best
