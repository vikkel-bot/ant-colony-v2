"""
scripts/harness/harness_paper_ant.py

Standalone test-harness voor PaperAnt.

Wat het doet:
  1. Bouwt een minimale Mission (crypto, BTC-EUR, €500 kapitaal)
  2. Registreert een stub BiomeAdapter die BTC prijs € 30.000 retourneert
  3. Schrijft één fake scout-signaal naar een tijdelijke logs-dir
  4. Draait één _tick() — exit-first, dan signaalverwerking
  5. Print open posities, kapitaalstatus en last_action

Veiligheid:
  - Geen live broker, geen echte orders, geen echt kapitaal
  - Fail-closed: crashes in _tick() worden gecatcht en geprint
  - Tijdelijke map wordt opgeruimd na afloop

Gebruik:
  python scripts/harness/harness_paper_ant.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.ants.paper_ant import PaperAnt
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
# Stub adapter — geen netwerk, vaste prijs
# ---------------------------------------------------------------------------

class _StubCryptoAdapter:
    biome_id = "crypto"
    _PRICES = {"BTC-EUR": 30_000.0, "ETH-EUR": 2_000.0}

    def is_available(self) -> bool:
        return True

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData:
        price = self._PRICES.get(symbol, 1_000.0)
        return MarketData(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=datetime.now(tz=timezone.utc),
            open=price,
            high=price * 1.005,
            low=price * 0.995,
            close=price,
            volume=50.0,
            biome_id="crypto",
        )

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
        mission_id="harness-paper-001",
        ant_type="paper_ant",
        allowed_node="harness-node",
        allowed_actions=["open_position", "close_position", "report"],
        market_scope=MarketScope(
            biome="crypto",
            symbols=["BTC-EUR"],
            timeframes=["1m"],
        ),
        capital_limit=500.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.20,
            max_position_size=250.0,
            daily_loss_limit=50.0,
        ),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="Harness smoke-test"),
    )


def _write_scout_signal(scouts_dir: Path, symbol: str) -> str:
    sig_id = f"sig-{uuid.uuid4().hex[:8]}"
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "payload": {
            "action": "opportunity_detected",
            "signal_id": sig_id,
            "symbol": symbol,
            "biome": "crypto",
            "signal_type": "price_move",
            "confidence": 0.85,
            "change_pct": 2.5,
            "detected_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    }
    scouts_dir.mkdir(parents=True, exist_ok=True)
    (scouts_dir / "stub.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    return sig_id


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("PaperAnt harness — één tick")
    print("=" * 60)

    mission = _build_mission()

    registry = BiomeRegistry()
    registry.register(_StubCryptoAdapter())

    scheduler = MagicMock(spec=ColonyScheduler)

    with tempfile.TemporaryDirectory(prefix="harness_paper_") as tmpdir:
        logs_root = Path(tmpdir)
        sig_id = _write_scout_signal(logs_root / "scouts", "BTC-EUR")
        print(f"Scout-signaal geschreven: {sig_id}")

        ant = PaperAnt(
            ant_id=f"harness-{uuid.uuid4().hex[:8]}",
            mission=mission,
            scheduler=scheduler,
            biome_registry=registry,
            logs_root=logs_root,
        )
        ant._status = AntStatus.RUNNING

        print("\n--- _tick() start ---")
        try:
            ant._tick()
        except Exception as exc:
            print(f"[FOUT] _tick() gooide: {exc!r}")
        print("--- _tick() klaar ---\n")

        open_pos = ant._ledger.open_positions
        print(f"Open posities  : {len(open_pos)}")
        print(f"Kapitaal totaal: € {mission.capital_limit:.2f}")
        print(f"Kapitaal vrij  : € {ant._ledger.capital_available:.2f}")
        print(f"Last action    : {ant._last_action}")

        if open_pos:
            print("\nPosities:")
            for pos in open_pos:
                print(
                    f"  {pos.symbol:10s} qty={pos.quantity:.8f}"
                    f"  entry=€{pos.entry_price:.2f}"
                    f"  SL=€{pos.stop_loss_price:.2f}"
                    f"  TP=€{pos.take_profit_price:.2f}"
                )
        else:
            print("(geen open posities — signaal mogelijk onder drempel of al in ledger)")

    print("\n" + "=" * 60)
    print("PaperAnt harness klaar — geen live orders geplaatst")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
