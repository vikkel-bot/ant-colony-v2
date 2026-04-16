"""
ant_colony/colony/multi_node_sim.py

MultiNodeSimulator — paper-mode simulatie van meerdere nodes tegelijk.

De simulator drijft meerdere PaperLoop-instanties aan, elk gebonden aan een
eigen node en mission. Eén simulatie-tick = alle nodes verwerken dezelfde prijs.
Zo kunnen strategieën over meerdere nodes naast elkaar worden getest zonder
echte concurrency.

Architectuur:
  - Elke node krijgt een eigen PaperLoop (eigen ledger, broker, evaluator)
  - De Queen is de enige autoriteit die missions uitgeeft en nodes registreert
  - De simulator roept de Queen aan om missions te valideren; hij heeft geen
    eigen kapitaal of missie-autoriteit
  - Eén tick → elke loop ontvangt dezelfde prijs, eigen optionele signalen
  - Tick-resultaten worden per node verzameld in een SimulationReport

Regels:
  - Nodes zonder actieve mission worden overgeslagen
  - Stale prijs (≤ 0): alle nodes overgeslagen voor die tick
  - SimulationReport is immutable na constructie
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ant_colony.entry.entry_signal import EntrySignal
from ant_colony.exit_chain.exit_conditions import (
    CheckContext,
    DrawdownCondition,
    StopLossCondition,
    TakeProfitCondition,
    TTLCondition,
)
from ant_colony.exit_chain.exit_evaluator import ExitEvaluator
from ant_colony.paper.paper_broker import PaperBroker
from ant_colony.paper.paper_ledger import PaperLedger
from ant_colony.paper.paper_loop import PaperLoop, TickResult
from ant_colony.queen.queen import Queen
from ant_colony.schemas.mission import Mission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NodeSlot — één node in de simulatie
# ---------------------------------------------------------------------------

@dataclass
class NodeSlot:
    """
    Eén node in de simulatie.

    node_id:   Node-identifier — moet geregistreerd zijn in de Queen.
    mission:   De actieve mission voor deze node.
    loop:      PaperLoop die ticks verwerkt.
    """
    node_id: str
    mission: Mission
    loop: PaperLoop


# ---------------------------------------------------------------------------
# SimulationReport — resultaten van één volledige simulatie
# ---------------------------------------------------------------------------

@dataclass
class NodeReport:
    """
    Resultaten van één node na afloop van de simulatie.

    node_id:        Node-identifier.
    mission_id:     Bijbehorende mission_id.
    ticks_run:      Totaal aantal verwerkte ticks (inclusief stale).
    trades_closed:  Totaal aantal gesloten trades.
    total_pnl:      Gesommeerde PnL over alle gesloten trades.
    capital_start:  Startkapitaal (= mission.capital_limit).
    capital_end:    Eindkapitaal na simulatie.
    tick_results:   Lijst van alle TickResult objecten.
    """
    node_id: str
    mission_id: str
    ticks_run: int
    trades_closed: int
    total_pnl: float
    capital_start: float
    capital_end: float
    tick_results: list[TickResult] = field(default_factory=list)

    @property
    def return_pct(self) -> float | None:
        """Rendement als percentage van startkapitaal. None als startkapitaal 0 is."""
        if self.capital_start == 0:
            return None
        return (self.total_pnl / self.capital_start) * 100.0


@dataclass
class SimulationReport:
    """
    Geaggregeerd rapport van een volledige MultiNodeSimulator-run.

    nodes:          Rapport per node, gesorteerd op node_id.
    total_ticks:    Aantal ticks waarvoor de simulatie is gedraaid.
    stale_ticks:    Aantal overgeslagen ticks (prijs ≤ 0).
    """
    nodes: list[NodeReport]
    total_ticks: int
    stale_ticks: int

    @property
    def active_nodes(self) -> int:
        """Aantal nodes met ten minste één verwerkte tick."""
        return len(self.nodes)

    @property
    def total_trades(self) -> int:
        """Totaal gesloten trades over alle nodes."""
        return sum(r.trades_closed for r in self.nodes)

    @property
    def total_pnl(self) -> float:
        """Gesommeerde PnL over alle nodes."""
        return sum(r.total_pnl for r in self.nodes)

    def node(self, node_id: str) -> NodeReport | None:
        """Zoek het rapport voor één node. None als niet aanwezig."""
        for r in self.nodes:
            if r.node_id == node_id:
                return r
        return None


# ---------------------------------------------------------------------------
# MultiNodeSimulator
# ---------------------------------------------------------------------------

class MultiNodeSimulator:
    """
    Paper-mode simulatie van meerdere nodes tegelijk.

    De simulator ontvangt een lijst (node_id, mission) paren. Voor elke node
    bouwt hij een PaperLoop met eigen ledger, broker en evaluator. De Queen
    valideert en registreert elke mission; de simulator start pas als alle
    missions geaccepteerd zijn.

    Args:
        queen:  Queen-instantie met geregistreerde nodes.

    Usage::

        sim = MultiNodeSimulator(queen=queen)
        sim.add_node("pc2-desktop", mission_a)
        sim.add_node("pc3-server",  mission_b)

        prices = [30_000.0, 30_500.0, 29_800.0, ...]
        signals = {"pc2-desktop": [signal_a, None, signal_a, ...]}

        report = sim.run(prices, signals)
        print(report.total_pnl, report.total_trades)
    """

    def __init__(self, queen: Queen) -> None:
        self._queen = queen
        self._slots: dict[str, NodeSlot] = {}

    # ------------------------------------------------------------------
    # Node configuratie
    # ------------------------------------------------------------------

    def add_node(self, node_id: str, mission: Mission) -> None:
        """
        Voeg een node toe aan de simulatie.

        De Queen valideert en registreert de mission. Bij afwijzing wordt
        een ValueError gegooid — add_node is een configuratiestap, geen
        runtime operatie.

        Args:
            node_id:  Node-identifier — moet geregistreerd zijn in de Queen.
            mission:  Mission voor deze node.

        Raises:
            ValueError: Als de Queen de mission afwijst.
        """
        result = self._queen.issue_mission(mission)
        if not result.accepted:
            raise ValueError(
                f"Queen rejected mission for node='{node_id}': "
                f"{result.rejection_reason} — {result.rejection_detail}"
            )

        ledger = PaperLedger(mission=mission, logs_root=None)
        broker = PaperBroker(mission=mission)
        evaluator = ExitEvaluator(conditions=[
            StopLossCondition(),
            TakeProfitCondition(),
            TTLCondition(),
            DrawdownCondition(
                max_drawdown_pct=mission.risk_limits.max_drawdown_pct
            ),
        ])
        loop = PaperLoop(
            mission=mission,
            broker=broker,
            ledger=ledger,
            evaluator=evaluator,
            logs_root=None,
        )
        self._slots[node_id] = NodeSlot(
            node_id=node_id,
            mission=mission,
            loop=loop,
        )
        logger.debug(
            "MultiNodeSimulator: node_id='%s' mission_id='%s' added",
            node_id, mission.mission_id,
        )

    @property
    def node_count(self) -> int:
        """Aantal geconfigureerde nodes."""
        return len(self._slots)

    # ------------------------------------------------------------------
    # Simulatie
    # ------------------------------------------------------------------

    def run(
        self,
        prices: list[float],
        signals: dict[str, list[EntrySignal | None]] | None = None,
    ) -> SimulationReport:
        """
        Voer de simulatie uit over de opgegeven prijsreeks.

        Eén iteratie = één prijs → alle nodes verwerken dezelfde prijs.
        Elk element van `signals[node_id]` correspondeert met de gelijknamige
        tick. Ontbrekende of te korte signaallijsten worden aangevuld met None.

        Stale prijs (≤ 0): alle nodes slaan deze tick over.

        Args:
            prices:   Lijst van prijzen (één per tick).
            signals:  Optionele mapping node_id → signaallijst (zelfde lengte
                      als prices of korter — ontbrekende posities zijn None).

        Returns:
            SimulationReport met resultaten per node.
        """
        if signals is None:
            signals = {}

        total_ticks = len(prices)
        stale_ticks = 0

        # tick_results[node_id] = lijst van TickResult
        tick_results: dict[str, list[TickResult]] = {
            node_id: [] for node_id in self._slots
        }

        for tick_idx, price in enumerate(prices):
            if price <= 0:
                stale_ticks += 1
                logger.warning(
                    "MultiNodeSimulator: tick %d stale price %.4f — all nodes skipped",
                    tick_idx + 1, price,
                )
                continue

            for node_id, slot in self._slots.items():
                node_signals = signals.get(node_id, [])
                signal = (
                    node_signals[tick_idx]
                    if tick_idx < len(node_signals)
                    else None
                )
                result = slot.loop.tick(price=price, signal=signal)
                tick_results[node_id].append(result)

        # Bouw NodeReports
        node_reports: list[NodeReport] = []
        for node_id in sorted(self._slots.keys()):
            slot = self._slots[node_id]
            ledger = slot.loop._ledger
            results = tick_results[node_id]

            total_pnl = sum(
                t.closed_position.realized_pnl() or 0.0
                for t in results
                if t.exit_occurred and t.closed_position is not None
            )
            node_reports.append(NodeReport(
                node_id=node_id,
                mission_id=slot.mission.mission_id,
                ticks_run=len(results),
                trades_closed=ledger.trade_count,
                total_pnl=total_pnl,
                capital_start=slot.mission.capital_limit,
                capital_end=ledger.capital_available,
                tick_results=results,
            ))

        logger.info(
            "MultiNodeSimulator: run complete — %d ticks, %d stale, %d nodes",
            total_ticks, stale_ticks, len(node_reports),
        )
        return SimulationReport(
            nodes=node_reports,
            total_ticks=total_ticks,
            stale_ticks=stale_ticks,
        )
