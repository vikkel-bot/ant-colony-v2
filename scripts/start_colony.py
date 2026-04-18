"""
scripts/start_colony.py

Bootstrap script voor ANT COLONY v2 op PC2.

Startkapitaal:
  Standaard haalt het script de echte AccountState op via alle geregistreerde
  BiomeAdapters en gebruikt de som van hun equity als colony-kapitaal.
  Dit betekent dat Queen altijd met het werkelijke saldo begint.

  Gebruik --capital om handmatig te overschrijven (bijv. voor tests of als
  de API tijdelijk niet bereikbaar is).

Gebruik:
    python scripts/start_colony.py                        # echte saldi
    python scripts/start_colony.py --capital 50000        # handmatige override
    python scripts/start_colony.py --capital 75000 --port 8001 --tick-interval 10
    python scripts/start_colony.py --help
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root op sys.path zetten zodat ant_colony importeerbaar is
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Start ANT COLONY v2 — Queen + Scheduler + Dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--capital",
        type=float,
        default=None,
        metavar="EUR",
        help=(
            "Startkapitaal in EUR. Standaard: som van echte adapter-saldi. "
            "Gebruik deze vlag om handmatig te overschrijven."
        ),
    )
    p.add_argument(
        "--host",
        default="0.0.0.0",
        help="Dashboard bind-adres",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Dashboard poort",
    )
    p.add_argument(
        "--tick-interval",
        type=int,
        default=5,
        metavar="SEC",
        help="Scheduler tick-interval in seconden",
    )
    p.add_argument(
        "--node-id",
        default="pc2-main",
        help="Node-identifier voor PC2",
    )
    p.add_argument(
        "--node-hostname",
        default=None,
        help="Hostname van PC2 (standaard: socket.gethostname())",
    )
    p.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
        metavar="SEC",
        help="Verwacht heartbeat-interval van agents in seconden",
    )
    p.add_argument(
        "--logs-root",
        type=Path,
        default=Path(r"C:\Trading\ANT_LOGS"),
        metavar="PATH",
        help="Root-map voor append-only colony logs",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=Path(r"C:\Trading\ANT_OUT"),
        metavar="PATH",
        help="Root-map voor algemene outputs",
    )
    p.add_argument(
        "--live-root",
        type=Path,
        default=Path(r"C:\Trading\ANT_LIVE"),
        metavar="PATH",
        help="Root-map voor live execution artifacts",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log-niveau voor de bootstrap logger",
    )
    return p


# ---------------------------------------------------------------------------
# Hulpfunctie: stop proces op een poort (Windows)
# ---------------------------------------------------------------------------

def _kill_port(port: int, log: logging.Logger) -> None:
    """Zoek en stop het proces dat luistert op `port` (Windows netstat)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        log.warning("netstat niet beschikbaar — poort %d niet vrijgemaakt: %s", port, exc)
        return

    pids: set[int] = set()
    for line in result.stdout.splitlines():
        # Zoek regels met :8000 in LISTENING of ESTABLISHED staat
        if f":{port}" in line and ("LISTENING" in line or "ESTABLISHED" in line):
            parts = line.split()
            if parts:
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    pass

    if not pids:
        log.info("Geen actief proces gevonden op poort %d.", port)
        return

    for pid in pids:
        log.info("Stoppen proces PID %d op poort %d …", pid, port)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log.warning("taskkill mislukt voor PID %d: %s", pid, exc)

    # Korte pauze zodat de OS-poort vrijkomt
    time.sleep(1)


# ---------------------------------------------------------------------------
# Scheduler thread
# ---------------------------------------------------------------------------

def _run_scheduler(scheduler, log: logging.Logger) -> None:
    log.info("Scheduler thread gestart (tick_interval=%ds).", scheduler._tick_interval)
    try:
        scheduler.start()
    except Exception:
        log.exception("Scheduler thread afgebroken met onverwachte fout.")


# ---------------------------------------------------------------------------
# Kapitaalberekening via live adapter-saldi
# ---------------------------------------------------------------------------

def _resolve_capital(
    registry,
    manual_override: float | None,
    log: logging.Logger,
    broker_names: dict[str, str] | None = None,
) -> float:
    """
    Bepaal het colony-startkapitaal.

    Als manual_override opgegeven is: gebruik dat bedrag direct.
    Anders: bereken reële equity per adapter:
      equity = EUR beschikbaar + marktwaarde van alle crypto holdings

    Stopt met sys.exit(1) als:
      - Geen adapter bereikbaar is EN er geen override opgegeven is.
      - Berekende equity nul of negatief is EN er geen override is.
    """
    if manual_override is not None:
        log.info("Startkapitaal: handmatige override — €%.2f", manual_override)
        return manual_override

    log.info("Reeel startkapitaal ophalen via live adapter-saldi …")

    _broker_names = broker_names or {}
    total_equity = 0.0
    any_connected = False
    per_broker: list[str] = []

    for biome_id in registry.list_biomes():
        adapter = registry.get(biome_id)
        if adapter is None:
            continue

        if not adapter.is_available():
            log.warning("  [%s] adapter niet bereikbaar — overgeslagen", biome_id)
            continue

        any_connected = True
        state = adapter.get_account_state()
        if state is None:
            log.warning("  [%s] get_account_state() → None (auth-fout?) — overgeslagen", biome_id)
            continue

        # Marktwaarde van alle crypto holdings optellen bij EUR saldo
        holdings_value = 0.0
        positions = adapter.get_positions()
        if positions:
            holdings_value = sum(p.market_value for p in positions)
            for p in positions:
                log.debug(
                    "    %s: %.6f @ €%.2f = €%.2f",
                    p.symbol, p.quantity, p.current_price, p.market_value,
                )

        adapter_equity = state.balance + holdings_value
        broker = _broker_names.get(biome_id, biome_id)
        log.info(
            "  [%s] EUR €%.2f  +  holdings €%.2f  =  €%.2f",
            broker, state.balance, holdings_value, adapter_equity,
        )
        per_broker.append(f"{broker}: €{adapter_equity:.2f}")
        total_equity += adapter_equity

    if not any_connected:
        log.error(
            "Geen adapter bereikbaar. "
            "Gebruik --capital om handmatig een startkapitaal op te geven."
        )
        sys.exit(1)

    if total_equity <= 0:
        log.error(
            "Berekende equity is €%.2f — kan colony niet starten met nul kapitaal. "
            "Controleer API keys of gebruik --capital.",
            total_equity,
        )
        sys.exit(1)

    contrib = "  ".join(per_broker)
    log.info("Reeel kapitaal berekend: €%.2f  (%s)", total_equity, contrib)
    return total_equity


# ---------------------------------------------------------------------------
# Main bootstrap
# ---------------------------------------------------------------------------

def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("start_colony")

    # --- Stap 1: stop bestaand dashboard-proces op de doelpoort ---
    log.info("Controleren of poort %d vrij is …", args.port)
    _kill_port(args.port, log)

    # --- Stap 2: importeer colony modules (na sys.path setup) ---
    from ant_colony.biome.adapters.bitvavo_adapter import BitvavoAdapter
    from ant_colony.biome.biome_registry import BiomeRegistry
    from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
    from ant_colony.dashboard.api import ColonyContext
    from ant_colony.dashboard.server import run as dashboard_run
    from ant_colony.queen.queen import Queen
    from ant_colony.schemas.ant import AntType
    from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths

    logs_root: Path = args.logs_root
    logs_root.mkdir(parents=True, exist_ok=True)
    log.info("Logs root: %s", logs_root)

    # --- Stap 3: initialiseer Scheduler ---
    scheduler = ColonyScheduler(
        logs_root=logs_root,
        tick_interval=args.tick_interval,
    )
    log.info("ColonyScheduler aangemaakt (tick_interval=%ds).", args.tick_interval)

    # --- Stap 4: maak BitvavaAdapter aan en registreer in BiomeRegistry ---
    api_key = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")
    if not api_key or not api_secret:
        log.warning(
            "BITVAVO_API_KEY of BITVAVO_API_SECRET niet gezet — "
            "balans en posities zijn niet beschikbaar in het dashboard"
        )

    # BITVAVO_PAPER_MODE wordt gelezen door BitvavoAdapter zelf; standaard True.
    bitvavo_adapter = BitvavoAdapter()
    biome_registry = BiomeRegistry()
    biome_registry.register(bitvavo_adapter)
    log.info(
        "BitvavoAdapter geregistreerd — paper_only=%s",
        bitvavo_adapter._paper_only,
    )

    # --- Stap 5: bereken startkapitaal (echte saldi of handmatige override) ---
    _broker_names = {"crypto": "Bitvavo"}
    capital = _resolve_capital(biome_registry, args.capital, log, broker_names=_broker_names)

    # --- Stap 6: initialiseer Queen met startkapitaal ---
    queen = Queen(
        capital_total=capital,
        scheduler=scheduler,
        logs_root=logs_root,
    )
    log.info("Queen aangemaakt — kapitaal: €%.2f", capital)

    # --- Stap 7: registreer PC2 node ---
    all_ant_types = [t.value for t in AntType]
    hostname = args.node_hostname or socket.gethostname()

    node = Node(
        node_id=args.node_id,
        hostname=hostname,
        allowed_biomes=["crypto"],
        allowed_ant_types=all_ant_types,
        heartbeat_interval=args.heartbeat_interval,
        status=NodeStatus.ACTIVE,
        runtime_paths=RuntimePaths(
            output=str(args.output_root),
            live=str(args.live_root),
            logs=str(logs_root),
        ),
    )
    queen.register_node(node)
    log.info(
        "Node geregistreerd — id: %s  hostname: %s  biomes: %s  ant_types: %s",
        args.node_id,
        hostname,
        ["crypto"],
        all_ant_types,
    )

    # --- Stap 7b: stel biome-kapitaal in op berekende equity ---
    queen.set_biome_capital("crypto", capital)
    log.info("Biome-kapitaal ingesteld — crypto: €%.2f", capital)

    # --- Stap 8: start scheduler in daemon thread ---
    scheduler_thread = threading.Thread(
        target=_run_scheduler,
        args=(scheduler, log),
        name="colony-scheduler",
        daemon=True,
    )
    scheduler_thread.start()

    # --- Stap 8b: bootstrap missions (scout + research + audit) --- #
    # Geeft alle drie scouting-missions uit via de Queen en start een ScoutAnt thread.
    # Research en audit missions worden uitgegeven (zichtbaar op dashboard) maar
    # krijgen geen thread — ResearchAnt en AuditAnt klassen bestaan nog niet.
    try:
        from datetime import timezone as _tz

        from ant_colony.ants.scout_ant import ScoutAnt
        from ant_colony.schemas.mission import (
            AbortConditions,
            MarketScope,
            Mission,
            RiskLimits,
            SuccessConditions,
        )

        _ts = datetime.now(tz=_tz.utc).strftime("%Y%m%d-%H%M%S")

        _obs_risk = RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        )
        _crypto_scope = MarketScope(
            biome="crypto",
            symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR"],
            timeframes=["1h", "4h", "1d"],
        )

        _bootstrap_missions = [
            Mission(
                mission_id=f"scout-crypto-{_ts}",
                ant_type="scout_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "detect_opportunity"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
                ttl=3600,
                heartbeat_interval=60,
                success_conditions=SuccessConditions(
                    description="Detecteer en rapporteer minstens één kansrijke marktstructuur "
                                "op BTC-EUR, ETH-EUR of SOL-EUR binnen de TTL.",
                    criteria={"min_opportunities_detected": 1},
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                    stale_market_data=True,
                ),
            ),
            Mission(
                mission_id=f"research-crypto-{_ts}",
                ant_type="research_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "backtest", "propose_candidate"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
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
                    stale_market_data=False,
                ),
            ),
            Mission(
                mission_id=f"audit-crypto-{_ts}",
                ant_type="audit_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "validate", "report"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
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

        scout_mission = None
        for mission in _bootstrap_missions:
            result = queen.issue_mission(mission)
            if result.accepted:
                log.info("Missie geaccepteerd — %s (%s)", mission.mission_id, mission.ant_type)
            else:
                log.warning(
                    "Missie geweigerd: %s — %s / %s",
                    mission.mission_id,
                    result.rejection_reason,
                    result.rejection_detail,
                )
            if mission.ant_type == "scout_ant" and result.accepted:
                scout_mission = mission

        if scout_mission is not None:
            scout_ant_id = f"scout-{uuid.uuid4().hex[:12]}"
            scout = ScoutAnt(
                ant_id=scout_ant_id,
                mission=scout_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            scout_thread = threading.Thread(
                target=scout.run,
                name=f"scout-{scout_ant_id[:16]}",
                daemon=True,
            )
            scout_thread.start()
            log.info(
                "ScoutAnt gestart | ant_id=%s  ttl=%ds  symbols=%s",
                scout_ant_id,
                scout_mission.ttl,
                scout_mission.market_scope.symbols,
            )
        else:
            log.warning("Scout-missie niet geaccepteerd — geen ScoutAnt thread gestart.")

        log.info(
            "Research en audit missions uitgegeven — geen threads (klassen nog niet geïmplementeerd)."
        )

    except Exception:
        log.exception("Mission bootstrap mislukt — colony start toch door.")

    # --- Stap 9: bouw ColonyContext en start dashboard (blocking) ---
    context = ColonyContext(
        queen=queen,
        scheduler=scheduler,
        logs_root=logs_root,
        broker_names={"crypto": "Bitvavo"},
        biome_registry=biome_registry,
    )

    log.info(
        "Dashboard starten op http://%s:%d — bereikbaar vanuit het netwerk.",
        args.host,
        args.port,
    )
    dashboard_run(context, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
