"""
ant_colony/execution/paper_gate.py

PaperExecutionGate — papier-mode tegenhanger van LiveExecutionGate.

Zelfde vijf pre-order checks als LiveExecutionGate. In plaats van een
echte adapter aan te roepen simuleert de gate een directe fill:
  - MARKET order → fill-prijs = market_data.close
  - LIMIT  order → fill-prijs = order.limit_price

De adapter-check (check 5) is vereenvoudigd: paper mode beschouwt de
"exchange" als altijd beschikbaar (geen externe verbinding).

Retourneert altijd OrderResult — nooit None. None is enkel voorbehouden
aan echte adapter-fouten (zie LiveExecutionGate).

Audit log:
  - Elke aanroep van execute() wordt gelogd naar
    ANT_LOGS/paper_execution/{order.order_id}.jsonl

Regels:
  - execute() gooit nooit (P2)
  - Geen code wordt uitgevoerd bij import (P7)
  - Zelfde contract als LiveExecutionGate — uitwisselbaar in tests
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path

from ant_colony.biome.biome_adapter import MarketData
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, ColonyStatus
from ant_colony.queen.queen import Queen
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.order import LiveOrder, OrderRejectionReason, OrderResult, OrderType

logger = logging.getLogger(__name__)


class PaperExecutionGate:
    """
    Paper-mode tegenhanger van LiveExecutionGate.

    Simuleert orderuitvoering zonder een echte exchange. Volgt hetzelfde
    contract als LiveExecutionGate zodat tests en agents uitwisselbaar zijn.

    Args:
        queen:               Queen-instantie — voor mission- en kapitaalcontrole.
        scheduler:           ColonyScheduler — voor kolonie-status controle.
        logs_root:           Root van ANT_LOGS. None = geen disk-logging (tests).
        market_data_max_age: Maximale leeftijd van marktdata in seconden (standaard 300).

    Usage::

        gate = PaperExecutionGate(queen=queen, scheduler=scheduler)
        result = gate.execute(order, market_data=data)
        if result.accepted:
            print("Paper fill:", result.avg_price)
    """

    def __init__(
        self,
        queen: Queen,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
        market_data_max_age: float = 300.0,
    ) -> None:
        self._queen = queen
        self._scheduler = scheduler
        self._logs_root = logs_root
        self._market_data_max_age = market_data_max_age
        self._log_sequence: int = 0

    # ------------------------------------------------------------------
    # Publieke API
    # ------------------------------------------------------------------

    def execute(
        self,
        order: LiveOrder,
        market_data: MarketData | None = None,
    ) -> OrderResult:
        """
        Simuleer de uitvoering van een live order in paper mode.

        Voert vier pre-order checks uit (kolonie, mission, kapitaal,
        marktdata). Check 5 (adapter beschikbaar) is altijd groen in
        paper mode. Bij alle checks groen wordt een gesimuleerde fill
        teruggegeven met een synthetisch exchange_order_id.

        Args:
            order:       Gevalideerd LiveOrder van de execution_ant.
            market_data: Meest recente MarketData voor het symbool.
                         Verplicht voor de staleness-check. None → afgewezen.

        Returns:
            OrderResult — altijd (nooit None).
        """
        # --- check 1: kolonie niet HALTED ---
        if self._scheduler.status == ColonyStatus.HALTED:
            result = OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.COLONY_HALTED,
                detail="colony status is HALTED",
            )
            self._log_execution(order, result)
            return result

        # --- check 2: mission actief ---
        if order.mission_id not in self._queen.active_missions:
            result = OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.MISSION_NOT_ACTIVE,
                detail=f"mission_id='{order.mission_id}' not in active missions",
            )
            self._log_execution(order, result)
            return result

        # --- check 3: kapitaal niet overschreden ---
        mission = self._queen.active_missions[order.mission_id]
        if mission.capital_limit <= 0:
            result = OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.CAPITAL_LIMIT_BREACHED,
                detail=f"mission capital_limit={mission.capital_limit:.2f} <= 0",
            )
            self._log_execution(order, result)
            return result

        # --- check 4: marktdata niet stale ---
        if market_data is None or market_data.is_stale(self._market_data_max_age):
            detail = (
                "market_data is None"
                if market_data is None
                else f"market_data age exceeds {self._market_data_max_age}s"
            )
            result = OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.MARKET_DATA_STALE,
                detail=detail,
            )
            self._log_execution(order, result)
            return result

        # --- alle checks groen: simuleer fill ---
        fill_price = (
            order.limit_price
            if order.order_type == OrderType.LIMIT and order.limit_price is not None
            else market_data.close
        )
        exchange_order_id = f"paper-{uuid.uuid4().hex[:12]}"
        result = OrderResult.accepted_result(
            order_id=order.order_id,
            exchange_order_id=exchange_order_id,
            filled_quantity=order.quantity,
            avg_price=fill_price,
        )

        logger.info(
            "PaperExecutionGate: simulated fill order_id='%s' symbol='%s' side=%s "
            "qty=%s fill_price=%.4f",
            order.order_id, order.symbol, order.side.value,
            order.quantity, fill_price,
        )
        self._log_execution(order, result)
        return result

    # ------------------------------------------------------------------
    # Intern — logging
    # ------------------------------------------------------------------

    def _next_sequence(self) -> int:
        self._log_sequence += 1
        return self._log_sequence

    def _log_execution(
        self,
        order: LiveOrder,
        result: OrderResult,
    ) -> None:
        if self._logs_root is None:
            return

        payload: dict = {
            "order_id": order.order_id,
            "mission_id": order.mission_id,
            "ant_id": order.ant_id,
            "symbol": order.symbol,
            "biome": order.biome,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "quantity": order.quantity,
        }

        if result.accepted:
            payload["outcome"] = "accepted_paper"
            payload["exchange_order_id"] = result.exchange_order_id
            payload["filled_quantity"] = result.filled_quantity
            payload["avg_price"] = result.avg_price
            event_type = AuditEventType.ACTION_EXECUTED
        else:
            payload["outcome"] = "rejected"
            payload["rejection_reason"] = result.rejection_reason.value if result.rejection_reason else None
            payload["rejection_detail"] = result.rejection_detail
            event_type = AuditEventType.RISK_BREACH

        event = AuditEvent(
            event_type=event_type,
            source="paper_execution_gate",
            mission_id=order.mission_id,
            payload=payload,
            sequence=self._next_sequence(),
        )

        log_path = self._logs_root / "paper_execution" / f"{order.order_id}.jsonl"
        self._append_to_log(log_path, event.model_dump(mode="json"))

    def _append_to_log(self, path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write paper execution log: %s", path)
