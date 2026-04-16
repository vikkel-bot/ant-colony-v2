"""
ant_colony/paper/paper_loop.py

PaperLoop — volledige paper trading tick-loop.

Verbindt entry (EntrySignal + PaperBroker) met exit (ExitEvaluator)
via de PaperLedger. Eén tick = één marktprijsupdate.

Volgorde per tick:
  1. Valideer prijs — stale prijs stopt verwerking
  2. Evalueer alle open posities via ExitEvaluator
  3. Sluit getriggerde posities via PaperLedger
  4. Als geen open positie én signal aanwezig: probeer te openen via PaperBroker
  5. Log tick-samenvatting (append-only)

Regels:
  - Maximaal één open positie tegelijk (P8: één markt, één strategie)
  - Loop genereert geen signalen — ontvangt ze van buiten
  - Loop heeft geen eigen kapitaal of mission authority
  - Stale prijs (≤ 0): tick wordt overgeslagen, geen state-wijziging
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.entry.entry_signal import EntrySignal
from ant_colony.exit_chain.exit_evaluator import ExitEvaluator
from ant_colony.exit_chain.position import PaperPosition
from ant_colony.paper.paper_broker import BrokerResult, PaperBroker
from ant_colony.paper.paper_ledger import PaperLedger
from ant_colony.schemas.mission import Mission

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tick-resultaat
# ---------------------------------------------------------------------------

@dataclass
class TickResult:
    """
    Samenvatting van één tick.

    tick_number:      Oplopend ticknummer (1-based)
    price:            Marktprijs waarmee geëvalueerd is
    stale_price:      True als prijs ongeldig was — tick overgeslagen
    exit_occurred:    True als een positie werd gesloten
    entry_occurred:   True als een nieuwe positie werd geopend
    closed_position:  Gesloten positie (of None)
    opened_position:  Geopende positie (of None)
    broker_result:    Resultaat van brokerpoging (of None als niet geprobeerd)
    """
    tick_number: int
    price: float
    stale_price: bool = False
    exit_occurred: bool = False
    entry_occurred: bool = False
    closed_position: PaperPosition | None = None
    opened_position: PaperPosition | None = None
    broker_result: BrokerResult | None = None


# ---------------------------------------------------------------------------
# PaperLoop
# ---------------------------------------------------------------------------

class PaperLoop:
    """
    Volledige paper trading tick-loop.

    Args:
        mission:    Actieve Mission.
        broker:     PaperBroker voor positie-opening.
        ledger:     PaperLedger voor kapitaal- en trade-tracking.
        evaluator:  ExitEvaluator met geconfigureerde exit-condities.
        logs_root:  Root van ANT_LOGS. None = geen tick-logging (tests).

    Usage::

        loop = PaperLoop(mission, broker, ledger, evaluator, logs_root)
        result = loop.tick(price=31_000.0, signal=signal)
        if result.exit_occurred:
            print('Positie gesloten:', result.closed_position.status)
    """

    def __init__(
        self,
        mission: Mission,
        broker: PaperBroker,
        ledger: PaperLedger,
        evaluator: ExitEvaluator,
        logs_root: Path | None = None,
    ) -> None:
        self._mission = mission
        self._broker = broker
        self._ledger = ledger
        self._evaluator = evaluator
        self._logs_root = logs_root
        self._tick_number: int = 0

    # ------------------------------------------------------------------
    # Publieke API
    # ------------------------------------------------------------------

    @property
    def tick_number(self) -> int:
        return self._tick_number

    def tick(
        self,
        price: float,
        signal: EntrySignal | None = None,
        now: datetime | None = None,
    ) -> TickResult:
        """
        Verwerk één marktprijsupdate.

        Args:
            price:   Huidige marktprijs.
            signal:  Optioneel EntrySignal van een agent.
            now:     Huidig tijdstip (injecteerbaar voor tests).

        Returns:
            TickResult met wat er in deze tick is gebeurd.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        self._tick_number += 1
        result = TickResult(tick_number=self._tick_number, price=price)

        # --- stap 1: valideer prijs ---
        if price <= 0:
            result.stale_price = True
            logger.warning("Tick %d: stale price %.4f — skipped", self._tick_number, price)
            self._log_tick(result)
            return result

        # --- stap 2 + 3: evalueer open posities ---
        ctx = self._ledger.make_check_context(now.date())
        for position in list(self._ledger.open_positions):
            eval_result = self._evaluator.evaluate(position, new_price=price, context=ctx)

            if eval_result.stale_price:
                continue

            if eval_result.exit_occurred:
                self._ledger.record_closed(eval_result.position)
                result.exit_occurred = True
                result.closed_position = eval_result.position
                logger.info(
                    "Tick %d: closed %s %s via %s  pnl=%.4f",
                    self._tick_number,
                    eval_result.position.side.value,
                    eval_result.position.symbol,
                    eval_result.position.status.value,
                    eval_result.position.realized_pnl() or 0.0,
                )
            else:
                self._ledger.update_open(eval_result.position)

        # --- stap 4: entry poging ---
        if signal is not None and len(self._ledger.open_positions) == 0:
            broker_result = self._broker.open_position(
                signal,
                capital_available=self._ledger.capital_available,
                now=now,
            )
            result.broker_result = broker_result

            if broker_result.accepted and broker_result.position is not None:
                self._ledger.record_opened(broker_result.position)
                result.entry_occurred = True
                result.opened_position = broker_result.position
                logger.info(
                    "Tick %d: opened %s %s qty=%.8f entry=%.4f",
                    self._tick_number,
                    broker_result.position.side.value,
                    broker_result.position.symbol,
                    broker_result.position.quantity,
                    broker_result.position.entry_price,
                )
            else:
                logger.debug(
                    "Tick %d: signal rejected — %s",
                    self._tick_number,
                    broker_result.rejection_reason,
                )
        elif signal is not None and len(self._ledger.open_positions) > 0:
            logger.debug(
                "Tick %d: signal ignored — position already open", self._tick_number
            )

        self._log_tick(result)
        return result

    # ------------------------------------------------------------------
    # Intern — logging
    # ------------------------------------------------------------------

    def _log_tick(self, result: TickResult) -> None:
        if self._logs_root is None:
            return
        log_path = (
            self._logs_root / "paper" / f"{self._mission.mission_id}_ticks.jsonl"
        )
        record = {
            "tick": result.tick_number,
            "price": result.price,
            "stale": result.stale_price,
            "exit": result.exit_occurred,
            "entry": result.entry_occurred,
            "closed_status": (
                result.closed_position.status.value if result.closed_position else None
            ),
            "closed_pnl": (
                result.closed_position.realized_pnl()
                if result.closed_position else None
            ),
            "opened_id": (
                result.opened_position.position_id if result.opened_position else None
            ),
            "broker_rejected": (
                result.broker_result.rejection_reason.value
                if result.broker_result and not result.broker_result.accepted
                else None
            ),
            "open_count": len(self._ledger.open_positions),
            "trade_count": self._ledger.trade_count,
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write tick log: %s", log_path)
