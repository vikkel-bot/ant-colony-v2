"""
scripts/harness/harness_watchtower_ant.py

Standalone test-harness voor WatchtowerAnt.

Wat het doet:
  1. Bouwt een minimale Mission (observatie-only, capital_limit=0)
  2. Injecteert een stub WatchtowerClient met twee synthetische signalen:
       - AAPL  (equities, score=0.75, conf=0.70) → moet doorkomen
       - BTC   (crypto, score=0.80, conf=0.75)   → biome_mismatch → gefilterd
  3. Draait één _tick() — offline-check, filtering, routing
  4. Print snapshot, gefilterde signalen en doorgestuurde candidates

Veiligheid:
  - Geen live broker, geen echte API-call, geen kapitaal
  - WatchtowerClient volledig gemocked — geen netwerk
  - Fail-closed: crashes worden gecatcht en geprint
  - Tijdelijke map voor logs wordt opgeruimd na afloop

Gebruik:
  python scripts/harness/harness_watchtower_ant.py
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

from ant_colony.ants.watchtower_ant import WatchtowerAnt
from ant_colony.clients.watchtower_client import WatchtowerClient
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Synthetische signalen
# ---------------------------------------------------------------------------

def _make_signals() -> list[dict]:
    now = datetime.now(tz=timezone.utc)
    return [
        {
            "signal_id": f"wt-{uuid.uuid4().hex[:8]}",
            "asset": "AAPL",
            "biome": "equities",
            "direction": "long",
            "entry_score": 0.75,
            "confidence": 0.70,
            "risk_flags": [],
            "created_at": now.isoformat(),
            "reason": "Harness test equities signaal",
        },
        {
            "signal_id": f"wt-{uuid.uuid4().hex[:8]}",
            "asset": "BTC",
            "biome": "crypto",
            "direction": "long",
            "entry_score": 0.80,
            "confidence": 0.75,
            "risk_flags": [],
            "created_at": now.isoformat(),
            "reason": "Harness test crypto signaal — wordt gefilterd",
        },
        {
            "signal_id": f"wt-{uuid.uuid4().hex[:8]}",
            "asset": "TSLA",
            "biome": "equities",
            "direction": "long",
            "entry_score": 0.35,
            "confidence": 0.30,
            "risk_flags": ["HIGH_RISK"],
            "created_at": now.isoformat(),
            "reason": "Harness test HIGH_RISK signaal — wordt gefilterd",
        },
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_mission() -> Mission:
    return Mission(
        mission_id="harness-watchtower-001",
        ant_type="watchtower_ant",
        allowed_node="harness-node",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["GLOBAL"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=1.0,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=86400,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="Harness smoke-test"),
    )


def _build_stub_client(signals: list[dict]) -> WatchtowerClient:
    client = MagicMock(spec=WatchtowerClient)
    client.base_url = "http://127.0.0.1:8011"
    client.last_get_succeeded = True
    client.get_signals.return_value = signals
    return client


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n" + "=" * 60)
    print("WatchtowerAnt harness — één tick")
    print("=" * 60)

    signals = _make_signals()
    print(f"Synthetische signalen: {len(signals)}")
    for s in signals:
        print(
            f"  {s['asset']:6s}  biome={s['biome']:8s}"
            f"  score={s['entry_score']:.2f}  conf={s['confidence']:.2f}"
            f"  risk={s['risk_flags']}"
        )

    mission = _build_mission()
    client = _build_stub_client(signals)
    scheduler = MagicMock(spec=ColonyScheduler)

    with tempfile.TemporaryDirectory(prefix="harness_watchtower_") as tmpdir:
        logs_root = Path(tmpdir)

        ant = WatchtowerAnt(
            ant_id=f"harness-{uuid.uuid4().hex[:8]}",
            mission=mission,
            scheduler=scheduler,
            logs_root=logs_root,
            client=client,
        )

        print("\n--- _tick() start ---")
        try:
            ant._tick()
        except Exception as exc:
            print(f"[FOUT] _tick() gooide: {exc!r}")
        print("--- _tick() klaar ---\n")

        # Snapshot lezen
        signals_path = logs_root / "watchtower" / "signals.jsonl"
        candidates_path = logs_root / "watchtower" / "candidates.jsonl"
        filtered_path = logs_root / "watchtower" / "filtered.jsonl"

        if signals_path.exists():
            snap = json.loads(signals_path.read_text(encoding="utf-8").splitlines()[-1])
            print(f"Snapshot — ontvangen:   {snap.get('received', 0)}")
            print(f"Snapshot — door filter: {snap.get('passed_filter', 0)}")
            print(f"Snapshot — candidates:  {snap.get('candidates_accepted', 0)}")
        else:
            print("(geen signals.jsonl gevonden)")

        if candidates_path.exists():
            lines = [l for l in candidates_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            print(f"\nDoorgestuurde candidates ({len(lines)}):")
            for line in lines:
                rec = json.loads(line)
                pl = rec.get("payload", {})
                print(
                    f"  ✓  {pl.get('asset'):6s}  biome={pl.get('biome'):8s}"
                    f"  score={pl.get('entry_score', 0):.2f}"
                    f"  conf={pl.get('confidence', 0):.2f}"
                )
        else:
            print("\n(geen doorgestuurde candidates)")

        if filtered_path.exists():
            lines = [l for l in filtered_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            print(f"\nGefilterde signalen ({len(lines)}):")
            for line in lines:
                rec = json.loads(line)
                pl = rec.get("payload", {})
                print(
                    f"  ✗  {str(pl.get('asset') or pl.get('symbol', '?')):6s}"
                    f"  reden={pl.get('rejection_reason') or pl.get('detail_reason', '?')}"
                )

    print("\n" + "=" * 60)
    print("WatchtowerAnt harness klaar — geen live API-calls")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
