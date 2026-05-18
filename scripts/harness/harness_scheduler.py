"""
scripts/harness/harness_scheduler.py

Standalone test-harness voor ColonyScheduler + ResearchAnt + PaperAnt in threads.

Doel: verifieer dat de supervisor-restart logica in start_colony.py werkt.
Ants met korte TTL (10s) worden automatisch herstart door de supervisor-loop;
alle 10 ticks moeten beide mieren actief (of net hergestart) zijn.

Threading-patroon (analoog aan start_colony.py):
  - sched_thread       : scheduler.start()         — daemon thread
  - research-supervisor: supervisor-loop per mier   — daemon thread
  - paper-supervisor   : supervisor-loop per mier   — daemon thread
  - main thread        : monitoring-loop, 10 × sleep(5s), rapportage

Scenario:
  TTL = 10s → ants voltooien na 2 ticks; supervisor herstart binnen 5s
  Max 3 herstarts per uur per ant (rate-limit)
  Dood-drempel = 15s (heartbeat-leeftijd)

Metingen per tick:
  - Seconden sinds laatste heartbeat (van huidige ant-instantie)
  - Mier-status in de scheduler (RUNNING / COMPLETED / PAUSED)
  - Herstarts tot nu toe per ant
  - Scheduler actief (agents met status RUNNING)
  - Thread count en geheugen (MB) indien beschikbaar

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
from collections import deque
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

TICK_INTERVAL         = 5    # seconden per monitoring-tick (= scheduler tick)
N_TICKS               = 10   # totaal ticks → 50s testduur
ANT_TTL               = 10   # seconden — ants voltooien na 2 ticks; daarna herstart
HB_INTERVAL           = 5    # heartbeat_interval voor beide ants
DEAD_THRESHOLD_S      = 15   # seconden zonder heartbeat → markeer als dood
_RESTART_DELAY_S      = 5    # wachttijd voor herstart (identiek aan start_colony.py)
_MAX_RESTARTS_PER_HOUR = 3   # rate-limiet per ant per uur

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
        success_conditions=SuccessConditions(description="Harness supervisor test"),
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
        success_conditions=SuccessConditions(description="Harness supervisor test"),
    )

# ---------------------------------------------------------------------------
# Mutable ant-referentie (wordt bijgewerkt bij elke herstart)
# ---------------------------------------------------------------------------

class _AntRef:
    """Houdt bij welk ant_id momenteel actief is voor een gesupervisede ant."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.ant_id: str = ""
        self.restart_count: int = 0
        self._lock = threading.Lock()

    def set(self, ant_id: str) -> None:
        with self._lock:
            self.ant_id = ant_id

    def get(self) -> str:
        with self._lock:
            return self.ant_id

    def increment_restarts(self) -> None:
        with self._lock:
            self.restart_count += 1

    def get_restarts(self) -> int:
        with self._lock:
            return self.restart_count

# ---------------------------------------------------------------------------
# Supervisor-loop per ant (identiek patroon aan _start_supervised_ant in start_colony.py)
# ---------------------------------------------------------------------------

def _start_supervised_ant_thread(
    ref: _AntRef,
    make_ant,
    mission: Mission,
    scheduler: ColonyScheduler,
    logs_root: Path,
) -> threading.Thread:
    """
    Start een supervisor-loop voor één ant in een daemon-thread.

    De supervisor herstart de ant automatisch na stop/crash, met rate-limiet
    van _MAX_RESTARTS_PER_HOUR. Patroon identiek aan _start_supervised_ant()
    in scripts/start_colony.py.
    """

    def _supervisor_loop() -> None:
        restart_times: deque[float] = deque()

        while True:
            ant_id = f"{ref.label.lower()}-{uuid.uuid4().hex[:12]}"

            if ref.get_restarts() > 0:
                now = monotonic()
                while restart_times and now - restart_times[0] > 3600:
                    restart_times.popleft()
                if len(restart_times) >= _MAX_RESTARTS_PER_HOUR:
                    log.critical(
                        "%s: %d herstarts per uur bereikt — supervisor gestopt"
                        " (restart-loop preventie)",
                        ref.label, _MAX_RESTARTS_PER_HOUR,
                    )
                    return
                restart_times.append(now)
                log.warning(
                    "%s herstart | restart=%d  (rate: %d/%d per uur)",
                    ref.label, ref.get_restarts(),
                    len(restart_times), _MAX_RESTARTS_PER_HOUR,
                )

            ref.set(ant_id)

            try:
                ant = make_ant(ant_id)
                scheduler.register_agent(AgentRecord(
                    ant_id=ant_id,
                    mission_id=mission.mission_id,
                    node_id=mission.allowed_node,
                    ant_type=mission.ant_type,
                    ttl=mission.ttl,
                    heartbeat_interval=mission.heartbeat_interval,
                ))
                log.info(
                    "%s gestart | ant_id=%s  ttl=%ds  restart=%d  fresh_ttl=true",
                    ref.label, ant_id[:16], mission.ttl, ref.get_restarts(),
                )
                status = ant.run()
                log.info(
                    "%s gestopt | ant_id=%s  status=%s — herstart over %ds",
                    ref.label, ant_id[:16],
                    getattr(status, "value", status), _RESTART_DELAY_S,
                )
            except Exception:
                log.exception(
                    "%s supervisor fout — herstart over %ds",
                    ref.label, _RESTART_DELAY_S,
                )

            ref.increment_restarts()
            time.sleep(_RESTART_DELAY_S)

    t = threading.Thread(
        target=_supervisor_loop,
        name=f"{ref.label.lower()}-supervisor",
        daemon=True,
    )
    t.start()
    return t

# ---------------------------------------------------------------------------
# Per-tick observatie
# ---------------------------------------------------------------------------

class TickObs(NamedTuple):
    tick_n:       int
    elapsed_s:    float
    threads:      int
    memory_mb:    float | None
    research:     tuple[float, str, bool]   # (hb_age, status, is_dead)
    paper:        tuple[float, str, bool]
    sched_active: int
    newly_dead:   list[str]
    r_restarts:   int
    p_restarts:   int

# ---------------------------------------------------------------------------
# Printen
# ---------------------------------------------------------------------------

_W = 88


def _print_header() -> None:
    print("=" * _W)
    print(f"{'HARNESS: ColonyScheduler + Supervisor-restart (ResearchAnt + PaperAnt)':^{_W}}")
    print(f"{'10 ticks × 5s — TTL=10s — herstart na 5s — dood-drempel=15s':^{_W}}")
    print("=" * _W)
    print(
        f"{'Tick':>4}  {'Elaps':>6}  {'Thrd':>4}  {'RAM':>6}  "
        f"{'Research':>18}  {'PaperAnt':>18}  {'Actief':>6}  {'#Rst':>5}"
    )
    print("-" * _W)


def _fmt_ant(hb_age: float, status: str, dead: bool) -> str:
    if dead:
        return f"{'DOOD':>10} ({hb_age:>4.0f}s)"
    return f"{hb_age:>5.1f}s [{status[:8]:>8}]"


def _print_tick(obs: TickObs) -> None:
    ram = f"{obs.memory_mb:.0f}MB" if obs.memory_mb is not None else "  N/A"
    r_str = _fmt_ant(*obs.research)
    p_str = _fmt_ant(*obs.paper)
    extra = ""
    if obs.newly_dead:
        extra = f"  ← {', '.join(obs.newly_dead)} gestopt"
    total_restarts = obs.r_restarts + obs.p_restarts
    print(
        f"{obs.tick_n:>4}  {obs.elapsed_s:>5.1f}s  {obs.threads:>4}  {ram:>6}  "
        f"{r_str:>18}  {p_str:>18}  {obs.sched_active:>6}  {total_restarts:>5}{extra}"
    )

# ---------------------------------------------------------------------------
# Monitoring-lus
# ---------------------------------------------------------------------------

def _monitor(
    scheduler: ColonyScheduler,
    r_ref: _AntRef,
    p_ref: _AntRef,
    harness_start: float,
) -> list[TickObs]:
    all_obs: list[TickObs] = []
    dead_since: dict[str, float] = {}

    for tick_n in range(1, N_TICKS + 1):
        time.sleep(TICK_INTERVAL)

        elapsed = round(monotonic() - harness_start, 1)

        r_rec = scheduler._agents.get(r_ref.get())
        p_rec = scheduler._agents.get(p_ref.get())

        def _ant_obs(rec: AgentRecord | None) -> tuple[float, str, bool]:
            if rec is None:
                return 9999.0, "ONBEKEND", True
            age    = rec.seconds_since_heartbeat()
            status = rec.status.value
            dead   = age > DEAD_THRESHOLD_S
            return round(age, 1), status, dead

        r_obs = _ant_obs(r_rec)
        p_obs = _ant_obs(p_rec)

        newly_dead: list[str] = []
        for ref, obs_tuple in [(r_ref, r_obs), (p_ref, p_obs)]:
            hb_age, _, is_dead = obs_tuple
            key = ref.label
            if is_dead and key not in dead_since:
                dead_since[key] = monotonic()
                newly_dead.append(ref.label)
            elif not is_dead and key in dead_since:
                # ant is hersteld (na herstart)
                del dead_since[key]

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
            r_restarts=r_ref.get_restarts(),
            p_restarts=p_ref.get_restarts(),
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
    r_ref: _AntRef,
    p_ref: _AntRef,
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

    print("Mier-overzicht:")
    r_final = all_obs[-1].research
    p_final = all_obs[-1].paper
    print(
        f"  ResearchAnt  status={r_final[1]:8s}  hb_leeftijd={r_final[0]:.1f}s"
        f"  dood={r_final[2]}  herstarts={r_ref.get_restarts()}"
    )
    print(
        f"  PaperAnt     status={p_final[1]:8s}  hb_leeftijd={p_final[0]:.1f}s"
        f"  dood={p_final[2]}  herstarts={p_ref.get_restarts()}"
    )
    print()

    sched_alone = sum(1 for obs in all_obs if obs.sched_active == 0)
    print(f"Scheduler zonder actieve mieren: {sched_alone} van {len(all_obs)} ticks")
    print(f"Scheduler stopper-callbacks    : {len(stopper_calls)}")
    for ant_id, reason in stopper_calls:
        label = "ResearchAnt" if "research" in ant_id else "PaperAnt"
        print(f"  {label:12s}  reden={reason}")
    print()

    _print_diagnosis(all_obs, sched_alone, r_ref, p_ref)

    print("=" * _W)
    print("Harness klaar — geen live orders, geen Bitvavo, geen echt kapitaal")
    print("=" * _W + "\n")


def _print_diagnosis(
    all_obs: list[TickObs],
    sched_alone: int,
    r_ref: _AntRef,
    p_ref: _AntRef,
) -> None:
    print("Diagnose:")
    both_running = sum(1 for obs in all_obs if obs.sched_active >= 1)
    ticks_with_dead = sum(
        1 for obs in all_obs if obs.research[2] or obs.paper[2]
    )
    total_restarts = r_ref.get_restarts() + p_ref.get_restarts()

    print(f"  Minstens één mier actief    : {both_running} van {len(all_obs)} ticks")
    print(f"  Ticks met dode mier         : {ticks_with_dead}")
    print(f"  Totaal herstarts            : {total_restarts}"
          f"  (ResearchAnt={r_ref.get_restarts()}, PaperAnt={p_ref.get_restarts()})")
    print(f"  Scheduler zonder mieren     : {sched_alone} ticks")
    print()

    if ticks_with_dead == 0 and total_restarts > 0:
        print("  ┌─────────────────────────────────────────────────────────────┐")
        print("  │  SUPERVISOR-RESTART WERKT CORRECT                           │")
        print("  │  Ants zijn meerdere keren herstart maar nooit als 'dood'    │")
        print("  │  gemarkeerd — heartbeat bleef altijd onder de drempel       │")
        print("  └─────────────────────────────────────────────────────────────┘")
    elif ticks_with_dead == 0:
        print("  ┌─────────────────────────────────────────────────────────────┐")
        print("  │  ALLE MIEREN ACTIEF GEDURENDE HELE TESTDUUR                 │")
        print("  │  Verhoog N_TICKS / verlaag ANT_TTL om herstarts te zien     │")
        print("  └─────────────────────────────────────────────────────────────┘")
    else:
        first_dead = next(
            (obs for obs in all_obs if obs.research[2] or obs.paper[2]), None
        )
        if first_dead:
            print("  ┌─────────────────────────────────────────────────────────────┐")
            print("  │  WAARSCHUWING: mieren als dood gemarkeerd                   │")
            print(f"  │  Eerste dode mier: tick {first_dead.tick_n} ({first_dead.elapsed_s:.0f}s)"
                  f"                         │")
            print("  │  Controleer herstart-delay en dood-drempel                  │")
            print("  └─────────────────────────────────────────────────────────────┘")

# ---------------------------------------------------------------------------
# Hoofd
# ---------------------------------------------------------------------------

def main() -> None:
    _print_header()

    with tempfile.TemporaryDirectory(prefix="harness_sched_") as tmpdir:
        logs_root = Path(tmpdir)

        _write_scout_signal(logs_root, "BTC-EUR")

        registry = BiomeRegistry()
        registry.register(_StubCryptoAdapter())

        stopper_calls: list[tuple[str, str]] = []

        def _stopper(ant_id: str, reason: str) -> None:
            stopper_calls.append((ant_id, reason))
            log.info("Scheduler stopper | ant=%s reden=%s", ant_id[:16], reason)

        scheduler = ColonyScheduler(
            logs_root=logs_root,
            tick_interval=TICK_INTERVAL,
            agent_stopper=_stopper,
        )

        r_mission = _research_mission()
        p_mission = _paper_mission()

        r_ref = _AntRef("ResearchAnt")
        p_ref = _AntRef("PaperAnt")

        def _make_research(ant_id: str) -> ResearchAnt:
            return ResearchAnt(
                ant_id=ant_id, mission=r_mission,
                scheduler=scheduler, biome_registry=registry,
                logs_root=logs_root,
            )

        def _make_paper(ant_id: str) -> PaperAnt:
            return PaperAnt(
                ant_id=ant_id, mission=p_mission,
                scheduler=scheduler, biome_registry=registry,
                logs_root=logs_root,
            )

        harness_start = monotonic()

        # Scheduler-thread (daemon)
        sched_thread = threading.Thread(
            target=scheduler.start,
            name="scheduler-thread",
            daemon=True,
        )
        sched_thread.start()
        log.info("Scheduler-thread gestart (tick_interval=%ds)", TICK_INTERVAL)

        time.sleep(0.5)

        # Supervisor-threads (daemon, herstart automatisch bij stop)
        _start_supervised_ant_thread(r_ref, _make_research, r_mission, scheduler, logs_root)
        _start_supervised_ant_thread(p_ref, _make_paper,    p_mission, scheduler, logs_root)

        time.sleep(0.5)

        log.info(
            "Monitoring gestart | %d ticks × %ds = %ds  TTL=%ds  herstart_delay=%ds",
            N_TICKS, TICK_INTERVAL, N_TICKS * TICK_INTERVAL, ANT_TTL, _RESTART_DELAY_S,
        )

        all_obs = _monitor(scheduler, r_ref, p_ref, harness_start)

        log.info("Harness klaar — scheduler wordt gestopt via Level-3 kill-switch")
        scheduler.kill_switch(KillLevel.COLONY)
        sched_thread.join(timeout=3.0)

        _print_report(all_obs, scheduler, r_ref, p_ref,
                      harness_start, stopper_calls)


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
