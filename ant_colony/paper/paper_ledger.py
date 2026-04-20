"""
ant_colony/paper/paper_ledger.py

PaperLedger — bijhoudt posities, kapitaal, PnL en dagverlies in paper mode.

Verantwoordelijkheden:
  - Open posities registreren en volgen
  - Gesloten trades registreren en opslaan
  - Beschikbaar kapitaal berekenen
  - Dagelijks verlies bijhouden (voor DailyLossCondition)
  - Gesloten trades append-only loggen naar ANT_LOGS

Kapitaallogica:
  capital_total     = mission.capital_limit  (vast)
  capital_in_use    = som(entry_price * quantity) over open posities
  capital_available = capital_total - capital_in_use

Dagverlies:
  - Berekend over gesloten trades op de huidige UTC-dag
  - Alleen negatieve PnL telt mee (absolute waarde)
  - Reset automatisch bij nieuwe kalenderdag (UTC)

Log:
  - Elke gesloten trade → append-only regel in
    ANT_LOGS\\paper\\{mission_id}_trades.jsonl
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.exit_chain.exit_conditions import CheckContext
from ant_colony.exit_chain.position import PaperPosition, PositionStatus
from ant_colony.schemas.mission import Mission

logger = logging.getLogger(__name__)

BROKER_FEE_PCT = 0.0025  # 0.25% Bitvavo maker/taker tarief per kant


class PaperLedger:
    """
    Stateful register van alle paper trades voor één mission.

    Args:
        mission:    Actieve Mission — bepaalt kapitaalgrens.
        logs_root:  Root van ANT_LOGS (bijv. Path(r'C:\\Trading\\ANT_LOGS')).
                    Als None: geen logging naar disk (handig voor tests).

    Usage::

        ledger = PaperLedger(mission=mission, logs_root=Path(r'C:\\Trading\\ANT_LOGS'))
        ledger.record_opened(position)
        # ... na exit:
        ledger.record_closed(closed_position)
        ctx = ledger.make_check_context()  # voor DailyLossCondition
    """

    def __init__(self, mission: Mission, logs_root: Path | None = None) -> None:
        self._mission = mission
        self._logs_root = logs_root
        self._open: dict[str, PaperPosition] = {}
        self._closed: list[PaperPosition] = []

    # ------------------------------------------------------------------
    # Posities registreren
    # ------------------------------------------------------------------

    def record_opened(self, position: PaperPosition) -> None:
        """Registreer een nieuw geopende positie."""
        if position.position_id in self._open:
            logger.warning(
                "Duplicate open: position_id=%s already registered", position.position_id
            )
            return
        self._open[position.position_id] = position
        logger.debug(
            "Opened %s %s qty=%.8f entry=%.4f",
            position.side.value, position.symbol,
            position.quantity, position.entry_price,
        )

    def record_closed(self, position: PaperPosition) -> None:
        """
        Registreer een gesloten positie.

        Verplaatst de positie van open naar closed en schrijft naar log.
        Negeert posities die niet in de open-lijst staan.
        """
        if position.is_open():
            logger.error(
                "record_closed called with open position %s — ignoring",
                position.position_id,
            )
            return

        self._open.pop(position.position_id, None)
        self._closed.append(position)

        pnl = self._net_pnl(position)
        logger.debug(
            "Closed %s %s via %s  pnl_net=%.4f",
            position.side.value, position.symbol,
            position.status.value, pnl,
        )
        self._append_trade_log(position)

    def update_open(self, position: PaperPosition) -> None:
        """
        Update de state van een open positie (bijv. na prijsupdate door evaluator).

        Gebruikt door PaperLoop na elke evaluate()-aanroep.
        """
        if position.position_id not in self._open:
            logger.warning(
                "update_open: position_id=%s not in open positions", position.position_id
            )
            return
        self._open[position.position_id] = position

    # ------------------------------------------------------------------
    # PnL helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _net_pnl(position: PaperPosition) -> float:
        """Netto PnL na aftrek van brokerkosten (beide kanten)."""
        gross = position.realized_pnl() or 0.0
        if position.exit_price is None:
            return gross
        fee = (position.entry_price + position.exit_price) * position.quantity * BROKER_FEE_PCT
        return gross - fee

    # ------------------------------------------------------------------
    # Kapitaal
    # ------------------------------------------------------------------

    @property
    def capital_total(self) -> float:
        return self._mission.capital_limit

    @property
    def capital_in_use(self) -> float:
        """Geblokkeerd kapitaal over alle open posities (entry_price * quantity)."""
        return sum(p.entry_price * p.quantity for p in self._open.values())

    @property
    def capital_available(self) -> float:
        """Vrij te gebruiken kapitaal."""
        return max(0.0, self.capital_total - self.capital_in_use)

    # ------------------------------------------------------------------
    # Dagverlies
    # ------------------------------------------------------------------

    def daily_loss_so_far(self, trading_day: date | None = None) -> float:
        """
        Totaal gerealiseerd verlies op de opgegeven handelsdag (UTC).

        Alleen negatieve PnL telt mee. Retourneert een positief getal.
        Bij geen verlies: 0.0.
        """
        if trading_day is None:
            trading_day = datetime.now(timezone.utc).date()

        total_loss = 0.0
        for trade in self._closed:
            if trade.closed_at is None:
                continue
            if trade.closed_at.astimezone(timezone.utc).date() != trading_day:
                continue
            pnl = self._net_pnl(trade)
            if pnl < 0:
                total_loss += abs(pnl)
        return total_loss

    def make_check_context(self, trading_day: date | None = None) -> CheckContext:
        """Maak een CheckContext met het huidige dagverlies — klaar voor de evaluator."""
        return CheckContext(daily_loss_so_far=self.daily_loss_so_far(trading_day))

    # ------------------------------------------------------------------
    # Statistieken
    # ------------------------------------------------------------------

    @property
    def open_positions(self) -> list[PaperPosition]:
        return list(self._open.values())

    @property
    def closed_trades(self) -> list[PaperPosition]:
        return list(self._closed)

    @property
    def trade_count(self) -> int:
        """Aantal gesloten trades."""
        return len(self._closed)

    @property
    def total_realized_pnl(self) -> float:
        """Som van alle gerealiseerde netto PnL (na brokerkosten) over gesloten trades."""
        return sum(self._net_pnl(t) for t in self._closed)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self._closed if self._net_pnl(t) > 0)

    @property
    def loss_count(self) -> int:
        return sum(1 for t in self._closed if self._net_pnl(t) <= 0)

    @property
    def win_rate(self) -> float | None:
        """Winstpercentage (0–1). None als er nog geen trades zijn."""
        if self.trade_count == 0:
            return None
        return self.win_count / self.trade_count

    def exit_breakdown(self) -> dict[str, int]:
        """Aantal gesloten trades per exit-reden."""
        counts: dict[str, int] = {}
        for trade in self._closed:
            key = trade.status.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def summary(self) -> dict:
        """Compact overzicht — handig voor logging en gate-bewijs."""
        return {
            "trade_count": self.trade_count,
            "open_count": len(self._open),
            "total_realized_pnl": round(self.total_realized_pnl, 4),
            "win_rate": round(self.win_rate, 4) if self.win_rate is not None else None,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "capital_total": self.capital_total,
            "capital_available": round(self.capital_available, 4),
            "capital_in_use": round(self.capital_in_use, 4),
            "daily_loss_today": round(self.daily_loss_so_far(), 4),
            "exit_breakdown": self.exit_breakdown(),
        }

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _append_trade_log(self, position: PaperPosition) -> None:
        if self._logs_root is None:
            return
        log_path = self._logs_root / "paper" / f"{self._mission.mission_id}_trades.jsonl"
        gross_pnl = position.realized_pnl()
        net_pnl   = self._net_pnl(position)
        fee_cost  = round((gross_pnl or 0.0) - net_pnl, 6)
        record = {
            "position_id":       position.position_id,
            "symbol":            position.symbol,
            "side":              position.side.value,
            "status":            position.status.value,
            "entry_price":       position.entry_price,
            "exit_price":        position.exit_price,
            "quantity":          position.quantity,
            "realized_pnl_gross": gross_pnl,
            "broker_fee_cost":   fee_cost,
            "realized_pnl":      net_pnl,
            "opened_at":         position.opened_at.isoformat() if position.opened_at else None,
            "closed_at":         position.closed_at.isoformat() if position.closed_at else None,
            "exit_reason":       position.exit_reason,
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.exception("Failed to write trade log: %s", log_path)
