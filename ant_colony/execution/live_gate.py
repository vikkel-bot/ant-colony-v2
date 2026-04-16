"""
ant_colony/execution/live_gate.py

LiveExecutionGate — enige doorgang voor live orders naar de exchange.

De gate staat tussen de execution_ant en de adapter. Geen enkel order
bereikt de exchange zonder door alle vijf checks te gaan. Fail-closed
op elk punt: een onzekere check blokkeert het order (P2).

Vijf pre-order checks (in volgorde):
  1. Kolonie niet HALTED (via ColonyScheduler.status)
  2. Mission actief (in Queen._active_missions)
  3. Kapitaal niet overschreden (mission.capital_limit)
  4. Marktdata niet stale (MarketData.is_stale())
  5. Adapter beschikbaar (BiomeAdapter.is_available())

Bij alle checks groen → adapter.place_order(order).
Bij elke check rood  → OrderResult.rejected(...) met de juiste reden.

Audit log:
  - Elke aanroep van execute() wordt gelogd naar
    ANT_LOGS/execution/{order.order_id}.jsonl
  - Zowel acceptatie als afwijzing worden gelogd

Regels:
  - execute() gooit nooit — retourneert altijd OrderResult of None
  - None wordt alleen teruggegeven als de adapter None teruggeeft
    (adapter-niveau fout; alle gate-afwijzingen zijn OrderResult)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_adapter import BiomeAdapter, MarketData
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, ColonyStatus
from ant_colony.queen.queen import Queen
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.order import LiveOrder, OrderRejectionReason, OrderResult

logger = logging.getLogger(__name__)


class LiveExecutionGate:
    """
    Enige doorgang voor live orders naar de exchange.

    Voert vijf veiligheidscontroles uit vóór elk order:
      1. Kolonie niet HALTED
      2. Mission actief
      3. Kapitaal niet overschreden
      4. Marktdata niet stale
      5. Adapter beschikbaar

    Args:
        queen:       Queen-instantie — bron van actieve missions en kapitaalstatus.
        scheduler:   ColonyScheduler — voor kolonie-status controle.
        adapter:     BiomeAdapter — exchange-verbinding.
        logs_root:   Root van ANT_LOGS. None = geen disk-logging (tests).
        market_data_max_age: Maximale leeftijd van marktdata in seconden (standaard 300).

    Usage::

        gate = LiveExecutionGate(queen=queen, scheduler=scheduler, adapter=adapter)
        result = gate.execute(order, market_data=data)
        if result and result.accepted:
            print("Order geplaatst:", result.exchange_order_id)
    """

    def __init__(
        self,
        queen: Queen,
        scheduler: ColonyScheduler,
        adapter: BiomeAdapter,
        logs_root: Path | None = None,
        market_data_max_age: float = 300.0,
    ) -> None:
        self._queen = queen
        self._scheduler = scheduler
        self._adapter = adapter
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
    ) -> OrderResult | None:
        """
        Voer een live order uit via de adapter.

        Voert alle vijf pre-order checks uit. Bij falen wordt een
        OrderResult.rejected(...) teruggegeven — nooit een exception.
        Bij adapter-niveau fout (adapter retourneert None) wordt None
        doorgegeven.

        Args:
            order:       Gevalideerd LiveOrder van de execution_ant.
            market_data: Meest recente MarketData voor het symbool.
                         Verplicht voor de staleness-check (check 4).
                         None → order wordt afgewezen met MARKET_DATA_STALE.

        Returns:
            OrderResult — bij gate-afwijzing of exchange-respons.
            None        — alleen als de adapter zelf None teruggeeft.
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

        # --- check 5: adapter beschikbaar ---
        if not self._adapter.is_available():
            result = OrderResult.rejected(
                order_id=order.order_id,
                reason=OrderRejectionReason.ADAPTER_UNAVAILABLE,
                detail=f"adapter biome_id='{self._adapter.biome_id}' is not available",
            )
            self._log_execution(order, result)
            return result

        # --- alle checks groen: delegeer naar adapter ---
        logger.info(
            "LiveExecutionGate: placing order order_id='%s' symbol='%s' side=%s qty=%s",
            order.order_id, order.symbol, order.side.value, order.quantity,
        )
        result = self._adapter.place_order(order)
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
        result: OrderResult | None,
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

        if result is None:
            payload["outcome"] = "adapter_error"
            event_type = AuditEventType.ACTION_EXECUTED
        elif result.accepted:
            payload["outcome"] = "accepted"
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
            source="live_execution_gate",
            mission_id=order.mission_id,
            payload=payload,
            sequence=self._next_sequence(),
        )

        log_path = self._logs_root / "execution" / f"{order.order_id}.jsonl"
        self._append_to_log(log_path, event.model_dump(mode="json"))

    def _append_to_log(self, path: Path, record: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write execution log: %s", path)
