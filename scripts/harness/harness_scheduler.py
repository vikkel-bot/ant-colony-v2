"""
scripts/harness/harness_scheduler.py

Standalone test-harness voor ColonyScheduler + ResearchAnt + PaperAnt in threads.

Reproduceert het crash-patroon waarbij alle mieren stoppen maar de scheduler
blijft draaien. De tijdschaal wordt gecomprimeerd naar 50 seconden
(10 ticks × 5s) zodat het scenario reproduceerbaar is.

Threading-patroon (analoog aan start_colony.py):
  - sched_thread   : scheduler.start()  — daemon thread
  - research_thread: research_ant.run() — daemon thread
  - paper_thread   : paper_ant.run()    — daemon thread
  - main thread    : monitoring-loop, 10 × sleep(5s), rapportage

Scenario:
  TTL = 20s → ants voltooien na tick 4
  Monitoring loopt door tot tick 10 (50s)
  → scheduler draait na tick 4 zonder actieve mieren
  → "dood"-markering bij heartbeat-leeftijd > 15s (threshold)

Metingen per tick:
  - Seconden sinds laatste heartbeat per mier
  - Mier-status in de scheduler (RUNNING / COMPLETED / ABORTED / PAUSED)
  - Scheduler actief (telt agents met status RUNNING)
  - Thread count (threading.active_count)
  - Geheugen in MB (psutil indien beschikbaar)

Veiligheid:
  - Geen live broker, geen orders, geen echt kapitaal
  - Alle adapters zijn stubs
  - Tijdelijke logmap wordt opgeruimd na afloop
  - Daemon-threads sluiten automatisch bij einde van de harness

Gebruik:
  python scripts/harness/harness_scheduler.py
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import NamedTuple

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ant_colony.ants.paper_ant import PaperAnt
from ant_colony.ants.research_ant import ResearchAnt
from ant_colony.biome.biome_adapter import MarketData
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import AgentRecord, ColonyScheduler, KillLevel
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s | %(message)s",
)
log = logging.getLogger("harness.scheduler")

# ---------------------------------------------------------------------------
# Configuratie
# ---------------------------------------------------------------------------

TICK_INTERVAL    = 5    # seconden per monitoring-tick (= scheduler tick)
N_TICKS          = 10   # totaal ticks → 50s testduur
ANT_TTL          = 20   # seconden — ants voltooien na ~4 ticks
HB_INTERVAL      = 5    # heartbeat_interval voor beide ants
DEAD_THRESHOLD_S = 15   # seconden zonder heartbeat → markeer als dood

# ---------------------------------------------------------------------------
# Optionele psutil
# ---------------------------------------------------------------------------

try:
    import psutil as _psutil
    _PROC = _psutil.Process(os.getpid())
    _HAS_PSUTIL = True
except ImportError:
    _PROC = None
    _HAS_PSUTIL = False


def _memory_mb() -> float | None:
    if not _HAS_PSUTIL or _PROC is None:
        return None
    try:
        return round(_PROC.memory_info().rss / 1024 / 1024, 1)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Synthetische OHLCV-candles (geen netwerk, geen yfinance)
# ---------------------------------------------------------------------------

def _synthetic_candles(symbol: str, n: int = 300, biome_id: str = "crypto") -> list[MarketData]:
    rng = random.Random(42)
    candles: list[MarketData] = []
    price = 30_000.0
    now   = datetime.now(tz=timezone.utc)
    up_bars, down_bars = 30, 5
    cycle = up_bars + down_bars
    for i in range(n):
        phase = i % cycle
        drift = 0.0025 if phase < up_bars else -0.025
        noise = rng.gauss(0, 0.0005 if phase < up_bars else 0.001)
        price = max(price * (1 + drift + noise), 1.0)
        ts    = now - timedelta(hours=n - i)
        spread = abs(rng.gauss(0, 0.001)) * price
        candles.append(MarketData(
            symbol=symbol, timeframe="1h", timestamp=ts,
            open=round(price * (1 + rng.uniform(-0.001, 0.001)), 2),
            high=round(price + spread, 2),
            low=round(max(price - spread, 1.0), 2),
            close=round(price, 2),
            volume=round(rng.uniform(10.0, 100.0), 4),
            biome_id=biome_id,
        ))
    return candles

# ---------------------------------------------------------------------------
# Stub adapter — geen netwerk, geen Bitvavo
# ---------------------------------------------------------------------------

class _StubCryptoAdapter:
    biome_id = "crypto"
    _PRICES  = {"BTC-EUR": 30_000.0, "ETH-EUR": 2_000.0}

    def is_available(self) -> bool:
        return True

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData:
        price = self._PRICES.get(symbol, 1_000.0)
        return MarketData(
            symbol=symbol, timeframe=timeframe,
            timestamp=datetime.now(tz=timezone.utc),
            open=price, high=price * 1.005, low=price * 0.995,
            close=price, volume=50.0, biome_id="crypto",
        )

    def get_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[MarketData]:
        return _synthetic_candles(symbol, n=min(limit, 300))

    def get_account_state(self):   return None
    def place_order(self, order):  return None
    def get_positions(self):       return []

# ---------------------------------------------------------------------------
# Missies
# ---------------------------------------------------------------------------

def _research_mission() -> Mission:
    return Mission(
        mission_id=f"harness-research-{uuid.uuid4().hex[:8]}",
        ant_type="research_ant",
        allowed_node="harness-node",
        allowed_actions=["analyze", "propose_candidate", "report"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"], timeframes=["1h"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=1.0, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=ANT_TTL,
        heartbeat_interval=HB_INTERVAL,
        success_conditions=SuccessConditions(description="Harness scheduler test"),
    )


def _paper_mission() -> Mission:
    return Mission(
        mission_id=f"harness-paper-{uuid.uuid4().hex[:8]}",
        ant_type="paper_ant",
        allowed_node="harness-node",
        allowed_actions=["open_position", "close_position", "report"],
        market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"], timeframes=["1m"]),
        capital_limit=500.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.20, max_position_size=250.0, daily_loss_limit=50.0,
        ),
        ttl=ANT_TTL,
        heartbeat_interval=HB_INTERVAL,
        success_conditions=SuccessConditions(description="Harness scheduler test"),
    )

# ---------------------------------------------------------------------------
# Per-tick observatie
# ---------------------------------------------------------------------------

class TickObs(NamedTuple):
    tick_n:           int
    elapsed_s:        float
    threads:          int
    memory_mb:        float | None
    # per ant: (hb_age_s, status_str, is_dead)
    research:         tuple[float, str, bool]
    paper:            tuple[float, str, bool]
    sched_active:     int   # agents met status RUNNING
    newly_dead:       list[str]   # labels van mieren die deze tick dood gingen

# ---------------------------------------------------------------------------
# Printen
# ---------------------------------------------------------------------------

_W = 80


def _print_header() -> None:
    print("=" * _W)
    print(f"{'HARNESS: ColonyScheduler + ResearchAnt + PaperAnt (threads)':^{_W}}")
    print(f"{'10 ticks × 5s — TTL=20s — dood-drempel=15s':^{_W}}")
    print("=" * _W)
    print(
        f"{'Tick':>4}  {'Elaps':>6}  {'Thrd':>4}  {'RAM':>6}  "
        f"{'Research':>16}  {'PaperAnt':>16}  {'Actief':>6}"
    )
    print("-" * _W)


def _fmt_ant(hb_age: float, status: str, dead: bool) -> str:
    if dead:
        return f"{'DOOD':>8} ({hb_age:>4.0f}s)"
    return f"{hb_age:>5.1f}s [{status[:7]:>7}]"


def _print_tick(obs: TickObs) -> None:
    ram = f"{obs.memory_mb:.0f}MB" if obs.memory_mb is not None else "  N/A"
    r_str = _fmt_ant(*obs.research)
    p_str = _fmt_ant(*obs.paper)
    extra = ""
    if obs.newly_dead:
        extra = f"  ← {', '.join(obs.newly_dead)} gestopt"
    print(
        f"{obs.tick_n:>4}  {obs.elapsed_s:>5.1f}s  {obs.threads:>4}  {ram:>6}  "
        f"{r_str:>16}  {p_str:>16}  {obs.sched_active:>6}{extra}"
    )

# ---------------------------------------------------------------------------
# Monitoring-lus
# ---------------------------------------------------------------------------

def _monitor(
    scheduler: ColonyScheduler,
    r_id: str,
    p_id: str,
    harness_start: float,
) -> list[TickObs]:
    all_obs: list[TickObs] = []
    dead_since: dict[str, float] = {}  # ant_id → monotonic tijdstip

    for tick_n in range(1, N_TICKS + 1):
        time.sleep(TICK_INTERVAL)

        elapsed = round(monotonic() - harness_start, 1)

        r_rec = scheduler._agents.get(r_id)
        p_rec = scheduler._agents.get(p_id)

        def _ant_obs(rec: AgentRecord | None, label: str) -> tuple[float, str, bool]:
            if rec is None:
                return 9999.0, "ONBEKEND", True
            age    = rec.seconds_since_heartbeat()
            status = rec.status.value
            dead   = age > DEAD_THRESHOLD_S
            return round(age, 1), status, dead

        r_obs = _ant_obs(r_rec, "ResearchAnt")
        p_obs = _ant_obs(p_rec, "PaperAnt")

        newly_dead: list[str] = []
        for ant_id, label, obs_tuple in [(r_id, "ResearchAnt", r_obs),
                                          (p_id, "PaperAnt",   p_obs)]:
            hb_age, _, is_dead = obs_tuple
            if is_dead and ant_id not in dead_since:
                dead_since[ant_id] = monotonic()
                newly_dead.append(label)

        active = sum(
            1 for rec in scheduler._agents.values()
            if rec.status == AntStatus.RUNNING
        )

        obs = TickObs(
            tick_n=tick_n,
            elapsed_s=elapsed,
            threads=threading.active_count(),
            memory_mb=_memory_mb(),
            research=r_obs,
            paper=p_obs,
            sched_active=active,
            newly_dead=newly_dead,
        )
        all_obs.append(obs)
        _print_tick(obs)

    return all_obs

# ---------------------------------------------------------------------------
# Eindrapport
# ---------------------------------------------------------------------------

def _print_report(
    all_obs: list[TickObs],
    scheduler: ColonyScheduler,
    r_id: str,
    p_id: str,
    ant_started_at: dict[str, float],
    harness_start: float,
    stopper_calls: list[tuple[str, str]],
) -> None:
    print()
    print("=" * _W)
    print(f"{'EINDRAPPORT':^{_W}}")
    print("=" * _W)

    last = all_obs[-1]
    print(f"Totaal ticks           : {len(all_obs)}")
    print(f"Looptijd               : {last.elapsed_s:.1f}s")
    print(f"Threads aan het einde  : {last.threads}")
    if last.memory_mb is not None:
        print(f"RAM aan het einde      : {last.memory_mb:.0f} MB")
    print()

    # Per ant: wanneer dood gegaan?
    def _death_tick(label_key: str) -> str:
        for obs in all_obs:
            for label in obs.newly_dead:
                if label_key.lower() in label.lower():
                    return f"tick {obs.tick_n} (~{obs.elapsed_s:.0f}s)"
        return "nooit dood gegaan in 10 ticks"

    print("Mier-overzicht:")
    r_final = all_obs[-1].research
    p_final = all_obs[-1].paper
    print(f"  ResearchAnt  status={r_final[1]:8s}  hb_leeftijd={r_final[0]:.1f}s"
          f"  dood={r_final[2]}  gestopt_op={_death_tick('research')}")
    print(f"  PaperAnt     status={p_final[1]:8s}  hb_leeftijd={p_final[0]:.1f}s"
          f"  dood={p_final[2]}  gestopt_op={_death_tick('paper')}")
    print()

    # Hoeveel ticks scheduler zonder actieve mieren?
    sched_alone = sum(1 for obs in all_obs if obs.sched_active == 0)
    print(f"Scheduler alleen (geen actieve mieren): {sched_alone} van {len(all_obs)} ticks")
    print(f"Scheduler stopper-callbacks            : {len(stopper_calls)}")
    for ant_id, reason in stopper_calls:
        label = "ResearchAnt" if r_id in ant_id else "PaperAnt"
        print(f"  {label:12s}  reden={reason}")
    print()

    # Diagnose
    _print_diagnosis(all_obs, sched_alone)

    print("=" * _W)
    print("Harness klaar — geen live orders, geen Bitvavo, geen echt kapitaal")
    print("=" * _W + "\n")


def _print_diagnosis(all_obs: list[TickObs], sched_alone: int) -> None:
    print("Diagnose:")
    both_running = sum(1 for obs in all_obs if obs.sched_active == 2)
    print(f"  Beide mieren actief          : {both_running} ticks")
    print(f"  Scheduler zonder mieren      : {sched_alone} ticks")
    print()

    if sched_alone > 0:
        first_alone = next(obs for obs in all_obs if obs.sched_active == 0)
        print("  ┌─────────────────────────────────────────────────────────┐")
        print("  │  CRASH-PATROON GEREPRODUCEERD                           │")
        print(f"  │  Vanaf tick {first_alone.tick_n} ({first_alone.elapsed_s:.0f}s): scheduler draait,"
              f" alle mieren weg     │")
        print("  │  → Dit is het 2–4 uurs patroon op PC2 in 50s nagebootst │")
        print("  └─────────────────────────────────────────────────────────┘")
        print()
        print("  Oorzaak: mieren voltooien (TTL) of crashen; scheduler")
        print("  detecteert dit, maar herstart ze niet automatisch.")
        print()
        print("  Oplossing: gebruik _start_supervised_ant() uit start_colony.py —")
        print("  die supervisor herstart een mier automatisch na stop/crash.")
    else:
        print("  Alle mieren actief gedurende de volledige testduur.")
        print("  Verhoog N_TICKS of verlaag ANT_TTL om het patroon te zien.")

# ---------------------------------------------------------------------------
# Hoofd
# ---------------------------------------------------------------------------

def main() -> None:
    _print_header()

    with tempfile.TemporaryDirectory(prefix="harness_sched_") as tmpdir:
        logs_root = Path(tmpdir)

        # Scout-signaal voor PaperAnt
        _write_scout_signal(logs_root, "BTC-EUR")

        registry = BiomeRegistry()
        registry.register(_StubCryptoAdapter())

        stopper_calls: list[tuple[str, str]] = []

        def _stopper(ant_id: str, reason: str) -> None:
            stopper_calls.append((ant_id, reason))
            log.info("Scheduler stopper | ant=%s reden=%s", ant_id[:12], reason)

        # Scheduler (tick_interval=5s, gelijk aan monitoring-interval)
        scheduler = ColonyScheduler(
            logs_root=logs_root,
            tick_interval=TICK_INTERVAL,
            agent_stopper=_stopper,
        )

        # Missies + ant-IDs
        r_mission = _research_mission()
        p_mission = _paper_mission()
        r_id = f"research-{uuid.uuid4().hex[:12]}"
        p_id = f"paper-{uuid.uuid4().hex[:12]}"

        # Ant-instanties
        research_ant = ResearchAnt(
            ant_id=r_id, mission=r_mission,
            scheduler=scheduler, biome_registry=registry,
            logs_root=logs_root,
        )
        paper_ant = PaperAnt(
            ant_id=p_id, mission=p_mission,
            scheduler=scheduler, biome_registry=registry,
            logs_root=logs_root,
        )

        ant_started_at: dict[str, float] = {}
        harness_start = monotonic()

        # ----------------------------------------------------------------
        # Threads starten — patroon identiek aan start_colony.py
        # ----------------------------------------------------------------

        def _start_ant_thread(ant, label: str, ant_id: str) -> threading.Thread:
            """Start een ant in een daemon-thread (zonder supervisor-loop)."""
            mission = ant.mission

            def _run() -> None:
                ant_started_at[ant_id] = monotonic()
                scheduler.register_agent(AgentRecord(
                    ant_id=ant_id,
                    mission_id=mission.mission_id,
                    node_id=mission.allowed_node,
                    ant_type=mission.ant_type,
                    ttl=mission.ttl,
                    heartbeat_interval=mission.heartbeat_interval,
                ))
                log.info("%s gestart | ant_id=%s ttl=%ds", label, ant_id[:12], mission.ttl)
                status = ant.run()
                log.info("%s gestopt  | ant_id=%s status=%s", label, ant_id[:12],
                         getattr(status, "value", status))

            t = threading.Thread(target=_run, name=f"{label.lower()}-thread", daemon=True)
            t.start()
            return t

        # Scheduler-thread starten (daemon) — identiek aan _run_scheduler()
        sched_thread = threading.Thread(
            target=scheduler.start,
            name="scheduler-thread",
            daemon=True,
        )
        sched_thread.start()
        log.info("Scheduler-thread gestart (tick_interval=%ds)", TICK_INTERVAL)

        # Even wachten zodat de scheduler zijn eerste tick al heeft gedaan
        time.sleep(0.5)

        # Ant-threads starten
        _start_ant_thread(research_ant, "ResearchAnt", r_id)
        _start_ant_thread(paper_ant,    "PaperAnt",    p_id)

        # Even wachten zodat de AgentRecords zeker geregistreerd zijn
        time.sleep(0.5)

        # ----------------------------------------------------------------
        # Monitoring-lus (main thread)
        # ----------------------------------------------------------------
        log.info("Monitoring gestart | %d ticks × %ds = %ds",
                 N_TICKS, TICK_INTERVAL, N_TICKS * TICK_INTERVAL)

        all_obs = _monitor(scheduler, r_id, p_id, harness_start)

        # ----------------------------------------------------------------
        # Scheduler stoppen
        # ----------------------------------------------------------------
        log.info("Harness klaar — scheduler wordt gestopt via Level-3 kill-switch")
        scheduler.kill_switch(KillLevel.COLONY)
        sched_thread.join(timeout=3.0)

        # ----------------------------------------------------------------
        # Eindrapport
        # ----------------------------------------------------------------
        _print_report(all_obs, scheduler, r_id, p_id,
                      ant_started_at, harness_start, stopper_calls)


# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------

def _write_scout_signal(logs_root: Path, symbol: str) -> None:
    scouts_dir = logs_root / "scouts"
    scouts_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "payload": {
            "action": "opportunity_detected",
            "signal_id": f"sig-{uuid.uuid4().hex[:8]}",
            "symbol": symbol,
            "biome": "crypto",
            "signal_type": "price_move",
            "confidence": 0.85,
            "change_pct": 2.5,
            "detected_at": datetime.now(tz=timezone.utc).isoformat(),
        },
    }
    (scouts_dir / "stub.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
