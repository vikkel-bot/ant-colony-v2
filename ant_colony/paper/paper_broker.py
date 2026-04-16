"""
ant_colony/paper/paper_broker.py

PaperBroker — simuleert order-acceptatie en positie-opening in paper mode.

Verantwoordelijkheden:
  1. Signal valideren (niet verlopen, stop-loss verplicht, kapitaal beschikbaar)
  2. Positiegrootte berekenen op basis van risicolimieten
  3. PaperPosition aanmaken bij acceptatie
  4. BrokerResult teruggeven — altijd, nooit een exception gooien bij zakelijk bezwaar

Stateless:
  - Broker heeft geen eigen staat
  - Beschikbaar kapitaal wordt meegegeven door de aanroeper (PaperLedger)
  - Mission-limieten worden bij elke aanroep opnieuw gecontroleerd

Positiegrootteberekening:
  - Als signal.suggested_quantity aanwezig is: gebruik dat als startpunt
  - Anders: vul volledig tot max_position_size
  - Altijd gecapped op: min(max_position_size, capital_available) / entry_price
  - Minimum: positie moet > 0 zijn na afronding

Paper fill:
  - Fill-prijs = signal.entry_price (geen slippage in paper mode)
  - peak_price wordt gelijkgesteld aan entry_price bij opening
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from ant_colony.entry.entry_signal import EntrySignal
from ant_colony.exit_chain.position import PaperPosition, PositionSide
from ant_colony.schemas.mission import Mission


class RejectionReason(str, Enum):
    SIGNAL_EXPIRED = "signal_expired"
    STOP_LOSS_MISSING = "stop_loss_missing"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    POSITION_SIZE_ZERO = "position_size_zero"
    CAPITAL_LIMIT_ZERO = "capital_limit_zero"
    SYMBOL_NOT_IN_SCOPE = "symbol_not_in_scope"


@dataclass
class BrokerResult:
    """
    Resultaat van een open_position()-aanroep.

    accepted:          True als de positie geopend is
    position:          Geopende PaperPosition, of None bij afwijzing
    rejection_reason:  Reden van afwijzing, of None bij acceptatie
    quantity:          Berekende positiegrootte (ook bij afwijzing beschikbaar)
    """
    accepted: bool
    position: PaperPosition | None = None
    rejection_reason: RejectionReason | None = None
    rejection_detail: str = ""
    quantity: float = 0.0

    @classmethod
    def rejected(
        cls,
        reason: RejectionReason,
        detail: str = "",
        quantity: float = 0.0,
    ) -> BrokerResult:
        return cls(
            accepted=False,
            rejection_reason=reason,
            rejection_detail=detail,
            quantity=quantity,
        )


class PaperBroker:
    """
    Simuleert order-acceptatie in paper mode.

    Args:
        mission: Actieve Mission — bepaalt risicolimieten en kapitaalgrens.

    Usage::

        broker = PaperBroker(mission=mission)
        result = broker.open_position(signal, capital_available=1000.0)
        if result.accepted:
            position = result.position
    """

    def __init__(self, mission: Mission) -> None:
        self._mission = mission

    # ------------------------------------------------------------------
    # Publieke API
    # ------------------------------------------------------------------

    def open_position(
        self,
        signal: EntrySignal,
        capital_available: float,
        now: datetime | None = None,
    ) -> BrokerResult:
        """
        Probeer een positie te openen op basis van een EntrySignal.

        Valideert het signal, berekent positiegrootte en maakt een
        PaperPosition aan. Gooit nooit een exception voor zakelijke
        afwijzingen — retourneert altijd een BrokerResult.

        Args:
            signal:            Het EntrySignal van de agent
            capital_available: Beschikbaar kapitaal op dit moment (van PaperLedger)
            now:               Huidig tijdstip (injecteerbaar voor tests)
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # --- validaties ---
        rejection = self._validate(signal, capital_available, now)
        if rejection is not None:
            return rejection

        # --- positiegrootte berekenen ---
        quantity = self._calculate_quantity(signal, capital_available)
        if quantity <= 0:
            return BrokerResult.rejected(
                RejectionReason.POSITION_SIZE_ZERO,
                detail=f"calculated quantity={quantity}",
                quantity=quantity,
            )

        # --- positie aanmaken ---
        position = PaperPosition(
            position_id=str(uuid.uuid4()),
            symbol=signal.symbol,
            biome=signal.biome,
            mission_id=signal.mission_id,
            ant_id=signal.ant_id,
            side=PositionSide(signal.side),
            entry_price=signal.entry_price,
            quantity=quantity,
            stop_loss_price=signal.stop_loss_price,
            take_profit_price=signal.take_profit_price,
            ttl=self._mission.ttl,
            current_price=signal.entry_price,
            peak_price=signal.entry_price,
            opened_at=now,
        )

        return BrokerResult(accepted=True, position=position, quantity=quantity)

    # ------------------------------------------------------------------
    # Intern — validatie
    # ------------------------------------------------------------------

    def _validate(
        self,
        signal: EntrySignal,
        capital_available: float,
        now: datetime,
    ) -> BrokerResult | None:
        """Retourneert een BrokerResult bij afwijzing, of None als alles ok is."""

        if signal.is_expired(now):
            return BrokerResult.rejected(
                RejectionReason.SIGNAL_EXPIRED,
                detail=f"valid_until={signal.valid_until} now={now}",
            )

        if self._mission.risk_limits.stop_loss_required and signal.stop_loss_price <= 0:
            return BrokerResult.rejected(
                RejectionReason.STOP_LOSS_MISSING,
                detail="stop_loss_required=True but stop_loss_price missing",
            )

        if self._mission.capital_limit <= 0:
            return BrokerResult.rejected(
                RejectionReason.CAPITAL_LIMIT_ZERO,
                detail=f"mission capital_limit={self._mission.capital_limit}",
            )

        if capital_available <= 0:
            return BrokerResult.rejected(
                RejectionReason.INSUFFICIENT_CAPITAL,
                detail=f"capital_available={capital_available:.2f}",
            )

        if signal.symbol not in self._mission.market_scope.symbols:
            return BrokerResult.rejected(
                RejectionReason.SYMBOL_NOT_IN_SCOPE,
                detail=(
                    f"symbol={signal.symbol} not in "
                    f"mission scope={self._mission.market_scope.symbols}"
                ),
            )

        return None

    # ------------------------------------------------------------------
    # Intern — positiegrootteberekening
    # ------------------------------------------------------------------

    def _calculate_quantity(
        self, signal: EntrySignal, capital_available: float
    ) -> float:
        """
        Bereken de positiegrootte.

        Logica:
          1. Startpunt: suggested_quantity of max_position_size / entry_price
          2. Cap op beschikbaar kapitaal: capital_available / entry_price
          3. Cap op mission capital_limit: capital_limit / entry_price
          4. Resultaat afgerond op 8 decimalen (crypto-precisie)
        """
        max_by_risk = self._mission.risk_limits.max_position_size / signal.entry_price
        max_by_capital = min(capital_available, self._mission.capital_limit) / signal.entry_price

        if signal.suggested_quantity is not None:
            quantity = min(signal.suggested_quantity, max_by_risk, max_by_capital)
        else:
            quantity = min(max_by_risk, max_by_capital)

        return round(quantity, 8)
