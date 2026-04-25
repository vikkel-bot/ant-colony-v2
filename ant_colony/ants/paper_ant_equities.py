"""
ant_colony/ants/paper_ant_equities.py

EquitiesPaperAnt — paper trading voor equities met trailing stop en handelsdag-TTL.

Verantwoordelijkheden:
  1. Leest signalen uit drie bronnen:
       - ANT_LOGS/scouts/*.jsonl          (SectorScoutAnt: biome=equities)
       - ANT_LOGS/equities/breakout/      (BreakoutAnt: breakout_signal)
       - ANT_LOGS/equities/dividend/      (DividendScoutAnt: dividend_candidate)
  2. Opent LONG paper posities (max 1 per symbool, max 10 tegelijk).
     Kapitaal per trade: 10% van beschikbaar kapitaal.
  3. Exit-logica (P3: exit vóór entry, elke tick):
       a. Trailing stop: sluit als current_price < peak_price * (1 - TRAILING_STOP_PCT)
       b. Harde SL: sluit als current_price < entry_price * (1 - HARD_SL_PCT)
       c. TTL noodrem: 10 handelsdagen (alleen tel markturen mee)
  4. Bijhoudt peak_price per positie; logt position_update events zodat
     peak_price hersteld kan worden na herstart.
  5. Ledger-herstel bij herstart: scan paper/*.jsonl op trade_opened (biome=equities)
     en position_update events. Geen zombie-drempel voor equities.

Regels:
  - Plaatst geen live orders (P1)
  - Gooit nooit een exception naar buiten (P2)
  - Exit-logica vóór entry-logica (P3)
  - Geen ICT Kill Zone filter — gebruikt _is_market_open() in plaats daarvan
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.exit_chain.position import PaperPosition, PositionSide, PositionStatus
from ant_colony.paper.paper_broker import PaperBroker
from ant_colony.paper.paper_ledger import BROKER_FEE_PCT, PaperLedger
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

_TRAILING_STOP_PCT      = 0.05    # 5% trailing stop onder peak_price
_HARD_SL_PCT            = 0.07    # 7% harde SL onder entry_price
_DUMMY_TP_MULTIPLIER    = 2.0     # dummy TP = entry * 2 (voldoet aan PaperPosition schema)
_TRADE_CAPITAL_FRACTION = 0.10    # 10% van beschikbaar kapitaal per trade
_MAX_OPEN_POSITIONS     = 10      # max open posities tegelijk

# NYSE / IBKR reguliere handelsuren in Amsterdam-tijd
_AMS_TZ             = ZoneInfo("Europe/Amsterdam")
_MARKET_OPEN_HOUR   = 15          # 15:30 AMS
_MARKET_OPEN_MIN    = 30
_MARKET_CLOSE_HOUR  = 22          # 22:00 AMS
_MARKET_CLOSE_MIN   = 0

_TRADING_SECONDS_PER_DAY = int(6.5 * 3600)           # 6h30 = 23400 s
_TTL_TRADING_DAYS        = 10
_TTL_TRADING_SECONDS     = _TTL_TRADING_DAYS * _TRADING_SECONDS_PER_DAY  # 234000 s

_CONFIDENCE_THRESHOLD    = 0.6


class EquitiesPaperAnt:
    """
    Paper trading agent voor equities.

    Args:
        ant_id:         Unieke identifier (UUID-string).
        mission:        Toegewezen Mission. biome='equities', capital_limit > 0.
        scheduler:      ColonyScheduler voor heartbeat-registratie.
        biome_registry: BiomeRegistry voor live-prijzen.
        logs_root:      Pad naar ANT_LOGS. None = geen disk-logging.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root

        self._broker  = PaperBroker(mission)
        self._ledger  = PaperLedger(mission, logs_root)

        self._status: AntStatus = AntStatus.IDLE
        self._budget_used: float = 0.0
        self._last_action: str = "init"
        self._log_seq: int = 0

        self._log = logging.getLogger(f"ant.eq_paper.{ant_id[:8]}")

        # Deduplicatie voor binnenkomende signalen
        self._seen_scout_ids:    set[str] = set()
        self._seen_breakout_ids: set[str] = set()
        self._seen_dividend_ids: set[str] = set()

        # Open posities: tracking voor dedup-guard (naast ledger)
        self._open_symbols: set[str] = set()

        # Bijhouden van handelstijd per positie voor TTL-noodrem
        # position_id → geaccumuleerde handelsseconden
        self._trading_seconds: dict[str, float] = {}

        # Peak_price per positie — wordt ook hersteld vanuit logs
        self._peak_prices: dict[str, float] = {}

        # Ledger-herstel bij herstart
        self._restore_from_logs()

        if self._open_symbols:
            self._log.info(
                "EquitiesPaperAnt herstart gedetecteerd — %d positie(s) hersteld in ledger",
                len(self._ledger.open_positions),
            )

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "EquitiesPaperAnt gestart | mission=%s ttl=%ds capital=%.2f",
            self.mission.mission_id,
            self.mission.ttl,
            self.mission.capital_limit,
        )

        started_at = datetime.now(tz=timezone.utc)
        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info(
                        "Mission TTL verlopen (%.1fs) — afsluiten", elapsed
                    )
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()
                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("EquitiesPaperAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._emit_pnl_summary()
            self._send_heartbeat()
            self._log.info("EquitiesPaperAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één handelscyclus (P3: exit vóór entry)."""
        in_market = self._is_market_open()
        self._process_exits(advance_trading_time=in_market)
        if in_market:
            self._process_scout_signals()
            self._process_breakout_signals()
            self._process_dividend_candidates()

        self._last_action = "tick"

    # ------------------------------------------------------------------
    # Markturen
    # ------------------------------------------------------------------

    def _is_market_open(self) -> bool:
        """True als NYSE open is (Mon-Fri 15:30-22:00 Amsterdam-tijd)."""
        now_ams = datetime.now(tz=_AMS_TZ)
        if now_ams.weekday() >= 5:   # zaterdag=5, zondag=6
            return False
        open_min  = _MARKET_OPEN_HOUR  * 60 + _MARKET_OPEN_MIN
        close_min = _MARKET_CLOSE_HOUR * 60 + _MARKET_CLOSE_MIN
        now_min   = now_ams.hour * 60 + now_ams.minute
        return open_min <= now_min < close_min

    # ------------------------------------------------------------------
    # Exit verwerking (P3: eerst)
    # ------------------------------------------------------------------

    def _process_exits(self, *, advance_trading_time: bool) -> None:
        """
        Evalueer alle open posities op trailing stop, harde SL en handelsdag-TTL.

        advance_trading_time: True als de markt open is — telt 1 seconde op bij
        alle open posities zodat de handelsdag-TTL correct bijgehouden wordt.
        """
        for position in list(self._ledger.open_positions):
            price = self._fetch_price(position.symbol)
            if price is None:
                if advance_trading_time:
                    self._trading_seconds[position.position_id] = (
                        self._trading_seconds.get(position.position_id, 0.0) + 1.0
                    )
                continue

            pos_id     = position.position_id
            entry      = position.entry_price
            peak       = self._peak_prices.get(pos_id, entry)

            # Update peak
            if price > peak:
                peak = price
                self._peak_prices[pos_id] = peak
                updated = position.model_copy(update={"peak_price": peak, "current_price": price})
                self._ledger.update_open(updated)
                self._emit_position_update(position, peak, price)
                position = updated

            # Accumuleer handelstijd
            if advance_trading_time:
                self._trading_seconds[pos_id] = (
                    self._trading_seconds.get(pos_id, 0.0) + 1.0
                )

            trading_secs = self._trading_seconds.get(pos_id, 0.0)

            # --- Exit-condities ---
            exit_type: str | None = None
            exit_price = price

            trailing_stop_price = peak * (1.0 - _TRAILING_STOP_PCT)
            hard_sl_price       = entry * (1.0 - _HARD_SL_PCT)

            if price < hard_sl_price:
                exit_type = "hard_stop_loss"
            elif price < trailing_stop_price:
                exit_type = "trailing_stop"
            elif trading_secs >= _TTL_TRADING_SECONDS:
                exit_type = "ttl_trading_days"

            if exit_type is not None:
                self._close_position(position, exit_price, exit_type)

    def _close_position(
        self, position: PaperPosition, exit_price: float, exit_type: str
    ) -> None:
        """Sluit een positie en log het event."""
        closed = position.model_copy(update={
            "status":     PositionStatus.CLOSED_STOP_LOSS
                          if exit_type in ("hard_stop_loss", "trailing_stop")
                          else PositionStatus.CLOSED_TTL,
            "exit_price": exit_price,
            "closed_at":  datetime.now(tz=timezone.utc),
            "exit_reason": exit_type,
        })
        self._open_symbols.discard(position.symbol)
        self._trading_seconds.pop(position.position_id, None)
        self._peak_prices.pop(position.position_id, None)
        self._ledger.record_closed(closed)
        self._emit_trade_closed(closed, exit_type)
        self._last_action = f"trade_closed:{position.symbol}"

        pnl = (exit_price - position.entry_price) * position.quantity
        self._log.info(
            "POSITIE GESLOTEN | %s via %s  exit=%.4f  pnl=%.4f",
            position.symbol, exit_type, exit_price, pnl,
        )

    # ------------------------------------------------------------------
    # Signaalverwerking
    # ------------------------------------------------------------------

    def _process_scout_signals(self) -> None:
        """Verwerk opportunity_detected signalen met biome=equities uit scouts/."""
        if self.logs_root is None:
            return
        scout_dir = self.logs_root / "scouts"
        if not scout_dir.exists():
            return

        for path in sorted(scout_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    if payload.get("action") != "opportunity_detected":
                        continue
                    if str(payload.get("biome") or "") != "equities":
                        continue

                    sig_id = str(payload.get("signal_id") or "")
                    if not sig_id or sig_id in self._seen_scout_ids:
                        continue
                    self._seen_scout_ids.add(sig_id)

                    confidence = float(payload.get("confidence") or 0.0)
                    if confidence < _CONFIDENCE_THRESHOLD:
                        continue

                    symbol = str(payload.get("symbol") or "")
                    if not symbol or symbol in self._open_symbols:
                        continue

                    self._try_open_position(symbol, source="sector_scout")

            except OSError:
                self._log.warning("Kan scout-log niet lezen: %s", path)

    def _process_breakout_signals(self) -> None:
        """Verwerk breakout_signal records uit equities/breakout/."""
        if self.logs_root is None:
            return
        breakout_dir = self.logs_root / "equities" / "breakout"
        if not breakout_dir.exists():
            return

        for path in sorted(breakout_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    if payload.get("action") != "breakout_signal":
                        continue

                    sig_id = str(
                        payload.get("candidate_id") or payload.get("signal_id") or ""
                    )
                    if not sig_id or sig_id in self._seen_breakout_ids:
                        continue
                    self._seen_breakout_ids.add(sig_id)

                    symbol = str(payload.get("symbol") or "")
                    if not symbol or symbol in self._open_symbols:
                        continue

                    self._try_open_position(symbol, source="breakout_ant")

            except OSError:
                self._log.warning("Kan breakout-log niet lezen: %s", path)

    def _process_dividend_candidates(self) -> None:
        """Verwerk dividend_candidate records uit equities/dividend/."""
        if self.logs_root is None:
            return
        dividend_dir = self.logs_root / "equities" / "dividend"
        if not dividend_dir.exists():
            return

        for path in sorted(dividend_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    if payload.get("action") != "dividend_candidate":
                        continue

                    sig_id = str(
                        payload.get("candidate_id") or payload.get("signal_id") or ""
                    )
                    if not sig_id or sig_id in self._seen_dividend_ids:
                        continue
                    self._seen_dividend_ids.add(sig_id)

                    symbol = str(payload.get("symbol") or "")
                    if not symbol or symbol in self._open_symbols:
                        continue

                    self._try_open_position(symbol, source="dividend_scout")

            except OSError:
                self._log.warning("Kan dividend-log niet lezen: %s", path)

    # ------------------------------------------------------------------
    # Positie openen
    # ------------------------------------------------------------------

    def _try_open_position(self, symbol: str, *, source: str) -> None:
        """Probeer een LONG positie te openen voor het gegeven symbool."""
        if symbol in self._open_symbols:
            return

        if len(self._ledger.open_positions) >= _MAX_OPEN_POSITIONS:
            self._log.debug(
                "Max posities bereikt (%d) — %s overgeslagen", _MAX_OPEN_POSITIONS, symbol
            )
            return

        price = self._fetch_price(symbol)
        if price is None or price <= 0:
            self._log.debug("Geen live prijs voor %s — positie niet geopend", symbol)
            return

        capital_available = self._ledger.capital_available
        capital_per_trade = capital_available * _TRADE_CAPITAL_FRACTION
        if capital_per_trade <= 0:
            self._log.debug("Geen kapitaal beschikbaar — %s overgeslagen", symbol)
            return

        quantity = capital_per_trade / price
        sl_price = price * (1.0 - _HARD_SL_PCT)
        tp_price = price * _DUMMY_TP_MULTIPLIER    # dummy — eigen exit-logica gebruikt trailing stop

        try:
            position = PaperPosition(
                position_id=uuid.uuid4().hex,
                symbol=symbol,
                biome="equities",
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                side=PositionSide.LONG,
                entry_price=price,
                quantity=quantity,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                ttl=self.mission.ttl,
                current_price=price,
                peak_price=price,
                opened_at=datetime.now(tz=timezone.utc),
            )
        except Exception:
            self._log.exception("Kan PaperPosition niet aanmaken voor %s", symbol)
            return

        self._open_symbols.add(symbol)
        self._peak_prices[position.position_id] = price
        self._trading_seconds[position.position_id] = 0.0
        self._ledger.record_opened(position)
        self._emit_trade_opened(position, source=source)
        self._last_action = f"trade_opened:{symbol}"

        self._log.info(
            "POSITIE GEOPEND | %s LONG %.6f @ %.4f  SL=%.4f  trailing=5%%  bron=%s",
            symbol, quantity, price, sl_price, source,
        )

    # ------------------------------------------------------------------
    # Live prijs ophalen
    # ------------------------------------------------------------------

    def _fetch_price(self, symbol: str) -> float | None:
        """Haal de actuele marktprijs op via BiomeAdapter (fail-closed)."""
        try:
            adapter = self.biome_registry.get("equities")
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
    # Ledger-herstel bij herstart
    # ------------------------------------------------------------------

    def _restore_from_logs(self) -> None:
        """
        Scan paper/*.jsonl voor trade_opened (biome=equities) en position_update events.

        Herstelt PaperPosition objecten in de ledger, peak_prices en trading_seconds.
        Geen zombie-drempel voor equities: posities kunnen weken open staan.
        """
        if self.logs_root is None:
            return
        paper_dir = self.logs_root / "paper"
        if not paper_dir.exists():
            return

        opened:         dict[str, dict]  = {}   # pos_id → payload
        opened_ts:      dict[str, str]   = {}   # pos_id → ISO timestamp
        peak_updates:   dict[str, float] = {}   # pos_id → laatste bekende peak_price
        trading_secs:   dict[str, float] = {}   # pos_id → geaccumuleerde handelsseconden
        closed_ids:     set[str]         = set()

        for path in paper_dir.glob("*.jsonl"):
            if "_trades" in path.name:
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    payload = record.get("payload") or {}
                    action  = payload.get("action")
                    pos_id  = str(payload.get("position_id") or "")
                    if not pos_id:
                        continue

                    if action == "trade_opened":
                        if str(payload.get("biome") or "") != "equities":
                            continue
                        sym = str(payload.get("symbol") or "")
                        if sym:
                            opened[pos_id]    = payload
                            opened_ts[pos_id] = str(record.get("timestamp") or "")

                    elif action == "trade_closed":
                        closed_ids.add(pos_id)

                    elif action == "position_update":
                        peak = payload.get("peak_price")
                        if peak is not None:
                            try:
                                peak_updates[pos_id] = float(peak)
                            except (TypeError, ValueError):
                                pass
                        tsecs = payload.get("trading_seconds")
                        if tsecs is not None:
                            try:
                                trading_secs[pos_id] = float(tsecs)
                            except (TypeError, ValueError):
                                pass

            except OSError:
                pass

        restored = 0
        for pos_id, payload in opened.items():
            if pos_id in closed_ids:
                continue

            pos = self._reconstruct_position(pos_id, payload, opened_ts.get(pos_id, ""))
            if pos is None:
                continue

            self._ledger.record_opened(pos)
            self._open_symbols.add(pos.symbol)
            self._peak_prices[pos_id] = peak_updates.get(pos_id, pos.entry_price)
            self._trading_seconds[pos_id] = trading_secs.get(
                pos_id,
                _estimate_trading_seconds_since(pos.opened_at),
            )
            restored += 1

        if restored:
            self._log.info("%d equities positie(s) hersteld in ledger", restored)

    def _reconstruct_position(
        self, pos_id: str, payload: dict, ts_str: str
    ) -> PaperPosition | None:
        """Reconstrueer een PaperPosition uit een trade_opened payload."""
        try:
            try:
                opened_at = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                opened_at = datetime.now(tz=timezone.utc)

            entry    = float(payload.get("entry_price") or 0)
            quantity = float(payload.get("quantity") or 0)
            sl       = float(payload.get("stop_loss") or 0)
            tp       = float(payload.get("take_profit") or 0)

            if entry <= 0 or quantity <= 0 or sl <= 0 or tp <= 0:
                self._log.warning(
                    "Onvolledige payload voor eq-positie %s — herstel overgeslagen", pos_id
                )
                return None

            return PaperPosition(
                position_id=pos_id,
                symbol=str(payload.get("symbol") or ""),
                biome="equities",
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                side=PositionSide.LONG,
                entry_price=entry,
                quantity=quantity,
                stop_loss_price=sl,
                take_profit_price=tp,
                ttl=self.mission.ttl,
                current_price=entry,
                peak_price=entry,
                opened_at=opened_at,
            )
        except Exception:
            self._log.warning(
                "Kan eq-positie %s niet reconstrueren — herstel overgeslagen", pos_id
            )
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_trade_opened(self, position: PaperPosition, *, source: str) -> None:
        """Log trade_opened event."""
        trailing_stop_price = position.entry_price * (1.0 - _TRAILING_STOP_PCT)
        self._write_log({
            "action":               "trade_opened",
            "position_id":          position.position_id,
            "symbol":               position.symbol,
            "biome":                position.biome,
            "side":                 position.side.value,
            "entry_price":          position.entry_price,
            "quantity":             position.quantity,
            "stop_loss":            position.stop_loss_price,
            "take_profit":          position.take_profit_price,
            "trailing_stop_price":  round(trailing_stop_price, 6),
            "highest_price":        position.entry_price,
            "hard_sl_pct":          _HARD_SL_PCT,
            "trailing_stop_pct":    _TRAILING_STOP_PCT,
            "source":               source,
        })

    def _emit_trade_closed(self, position: PaperPosition, exit_type: str) -> None:
        """Log trade_closed event met fee-gecorrigeerde PnL."""
        pnl_gross  = position.realized_pnl() or 0.0
        entry_fee  = position.entry_price * position.quantity * BROKER_FEE_PCT
        exit_fee   = (position.exit_price or 0.0) * position.quantity * BROKER_FEE_PCT
        fee_total  = round(entry_fee + exit_fee, 6)
        pnl_net    = round(pnl_gross - fee_total, 4)
        entry_val  = position.entry_price * position.quantity
        pnl_pct    = round(pnl_net / entry_val * 100, 4) if entry_val > 0 else 0.0

        trailing_stop_price = self._peak_prices.get(
            position.position_id, position.entry_price
        ) * (1.0 - _TRAILING_STOP_PCT)

        self._write_log({
            "action":               "trade_closed",
            "position_id":          position.position_id,
            "symbol":               position.symbol,
            "biome":                position.biome,
            "side":                 position.side.value,
            "entry_price":          position.entry_price,
            "exit_price":           position.exit_price,
            "quantity":             position.quantity,
            "exit_type":            exit_type,
            "exit_reason":          exit_type,
            "trailing_stop_price":  round(trailing_stop_price, 6),
            "broker_fee_cost":      fee_total,
            "realized_pnl_gross":   round(pnl_gross, 4),
            "realized_pnl":         pnl_net,
            "pnl_pct":              pnl_pct,
            "trading_seconds":      self._trading_seconds.get(position.position_id, 0.0),
        })
        self._log.info(
            "POSITIE GESLOTEN | %s via %s  pnl_gross=%.4f fee=%.4f pnl_net=%.4f (%.2f%%)",
            position.symbol, exit_type, pnl_gross, fee_total, pnl_net, pnl_pct,
        )

    def _emit_position_update(
        self, position: PaperPosition, new_peak: float, current_price: float
    ) -> None:
        """Log position_update event wanneer peak_price stijgt (voor restart-herstel)."""
        trailing_stop_price = new_peak * (1.0 - _TRAILING_STOP_PCT)
        self._write_log({
            "action":              "position_update",
            "position_id":         position.position_id,
            "symbol":              position.symbol,
            "biome":               position.biome,
            "peak_price":          round(new_peak, 6),
            "current_price":       round(current_price, 6),
            "trailing_stop_price": round(trailing_stop_price, 6),
            "trading_seconds":     self._trading_seconds.get(position.position_id, 0.0),
        })

    def _emit_pnl_summary(self) -> None:
        """Log samenvatting van alle trades bij afsluiting."""
        summary = self._ledger.summary()
        self._write_log({"action": "pnl_summary", **summary})

    def _write_log(self, payload: dict) -> None:
        """Schrijf AuditEvent naar ANT_LOGS/paper/{ant_id}.jsonl."""
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
            self._log.exception("Kon event niet schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
        """Stuur heartbeat naar scheduler (fail-closed)."""
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
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")


# ---------------------------------------------------------------------------
# Hulpfunctie — buiten de klasse (ook bruikbaar in tests)
# ---------------------------------------------------------------------------

def _estimate_trading_seconds_since(opened_at: datetime) -> float:
    """
    Schat het aantal geaccumuleerde handelsseconden sinds opened_at.

    Telt alleen Mon-Fri 15:30-22:00 Amsterdam-tijd mee.
    Gebruikt als fallback bij ledger-herstel als position_update events ontbreken.
    """
    if opened_at is None:
        return 0.0

    now = datetime.now(tz=timezone.utc)
    if opened_at > now:
        return 0.0

    # Converteer naar Amsterdam-tijd voor markturen-check
    opened_ams = opened_at.astimezone(_AMS_TZ)
    now_ams    = now.astimezone(_AMS_TZ)

    total = 0.0
    cursor = opened_ams
    market_open_delta  = timedelta(hours=_MARKET_OPEN_HOUR,  minutes=_MARKET_OPEN_MIN)
    market_close_delta = timedelta(hours=_MARKET_CLOSE_HOUR, minutes=_MARKET_CLOSE_MIN)

    # Loop per kalenderdag — maximaal 30 dagen terug (anders te langzaam)
    max_days = 30
    days_checked = 0
    while cursor.date() <= now_ams.date() and days_checked < max_days:
        days_checked += 1
        day_start = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        market_open  = day_start + market_open_delta
        market_close = day_start + market_close_delta

        if cursor.weekday() < 5:   # weekdag
            window_start = max(cursor, market_open)
            window_end   = min(now_ams, market_close)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds()

        # Volgende dag vanaf marktopen
        cursor = (day_start + timedelta(days=1)).replace(
            hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MIN, second=0, microsecond=0
        )

    return total
