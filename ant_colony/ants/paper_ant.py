"""
ant_colony/ants/paper_ant.py

PaperAnt — voert paper trades uit op basis van OpportunitySignals van ScoutAnt.

Verantwoordelijkheden:
  1. Luistert naar OpportunitySignals in ANT_LOGS/scouts/*.jsonl
     - Alleen signalen met confidence > drempel (standaard 0.6)
     - Dedupliceert op signal_id om dubbele verwerking te voorkomen
  2. Per nieuw signaal een paper positie openen via PaperBroker (LONG):
     - stop_loss  = entry_price × 0.98
     - take_profit = entry_price × 1.03
     - Capital per trade: 10% van beschikbaar kapitaal
     - Maximaal één open positie per symbool tegelijk
  3. Open posities evalueren via ExitEvaluator (exit-first doctrine P3):
     - Haalt live prijs op via BiomeAdapter
     - Sluit bij TP / SL / TTL hit
  4. Events loggen naar ANT_LOGS/paper/{ant_id}.jsonl:
     - trade_opened, trade_closed, pnl_summary
  5. Heartbeat rapporteren aan scheduler na elke tick
  6. Zichzelf netjes beëindigen bij TTL expiry

Regels:
  - Plaatst geen live orders — uitsluitend paper (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Exit-logica vóór entry-logica in elke tick (P3)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.entry.entry_signal import EntrySignal, SignalSource
from ant_colony.exit_chain.exit_conditions import (
    CheckContext,
    StopLossCondition,
    TakeProfitCondition,
    TTLCondition,
)
from ant_colony.exit_chain.exit_evaluator import ExitEvaluator
from ant_colony.paper.paper_broker import PaperBroker
from ant_colony.paper.paper_ledger import PaperLedger
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_TRADE_CAPITAL_FRACTION = 0.10   # 10% van beschikbaar kapitaal per trade
_SL_PCT  = 0.02                  # 2% stop-loss onder entry
_TP_PCT  = 0.03                  # 3% take-profit boven entry
_SIGNAL_VALIDITY_TICKS = 2       # signal geldig voor heartbeat_interval × 2 seconden


class PaperAnt:
    """
    Vertaalt OpportunitySignals van ScoutAnt naar paper trades.

    Args:
        ant_id:               Unieke identifier (UUID-string).
        mission:              Toegewezen Mission. capital_limit > 0 vereist voor trades.
        scheduler:            ColonyScheduler voor heartbeat-registratie.
        biome_registry:       BiomeRegistry voor live-prijzen via de adapter.
        logs_root:            Pad naar ANT_LOGS. None = geen disk-logging.
        confidence_threshold: Minimale confidence voor signaalverwerking (standaard 0.6).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.ant_id              = ant_id
        self.mission             = mission
        self.scheduler           = scheduler
        self.biome_registry      = biome_registry
        self.logs_root           = logs_root
        self._confidence_threshold = confidence_threshold

        self._broker    = PaperBroker(mission)
        self._ledger    = PaperLedger(mission, logs_root)
        self._evaluator = ExitEvaluator([
            StopLossCondition(),
            TakeProfitCondition(),
            TTLCondition(),
        ])

        self._processed_signals: set[str] = set()
        self._seen_approved_ids: set[str] = set()

        self._status: AntStatus = AntStatus.IDLE
        self._budget_used: float = 0.0
        self._last_action: str = "init"
        self._log_seq: int = 0

        self._log = logging.getLogger(f"ant.paper.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Blokkerende tick-loop. Retourneert AntStatus bij afsluiting.

        Elke tick:
          1. TTL controleren
          2. Exits verwerken (exit-first P3)
          3. Nieuwe signalen lezen en posities openen
          4. Heartbeat sturen
          5. Wachten tot volgende tick
        """
        self._status = AntStatus.RUNNING
        self._log.info(
            "PaperAnt gestart | mission=%s ttl=%ds capital=%.2f",
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
                        "TTL verlopen (%.1fs / %ds) — afsluiten", elapsed, self.mission.ttl
                    )
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("PaperAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._emit_pnl_summary()
            self._send_heartbeat()
            self._log.info("PaperAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """
        Één handelscyclus.

        Volgorde (P3: exit vóór entry):
          1. Evalueer alle open posities op exit-condities
          2. Verwerk nieuwe scout-signalen en open eventueel nieuwe posities
        """
        self._process_exits()
        self._process_new_signals()
        self._process_approved_candidates()
        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Exit verwerking (P3: eerst)
    # ------------------------------------------------------------------

    def _process_exits(self) -> None:
        """Evalueer alle open posities; sluit bij TP/SL/TTL hit."""
        context = self._ledger.make_check_context()

        for position in list(self._ledger.open_positions):
            price = self._fetch_price(position.symbol)
            if price is None:
                self._log.debug("Geen prijs voor %s — positie niet geüpdate", position.symbol)
                continue

            try:
                result = self._evaluator.evaluate(position, price, context)
            except Exception:
                self._log.exception("ExitEvaluator fout voor positie %s", position.position_id)
                continue

            if result.position.is_open():
                self._ledger.update_open(result.position)
            else:
                self._ledger.record_closed(result.position)
                self._emit_trade_closed(result.position)
                self._last_action = f"trade_closed:{result.position.symbol}"

    # ------------------------------------------------------------------
    # Signaalverwerking en entry
    # ------------------------------------------------------------------

    def _process_new_signals(self) -> None:
        """Verwerk nieuwe scout-signalen en open posities indien van toepassing."""
        signals = self._read_new_scout_signals()
        for sig in signals:
            confidence = sig.get("confidence", 0.0)
            if confidence <= self._confidence_threshold:
                self._log.debug(
                    "Signaal %s overgeslagen — confidence %.2f <= %.2f",
                    sig.get("signal_id"), confidence, self._confidence_threshold,
                )
                continue

            symbol = sig.get("symbol", "")
            if not symbol:
                continue

            # Vroeg afkappen — _try_open_position blokkeert het ook, maar dit bespaart werk
            if self._has_open_position(symbol):
                self._log.debug("Al een open positie voor %s — signaal overgeslagen", symbol)
                continue

            # Sla downward PRICE_MOVE over (we gaan alleen long)
            change_pct = sig.get("change_pct", 0.0)
            signal_type = sig.get("signal_type", "")
            if signal_type == "price_move" and change_pct < 0:
                self._log.debug("Negatieve price_move voor %s — overgeslagen", symbol)
                continue

            self._try_open_position(sig)

    def _has_open_position(self, symbol: str) -> bool:
        """True als er al een open positie is voor dit symbool."""
        return any(p.symbol == symbol for p in self._ledger.open_positions)

    def _try_open_position(self, sig: dict) -> None:
        """Bouw een EntrySignal en probeer een LONG positie te openen via PaperBroker."""
        symbol      = sig.get("symbol", "")
        entry_price = sig.get("current_price", 0.0)

        if not symbol or entry_price <= 0:
            return

        # Definitieve guard — blokkeert duplicaten ongeacht aanroeppad
        if self._has_open_position(symbol):
            self._log.debug(
                "_try_open_position: al open positie voor %s — geblokkeerd", symbol
            )
            return

        sl = entry_price * (1.0 - _SL_PCT)
        tp = entry_price * (1.0 + _TP_PCT)

        capital_available = self._ledger.capital_available
        capital_per_trade = capital_available * _TRADE_CAPITAL_FRACTION
        suggested_qty     = capital_per_trade / entry_price if entry_price > 0 else None

        try:
            entry_signal = EntrySignal(
                symbol=symbol,
                biome=sig.get("biome", self.mission.market_scope.biome),
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                source=SignalSource.SCOUT_ANT,
                side="long",
                entry_price=entry_price,
                stop_loss_price=sl,
                take_profit_price=tp,
                suggested_quantity=suggested_qty,
                confidence=sig.get("confidence"),
                valid_until=(
                    datetime.now(tz=timezone.utc)
                    + timedelta(seconds=self.mission.heartbeat_interval * _SIGNAL_VALIDITY_TICKS)
                ),
            )
        except Exception:
            self._log.exception("Kan EntrySignal niet aanmaken voor %s @ %.4f", symbol, entry_price)
            return

        result = self._broker.open_position(entry_signal, capital_available)

        if result.accepted and result.position is not None:
            self._ledger.record_opened(result.position)
            self._emit_trade_opened(result.position, sig)
            self._last_action = f"trade_opened:{symbol}"
            self._log.info(
                "POSITIE GEOPEND | %s LONG %.8f @ %.4f  SL=%.4f  TP=%.4f",
                symbol, result.position.quantity, entry_price, sl, tp,
            )
        else:
            self._log.debug(
                "Positie geweigerd voor %s — %s: %s",
                symbol, result.rejection_reason, result.rejection_detail,
            )

    # ------------------------------------------------------------------
    # Approved-kandidaten verwerken
    # ------------------------------------------------------------------

    def _process_approved_candidates(self) -> None:
        """Verwerk APPROVED StrategyCandidate records uit ANT_LOGS/approved/*.jsonl."""
        if self.logs_root is None:
            return

        approved_dir = self.logs_root / "approved"
        if not approved_dir.exists():
            return

        for jsonl_path in sorted(approved_dir.glob("*.jsonl")):
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    candidate_id = str(record.get("candidate_id") or "")
                    if not candidate_id or candidate_id in self._seen_approved_ids:
                        continue
                    self._seen_approved_ids.add(candidate_id)

                    self._open_from_candidate(record)

            except OSError:
                self._log.warning("Kan approved-log niet lezen: %s", jsonl_path)

    def _open_from_candidate(self, record: dict) -> None:
        """Open een paper positie op basis van een APPROVED StrategyCandidate record."""
        market_scope = record.get("market_scope") or {}
        # Ondersteunt zowel "symbol": "BTC-EUR" als "symbols": ["BTC-EUR", ...]
        raw_symbol = market_scope.get("symbol") or ""
        if not raw_symbol:
            symbols_list = market_scope.get("symbols") or []
            raw_symbol = symbols_list[0] if symbols_list else ""
        symbol = str(raw_symbol)
        if not symbol:
            return

        parameters  = record.get("parameters") or {}
        entry_cond  = record.get("entry_conditions") or {}
        direction   = str(
            entry_cond.get("direction")
            or parameters.get("direction")
            or "long"
        )

        # Alleen long-posities (consistent met bestaande PaperAnt doctrine)
        if direction != "long":
            self._log.debug(
                "Approved candidate %s heeft direction=%s — overgeslagen",
                record.get("candidate_id"), direction,
            )
            return

        price = self._fetch_price(symbol)
        if price is None or price <= 0:
            self._log.debug("Geen prijs beschikbaar voor approved candidate %s", symbol)
            return

        # Sla over als er al een open positie is voor dit symbool
        if any(p.symbol == symbol for p in self._ledger.open_positions):
            self._log.debug("Al een open positie voor %s — approved candidate overgeslagen", symbol)
            return

        sig = {
            "symbol":        symbol,
            "current_price": price,
            "confidence":    float(record.get("fitness_score") or 0.7),
            "biome":         str(record.get("biome") or self.mission.market_scope.biome),
        }
        self._try_open_position(sig)

    # ------------------------------------------------------------------
    # Scout-signalen lezen
    # ------------------------------------------------------------------

    def _read_new_scout_signals(self) -> list[dict]:
        """
        Scan ANT_LOGS/scouts/*.jsonl voor nieuwe OpportunitySignal-records.

        Selecteert regels met payload.action == "opportunity_detected" die
        nog niet verwerkt zijn (signal_id niet in _processed_signals).

        Retourneert een lijst van payload-dicts.
        """
        if self.logs_root is None:
            return []

        scout_dir = self.logs_root / "scouts"
        if not scout_dir.exists():
            return []

        new_signals: list[dict] = []

        for jsonl_path in sorted(scout_dir.glob("*.jsonl")):
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    if payload.get("action") != "opportunity_detected":
                        continue

                    signal_id = str(payload.get("signal_id") or "")
                    if not signal_id:
                        continue
                    if signal_id in self._processed_signals:
                        continue

                    self._processed_signals.add(signal_id)
                    new_signals.append(payload)

            except OSError:
                self._log.warning("Kan scout-log niet lezen: %s", jsonl_path)

        return new_signals

    # ------------------------------------------------------------------
    # Live prijs ophalen
    # ------------------------------------------------------------------

    def _fetch_price(self, symbol: str) -> float | None:
        """Haal de actuele marktprijs op via de BiomeAdapter (fail-closed)."""
        try:
            adapter = self.biome_registry.get(self.mission.market_scope.biome)
            if adapter is None or not adapter.is_available():
                return None
            md = adapter.get_market_data(symbol, "1m")
            if md is None or not md.is_valid_price or md.is_stale():
                return None
            return md.close
        except Exception:
            self._log.exception("Fout bij ophalen prijs voor %s", symbol)
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_trade_opened(self, position, signal_data: dict) -> None:
        """Log een trade_opened event naar ANT_LOGS/paper/{ant_id}.jsonl."""
        self._write_log({
            "action":        "trade_opened",
            "position_id":   position.position_id,
            "symbol":        position.symbol,
            "side":          position.side.value,
            "entry_price":   position.entry_price,
            "quantity":      position.quantity,
            "stop_loss":     position.stop_loss_price,
            "take_profit":   position.take_profit_price,
            "from_signal_id": signal_data.get("signal_id"),
            "confidence":    signal_data.get("confidence"),
        })

    def _emit_trade_closed(self, position) -> None:
        """Log een trade_closed event met PnL."""
        pnl = position.realized_pnl() or 0.0
        entry_value = position.entry_price * position.quantity
        pnl_pct = round(pnl / entry_value * 100, 4) if entry_value > 0 else 0.0

        self._write_log({
            "action":       "trade_closed",
            "position_id":  position.position_id,
            "symbol":       position.symbol,
            "side":         position.side.value,
            "entry_price":  position.entry_price,
            "exit_price":   position.exit_price,
            "quantity":     position.quantity,
            "exit_reason":  position.exit_reason,
            "realized_pnl": round(pnl, 4),
            "pnl_pct":      pnl_pct,
        })
        self._log.info(
            "POSITIE GESLOTEN | %s via %s  pnl=%.4f (%.2f%%)",
            position.symbol, position.exit_reason, pnl, pnl_pct,
        )

    def _emit_pnl_summary(self) -> None:
        """Log een samenvatting van alle trades bij afsluiting."""
        summary = self._ledger.summary()
        self._write_log({"action": "pnl_summary", **summary})

    def _write_log(self, payload: dict) -> None:
        """Schrijf een AuditEvent met gegeven payload naar ANT_LOGS/paper/{ant_id}.jsonl."""
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

        log_path = self.logs_root / "paper" / f"{self.ant_id}.jsonl"
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
                budget_used=self._budget_used,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
            self._log.debug("Heartbeat gestuurd | action=%s", self._last_action)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
