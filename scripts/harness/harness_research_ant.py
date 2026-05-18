"""
scripts/harness/harness_research_ant.py

Standalone test-harness voor ResearchAnt.

Wat het doet:
  1. Bouwt een minimale Mission (crypto, BTC-EUR)
  2. Registreert een stub BiomeAdapter met get_candles() die 300 synthetische
     OHLCV-bars retourneert (geen netwerk, geen echte data)
  3. Draait één _tick() — alle drie strategiechecks: SMA, RSI, Bollinger
  4. Print welke kandidaten geaccepteerd of afgewezen zijn

Veiligheid:
  - Geen live broker, geen orders, geen kapitaal
  - Fail-closed: crashes worden gecatcht en geprint
  - Geen disk-logging (logs_root=None)

Gebruik:
  python scripts/harness/harness_research_ant.py
"""

from __future__ import annotations

import logging
import math
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.ants.research_ant import ResearchAnt
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Synthetische OHLCV-generator — geen netwerk
# ---------------------------------------------------------------------------

def _synthetic_candles(
    symbol: str,
    n: int = 300,
    base_price: float = 30_000.0,
    biome_id: str = "crypto",
) -> list[MarketData]:
    """
    Synthetische candles ontworpen om zowel een entry-signaal als een positieve
    backtestresultaten te produceren.

    Structuur (cyclus van 35 bars):
      - 30 bars langzaam omhoog  (+0.25 % per bar, kleine ruis)
      - 5 bars scherp omlaag     (-2.50 % per bar, kleine ruis)
        → RSI < 30 bij de laatste bar van de daling (backtester-drempel)
        → Instapmomenten zijn vlak vóór herstel → TP wordt geraakt, SL niet
    Over 300 bars geeft dit ~8 cycli → backtester heeft ≥ 5 trades.
    Netto drift per cyclus: +7.5 % − 12.5 % ≈ negatief …
    Laatste bar eindigt op een scherpe daling zodat RSI < 30 is.
    """
    rng = random.Random(42)
    candles: list[MarketData] = []
    price = base_price
    now = datetime.now(tz=timezone.utc)

    up_bars, down_bars = 30, 5
    cycle = up_bars + down_bars

    for i in range(n):
        phase = i % cycle
        if phase < up_bars:
            drift = 0.0025
            noise = rng.gauss(0, 0.0005)
        else:
            drift = -0.025
            noise = rng.gauss(0, 0.001)

        price = max(price * (1 + drift + noise), 1.0)
        ts = now - timedelta(hours=n - i)
        spread = abs(rng.gauss(0, 0.001)) * price
        candles.append(
            MarketData(
                symbol=symbol,
                timeframe="1h",
                timestamp=ts,
                open=round(price * (1 + rng.uniform(-0.001, 0.001)), 2),
                high=round(price + spread, 2),
                low=round(max(price - spread, 1.0), 2),
                close=round(price, 2),
                volume=round(rng.uniform(10.0, 100.0), 4),
                biome_id=biome_id,
            )
        )
    return candles


# ---------------------------------------------------------------------------
# Stub adapter — levert synthetische candles
# ---------------------------------------------------------------------------

class _StubCryptoAdapter:
    biome_id = "crypto"

    def is_available(self) -> bool:
        return True

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        bars = _synthetic_candles(symbol, n=5)
        return bars[-1] if bars else None

    def get_candles(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> list[MarketData]:
        return _synthetic_candles(symbol, n=min(limit, 300))

    def get_account_state(self):
        return None

    def place_order(self, order):
        return None

    def get_positions(self):
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_mission() -> Mission:
    return Mission(
        mission_id="harness-research-001",
        ant_type="research_ant",
        allowed_node="harness-node",
        allowed_actions=["analyze", "propose_candidate", "report"],
        market_scope=MarketScope(
            biome="crypto",
            symbols=["BTC-EUR"],
            timeframes=["1h"],
        ),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=1.0,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="Harness smoke-test"),
    )


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("ResearchAnt harness — één tick")
    print("=" * 60)

    mission = _build_mission()

    registry = BiomeRegistry()
    registry.register(_StubCryptoAdapter())

    scheduler = MagicMock(spec=ColonyScheduler)

    ant = ResearchAnt(
        ant_id=f"harness-{uuid.uuid4().hex[:8]}",
        mission=mission,
        scheduler=scheduler,
        biome_registry=registry,
        logs_root=None,  # geen disk-logging
    )
    ant._status = AntStatus.RUNNING

    # Snapshot van last_emitted vóór tick
    before = dict(ant._last_emitted)

    print("\n--- _tick() start ---")
    try:
        ant._tick()
    except Exception as exc:
        print(f"[FOUT] _tick() gooide: {exc!r}")
    print("--- _tick() klaar ---\n")

    after = dict(ant._last_emitted)
    new_candidates = {k: v for k, v in after.items() if k not in before}

    print(f"Symbolen geanalyseerd : {mission.market_scope.symbols}")
    print(f"Geëmitteerde kandidaten: {len(new_candidates)}")

    if new_candidates:
        print("\nKandidaten:")
        for (symbol, signal_type), cid in new_candidates.items():
            print(f"  ✓  {symbol:10s}  {signal_type:25s}  id={cid}")
    else:
        print(
            "(geen kandidaten boven drempel — "
            "synthetische data heeft mogelijk te lage sharpe of win_rate)"
        )

    print(f"\nLast action: {ant._last_action}")

    print("\n" + "=" * 60)
    print("ResearchAnt harness klaar — geen orders, geen kapitaal")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
