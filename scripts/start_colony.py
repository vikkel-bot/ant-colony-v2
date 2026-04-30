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

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

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
# Dashboard watchdog thread
# ---------------------------------------------------------------------------

_DASHBOARD_RESTART_DELAY_S = 5


def _run_dashboard_watchdog(run_fn, ctx, host: str, port: int, log: logging.Logger) -> None:
    """Daemon thread: herstart dashboard automatisch bij onverwachte exit of crash."""
    attempt = 0
    while True:
        attempt += 1
        if attempt > 1:
            log.warning(
                "Dashboard watchdog: herstart poging %d — wacht %ds ...",
                attempt, _DASHBOARD_RESTART_DELAY_S,
            )
            time.sleep(_DASHBOARD_RESTART_DELAY_S)
        try:
            run_fn(ctx, host=host, port=port)
            log.info("Dashboard clean exit — watchdog eindigt.")
            return
        except Exception:
            log.warning(
                "Dashboard gecrasht (poging %d) — watchdog herstart.",
                attempt,
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Advisor thread (elke 5 minuten = 60 scheduler-ticks van 5 seconden)
# ---------------------------------------------------------------------------

_ADVISOR_INTERVAL = 300   # seconden


def _run_advisor_loop(advisor, queen, log: logging.Logger) -> None:
    """Daemon thread: één advies-cyclus per 5 minuten."""
    log.info("QueenAdvisor thread gestart (interval=%ds).", _ADVISOR_INTERVAL)
    while True:
        try:
            import time as _time
            _time.sleep(_ADVISOR_INTERVAL)
            decision = advisor.advise()
            queen.apply_advisor_decision(decision)
        except Exception:
            log.exception("QueenAdvisor loop fout — volgende cyclus over %ds.", _ADVISOR_INTERVAL)


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

    _equities_enabled = os.getenv("EQUITIES_ENABLED", "false").lower() == "true"
    _allowed_biomes   = ["crypto", "equities"] if _equities_enabled else ["crypto"]

    node = Node(
        node_id=args.node_id,
        hostname=hostname,
        allowed_biomes=_allowed_biomes,
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
        _allowed_biomes,
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
        from ant_colony.ants.audit_ant import AuditAnt
        from ant_colony.ants.execution_ant import ExecutionAnt
        from ant_colony.ants.ingestion_ant import IngestionAnt
        from ant_colony.ants.paper_ant import PaperAnt
        from ant_colony.ants.research_ant import ResearchAnt
        from ant_colony.ants.scout_ant import ScoutAnt
        from ant_colony.ants.strategy_ant import StrategyAnt
        from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
        from ant_colony.schemas.mission import (
            AbortConditions,
            MarketScope,
            Mission,
            RiskLimits,
            SuccessConditions,
        )

        _ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")

        _obs_risk = RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        )
        _crypto_scope = MarketScope(
            biome="crypto",
            symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
            timeframes=["1h", "4h", "1d"],
        )

        _paper_mode_active = os.getenv("BITVAVO_PAPER_MODE", "true").lower() == "true"
        _INGESTION_ANT_ENABLED = False  # uitgeschakeld: hoog CPU-gebruik, weinig waarde

        _bootstrap_missions = [
            Mission(
                mission_id=f"scout-crypto-{_ts}",
                ant_type="scout_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "detect_opportunity"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
                ttl=86400,
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
                ttl=86400,
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
                mission_id=f"paper-crypto-{_ts}",
                ant_type="paper_ant",
                allowed_node=args.node_id,
                allowed_actions=["open_position", "close_position"],
                market_scope=MarketScope(
                    biome="crypto",
                    symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
                ),
                capital_limit=500.0,
                risk_limits=RiskLimits(
                    max_drawdown_pct=0.10,
                    max_position_size=500.0,
                    daily_loss_limit=50.0,
                    stop_loss_required=True,
                ),
                ttl=86400,
                heartbeat_interval=60,
                success_conditions=SuccessConditions(
                    description="Valideer paper trades op BTC-EUR, ETH-EUR en SOL-EUR "
                                "op basis van ScoutAnt-signalen.",
                    criteria={"min_trades": 1},
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=True,
                    risk_limit_breach=True,
                    ttl_expired=True,
                    stale_market_data=True,
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
            *([Mission(
                mission_id=f"ingestion-crypto-{_ts}",
                ant_type="ingestion_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "ingest_candidate"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
                ttl=86400,
                heartbeat_interval=120,
                success_conditions=SuccessConditions(
                    description="Zoek publieke strategiebronnen en normaliseer naar "
                                "StrategyCandidate met status INGESTED.",
                    criteria={"min_candidates_ingested": 1},
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                    stale_market_data=False,
                ),
            )] if _INGESTION_ANT_ENABLED else []),
            Mission(
                mission_id=f"strategy-crypto-{_ts}",
                ant_type="strategy_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "backtest", "propose_candidate"],
                market_scope=_crypto_scope,
                capital_limit=0.0,
                risk_limits=_obs_risk,
                ttl=86400,
                heartbeat_interval=120,
                success_conditions=SuccessConditions(
                    description="Genereer en valideer strategie-varianten via walk-forward "
                                "backtest en emit kandidaten met status RESEARCH.",
                    criteria={"min_variants_emitted": 1},
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                    stale_market_data=False,
                ),
            ),
        ] + (
            [
                Mission(
                    mission_id=f"execution-crypto-{_ts}",
                    ant_type="execution_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["live_execute", "read_data"],
                    market_scope=_crypto_scope,
                    capital_limit=500.0,
                    risk_limits=RiskLimits(
                        max_drawdown_pct=0.10,
                        max_position_size=500.0,
                        daily_loss_limit=50.0,
                        stop_loss_required=True,
                    ),
                    ttl=86400,
                    heartbeat_interval=60,
                    success_conditions=SuccessConditions(
                        description="Voer live trades uit op basis van gevalideerde "
                                    "PaperAnt-resultaten met Queen-goedkeuring.",
                        criteria={"min_trades": 1},
                    ),
                    abort_conditions=AbortConditions(
                        stale_heartbeat=True,
                        capital_limit_breach=True,
                        risk_limit_breach=True,
                        ttl_expired=True,
                        stale_market_data=True,
                    ),
                ),
            ]
            if _paper_mode_active
            else []
        )

        scout_mission     = None
        research_mission  = None
        paper_mission     = None
        audit_mission     = None
        ingestion_mission = None
        strategy_mission  = None
        execution_mission = None

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
            elif mission.ant_type == "research_ant" and result.accepted:
                research_mission = mission
            elif mission.ant_type == "paper_ant" and result.accepted:
                paper_mission = mission
            elif mission.ant_type == "audit_ant" and result.accepted:
                audit_mission = mission
            elif mission.ant_type == "ingestion_ant" and result.accepted:
                ingestion_mission = mission
            elif mission.ant_type == "strategy_ant" and result.accepted:
                strategy_mission = mission
            elif mission.ant_type == "execution_ant" and result.accepted:
                execution_mission = mission

        if scout_mission is not None:
            scout_ant_id = f"scout-{uuid.uuid4().hex[:12]}"
            scout = ScoutAnt(
                ant_id=scout_ant_id,
                mission=scout_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            threading.Thread(
                target=scout.run,
                name=f"scout-{scout_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=scout_ant_id,
                mission_id=scout_mission.mission_id,
                node_id=args.node_id,
                ant_type="scout_ant",
                ttl=scout_mission.ttl,
                heartbeat_interval=scout_mission.heartbeat_interval,
            ))
            log.info(
                "ScoutAnt gestart | ant_id=%s  ttl=%ds  symbols=%s",
                scout_ant_id,
                scout_mission.ttl,
                scout_mission.market_scope.symbols,
            )
        else:
            log.warning("Scout-missie niet geaccepteerd — geen ScoutAnt thread gestart.")

        if research_mission is not None:
            research_ant_id = f"research-{uuid.uuid4().hex[:12]}"
            research = ResearchAnt(
                ant_id=research_ant_id,
                mission=research_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            threading.Thread(
                target=research.run,
                name=f"research-{research_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=research_ant_id,
                mission_id=research_mission.mission_id,
                node_id=args.node_id,
                ant_type="research_ant",
                ttl=research_mission.ttl,
                heartbeat_interval=research_mission.heartbeat_interval,
            ))
            log.info(
                "ResearchAnt gestart | ant_id=%s  ttl=%ds  symbols=%s",
                research_ant_id,
                research_mission.ttl,
                research_mission.market_scope.symbols,
            )
        else:
            log.warning("Research-missie niet geaccepteerd — geen ResearchAnt thread gestart.")

        _paper_ledgers: list = []   # in-memory ledgers geïnjecteerd in ColonyContext

        if paper_mission is not None:
            paper_ant_id = f"paper-{uuid.uuid4().hex[:12]}"
            paper = PaperAnt(
                ant_id=paper_ant_id,
                mission=paper_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            threading.Thread(
                target=paper.run,
                name=f"paper-{paper_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=paper_ant_id,
                mission_id=paper_mission.mission_id,
                node_id=args.node_id,
                ant_type="paper_ant",
                ttl=paper_mission.ttl,
                heartbeat_interval=paper_mission.heartbeat_interval,
            ))
            _paper_ledgers.append(paper._ledger)
            log.info(
                "PaperAnt gestart | ant_id=%s  capital=€%.2f  ttl=%ds  symbols=%s",
                paper_ant_id,
                paper_mission.capital_limit,
                paper_mission.ttl,
                paper_mission.market_scope.symbols,
            )
        else:
            log.warning("Paper-missie niet geaccepteerd — geen PaperAnt thread gestart.")

        if audit_mission is not None:
            audit_ant_id = f"audit-{uuid.uuid4().hex[:12]}"
            audit = AuditAnt(
                ant_id=audit_ant_id,
                mission=audit_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
                scheduler_tick_interval=args.tick_interval,
            )
            threading.Thread(
                target=audit.run,
                name=f"audit-{audit_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=audit_ant_id,
                mission_id=audit_mission.mission_id,
                node_id=args.node_id,
                ant_type="audit_ant",
                ttl=audit_mission.ttl,
                heartbeat_interval=audit_mission.heartbeat_interval,
            ))
            log.info(
                "AuditAnt gestart | ant_id=%s  ttl=%ds",
                audit_ant_id,
                audit_mission.ttl,
            )
        else:
            log.warning("Audit-missie niet geaccepteerd — geen AuditAnt thread gestart.")

        if ingestion_mission is not None:
            ingestion_ant_id = f"ingestion-{uuid.uuid4().hex[:12]}"
            ingestion = IngestionAnt(
                ant_id=ingestion_ant_id,
                mission=ingestion_mission,
                scheduler=scheduler,
                logs_root=logs_root,
            )
            threading.Thread(
                target=ingestion.run,
                name=f"ingestion-{ingestion_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=ingestion_ant_id,
                mission_id=ingestion_mission.mission_id,
                node_id=args.node_id,
                ant_type="ingestion_ant",
                ttl=ingestion_mission.ttl,
                heartbeat_interval=ingestion_mission.heartbeat_interval,
            ))
            log.info(
                "IngestionAnt gestart | ant_id=%s  ttl=%ds",
                ingestion_ant_id,
                ingestion_mission.ttl,
            )
        else:
            log.info("IngestionAnt uitgeschakeld (_INGESTION_ANT_ENABLED=False).")

        if strategy_mission is None:
            log.warning("Strategy-missie niet geaccepteerd — geen StrategyAnt thread gestart.")

        if execution_mission is not None:
            from ant_colony.execution.live_gate import LiveExecutionGate
            execution_ant_id = f"execution-{uuid.uuid4().hex[:12]}"
            exec_gate = LiveExecutionGate(
                queen=queen,
                scheduler=scheduler,
                adapter=biome_registry.get("crypto"),
                logs_root=logs_root,
            )
            execution = ExecutionAnt(
                ant_id=execution_ant_id,
                mission=execution_mission,
                scheduler=scheduler,
                gate=exec_gate,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            threading.Thread(
                target=execution.run,
                name=f"execution-{execution_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=execution_ant_id,
                mission_id=execution_mission.mission_id,
                node_id=args.node_id,
                ant_type="execution_ant",
                ttl=execution_mission.ttl,
                heartbeat_interval=execution_mission.heartbeat_interval,
            ))
            log.info(
                "ExecutionAnt gestart | ant_id=%s  capital=€%.2f  ttl=%ds  paper_mode=%s",
                execution_ant_id,
                execution_mission.capital_limit,
                execution_mission.ttl,
                _paper_mode_active,
            )
        elif _paper_mode_active:
            log.warning("Execution-missie niet geaccepteerd — geen ExecutionAnt thread gestart.")
        else:
            log.info(
                "ExecutionAnt NIET gestart — BITVAVO_PAPER_MODE is niet 'true' "
                "(veiligheidscheck: execution vereist paper mode)."
            )

    except Exception:
        log.exception("Mission bootstrap mislukt — colony start toch door.")

    # --- Stap 8b-extra: StrategyAnt (eigen try-blok zodat bootstrap-fouten het niet blokkeren) ---
    try:
        # strategy_mission is set inside the main bootstrap block above.
        # Using locals() lookup so this block stays independent even if bootstrap threw.
        _strategy_mission = locals().get("strategy_mission")
        if _strategy_mission is not None:
            from ant_colony.ants.strategy_ant import StrategyAnt
            from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
            strategy_ant_id = f"strategy-{uuid.uuid4().hex[:12]}"
            strategy = StrategyAnt(
                ant_id=strategy_ant_id,
                mission=_strategy_mission,
                scheduler=scheduler,
                biome_registry=biome_registry,
                logs_root=logs_root,
            )
            threading.Thread(
                target=strategy.run,
                name=f"strategy-{strategy_ant_id[:16]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=strategy_ant_id,
                mission_id=_strategy_mission.mission_id,
                node_id=args.node_id,
                ant_type="strategy_ant",
                ttl=_strategy_mission.ttl,
                heartbeat_interval=_strategy_mission.heartbeat_interval,
            ))
            log.info(
                "StrategyAnt gestart | ant_id=%s  ttl=%ds  symbols=%s",
                strategy_ant_id,
                _strategy_mission.ttl,
                _strategy_mission.market_scope.symbols,
            )
        else:
            log.warning("StrategyAnt niet gestart — geen geaccepteerde strategy-missie.")
    except Exception:
        log.exception("StrategyAnt bootstrap mislukt — colony draait door zonder StrategyAnt.")

    # --- Stap 8b-extra2: KillZoneAnt (opt-in via KILLZONE_ANT_ENABLED=True) ---
    try:
        _KILLZONE_ANT_ENABLED = False  # uitgeschakeld: zet True om te activeren
        if _KILLZONE_ANT_ENABLED:
            from ant_colony.ants.killzone_ant import KillZoneAnt
            from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
            from ant_colony.schemas.mission import (
                AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
            )

            _ts_kz      = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _obs_risk_kz = RiskLimits(
                max_drawdown_pct=0.01, max_position_size=1.0,
                daily_loss_limit=1.0, stop_loss_required=False,
            )
            _crypto_scope_kz = MarketScope(
                biome="crypto",
                symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR",
                         "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
                timeframes=["1h"],
            )
            kz_ant_id  = f"killzone-{uuid.uuid4().hex[:12]}"
            kz_mission = Mission(
                mission_id=f"killzone-crypto-{_ts_kz}",
                ant_type="killzone_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "propose_candidate"],
                market_scope=_crypto_scope_kz,
                capital_limit=0.0,
                risk_limits=_obs_risk_kz,
                ttl=86400,
                heartbeat_interval=120,
                success_conditions=SuccessConditions(
                    description="Genereer hoge-confidence LONG kandidaten tijdens ICT kill zones "
                                "op basis van RSI, volume en prijs richting.",
                    criteria={"min_candidates_emitted": 1},
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                    stale_market_data=False,
                ),
            )
            kz_result = queen.issue_mission(kz_mission)
            if not kz_result.accepted:
                log.warning(
                    "KillZoneAnt missie geweigerd: %s — %s",
                    kz_result.rejection_reason, kz_result.rejection_detail,
                )
            else:
                kz_ant = KillZoneAnt(
                    ant_id=kz_ant_id,
                    mission=kz_mission,
                    scheduler=scheduler,
                    biome_registry=biome_registry,
                    logs_root=logs_root,
                )
                threading.Thread(
                    target=kz_ant.run,
                    name=f"killzone-{kz_ant_id[:16]}",
                    daemon=True,
                ).start()
                scheduler.register_agent(AgentRecord(
                    ant_id=kz_ant_id,
                    mission_id=kz_mission.mission_id,
                    node_id=args.node_id,
                    ant_type="killzone_ant",
                    ttl=kz_mission.ttl,
                    heartbeat_interval=kz_mission.heartbeat_interval,
                ))
                log.info(
                    "KillZoneAnt gestart | ant_id=%s  ttl=%ds  symbols=%s",
                    kz_ant_id,
                    kz_mission.ttl,
                    kz_mission.market_scope.symbols,
                )
        else:
            log.info("KillZoneAnt uitgeschakeld (_KILLZONE_ANT_ENABLED=False).")
    except Exception:
        log.exception("KillZoneAnt bootstrap mislukt — colony draait door zonder KillZoneAnt.")

    # --- Stap 8c: ClaudeAnt (opt-in, eigen try-blok zodat andere fouten het niet blokkeren) ---
    try:
        _CLAUDE_ANT_ENABLED = False  # uitgeschakeld: API-kosten, zet True + env-var om te activeren
        _claude_enabled = _CLAUDE_ANT_ENABLED and os.getenv("CLAUDE_ANT_ENABLED", "false").lower() == "true"
        if _claude_enabled:
            from ant_colony.ants.claude_ant import ClaudeAnt
            from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
            from ant_colony.schemas.mission import (
                AbortConditions,
                MarketScope,
                Mission,
                RiskLimits,
                SuccessConditions,
            )

            _ts_claude   = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _obs_risk_c  = RiskLimits(
                max_drawdown_pct=0.01, max_position_size=1.0,
                daily_loss_limit=1.0, stop_loss_required=False,
            )
            _crypto_scope_c = MarketScope(
                biome="crypto",
                symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
                timeframes=["1h", "4h", "1d"],
            )
            claude_ant_id  = f"ant-claude-{_ts_claude}"
            claude_mission = Mission(
                mission_id=f"claude-{_ts_claude}",
                ant_type="claude_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "report"],
                market_scope=_crypto_scope_c,
                capital_limit=0.0,
                risk_limits=_obs_risk_c,
                ttl=86400,
                heartbeat_interval=300,
                success_conditions=SuccessConditions(
                    description="Analyseer top-kandidaten via Anthropic API en genereer varianten.",
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                ),
            )
            result = queen.issue_mission(claude_mission)
            if not result.accepted:
                log.warning(
                    "ClaudeAnt missie geweigerd: %s — %s",
                    result.rejection_reason, result.rejection_detail,
                )
            else:
                claude_ant = ClaudeAnt(
                    ant_id=claude_ant_id,
                    mission=claude_mission,
                    scheduler=scheduler,
                    logs_root=logs_root,
                )
                threading.Thread(
                    target=claude_ant.run,
                    name=f"claude-{claude_ant_id[:16]}",
                    daemon=True,
                ).start()
                scheduler.register_agent(AgentRecord(
                    ant_id=claude_ant_id,
                    mission_id=claude_mission.mission_id,
                    node_id=args.node_id,
                    ant_type="claude_ant",
                    ttl=claude_mission.ttl,
                    heartbeat_interval=claude_mission.heartbeat_interval,
                ))
                log.info(
                    "ClaudeAnt gestart | ant_id=%s  budget=€%.2f  ttl=%ds",
                    claude_ant_id,
                    float(os.getenv("CLAUDE_ANT_MONTHLY_BUDGET_EUR", "10.0")),
                    claude_mission.ttl,
                )
        else:
            log.info("ClaudeAnt uitgeschakeld (_CLAUDE_ANT_ENABLED=False).")
    except Exception:
        log.exception("ClaudeAnt bootstrap mislukt — colony draait door zonder ClaudeAnt.")

    # --- Stap 8d: QueenAdvisor (eigen try-blok) ---
    try:
        from ant_colony.queen.queen_advisor import QueenAdvisor

        advisor = QueenAdvisor(queen=queen, logs_root=logs_root)
        threading.Thread(
            target=_run_advisor_loop,
            args=(advisor, queen, log),
            name="queen-advisor",
            daemon=True,
        ).start()
        log.info("QueenAdvisor gestart | interval=%ds", _ADVISOR_INTERVAL)
    except Exception:
        log.exception("QueenAdvisor bootstrap mislukt — colony draait door.")

    # --- Stap 8e: OperatorAnt (eigen try-blok) ---
    try:
        from ant_colony.ants.operator_ant import OperatorAnt
        from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
        from ant_colony.schemas.mission import (
            AbortConditions,
            MarketScope,
            Mission,
            RiskLimits,
            SuccessConditions,
        )

        _ts_op     = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        _obs_risk_op = RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        )
        _crypto_scope_op = MarketScope(
            biome="crypto",
            symbols=["BTC-EUR", "ETH-EUR", "SOL-EUR", "XRP-EUR", "ADA-EUR", "LINK-EUR", "DOT-EUR", "LTC-EUR"],
            timeframes=["1h"],
        )
        operator_ant_id  = f"ant-operator-{_ts_op}"
        operator_mission = Mission(
            mission_id=f"operator-{_ts_op}",
            ant_type="operator_ant",
            allowed_node=args.node_id,
            allowed_actions=["read_data", "report"],
            market_scope=_crypto_scope_op,
            capital_limit=0.0,
            risk_limits=_obs_risk_op,
            ttl=86400,
            heartbeat_interval=60,
            success_conditions=SuccessConditions(
                description="Verwerk operator input (URL/tekst/code) naar ingestion-kandidaten.",
            ),
            abort_conditions=AbortConditions(
                stale_heartbeat=True,
                capital_limit_breach=False,
                risk_limit_breach=False,
                ttl_expired=True,
            ),
        )
        op_result = queen.issue_mission(operator_mission)
        if not op_result.accepted:
            log.warning(
                "OperatorAnt missie geweigerd: %s — %s",
                op_result.rejection_reason, op_result.rejection_detail,
            )
        else:
            operator_ant = OperatorAnt(
                ant_id=operator_ant_id,
                mission=operator_mission,
                scheduler=scheduler,
                logs_root=logs_root,
                node_id=args.node_id,
            )
            threading.Thread(
                target=operator_ant.run,
                name=f"operator-{operator_ant_id[:20]}",
                daemon=True,
            ).start()
            scheduler.register_agent(AgentRecord(
                ant_id=operator_ant_id,
                mission_id=operator_mission.mission_id,
                node_id=args.node_id,
                ant_type="operator_ant",
                ttl=operator_mission.ttl,
                heartbeat_interval=operator_mission.heartbeat_interval,
            ))
            log.info("OperatorAnt gestart | ant_id=%s  ttl=%ds", operator_ant_id, operator_mission.ttl)
    except Exception:
        log.exception("OperatorAnt bootstrap mislukt — colony draait door zonder OperatorAnt.")

    # --- Stap 8e-extra: TimeFilterAnt (opt-in via TIME_FILTER_ENABLED=true) ---
    try:
        _time_filter_enabled = os.getenv("TIME_FILTER_ENABLED", "false").lower() == "true"
        if _time_filter_enabled:
            from ant_colony.ants.time_filter_ant import TimeFilterAnt
            from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
            from ant_colony.schemas.mission import (
                AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
            )

            _ts_tf   = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _obs_risk_tf = RiskLimits(
                max_drawdown_pct=0.01, max_position_size=1.0,
                daily_loss_limit=1.0, stop_loss_required=False,
            )
            tf_ant_id  = f"ant-time-filter-{_ts_tf}"
            tf_mission = Mission(
                mission_id=f"time-filter-{_ts_tf}",
                ant_type="time_filter_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "report"],
                market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
                capital_limit=0.0,
                risk_limits=_obs_risk_tf,
                ttl=86400,
                heartbeat_interval=60,
                success_conditions=SuccessConditions(
                    description="Schrijf ICT Kill Zone TimeSignals elke minuut.",
                ),
                abort_conditions=AbortConditions(
                    stale_heartbeat=True,
                    capital_limit_breach=False,
                    risk_limit_breach=False,
                    ttl_expired=True,
                ),
            )
            tf_result = queen.issue_mission(tf_mission)
            if not tf_result.accepted:
                log.warning(
                    "TimeFilterAnt missie geweigerd: %s — %s",
                    tf_result.rejection_reason, tf_result.rejection_detail,
                )
            else:
                tf_ant = TimeFilterAnt(
                    ant_id=tf_ant_id,
                    mission=tf_mission,
                    scheduler=scheduler,
                    logs_root=logs_root,
                )
                threading.Thread(
                    target=tf_ant.run,
                    name=f"time-filter-{tf_ant_id[:20]}",
                    daemon=True,
                ).start()
                scheduler.register_agent(AgentRecord(
                    ant_id=tf_ant_id,
                    mission_id=tf_mission.mission_id,
                    node_id=args.node_id,
                    ant_type="time_filter_ant",
                    ttl=tf_mission.ttl,
                    heartbeat_interval=tf_mission.heartbeat_interval,
                ))
                log.info("TimeFilterAnt gestart | ant_id=%s  ttl=%ds", tf_ant_id, tf_mission.ttl)
        else:
            log.info("TimeFilterAnt uitgeschakeld (zet TIME_FILTER_ENABLED=true om te activeren).")
    except Exception:
        log.exception("TimeFilterAnt bootstrap mislukt — colony draait door zonder TimeFilterAnt.")

    # --- Stap 8f: Equities ants (opt-in via EQUITIES_ENABLED=true) ---
    try:
        if _equities_enabled:
            from ant_colony.ants.equities.sector_scout_ant import SectorScoutAnt
            from ant_colony.ants.equities.fundamental_ant import FundamentalAnt
            from ant_colony.ants.equities.dividend_scout_ant import DividendScoutAnt
            from ant_colony.ants.equities.piotroski_ant import PiotroskiAnt
            from ant_colony.ants.equities.breakout_ant import BreakoutAnt
            from ant_colony.ants.equities.rs_regime_ant import RSRegimeAnt
            from ant_colony.ants.paper_ant_equities import EquitiesPaperAnt
            from ant_colony.biome.adapters.ibkr_adapter import IBKRAdapter
            from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
            from ant_colony.biome.biome_registry import BiomeRegistry
            from ant_colony.colony.scheduler.colony_scheduler import AgentRecord
            from ant_colony.schemas.mission import (
                AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
            )

            _ts_eq      = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _yf_adapter = YahooFinanceAdapter()
            _eq_registry = BiomeRegistry()
            _eq_registry.register(_yf_adapter)   # data-fallback (geen auth vereist)

            # IBKR als primaire equities adapter (overschrijft YF als verbinding lukt)
            _ibkr_host = os.getenv("IBKR_HOST", "127.0.0.1")
            _ibkr_port = int(os.getenv("IBKR_PORT", "7497"))
            _ibkr_adapter = IBKRAdapter(host=_ibkr_host, port=_ibkr_port)
            _ibkr_connected = _ibkr_adapter.connect()
            if _ibkr_connected:
                _eq_registry.register(_ibkr_adapter)
                log.info(
                    "IBKRAdapter verbonden | host=%s port=%d",
                    _ibkr_host, _ibkr_port,
                )
            else:
                log.warning(
                    "IBKRAdapter niet verbonden | host=%s port=%d"
                    " — equities ants gebruiken YahooFinance als data-bron",
                    _ibkr_host, _ibkr_port,
                )
            _obs_risk_eq = RiskLimits(
                max_drawdown_pct=0.01, max_position_size=1.0,
                daily_loss_limit=1.0, stop_loss_required=False,
            )
            _eq_scope = MarketScope(
                biome="equities",
                symbols=["XLK", "XLE", "XLV", "XLF", "XLI", "XLB",
                         "XLP", "XLY", "XLU", "XLRE", "XLC"],
            )
            _eq_missions = [
                Mission(
                    mission_id=f"sector-scout-{_ts_eq}",
                    ant_type="sector_scout_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "report"],
                    market_scope=_eq_scope,
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Rank SPDR sector ETFs op 3-maands momentum en emiteer signalen.",
                    ),
                ),
                Mission(
                    mission_id=f"fundamental-{_ts_eq}",
                    ant_type="fundamental_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "propose_candidate"],
                    market_scope=MarketScope(biome="equities", symbols=["AAPL", "MSFT"]),
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Screen S&P500 top 50 op Piotroski F-Score en momentum.",
                    ),
                ),
                Mission(
                    mission_id=f"dividend-scout-{_ts_eq}",
                    ant_type="dividend_scout_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "report"],
                    market_scope=MarketScope(biome="equities", symbols=["JNJ", "KO"]),
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Screen Dividend Aristocrats en geef VIX hedge-signaal.",
                    ),
                ),
                Mission(
                    mission_id=f"piotroski-{_ts_eq}",
                    ant_type="piotroski_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "report"],
                    market_scope=MarketScope(biome="equities", symbols=["AAPL"]),
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Hervalideer Piotroski F-Score en stuur kandidaten door naar BreakoutAnt.",
                    ),
                ),
                Mission(
                    mission_id=f"breakout-{_ts_eq}",
                    ant_type="breakout_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "report"],
                    market_scope=MarketScope(biome="equities", symbols=["AAPL"]),
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Bevestig 52-weeks high breakout en emitteer entry-signalen.",
                    ),
                ),
                Mission(
                    mission_id=f"rs-regime-{_ts_eq}",
                    ant_type="rs_regime_ant",
                    allowed_node=args.node_id,
                    allowed_actions=["read_data", "report"],
                    market_scope=MarketScope(
                        biome="equities",
                        symbols=["QQQ", "XLV", "XLP", "XLU", "XLE", "GLD"],
                    ),
                    capital_limit=0.0,
                    risk_limits=_obs_risk_eq,
                    ttl=86400,
                    heartbeat_interval=3600,
                    success_conditions=SuccessConditions(
                        description="Classificeer marktregime op basis van QQQ vs defensive basket "
                                    "en schrijf RegimeSignal naar ANT_LOGS/rs_regime/.",
                    ),
                ),
            ]

            _eq_ant_classes = {
                "sector_scout_ant":   (SectorScoutAnt,   {"biome_registry": _eq_registry}),
                "fundamental_ant":    (FundamentalAnt,   {"biome_registry": _eq_registry}),
                "dividend_scout_ant": (DividendScoutAnt, {"biome_registry": _eq_registry}),
                "piotroski_ant":      (PiotroskiAnt,     {"biome_registry": _eq_registry}),
                "breakout_ant":       (BreakoutAnt,      {"biome_registry": _eq_registry}),
                "rs_regime_ant":      (RSRegimeAnt,      {"biome_registry": _eq_registry}),
            }

            for eq_mission in _eq_missions:
                try:
                    eq_result = queen.issue_mission(eq_mission)
                    if not eq_result.accepted:
                        log.warning(
                            "Equities-missie geweigerd: %s — %s",
                            eq_mission.mission_id,
                            eq_result.rejection_reason,
                        )
                        continue

                    ant_class, extra_kwargs = _eq_ant_classes[eq_mission.ant_type]
                    eq_ant_id = f"{eq_mission.ant_type[:8]}-{uuid.uuid4().hex[:12]}"
                    eq_ant = ant_class(
                        ant_id=eq_ant_id,
                        mission=eq_mission,
                        scheduler=scheduler,
                        logs_root=logs_root,
                        **extra_kwargs,
                    )
                    threading.Thread(
                        target=eq_ant.run,
                        name=f"eq-{eq_ant_id[:20]}",
                        daemon=True,
                    ).start()
                    scheduler.register_agent(AgentRecord(
                        ant_id=eq_ant_id,
                        mission_id=eq_mission.mission_id,
                        node_id=args.node_id,
                        ant_type=eq_mission.ant_type,
                        ttl=eq_mission.ttl,
                        heartbeat_interval=eq_mission.heartbeat_interval,
                    ))
                    log.info(
                        "Equities ant gestart | type=%s  ant_id=%s  ttl=%ds",
                        eq_mission.ant_type,
                        eq_ant_id,
                        eq_mission.ttl,
                    )
                except Exception:
                    log.exception(
                        "Equities ant kon niet worden gestart: %s — gaat door met volgende ant",
                        eq_mission.mission_id,
                    )

            # --- EquitiesPaperAnt ---
            _eq_paper_symbols = [
                # Sector ETF's (SectorScoutAnt doelwitten)
                "XLK", "XLE", "XLV", "XLF", "XLI", "XLB",
                "XLP", "XLY", "XLU", "XLRE", "XLC",
                # Positive-edge momentum canaries (3yr audit)
                "GLD", "QQQ",
                # Dividend Aristocrats (DividendScoutAnt doelwitten)
                "KO", "PG", "ABT", "PEP", "MCD",
                "WMT", "XOM", "CVX", "CL", "GD", "EMR",
                # Breakout kandidaten (BreakoutAnt doelwitten)
                "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA",
                "JPM", "V", "UNH", "HD",
            ]
            _eq_paper_mission = Mission(
                mission_id=f"eq-paper-{_ts_eq}",
                ant_type="paper_ant_equities",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "paper_trade"],
                market_scope=MarketScope(
                    biome="equities",
                    symbols=_eq_paper_symbols,
                ),
                capital_limit=500.0,
                risk_limits=RiskLimits(
                    max_drawdown_pct=0.25,
                    max_position_size=500.0,
                    daily_loss_limit=50.0,
                    stop_loss_required=False,
                ),
                ttl=86400 * 90,        # 90 dagen missie-TTL (10-dagse handels-TTL per positie)
                heartbeat_interval=60,
                success_conditions=SuccessConditions(
                    description="Equities paper trading — 30 handelsdagen, Sharpe > 0.5, max drawdown < 25%.",
                ),
            )
            _eq_paper_result = queen.issue_mission(_eq_paper_mission)
            if _eq_paper_result.accepted:
                _eq_paper_ant_id = f"eq-paper-{uuid.uuid4().hex[:12]}"
                _eq_paper_ant = EquitiesPaperAnt(
                    ant_id=_eq_paper_ant_id,
                    mission=_eq_paper_mission,
                    scheduler=scheduler,
                    biome_registry=_eq_registry,
                    logs_root=logs_root,
                )
                threading.Thread(
                    target=_eq_paper_ant.run,
                    name=f"eq-paper-{_eq_paper_ant_id[:16]}",
                    daemon=True,
                ).start()
                scheduler.register_agent(AgentRecord(
                    ant_id=_eq_paper_ant_id,
                    mission_id=_eq_paper_mission.mission_id,
                    node_id=args.node_id,
                    ant_type="paper_ant_equities",
                    ttl=_eq_paper_mission.ttl,
                    heartbeat_interval=_eq_paper_mission.heartbeat_interval,
                ))
                _paper_ledgers.append(_eq_paper_ant._ledger)
                log.info(
                    "EquitiesPaperAnt gestart | ant_id=%s  capital=€%.2f  symbols=%s",
                    _eq_paper_ant_id,
                    _eq_paper_mission.capital_limit,
                    ",".join(_eq_paper_symbols),
                )
            else:
                log.warning(
                    "EquitiesPaperAnt-missie geweigerd — %s",
                    _eq_paper_result.rejection_reason,
                )
                _eq_paper_ant = None

        else:
            _eq_paper_ant = None
            log.info(
                "Equities ants uitgeschakeld (opt-in vereist — zet EQUITIES_ENABLED=true)."
            )
    except Exception:
        _eq_paper_ant = None
        log.exception("Equities bootstrap mislukt — colony draait door zonder equities ants.")

    # --- Stap 8g: NewsAnt (opt-in via NEWS_ANT_ENABLED=true) ---
    _news_api_key    = os.getenv("NEWS_API_KEY", "")
    _news_ant_enabled = os.getenv("NEWS_ANT_ENABLED", "false").lower() == "true"
    try:
        if _news_ant_enabled and _news_api_key:
            from ant_colony.ants.news_ant import NewsAnt
            from ant_colony.schemas.mission import (
                MarketScope as _NMarketScope,
                Mission as _NMission,
                RiskLimits as _NRiskLimits,
                SuccessConditions as _NSuccessConditions,
            )
            _ts_news = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _news_mission = _NMission(
                mission_id=f"news-{_ts_news}",
                ant_type="news_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "report"],
                market_scope=_NMarketScope(biome="crypto", symbols=["GLOBAL"]),
                capital_limit=0.0,
                risk_limits=_NRiskLimits(
                    max_drawdown_pct=1.0,
                    max_position_size=1.0,
                    daily_loss_limit=1.0,
                    stop_loss_required=False,
                ),
                ttl=86400 * 365,
                heartbeat_interval=1800,
                success_conditions=_NSuccessConditions(
                    description="Monitort nieuwssentiment via NewsAPI (100 calls/dag).",
                ),
            )
            _news_result = queen.issue_mission(_news_mission)
            if _news_result.accepted:
                _news_ant_id = f"news-{uuid.uuid4().hex[:12]}"
                _news_ant = NewsAnt(
                    ant_id=_news_ant_id,
                    mission=_news_mission,
                    scheduler=scheduler,
                    logs_root=logs_root,
                    api_key=_news_api_key,
                )
                threading.Thread(
                    target=_news_ant.run,
                    name=f"news-{_news_ant_id[:16]}",
                    daemon=True,
                ).start()
                log.info("NewsAnt gestart | ant_id=%s", _news_ant_id)
            else:
                log.warning(
                    "NewsAnt-missie geweigerd — %s", _news_result.rejection_reason
                )
        elif _news_ant_enabled and not _news_api_key:
            log.warning(
                "NEWS_ANT_ENABLED=true maar NEWS_API_KEY ontbreekt — NewsAnt niet gestart."
            )
        else:
            log.info("NewsAnt uitgeschakeld (zet NEWS_ANT_ENABLED=true om te activeren).")
    except Exception:
        log.exception("NewsAnt bootstrap mislukt — colony draait door zonder NewsAnt.")

    # --- Stap 8h: WatchtowerAnt (opt-in via WATCHTOWER_ENABLED=true) ---
    try:
        _watchtower_enabled = os.getenv("WATCHTOWER_ENABLED", "false").lower() == "true"
        if _watchtower_enabled:
            from ant_colony.ants.watchtower_ant import WatchtowerAnt
            from ant_colony.clients.watchtower_client import WatchtowerClient
            from ant_colony.schemas.mission import (
                MarketScope as _WTMarketScope,
                Mission as _WTMission,
                RiskLimits as _WTRiskLimits,
                SuccessConditions as _WTSuccessConditions,
            )

            _wt_client = WatchtowerClient()
            _wt_healthy = _wt_client.is_healthy()
            log.info(
                "Watchtower health check | url=%s  status=%s",
                _wt_client.base_url,
                "ONLINE" if _wt_healthy else "OFFLINE",
            )

            _ts_wt = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
            _wt_mission = _WTMission(
                mission_id=f"watchtower-{_ts_wt}",
                ant_type="watchtower_ant",
                allowed_node=args.node_id,
                allowed_actions=["read_data", "report"],
                market_scope=_WTMarketScope(biome="crypto", symbols=["GLOBAL"]),
                capital_limit=0.0,
                risk_limits=_WTRiskLimits(
                    max_drawdown_pct=1.0,
                    max_position_size=1.0,
                    daily_loss_limit=1.0,
                    stop_loss_required=False,
                ),
                ttl=86400 * 365,
                heartbeat_interval=int(os.getenv("WATCHTOWER_POLL_INTERVAL", "300")),
                success_conditions=_WTSuccessConditions(
                    description="Pollt Watchtower entry-intelligence en filtert bruikbare signalen.",
                ),
            )
            _wt_result = queen.issue_mission(_wt_mission)
            if _wt_result.accepted:
                _wt_ant_id = f"watchtower-{uuid.uuid4().hex[:12]}"
                _wt_ant = WatchtowerAnt(
                    ant_id=_wt_ant_id,
                    mission=_wt_mission,
                    scheduler=scheduler,
                    logs_root=logs_root,
                    client=_wt_client,
                )
                threading.Thread(
                    target=_wt_ant.run,
                    name=f"watchtower-{_wt_ant_id[:16]}",
                    daemon=True,
                ).start()
                log.info(
                    "WatchtowerAnt gestart | ant_id=%s  url=%s",
                    _wt_ant_id, _wt_client.base_url,
                )
            else:
                log.warning(
                    "WatchtowerAnt-missie geweigerd — %s", _wt_result.rejection_reason
                )
        else:
            log.info("WatchtowerAnt uitgeschakeld (zet WATCHTOWER_ENABLED=true om te activeren).")
    except Exception:
        log.exception("WatchtowerAnt bootstrap mislukt — colony draait door zonder WatchtowerAnt.")

    # --- Stap 9: bouw ColonyContext en start dashboard in watchdog thread ---
    context = ColonyContext(
        queen=queen,
        scheduler=scheduler,
        logs_root=logs_root,
        broker_names={"crypto": "Bitvavo"},
        biome_registry=biome_registry,
        paper_ledgers=_paper_ledgers,
    )

    log.info(
        "Dashboard watchdog starten op http://%s:%d — bereikbaar vanuit het netwerk.",
        args.host,
        args.port,
    )
    threading.Thread(
        target=_run_dashboard_watchdog,
        args=(dashboard_run, context, args.host, args.port, log),
        name="dashboard-watchdog",
        daemon=True,
    ).start()

    # Hoofdthread blijft leven zodat daemon threads (scheduler, ants, dashboard)
    # niet worden beëindigd bij een clean return van main().
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Colony gestopt door operator (KeyboardInterrupt).")


if __name__ == "__main__":
    main()
