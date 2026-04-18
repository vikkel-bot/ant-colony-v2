"""
ant_colony/ants/execution_ant.py

ExecutionAnt — de enige agent die live orders naar de exchange mag sturen.
Mag NOOIT starten zonder expliciete Queen-goedkeuring via mission.allowed_actions.

Verantwoordelijkheden:
  1. Startupvalidatie (fail-closed, ABORTED bij elke mislukking):
       - "live_execute" in mission.allowed_actions
       - capital_limit > 0
       - LiveExecutionGate niet None
  2. PaperAnt-resultaten evalueren:
       - Leest ANT_LOGS/paper/*.jsonl voor pnl_summary records
       - Sessie-criteria: win_rate > 0.55, trade_count >= 10, total_realized_pnl > 0
       - Per-symbool analyse: minimaal 3 trades, win_rate > 0.55
  3. Live order plaatsen voor het best scorende symbool:
       - Maximaal 1 open positie tegelijk
       - SL en TP verplicht bij elk order
       - Via LiveExecutionGate (alle 5 veiligheidschecks)
  4. Open positie monitoren via BiomeAdapter.get_positions():
       - Zodra symbool verdwijnt uit positions → positie gesloten → loggen
  5. Elke actie loggen naar ANT_LOGS/execution/{ant_id}.jsonl
  6. Heartbeat rapporteren aan scheduler na elke tick
  7. TTL expiry stopt de ant — open posities worden NIET automatisch gesloten

Strikte regels:
  - Plaatst NOOIT een order zonder "live_execute" in allowed_actions (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Monitor vóór entry in elke tick (exit-first P3)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.execution.live_gate import LiveExecutionGate
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.order import LiveOrder, OrderResult, OrderSide, OrderType

_LIVE_EXECUTE_ACTION = "live_execute"

_MIN_WIN_RATE        = 0.55   # minimale win-rate voor een paper-sessie
_MIN_PAPER_TRADES    = 10     # minimaal aantal trades in een paper-sessie
_MIN_SYMBOL_TRADES   = 3      # minimaal aantal trades per symbool
_CAPITAL_FRACTION    = 0.10   # 10% van mission.capital_limit per order
_SL_PCT              = 0.02   # 2% stop-loss onder entry
_TP_PCT              = 0.03   # 3% take-profit boven entry


class ExecutionAnt:
    """
    Plaatst live orders via LiveExecutionGate op basis van bewezen paper-resultaten.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission. Vereist "live_execute" in allowed_actions
                         en capital_limit > 0.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        gate:            LiveExecutionGate — de enige doorgang naar de exchange.
                         None is expliciet verboden: ant weigert dan te starten.
        biome_registry:  BiomeRegistry voor live-prijzen en positie-snapshots.
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        gate: LiveExecutionGate | None,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id          = ant_id
        self.mission         = mission
        self.scheduler       = scheduler
        self._gate           = gate
        self.biome_registry  = biome_registry
        self.logs_root       = logs_root

        self._open_order: LiveOrder | None = None
        self._log_seq: int   = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str  = "init"

        self._log = logging.getLogger(f"ant.execution.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "execution"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log.info("Logs map: %s", log_dir)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Valideert startup, daarna blokkerende tick-loop.
        Retourneert AntStatus bij afsluiting.
        """
        if not self._validate_startup():
            self._send_heartbeat()
            return self._status

        self._status = AntStatus.RUNNING
        self._log.info(
            "ExecutionAnt gestart | mission=%s ttl=%ds capital=%.2f",
            self.mission.mission_id,
            self.mission.ttl,
            self.mission.capital_limit,
        )

        started_at     = datetime.now(tz=timezone.utc)
        last_heartbeat = started_at

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info(
                        "TTL verlopen (%.1fs / %ds) — afsluiten. "
                        "Open posities worden NIET automatisch gesloten.",
                        elapsed, self.mission.ttl,
                    )
                    if self._open_order is not None:
                        self._emit_log({
                            "action":   "ttl_expired_with_open_order",
                            "symbol":   self._open_order.symbol,
                            "order_id": self._open_order.order_id,
                            "warning":  "Operator actie vereist — positie staat nog open",
                        })
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("ExecutionAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("ExecutionAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Startupvalidatie
    # ------------------------------------------------------------------

    def _validate_startup(self) -> bool:
        """
        Controleert alle verplichte precondities. Fail-closed: ABORTED bij elke mislukking.
        Retourneert True alleen als alle drie checks groen zijn.
        """
        if _LIVE_EXECUTE_ACTION not in self.mission.allowed_actions:
            reason = (
                f"'live_execute' ontbreekt in mission.allowed_actions "
                f"({self.mission.allowed_actions})"
            )
            self._log.error("Startup geweigerd — %s", reason)
            self._emit_log({"action": "startup_rejected", "reason": reason})
            self._status = AntStatus.ABORTED
            return False

        if self.mission.capital_limit <= 0:
            reason = f"capital_limit={self.mission.capital_limit} — moet > 0 zijn voor live execution"
            self._log.error("Startup geweigerd — %s", reason)
            self._emit_log({"action": "startup_rejected", "reason": reason})
            self._status = AntStatus.ABORTED
            return False

        if self._gate is None:
            reason = "LiveExecutionGate is None — geen doorgang naar exchange"
            self._log.error("Startup geweigerd — %s", reason)
            self._emit_log({"action": "startup_rejected", "reason": reason})
            self._status = AntStatus.ABORTED
            return False

        self._log.info(
            "Startup geslaagd — live_execute=OK  capital=%.2f  gate=OK",
            self.mission.capital_limit,
        )
        return True

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """
        Één handelscyclus.

        Volgorde (P3: monitor vóór entry):
          1. Bestaande open positie monitoren
          2. Nieuwe positie openen als er geen open positie is
        """
        self._monitor_open_order()
        if self._open_order is None:
            self._try_open_position()
        self._last_action = self._last_action if self._last_action != "tick" else "tick"
        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Positiemonitoring (P3: eerst)
    # ------------------------------------------------------------------

    def _monitor_open_order(self) -> None:
        """
        Controleert of de open positie nog bestaat bij de exchange.
        Zodra het symbool verdwijnt uit get_positions() → positie gesloten.
        Open posities worden NIET automatisch gesloten door de ant.
        """
        if self._open_order is None:
            return

        adapter = self._get_adapter()
        if adapter is None:
            self._log.debug("Adapter niet beschikbaar — positiestatus onbekend")
            return

        try:
            positions = adapter.get_positions()
        except Exception:
            self._log.exception("get_positions() mislukt — positiestatus onbekend")
            return

        if positions is None:
            self._log.debug("get_positions() → None — positiestatus onbekend")
            return

        open_symbols = {p.symbol for p in positions}
        if self._open_order.symbol not in open_symbols:
            self._log.info(
                "POSITIE GESLOTEN (exchange) | %s  order_id=%s",
                self._open_order.symbol,
                self._open_order.order_id,
            )
            self._emit_log({
                "action":   "position_closed",
                "symbol":   self._open_order.symbol,
                "order_id": self._open_order.order_id,
            })
            self._last_action = f"position_closed:{self._open_order.symbol}"
            self._open_order = None

    # ------------------------------------------------------------------
    # Order plaatsen
    # ------------------------------------------------------------------

    def _try_open_position(self) -> None:
        """
        Leest paper-resultaten, kiest het best scorende symbool en
        plaatst een live BUY order via de gate.
        """
        candidates = self._read_paper_candidates()
        if not candidates:
            self._log.debug("Geen geschikte paper-kandidaten gevonden")
            return

        for candidate in candidates:
            symbol = candidate["symbol"]

            if symbol not in self.mission.market_scope.symbols:
                self._log.debug("Symbool %s niet in mission scope — overgeslagen", symbol)
                continue

            md = self._fetch_market_data(symbol)
            if md is None or not md.is_valid_price or md.is_stale():
                self._log.debug(
                    "Geen verse marktdata voor %s — overgeslagen", symbol
                )
                continue

            order = self._build_order(symbol, md.close)
            if order is None:
                continue

            result = self._gate.execute(order, market_data=md)
            self._emit_order_log(order, result)

            if result is not None and result.accepted:
                self._open_order = order
                self._last_action = f"order_placed:{symbol}"
                self._log.info(
                    "LIVE ORDER GEPLAATST | %s BUY %.8f @ %.4f  SL=%.4f  TP=%.4f",
                    symbol,
                    order.quantity,
                    md.close,
                    order.stop_loss_price,
                    order.take_profit_price,
                )
                break
            else:
                rejection = result.rejection_reason.value if (result and result.rejection_reason) else "adapter_error"
                self._log.warning(
                    "Order geweigerd voor %s — %s", symbol, rejection
                )

    def _build_order(self, symbol: str, price: float) -> LiveOrder | None:
        """Bouw een LiveOrder voor een LONG (BUY) positie."""
        try:
            sl       = price * (1.0 - _SL_PCT)
            tp       = price * (1.0 + _TP_PCT)
            qty      = (self.mission.capital_limit * _CAPITAL_FRACTION) / price
            return LiveOrder(
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                symbol=symbol,
                biome=self.mission.market_scope.biome,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=round(qty, 8),
                stop_loss_price=sl,
                take_profit_price=tp,
            )
        except Exception:
            self._log.exception("Kan LiveOrder niet bouwen voor %s @ %.4f", symbol, price)
            return None

    # ------------------------------------------------------------------
    # Paper-resultaten lezen
    # ------------------------------------------------------------------

    def _read_paper_candidates(self) -> list[dict]:
        """
        Scan ANT_LOGS/paper/*.jsonl voor bewezen paper-sessies.

        Sessie-criteria:
          - pnl_summary aanwezig
          - win_rate > 0.55
          - trade_count >= 10
          - total_realized_pnl > 0

        Per symbool (min 3 trades, win_rate > 0.55) — gesorteerd op win_rate desc.
        Retourneert een lijst van dicts met: symbol, trade_count, win_rate, pnl.
        """
        if self.logs_root is None:
            return []

        paper_dir = self.logs_root / "paper"
        if not paper_dir.exists():
            return []

        all_candidates: list[dict] = []

        for jsonl_path in sorted(paper_dir.glob("*.jsonl")):
            if "_trades" in jsonl_path.name:
                continue
            try:
                candidates = self._analyze_paper_file(jsonl_path)
                all_candidates.extend(candidates)
            except Exception:
                self._log.exception("Kan paper-bestand niet analyseren: %s", jsonl_path)

        all_candidates.sort(key=lambda c: c["win_rate"], reverse=True)
        return all_candidates

    def _analyze_paper_file(self, path: Path) -> list[dict]:
        """
        Analyseer één paper JSONL-bestand.

        Retourneert lijst van kwalificerende symbolen als de sessie-criteria passen.
        """
        pnl_summary: dict | None = None
        symbol_stats: dict[str, dict] = {}

        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record  = json.loads(line)
                    payload = record.get("payload") or {}
                except json.JSONDecodeError:
                    continue

                action = payload.get("action", "")

                if action == "pnl_summary":
                    pnl_summary = payload  # gebruik het laatste exemplaar

                elif action == "trade_closed":
                    symbol = payload.get("symbol", "")
                    pnl    = float(payload.get("realized_pnl") or 0.0)
                    if symbol:
                        if symbol not in symbol_stats:
                            symbol_stats[symbol] = {"wins": 0, "losses": 0, "pnl": 0.0}
                        symbol_stats[symbol]["pnl"] += pnl
                        if pnl > 0:
                            symbol_stats[symbol]["wins"] += 1
                        else:
                            symbol_stats[symbol]["losses"] += 1

        except OSError:
            self._log.warning("Kan paper-log niet lezen: %s", path)
            return []

        if pnl_summary is None:
            return []

        session_win_rate   = float(pnl_summary.get("win_rate")            or 0.0)
        session_trades     = int(pnl_summary.get("trade_count")           or 0)
        session_pnl        = float(pnl_summary.get("total_realized_pnl")  or 0.0)

        if session_win_rate <= _MIN_WIN_RATE:
            return []
        if session_trades < _MIN_PAPER_TRADES:
            return []
        if session_pnl <= 0:
            return []

        candidates: list[dict] = []
        for symbol, stats in symbol_stats.items():
            total = stats["wins"] + stats["losses"]
            if total < _MIN_SYMBOL_TRADES:
                continue
            sym_win_rate = stats["wins"] / total
            if sym_win_rate > _MIN_WIN_RATE:
                candidates.append({
                    "symbol":      symbol,
                    "trade_count": total,
                    "win_rate":    sym_win_rate,
                    "pnl":         stats["pnl"],
                })

        return candidates

    # ------------------------------------------------------------------
    # Marktdata
    # ------------------------------------------------------------------

    def _get_adapter(self):
        """Haal de adapter op via BiomeRegistry (fail-closed: None bij fout)."""
        try:
            adapter = self.biome_registry.get(self.mission.market_scope.biome)
            if adapter is None or not adapter.is_available():
                return None
            return adapter
        except Exception:
            self._log.exception("BiomeRegistry.get() mislukt")
            return None

    def _fetch_market_data(self, symbol: str):
        """Haal verse marktdata op voor het opgegeven symbool (fail-closed)."""
        adapter = self._get_adapter()
        if adapter is None:
            return None
        try:
            return adapter.get_market_data(symbol, "1m")
        except Exception:
            self._log.exception("get_market_data(%s) mislukt", symbol)
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_order_log(self, order: LiveOrder, result: OrderResult | None) -> None:
        """Log het resultaat van een gate.execute()-aanroep."""
        payload: dict = {
            "action":    "order_attempted",
            "order_id":  order.order_id,
            "symbol":    order.symbol,
            "side":      order.side.value,
            "quantity":  order.quantity,
            "sl":        order.stop_loss_price,
            "tp":        order.take_profit_price,
        }
        if result is None:
            payload["outcome"] = "adapter_error"
        elif result.accepted:
            payload["outcome"]           = "accepted"
            payload["exchange_order_id"] = result.exchange_order_id
            payload["avg_price"]         = result.avg_price
        else:
            payload["outcome"]           = "rejected"
            payload["rejection_reason"]  = (
                result.rejection_reason.value if result.rejection_reason else None
            )
            payload["rejection_detail"]  = result.rejection_detail
        self._emit_log(payload)

    def _emit_log(self, payload: dict) -> None:
        """Schrijf een AuditEvent met gegeven payload naar ANT_LOGS/execution/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload=payload,
        )
        self._log_seq += 1

        log_path = self.logs_root / "execution" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon event niet naar disk schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
        """Stuur heartbeat naar de scheduler (fail-closed: negeert fouten)."""
        try:
            hb_status = (
                HeartbeatStatus.RUNNING
                if self._status == AntStatus.RUNNING
                else HeartbeatStatus.PAUSED
            )
            hb = Heartbeat(
                ant_id=self.ant_id,
                mission_id=self.mission.mission_id,
                node_id=self.mission.allowed_node,
                status=hb_status,
                budget_used=0.0,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
