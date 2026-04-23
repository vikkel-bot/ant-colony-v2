"""
scripts/launch_ants.py

Geeft de eerste drie scouting-missies uit via POST /api/missions naar de
draaiende colony. De missions verschijnen direct op het dashboard.

Missies (capital_limit=0 — geen kapitaal ingezet):
  scout-crypto-001    scout_ant     TTL  1u   BTC/ETH/SOL observeren
  research-crypto-001 research_ant  TTL  2u   backtesten en candidates voorstellen
  audit-crypto-001    audit_ant     TTL 24u   valideren en rapporteren

Gebruik:
    python scripts/launch_ants.py
    python scripts/launch_ants.py --colony-url http://192.168.1.10:8000
    python scripts/launch_ants.py --node-id pc2-main --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Geef scouting-missies uit via de draaiende colony API",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--colony-url",
        default="http://localhost:8000",
        metavar="URL",
        help="Base URL van de draaiende colony (start_colony.py)",
    )
    p.add_argument(
        "--node-id",
        default="pc2-main",
        help="Node waarop de missions mogen draaien",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Valideer missions lokaal maar stuur niets naar de colony",
    )
    p.add_argument(
        "--logs-root",
        type=Path,
        default=Path(r"C:\Trading\ANT_LOGS"),
        metavar="PATH",
        help="Root-map voor ANT_LOGS (scout-signalen worden hier geschreven)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


# ---------------------------------------------------------------------------
# Mission definities
# ---------------------------------------------------------------------------

def _ts() -> str:
    """Timestamp suffix voor mission_ids — bijv. 20260418-092130."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _build_missions(node_id: str):
    """Construeer de drie scouting-missions. Importeert schemas lazy."""
    from ant_colony.schemas.mission import (
        AbortConditions,
        MarketScope,
        Mission,
        RiskLimits,
        SuccessConditions,
    )

    # Nominale RiskLimits voor zero-capital observatie-ants.
    # Velden zijn gt=0 verplicht; deze waarden zijn te klein om ooit te
    # triggeren bij capital_limit=0.
    _OBS_RISK = RiskLimits(
        max_drawdown_pct=0.01,      # 1 % — symbolisch
        max_position_size=1.0,      # €1 — symbolisch
        daily_loss_limit=1.0,       # €1 — symbolisch
        stop_loss_required=False,   # geen trading → geen stop-loss nodig
    )

    _CRYPTO_SCOPE = MarketScope(
        biome="crypto",
        symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
        timeframes=["1h", "4h", "1d"],
    )

    ts = _ts()

    return [
        # ── Mier 1: scout ─────────────────────────────────────────────
        Mission(
            mission_id=f"scout-crypto-{ts}",
            ant_type="scout_ant",
            allowed_node=node_id,
            allowed_actions=["read_data", "detect_opportunity"],
            market_scope=_CRYPTO_SCOPE,
            capital_limit=0.0,
            risk_limits=_OBS_RISK,
            ttl=3600,
            heartbeat_interval=60,
            success_conditions=SuccessConditions(
                description="Detecteer en rapporteer minstens één kansrijke marktstructuur "
                            "op BTC-EUR, ETH-EUR of SOL-EUR binnen de TTL.",
                criteria={"min_opportunities_detected": 1},
            ),
            abort_conditions=AbortConditions(
                stale_heartbeat=True,
                capital_limit_breach=False,   # geen kapitaal → niet relevant
                risk_limit_breach=False,
                ttl_expired=True,
                stale_market_data=True,
            ),
        ),

        # ── Mier 2: research ──────────────────────────────────────────
        Mission(
            mission_id=f"research-crypto-{ts}",
            ant_type="research_ant",
            allowed_node=node_id,
            allowed_actions=["read_data", "backtest", "propose_candidate"],
            market_scope=_CRYPTO_SCOPE,
            capital_limit=0.0,
            risk_limits=_OBS_RISK,
            ttl=7200,
            heartbeat_interval=120,
            success_conditions=SuccessConditions(
                description="Voer minimaal één backtest uit en dien een StrategyCandidate "
                            "in met status RESEARCH binnen de TTL.",
                criteria={"min_backtests": 1, "candidate_status": "research"},
            ),
            abort_conditions=AbortConditions(
                stale_heartbeat=True,
                capital_limit_breach=False,
                risk_limit_breach=False,
                ttl_expired=True,
                stale_market_data=False,   # research mag werken op gecachte data
            ),
        ),

        # ── Mier 3: audit ─────────────────────────────────────────────
        Mission(
            mission_id=f"audit-crypto-{ts}",
            ant_type="audit_ant",
            allowed_node=node_id,
            allowed_actions=["read_data", "validate", "report"],
            market_scope=_CRYPTO_SCOPE,
            capital_limit=0.0,
            risk_limits=_OBS_RISK,
            ttl=86400,
            heartbeat_interval=300,
            success_conditions=SuccessConditions(
                description="Valideer alle colony audit logs en schrijf een dagrapport "
                            "binnen de TTL.",
                criteria={"report_written": True},
            ),
            abort_conditions=AbortConditions(
                stale_heartbeat=True,
                capital_limit_breach=False,
                risk_limit_breach=False,
                ttl_expired=True,
                stale_market_data=False,
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# HTTP client — POST mission naar draaiende colony
# ---------------------------------------------------------------------------

def _post_mission(colony_url: str, mission, log: logging.Logger) -> tuple[bool, str, str]:
    """
    POST een mission als JSON naar POST /api/missions.

    Returns:
        (accepted, rejection_reason, rejection_detail)
        accepted=False + rejection_reason="CONNECTION_ERROR" bij verbindingsfout.
    """
    import httpx

    url = colony_url.rstrip("/") + "/api/missions"
    payload = mission.model_dump(mode="json")

    try:
        r = httpx.post(url, json=payload, timeout=10.0)
    except httpx.ConnectError:
        log.error("Verbinding geweigerd — draait start_colony.py op %s?", colony_url)
        return False, "CONNECTION_ERROR", f"Kan niet verbinden met {colony_url}"
    except httpx.TimeoutException:
        log.error("Timeout bij verbinden met %s", colony_url)
        return False, "TIMEOUT", f"Timeout na 10s op {colony_url}"
    except Exception as exc:
        log.exception("Onverwachte HTTP-fout voor %s", mission.mission_id)
        return False, "HTTP_ERROR", str(exc)

    if r.status_code == 503:
        return False, "COLONY_UNAVAILABLE", "Colony niet geinitialiseerd (queen=None)"
    if r.status_code == 422:
        detail = r.json().get("detail", str(r.text))
        return False, "VALIDATION_ERROR", str(detail)
    if not r.is_success:
        return False, "HTTP_ERROR", f"HTTP {r.status_code}: {r.text[:120]}"

    data = r.json()
    return (
        data.get("accepted", False),
        data.get("rejection_reason") or "",
        data.get("rejection_detail") or "",
    )


# ---------------------------------------------------------------------------
# ScoutAnt thread
# ---------------------------------------------------------------------------

def _build_local_registry(log: logging.Logger):
    """Bouw een BiomeRegistry met een BitvavoAdapter voor standalone gebruik."""
    import os
    from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter
    from ant_colony.biome.biome_registry import BiomeRegistry

    api_key    = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")
    if not api_key or not api_secret:
        log.warning(
            "BITVAVO_API_KEY / BITVAVO_API_SECRET niet gezet — "
            "adapter draait in paper-only modus"
        )

    adapter  = BitvavoAdapter()
    registry = BiomeRegistry()
    registry.register(adapter)
    return registry


def _start_scout_thread(
    mission,
    scheduler,
    biome_registry,
    logs_root: Path,
    log: logging.Logger,
) -> threading.Thread:
    """
    Maak een ScoutAnt aan en start hem in een daemon thread.

    De thread logt OpportunitySignals naar logs_root/scouts/{ant_id}.jsonl.
    De dashboard-tikker pikt deze JSONL-bestanden automatisch op via rglob.

    Returns:
        De gestarte Thread (daemon=True).
    """
    from ant_colony.ants.scout_ant import ScoutAnt

    ant_id = f"scout-{uuid.uuid4().hex[:12]}"
    scout  = ScoutAnt(
        ant_id=ant_id,
        mission=mission,
        scheduler=scheduler,
        biome_registry=biome_registry,
        logs_root=logs_root,
    )

    t = threading.Thread(
        target=scout.run,
        name=f"scout-{ant_id[:16]}",
        daemon=True,
    )
    t.start()
    log.info(
        "ScoutAnt gestart | ant_id=%s  symbols=%s  ttl=%ds  logs=%s/scouts/",
        ant_id,
        mission.market_scope.symbols,
        mission.ttl,
        logs_root,
    )
    return t


# ---------------------------------------------------------------------------
# Uitgifte + rapportage
# ---------------------------------------------------------------------------

_SEPARATOR = "-" * 52


def _print_accepted(mission, prefix: str = "") -> None:
    print(f"  {prefix}[OK]  GEACCEPTEERD")
    print(f"     mission_id : {mission.mission_id}")
    print(f"     ant_type   : {mission.ant_type}")
    print(f"     node       : {mission.allowed_node}")
    print(f"     ttl        : {mission.ttl}s  ({mission.ttl // 3600}u{(mission.ttl % 3600) // 60}m)")
    print(f"     capital    : EUR {mission.capital_limit:.2f}")


def _print_rejected(mission, reason: str, detail: str, prefix: str = "") -> None:
    print(f"  {prefix}[!!] GEWEIGERD")
    print(f"     mission_id : {mission.mission_id}")
    print(f"     reden      : {reason}")
    if detail:
        print(f"     detail     : {detail}")


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("launch_ants")

    print()
    print("=" * 52)
    print("  ANT COLONY v2 — Mission Launch")
    if args.dry_run:
        print("  MODE: DRY-RUN (alleen lokale validatie)")
    else:
        print(f"  colony: {args.colony_url}")
    print("=" * 52)

    # --- Missions bouwen (Pydantic valideert bij constructie) ---
    try:
        missions = _build_missions(args.node_id)
    except Exception:
        log.exception("Fout bij aanmaken mission-objecten")
        sys.exit(1)

    log.info("%d missions aangemaakt — uitgifte starten …", len(missions))

    # --- Uitgifte ---
    accepted   = 0
    rejected   = 0
    scout_thread: threading.Thread | None = None

    for mission in missions:
        print()
        print(_SEPARATOR)
        print(f"  {mission.ant_type}  /  {mission.mission_id}")
        print(_SEPARATOR)

        if args.dry_run:
            _print_accepted(mission, prefix="[DRY-RUN] ")
            accepted += 1
            continue

        ok, reason, detail = _post_mission(args.colony_url, mission, log)

        if ok:
            _print_accepted(mission)
            accepted += 1

            # Na succesvolle uitgifte van de scout-missie: start lokale ScoutAnt thread.
            # De thread logt signals naar ANT_LOGS/scouts/ — de dashboard-tikker
            # pikt ze automatisch op via rglob("*.jsonl").
            if mission.ant_type == "scout_ant" and scout_thread is None:
                from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
                local_scheduler = ColonyScheduler(
                    logs_root=args.logs_root,
                    tick_interval=5,
                )
                local_registry = _build_local_registry(log)
                scout_thread = _start_scout_thread(
                    mission=mission,
                    scheduler=local_scheduler,
                    biome_registry=local_registry,
                    logs_root=args.logs_root,
                    log=log,
                )
        else:
            _print_rejected(mission, reason, detail)
            rejected += 1

            if reason in ("CONNECTION_ERROR", "TIMEOUT", "COLONY_UNAVAILABLE"):
                print()
                print(f"  Uitgifte gestaakt — colony niet bereikbaar op {args.colony_url}")
                print(f"  Controleer of start_colony.py draait.")
                break

    # --- Samenvatting ---
    print()
    print("=" * 52)
    print(f"  Resultaat: {accepted} geaccepteerd  /  {rejected} geweigerd")
    if not args.dry_run and accepted > 0:
        print(f"  Dashboard: {args.colony_url}")
    print("=" * 52)
    print()

    if rejected > 0:
        sys.exit(1)

    # Als een ScoutAnt thread gestart is, blijf actief zodat de daemon thread
    # blijft draaien. Ctrl+C stopt het proces netjes.
    if scout_thread is not None:
        log.info("ScoutAnt draait — Ctrl+C om te stoppen.")
        try:
            scout_thread.join()
        except KeyboardInterrupt:
            log.info("launch_ants gestopt door operator.")


if __name__ == "__main__":
    main()
