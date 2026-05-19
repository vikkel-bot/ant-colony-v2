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
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.ants.time_filter_ant import read_latest_time_signal
from ant_colony.ants._queen_regime import read_latest_queen_regime
from ant_colony.ants._market_signal import read_latest_market_signal
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
from ant_colony.exit_chain.position import PaperPosition, PositionSide
from ant_colony.paper.paper_broker import PaperBroker
from ant_colony.paper.paper_ledger import BROKER_FEE_PCT, PaperLedger
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_TRADE_CAPITAL_FRACTION = 0.10   # 10% van beschikbaar kapitaal per trade
_COMMODITY_CAPITAL_FRACTION = 0.05  # 5% van beschikbaar kapitaal per commodity trade
_SL_PCT  = 0.02                  # 2% stop-loss onder entry
_TP_PCT  = 0.03                  # 3% take-profit boven entry
_SHORT_SL_PCT = 0.03             # shorts: 3% stop-loss boven entry
_SHORT_TP_PCT = 0.04             # shorts: 4% take-profit onder entry
_SIGNAL_VALIDITY_TICKS = 2       # signal geldig voor heartbeat_interval × 2 seconden
_MAX_OPEN_POSITIONS    = 10      # maximaal 10 open posities tegelijk (1 per symbool)
_MAX_OPEN_SHORT_POSITIONS = 2    # shorts apart gelimiteerd naast long posities
_STALE_SIGNAL_MINUTES  = 5       # signalen ouder dan dit worden genegeerd
_ZOMBIE_POSITION_HOURS = 168     # posities zonder close ouder dan dit → zombie (7 dagen)
_PAPER_CANDIDATE_WATCHDOG_SECONDS = 15 * 60
_PAPER_TICK_WATCHDOG_SECONDS = float(os.getenv("PAPER_ANT_WATCHDOG_SECONDS", "600"))
_MIN_ENTRY_SHARPE = 0.15
_MIN_ENTRY_WIN_RATE = 0.45
_BIOME_MISMATCH_LOG_INTERVAL_SECONDS = 60.0

# Commodity symbool → yfinance ticker mapping (analoog aan research_ant)
_COMMODITY_YFINANCE_TICKERS: dict[str, str] = {
    "NATGAS": "NG=F",
    "COPPER": "HG=F",
    "SILVER": "SI=F",
}

# --- Regime-gebaseerde entry filtering ---
_SIDEWAYS_ALLOWED_STRATEGY_TYPES = frozenset(["mean_reversion", "rsi_based"])
_SHORT_ALLOWED_REGIMES = frozenset(["SIDEWAYS", "BEAR", "BEARISH", "RISK_OFF"])
_VOLATILE_MIN_SHARPE    = 0.3    # minimale sharpe voor entry in VOLATILE regime
_VOLATILE_SL_MULTIPLIER = 1.5    # SL-percentage 50% groter in VOLATILE regime
_VOLATILE_CAPITAL_MULT  = 0.5    # positiegrootte halveren in VOLATILE regime


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
        # Pre-loaden bij startup: alleen oude candidate_ids als gezien markeren.
        # Verse candidates blijven in de queue zodat PaperAnt na een herstart
        # geen net-geaccepteerde ResearchAnt kandidaten mist.
        self._seen_research_ids: set[str] = self._preload_seen_research_ids(logs_root)

        self._status: AntStatus = AntStatus.IDLE
        self._budget_used: float = 0.0
        self._last_action: str = "init"
        self._log_seq: int = 0

        self._log = logging.getLogger(f"ant.paper.{ant_id[:8]}")
        self._last_candidate_processed_at = time.monotonic()
        self._paper_watchdog_restarts = 0
        self._last_tick_completed_monotonic = monotonic()
        self._last_tick_started_monotonic: float | None = None
        self._tick_watchdog_restarts = 0
        self._biome_mismatch_log_times: dict[str, float] = {}

        # Scout-posities: per symbool (1 per symbool tegelijk).
        self._open_symbols: set[str] = self._load_open_symbols_from_logs()

        # Research-posities: per (symbool, strategy_type) — meerdere per symbool mogelijk.
        self._open_research_keys: set[tuple[str, str]]
        self._pos_id_to_research_key: dict[str, tuple[str, str]]
        self._open_research_keys, self._pos_id_to_research_key = (
            self._load_research_keys_from_logs()
        )

        if self._open_symbols or self._open_research_keys:
            self._log.info(
                "Herstart gedetecteerd — %d scout positie(s), %d research positie(s), %d in ledger",
                len(self._open_symbols), len(self._open_research_keys),
                len(self._ledger.open_positions),
            )

        # Watchtower feedback client (opt-in via WATCHTOWER_ENABLED=true)
        if os.getenv("WATCHTOWER_ENABLED", "false").lower() == "true":
            from ant_colony.clients.watchtower_client import WatchtowerClient
            self._watchtower_client: "WatchtowerClient | None" = WatchtowerClient()
        else:
            self._watchtower_client = None

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

        started_at = datetime.now(tz=timezone.utc)

        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

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

                self._run_tick_with_watchdog()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("PaperAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._emit_pnl_summary()
            self._send_heartbeat()
            self._log.info("PaperAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _run_tick_with_watchdog(self) -> None:
        """
        Voer één paper tick uit met dezelfde harde liveness watchdog als ResearchAnt.

        Bij timeout wordt de geblokkeerde worker als daemon achtergelaten en wordt
        de volgende evaluatiecyclus gestart. De PaperAnt opent nooit posities buiten
        de normale _tick guards; dit is alleen liveness-herstel.
        """
        timeout = max(0.0, float(_PAPER_TICK_WATCHDOG_SECONDS))
        if timeout <= 0:
            self._tick()
            self._last_tick_completed_monotonic = monotonic()
            return

        completed = threading.Event()
        errors: list[BaseException] = []
        started = monotonic()
        self._last_tick_started_monotonic = started

        def worker() -> None:
            try:
                self._tick()
            except BaseException as exc:  # pragma: no cover - opnieuw gegooid in hoofdthread
                errors.append(exc)
            finally:
                completed.set()

        tick_thread = threading.Thread(
            target=worker,
            name=f"paper-cycle-{self.ant_id[:8]}",
            daemon=True,
        )
        tick_thread.start()

        if not completed.wait(timeout=timeout):
            age = monotonic() - started
            self._tick_watchdog_restarts += 1
            self._last_action = "tick_watchdog_restart"
            self._log.error(
                "PaperAnt watchdog: geen tick voltooid in %.1fs (> %.1fs) — "
                "paper-evaluatie cyclus wordt opnieuw gestart | restarts=%d",
                age,
                timeout,
                self._tick_watchdog_restarts,
            )
            self._write_log({
                "action": "paper_watchdog_restart",
                "duration_seconds": round(age, 3),
                "threshold_seconds": timeout,
                "watchdog_restarts": self._tick_watchdog_restarts,
            })
            self._send_heartbeat()
            return

        if errors:
            raise errors[0]

        self._last_tick_completed_monotonic = monotonic()

    def _tick(self) -> None:
        """
        Één handelscyclus.

        Volgorde (P3: exit vóór entry):
          1. Evalueer alle open posities op exit-condities
          2. Verwerk nieuwe scout-signalen en open eventueel nieuwe posities
        """
        self._process_exits()
        open_before = len(self._ledger.open_positions)
        trading_allowed = self._is_trading_allowed()
        regime         = self._read_regime()
        scout_count    = self._process_new_signals(regime=regime)
        approved_count = self._process_approved_candidates(regime=regime)
        research_count = self._process_research_candidates(regime=regime)
        candidate_count = approved_count + research_count
        if candidate_count > 0:
            self._last_candidate_processed_at = time.monotonic()
        else:
            self._paper_candidate_watchdog(regime=regime)
        opened = len(self._ledger.open_positions) - open_before
        filtered = 1 if not trading_allowed else 0
        pending = self._count_pending_candidates()
        self._log.info(
            "paper tick | regime=%s scout=%d research=%d approved=%d filtered=%s opened=%d",
            regime or "—",
            scout_count, research_count, approved_count,
            "blocked" if filtered else 0,
            opened,
        )
        self._write_log({
            "action": "paper_tick",
            "regime": regime,
            "scout": scout_count,
            "research": research_count,
            "approved": approved_count,
            "filtered": "blocked" if filtered else 0,
            "opened": opened,
            "pending_candidates": pending,
            "open_positions": len(self._ledger.open_positions),
        })
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
                research_key = self._pos_id_to_research_key.pop(
                    result.position.position_id, None
                )
                if research_key:
                    self._open_research_keys.discard(research_key)
                else:
                    self._open_symbols.discard(result.position.symbol)
                self._ledger.record_closed(result.position)
                self._emit_trade_closed(result.position)
                self._last_action = f"trade_closed:{result.position.symbol}"

    # ------------------------------------------------------------------
    # Signaalverwerking en entry
    # ------------------------------------------------------------------

    def _is_trading_allowed(self) -> bool:
        """True als trading is toegestaan op basis van biome en kill zone status.

        Crypto-symbolen (eindigen op -EUR of -USD) handelen 24/7 en zijn
        vrijgesteld van de ICT Kill Zone filter. De kill zone check wordt
        alleen toegepast als de missie equities-symbolen bevat.
        """
        symbols = list(self.mission.market_scope.symbols or [])
        if symbols and all(s.endswith(("-EUR", "-USD")) for s in symbols):
            return True  # crypto handelt 24/7 — geen kill zone beperking
        if self.mission.market_scope.biome == "commodity":
            return True  # commodity futures handelen bijna 24/7 — geen ICT kill zone

        if self.logs_root is None:
            return True
        sig = read_latest_time_signal(self.logs_root)
        if sig is None:
            return True  # filter niet actief → fail-open
        allowed = sig.get("trade_allowed", True)
        if not allowed:
            self._log.debug(
                "Nieuwe posities gepauzeerd — buiten kill zone: session=%s",
                sig.get("session", "unknown"),
            )
        return allowed

    def _read_regime(self) -> str | None:
        """Lees Queen regime uit ANT_LOGS/queen/regime.jsonl. None = fail-open."""
        if self.logs_root is None:
            return None
        return read_latest_queen_regime(self.logs_root)

    def _is_entry_allowed_by_regime(
        self,
        regime: str | None,
        *,
        strategy_type: str | None = None,
        sharpe: float | None = None,
        is_scout: bool = False,
        symbol: str = "",
    ) -> tuple[bool, str]:
        """
        Controleer of een entry is toegestaan gegeven het huidige marktregime.

        SIDEWAYS: alleen mean_reversion en rsi_based; scout-signalen geblokkeerd.
        TRENDING: alles toegestaan (default/fail-open gedrag).
        VOLATILE: alleen kandidaten met sharpe > _VOLATILE_MIN_SHARPE.
        None:     fail-open → alle entries toegestaan.

        Returns:
            (allowed: bool, reden: str)
        """
        if regime is None or regime == "TRENDING":
            return True, ""

        if regime == "SIDEWAYS":
            if is_scout:
                return (
                    False,
                    f"{symbol} scout-signaal niet toegestaan in SIDEWAYS regime",
                )
            if strategy_type and strategy_type not in _SIDEWAYS_ALLOWED_STRATEGY_TYPES:
                return (
                    False,
                    f"{symbol} {strategy_type} niet toegestaan in SIDEWAYS regime",
                )
            return True, ""

        if regime == "VOLATILE":
            if sharpe is not None and sharpe <= _VOLATILE_MIN_SHARPE:
                return (
                    False,
                    f"{symbol} sharpe={sharpe:.2f} ≤ {_VOLATILE_MIN_SHARPE} in VOLATILE regime",
                )
            return True, ""

        return True, ""  # onbekend regime → fail-open

    def _is_short_allowed_by_regime(self, regime: str | None, symbol: str = "") -> tuple[bool, str]:
        """Shorts alleen toestaan in expliciet short-vriendelijk regime."""
        normalized = str(regime or "").upper()
        if normalized in _SHORT_ALLOWED_REGIMES:
            return True, ""
        if not normalized:
            return False, f"{symbol} short geblokkeerd: geen expliciet SIDEWAYS/BEAR regime"
        return False, f"{symbol} short geblokkeerd in regime={normalized}"

    def _open_short_count(self) -> int:
        return sum(1 for p in self._ledger.open_positions if p.side == PositionSide.SHORT)

    def _short_capacity_available(self, symbol: str) -> tuple[bool, str]:
        open_shorts = self._open_short_count()
        if open_shorts >= _MAX_OPEN_SHORT_POSITIONS:
            return (
                False,
                f"{symbol} short geblokkeerd: max_open_shorts={open_shorts}/{_MAX_OPEN_SHORT_POSITIONS}",
            )
        return True, ""

    def _passes_entry_thresholds(
        self,
        *,
        symbol: str,
        strategy_type: str,
        sharpe: float,
        win_rate: float,
    ) -> tuple[bool, str]:
        if sharpe < _MIN_ENTRY_SHARPE:
            return (
                False,
                f"{symbol} {strategy_type} sharpe={sharpe:.3f} < {_MIN_ENTRY_SHARPE:.2f}",
            )
        if win_rate < _MIN_ENTRY_WIN_RATE:
            return (
                False,
                f"{symbol} {strategy_type} win_rate={win_rate:.3f} < {_MIN_ENTRY_WIN_RATE:.2f}",
            )
        return True, ""

    @staticmethod
    def _is_stale_timestamp(ts_str: str | None) -> bool:
        """True als ts_str meer dan _STALE_SIGNAL_MINUTES oud is. Geen timestamp → False (fail-open)."""
        if not ts_str:
            return False
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            return (datetime.now(tz=timezone.utc) - ts) > timedelta(minutes=_STALE_SIGNAL_MINUTES)
        except (ValueError, TypeError):
            return False

    def _paper_candidate_watchdog(self, regime: str | None = None) -> None:
        """Herstart de paper-evaluatie als verse candidates in de queue blijven staan."""
        if not self._is_trading_allowed():
            return
        pending = self._count_pending_candidates()
        if pending <= 0:
            return
        idle_for = time.monotonic() - self._last_candidate_processed_at
        if idle_for < _PAPER_CANDIDATE_WATCHDOG_SECONDS:
            return
        self._paper_watchdog_restarts += 1
        self._log.error(
            "PaperAnt watchdog | pending_candidates=%d idle=%.0fs — herstart paper-evaluatie cyclus",
            pending,
            idle_for,
        )
        approved_count = self._process_approved_candidates(regime=regime)
        research_count = self._process_research_candidates(regime=regime)
        if approved_count + research_count > 0:
            self._last_candidate_processed_at = time.monotonic()

    def _count_pending_candidates(self) -> int:
        """Aantal verse accepted/approved candidates die nog niet gezien zijn."""
        if self.logs_root is None:
            return 0
        return self._count_pending_research_candidates() + self._count_pending_approved_candidates()

    def _count_pending_research_candidates(self) -> int:
        if self.logs_root is None:
            return 0
        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return 0
        pending = 0
        for jsonl_path in sorted(research_dir.glob("*.jsonl")):
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "candidate_accepted":
                        continue
                    candidate_id = str(payload.get("candidate_id") or "")
                    if not candidate_id or candidate_id in self._seen_research_ids:
                        continue
                    if self._is_stale_timestamp(record.get("timestamp")):
                        continue
                    pending += 1
            except OSError:
                continue
        return pending

    def _count_pending_approved_candidates(self) -> int:
        if self.logs_root is None:
            return 0
        approved_dir = self.logs_root / "approved"
        if not approved_dir.exists():
            return 0
        pending = 0
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
                    if self._is_stale_timestamp(record.get("timestamp")):
                        continue
                    pending += 1
            except OSError:
                continue
        return pending

    def _is_zombie_position(self, ts_str: str, now: datetime, *, biome: str = "") -> bool:
        """True als een ongesloten positie ouder is dan _ZOMBIE_POSITION_HOURS.

        Crypto-posities (biome='crypto') worden nooit als zombie beschouwd —
        die handelen 24/7 en kunnen weken open staan. Geen timestamp → False (fail-open).
        """
        if biome == "crypto":
            return False
        if not ts_str:
            return False
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            return (now - ts).total_seconds() > _ZOMBIE_POSITION_HOURS * 3600
        except (ValueError, TypeError):
            return False

    def _reconstruct_position(
        self, pos_id: str, payload: dict, ts_str: str
    ) -> PaperPosition | None:
        """Reconstrueer een PaperPosition uit een trade_opened payload voor ledger-herstel."""
        try:
            try:
                opened_at = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                opened_at = datetime.now(tz=timezone.utc)

            entry_price = float(payload.get("entry_price") or 0)
            quantity    = float(payload.get("quantity") or 0)
            sl          = float(payload.get("stop_loss") or 0)
            tp          = float(payload.get("take_profit") or 0)

            if entry_price <= 0 or quantity <= 0 or sl <= 0 or tp <= 0:
                self._log.warning(
                    "Onvolledige payload voor positie %s — ledger-herstel overgeslagen", pos_id
                )
                return None

            side_str = str(payload.get("side") or "long")
            try:
                side = PositionSide(side_str)
            except ValueError:
                side = PositionSide.LONG

            return PaperPosition(
                position_id=pos_id,
                symbol=str(payload.get("symbol") or ""),
                biome=str(payload.get("biome") or self.mission.market_scope.biome),
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss_price=sl,
                take_profit_price=tp,
                ttl=self.mission.ttl,
                current_price=entry_price,
                peak_price=entry_price,
                opened_at=opened_at,
            )
        except Exception:
            self._log.warning(
                "Kan positie %s niet reconstrueren uit log — ledger-herstel overgeslagen", pos_id
            )
            return None

    def _process_new_signals(self, regime: str | None = None) -> int:
        """Verwerk nieuwe scout-signalen en open posities indien van toepassing."""
        if not self._is_trading_allowed():
            return 0

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

            # Regime-filter: scout-signalen zijn price_move/volume_spike, niet RSI/Bollinger
            allowed, reason = self._is_entry_allowed_by_regime(
                regime, is_scout=True, symbol=symbol
            )
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                continue

            self._try_open_position(sig, regime=regime)

        return len(signals)

    def _has_open_position(self, symbol: str) -> bool:
        """True als er al een scout-positie open is voor dit symbool."""
        return (
            symbol in self._open_symbols
            or any(p.symbol == symbol for p in self._ledger.open_positions)
        )

    def _has_open_research_position(self, symbol: str, strategy_type: str) -> bool:
        """True als er al een research-positie open is voor (symbool, strategy_type)."""
        return (symbol, strategy_type) in self._open_research_keys

    def _load_open_symbols_from_logs(self) -> set[str]:
        """
        Scan ANT_LOGS/paper/*.jsonl op trade_opened / trade_closed events.

        Retourneert de set van symbolen met een open (onafgesloten) positie.
        Herstelt ook PaperPosition objecten in de ledger voor SL/TP bewaking.
        """
        if self.logs_root is None:
            return set()
        paper_dir = self.logs_root / "paper"
        if not paper_dir.exists():
            return set()

        opened: dict[str, str] = {}             # position_id → symbol
        opened_ts: dict[str, str] = {}          # position_id → timestamp str
        opened_payload: dict[str, dict] = {}    # position_id → full payload
        closed_ids: set[str] = set()

        for path in paper_dir.glob("*.jsonl"):
            if path.name.endswith("_trades.jsonl"):
                continue   # alleen closed trades — niet bruikbaar voor recovery
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
                        sym = str(payload.get("symbol") or "")
                        if sym:
                            opened[pos_id] = sym
                            opened_ts[pos_id] = str(record.get("timestamp") or "")
                            opened_payload[pos_id] = payload
                    elif action == "trade_closed":
                        closed_ids.add(pos_id)
            except OSError:
                pass

        result: set[str] = set()
        restored = 0
        now = datetime.now(tz=timezone.utc)
        for pid, sym in opened.items():
            if pid in closed_ids:
                continue
            payload = opened_payload.get(pid, {})
            biome   = str(payload.get("biome") or "")
            ts_str  = opened_ts.get(pid, "")
            if self._is_zombie_position(ts_str, now, biome=biome):
                self._log.info(
                    "Zombie positie genegeerd bij herstel: symbol=%s position_id=%s ts=%s",
                    sym, pid, ts_str,
                )
                continue
            result.add(sym)
            pos = self._reconstruct_position(pid, payload, ts_str)
            if pos is not None:
                self._ledger.record_opened(pos)
                restored += 1

        if restored:
            self._log.info("%d positie(s) hersteld in ledger", restored)
        return result

    def _load_research_keys_from_logs(
        self,
    ) -> tuple[set[tuple[str, str]], dict[str, tuple[str, str]]]:
        """
        Scan ANT_LOGS/paper/*.jsonl op trade_opened events met strategy_type.

        Retourneert (open_research_keys, pos_id_to_key) voor herstel bij herstart.
        """
        keys: set[tuple[str, str]] = set()
        pos_map: dict[str, tuple[str, str]] = {}

        if self.logs_root is None:
            return keys, pos_map
        paper_dir = self.logs_root / "paper"
        if not paper_dir.exists():
            return keys, pos_map

        opened: dict[str, tuple[str, str]] = {}    # pos_id → (symbol, strategy_type)
        opened_ts: dict[str, str] = {}              # pos_id → timestamp str
        opened_biome: dict[str, str] = {}           # pos_id → biome
        closed_ids: set[str] = set()

        for path in paper_dir.glob("*.jsonl"):
            if path.name.endswith("_trades.jsonl"):
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload  = record.get("payload") or {}
                    action   = payload.get("action")
                    pos_id   = str(payload.get("position_id") or "")
                    st       = str(payload.get("strategy_type") or "")
                    if not pos_id or not st:
                        continue
                    if action == "trade_opened":
                        sym = str(payload.get("symbol") or "")
                        if sym:
                            opened[pos_id] = (sym, st)
                            opened_ts[pos_id] = str(record.get("timestamp") or "")
                            opened_biome[pos_id] = str(payload.get("biome") or "")
                    elif action == "trade_closed":
                        closed_ids.add(pos_id)
            except OSError:
                pass

        now = datetime.now(tz=timezone.utc)
        for pid, key in opened.items():
            if pid in closed_ids:
                continue
            ts_str = opened_ts.get(pid, "")
            biome  = opened_biome.get(pid, "")
            if self._is_zombie_position(ts_str, now, biome=biome):
                self._log.info(
                    "Zombie research positie genegeerd bij herstel: key=%s position_id=%s ts=%s",
                    key, pid, ts_str,
                )
                continue
            keys.add(key)
            pos_map[pid] = key

        return keys, pos_map

    # ------------------------------------------------------------------
    # Research-kandidaten verwerken
    # ------------------------------------------------------------------

    @staticmethod
    def _preload_seen_research_ids(logs_root: Path | None) -> set[str]:
        """
        Scan ANT_LOGS/research/*.jsonl bij startup en retourneer oude candidate_ids.

        Doel: voorkomt dat historische kandidaten opnieuw verwerkt worden na herstart,
        maar laat verse candidates in de queue staan voor PaperAnt.
        """
        seen: set[str] = set()
        if logs_root is None:
            return seen
        research_dir = logs_root / "research"
        if not research_dir.exists():
            return seen
        for path in sorted(research_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "candidate_accepted":
                        continue
                    cid = str(payload.get("candidate_id") or "")
                    if cid and (
                        not record.get("timestamp")
                        or PaperAnt._is_stale_timestamp(record.get("timestamp"))
                    ):
                        seen.add(cid)
            except OSError:
                pass
        return seen

    def _process_research_candidates(self, regime: str | None = None) -> int:
        """Verwerk ACCEPTED StrategyCandidate records uit ANT_LOGS/research/*.jsonl."""
        if not self._is_trading_allowed():
            return 0
        if self.logs_root is None:
            return 0
        research_dir = self.logs_root / "research"
        if not research_dir.exists():
            return 0

        count = 0
        for jsonl_path in sorted(research_dir.glob("*.jsonl")):
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload") or {}
                    except json.JSONDecodeError:
                        continue

                    if payload.get("action") != "candidate_accepted":
                        continue

                    candidate_id = str(payload.get("candidate_id") or "")
                    if not candidate_id or candidate_id in self._seen_research_ids:
                        continue
                    self._seen_research_ids.add(candidate_id)
                    count += 1

                    if self._is_stale_timestamp(record.get("timestamp")):
                        self._log.debug(
                            "research candidate skipped | symbol=%s strategy=%s reason=stale_timestamp ts=%s",
                            payload.get("symbol", "?"),
                            payload.get("strategy_type", "?"),
                            record.get("timestamp"),
                        )
                        continue

                    self._open_from_research_candidate(payload, regime=regime)

            except OSError:
                self._log.warning("Kan research-log niet lezen: %s", jsonl_path)

        return count

    def _open_from_research_candidate(self, payload: dict, regime: str | None = None) -> None:
        """Open een paper positie op basis van een candidate_accepted research record."""
        symbol = str(payload.get("symbol") or "")
        if not symbol:
            return

        direction = str(payload.get("direction") or "long")
        strategy_type = str(payload.get("strategy_type") or "unknown")
        biome = str(payload.get("biome") or self.mission.market_scope.biome)

        if direction not in ("long", "short"):
            self._log.debug(
                "research candidate skipped | symbol=%s strategy=%s reason=unsupported_direction direction=%s",
                symbol, strategy_type, direction,
            )
            return

        if direction == "short":
            tp_pct = _SHORT_TP_PCT
            sl_pct = _SHORT_SL_PCT
        else:
            tp_pct = float(payload.get("tp_pct") or _TP_PCT)
            sl_pct = float(payload.get("sl_pct") or _SL_PCT)

        # Regime-filter: strategy_type en sharpe valideren
        sharpe = float(payload.get("sharpe") or payload.get("sharpe_ratio") or 0.0)
        win_rate = float(payload.get("win_rate") or 0.0)
        if direction == "short":
            if biome != "crypto":
                self._log.info("Entry geblokkeerd: %s short alleen toegestaan in crypto biome", symbol)
                return
            allowed, reason = self._is_short_allowed_by_regime(regime, symbol=symbol)
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return
            allowed, reason = self._short_capacity_available(symbol)
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return
            allowed, reason = self._passes_entry_thresholds(
                symbol=symbol,
                strategy_type=strategy_type,
                sharpe=sharpe,
                win_rate=win_rate,
            )
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return

        if direction == "long":
            allowed, reason = self._is_entry_allowed_by_regime(
                regime,
                strategy_type=strategy_type,
                sharpe=sharpe,
                symbol=symbol,
            )
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return

        if self._has_open_research_position(symbol, strategy_type):
            self._log.debug(
                "research candidate skipped | symbol=%s strategy=%s reason=already_open",
                symbol, strategy_type,
            )
            return

        price = self._fetch_price(symbol)
        if price is None or price <= 0:
            self._log.debug(
                "research candidate skipped | symbol=%s strategy=%s reason=no_live_price",
                symbol, strategy_type,
            )
            return

        sig = {
            "symbol":        symbol,
            "current_price": price,
            "confidence":    1.0,
            "biome":         biome,
            "signal_id":     payload.get("candidate_id"),
        }
        commodity_fraction = _COMMODITY_CAPITAL_FRACTION if biome == "commodity" else None
        self._try_open_position(
            sig,
            strategy_type=strategy_type,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            regime=regime,
            side=direction,
            sharpe=sharpe,
            capital_fraction=commodity_fraction,
        )

    def _try_open_position(
        self,
        sig: dict,
        *,
        strategy_type: str | None = None,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
        regime: str | None = None,
        side: str = "long",
        sharpe: float | None = None,
        capital_fraction: float | None = None,
    ) -> None:
        """Bouw een EntrySignal en probeer een positie te openen via PaperBroker.

        strategy_type: als opgegeven, wordt dit als research-positie geregistreerd
                       (dedup via _open_research_keys). Zonder strategy_type: scout-pad.
        sl_pct/tp_pct: override voor stop-loss / take-profit percentages.
                       Valt terug op module-defaults als None.
        capital_fraction: override voor positiegrootte als fractie van kapitaal.
                          Valt terug op _TRADE_CAPITAL_FRACTION als None.
        """
        symbol = sig.get("symbol", "")
        if not symbol:
            return
        if side not in ("long", "short"):
            self._log.debug("unsupported side voor %s: %s", symbol, side)
            return

        entry_price = self._fetch_price(symbol)
        if entry_price is None or entry_price <= 0:
            self._log.debug("Geen live prijs voor %s — positie niet geopend", symbol)
            return

        # Definitieve guard — blokkeert duplicaten ongeacht aanroeppad
        if strategy_type:
            if self._has_open_research_position(symbol, strategy_type):
                self._log.debug(
                    "_try_open_position: research positie al open voor (%s, %s) — geblokkeerd",
                    symbol, strategy_type,
                )
                return
        else:
            if self._has_open_position(symbol):
                self._log.debug(
                    "_try_open_position: al open positie voor %s — geblokkeerd", symbol
                )
                return

        # Globale cap: maximaal _MAX_OPEN_POSITIONS posities tegelijk
        in_ledger_symbols = {p.symbol for p in self._ledger.open_positions}
        scout_extra = len(self._open_symbols - in_ledger_symbols)
        research_extra = len({s for s, _ in self._open_research_keys} - in_ledger_symbols)
        current_open = len(self._ledger.open_positions) + scout_extra + research_extra
        if current_open >= _MAX_OPEN_POSITIONS:
            self._log.debug(
                "research candidate skipped | symbol=%s strategy=%s reason=max_positions_reached"
                " open=%d max=%d",
                symbol, strategy_type or "scout",
                current_open, _MAX_OPEN_POSITIONS,
            )
            return

        used_sl_pct = sl_pct if sl_pct is not None else (_SHORT_SL_PCT if side == "short" else _SL_PCT)
        used_tp_pct = tp_pct if tp_pct is not None else (_SHORT_TP_PCT if side == "short" else _TP_PCT)
        capital_fraction = capital_fraction if capital_fraction is not None else _TRADE_CAPITAL_FRACTION

        # Market signal: nieuws + regime gecombineerd — komt bovenop VOLATILE aanpassing
        mkt = read_latest_market_signal(self.logs_root) if self.logs_root else None
        if mkt is not None:
            pos_mult = float(mkt.get("position_size_mult") or 1.0)
            sl_m     = float(mkt.get("sl_mult") or 1.0)
            if pos_mult != 1.0 or sl_m != 1.0:
                self._log.debug(
                    "market_signal aanpassing | %s signal=%s pos_mult=%.2f sl_mult=%.2f",
                    symbol, mkt.get("combined_signal", "normal"), pos_mult, sl_m,
                )
            capital_fraction *= pos_mult
            used_sl_pct      *= sl_m

        # VOLATILE: grotere SL-buffer en kleinere positiegrootte (op top van market_signal)
        if regime == "VOLATILE":
            used_sl_pct  = used_sl_pct * _VOLATILE_SL_MULTIPLIER
            capital_fraction = capital_fraction * _VOLATILE_CAPITAL_MULT
            self._log.debug(
                "VOLATILE aanpassing | %s sl_pct=%.3f capital_fraction=%.3f",
                symbol, used_sl_pct, capital_fraction,
            )

        if side == "short":
            sl = entry_price * (1.0 + used_sl_pct)
            tp = entry_price * (1.0 - used_tp_pct)
        else:
            sl = entry_price * (1.0 - used_sl_pct)
            tp = entry_price * (1.0 + used_tp_pct)

        capital_available = self._ledger.capital_available
        capital_per_trade = capital_available * capital_fraction
        suggested_qty     = capital_per_trade / entry_price if entry_price > 0 else None

        try:
            entry_signal = EntrySignal(
                symbol=symbol,
                biome=sig.get("biome", self.mission.market_scope.biome),
                mission_id=self.mission.mission_id,
                ant_id=self.ant_id,
                source=SignalSource.SCOUT_ANT,
                side=side,
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
            if strategy_type:
                key = (symbol, strategy_type)
                self._open_research_keys.add(key)
                self._pos_id_to_research_key[result.position.position_id] = key
            else:
                self._open_symbols.add(symbol)
            self._ledger.record_opened(result.position)
            self._emit_trade_opened(
                result.position, sig,
                strategy_type=strategy_type,
                sl_pct=used_sl_pct,
                tp_pct=used_tp_pct,
            )
            self._last_action = f"trade_opened:{symbol}"
            if side == "short":
                self._log.info(
                    "SHORT GEOPEND | %s direction=short sharpe=%s qty=%.8f @ %.4f SL=%.4f TP=%.4f strategie=%s",
                    symbol,
                    f"{sharpe:.3f}" if sharpe is not None else "n/a",
                    result.position.quantity,
                    entry_price,
                    sl,
                    tp,
                    strategy_type or "scout",
                )
            else:
                self._log.info(
                    "POSITIE GEOPEND | %s LONG %.8f @ %.4f  SL=%.4f  TP=%.4f  strategie=%s",
                    symbol, result.position.quantity, entry_price, sl, tp,
                    strategy_type or "scout",
                )
        else:
            self._log.debug(
                "Positie geweigerd voor %s — %s: %s",
                symbol, result.rejection_reason, result.rejection_detail,
            )

    # ------------------------------------------------------------------
    # Approved-kandidaten verwerken
    # ------------------------------------------------------------------

    def _process_approved_candidates(self, regime: str | None = None) -> int:
        """Verwerk APPROVED StrategyCandidate records uit ANT_LOGS/approved/*.jsonl."""
        if not self._is_trading_allowed():
            return 0
        if self.logs_root is None:
            return 0

        approved_dir = self.logs_root / "approved"
        if not approved_dir.exists():
            return 0

        count = 0
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
                    count += 1

                    if self._is_stale_timestamp(record.get("timestamp")):
                        self._log.debug(
                            "Stale approved-kandidaat %s overgeslagen (timestamp=%s)",
                            candidate_id, record.get("timestamp"),
                        )
                        continue

                    self._open_from_candidate(record, regime=regime)

            except OSError:
                self._log.warning("Kan approved-log niet lezen: %s", jsonl_path)

        return count

    def _open_from_candidate(self, record: dict, regime: str | None = None) -> None:
        """Open een paper positie op basis van een APPROVED StrategyCandidate record."""
        market_scope = record.get("market_scope") or {}
        # Ondersteunt zowel "symbol": "BTC-EUR" als "symbols": ["BTC-EUR", ...]
        raw_symbol = market_scope.get("symbol") or ""
        if not raw_symbol:
            symbols_list = market_scope.get("symbols") or []
            raw_symbol = symbols_list[0] if symbols_list else ""
        symbol = str(raw_symbol)
        if not symbol:
            self._log.info(
                "Approved kandidaat overgeslagen: geen symbool | %s",
                record.get("candidate_id"),
            )
            return

        parameters  = record.get("parameters") or {}
        entry_cond  = record.get("entry_conditions") or {}
        direction   = str(
            entry_cond.get("direction")
            or parameters.get("direction")
            or "long"
        )

        if direction not in ("long", "short"):
            self._log.info(
                "Approved kandidaat overgeslagen: ongeldige direction=%s | %s",
                direction, record.get("candidate_id"),
            )
            return

        price = self._fetch_price(symbol)
        if price is None or price <= 0:
            self._log.info(
                "Approved kandidaat overgeslagen: geen prijs voor %s | %s",
                symbol, record.get("candidate_id"),
            )
            return

        # Sla over als er al een open positie is voor dit symbool
        if self._has_open_position(symbol):
            self._log.info(
                "Approved kandidaat overgeslagen: al open positie voor %s | %s",
                symbol, record.get("candidate_id"),
            )
            return

        # Regime-filter op approved candidates
        strategy_type = str(
            record.get("strategy_type")
            or (record.get("parameters") or {}).get("strategy_type")
            or "unknown"
        )
        biome = str(record.get("biome") or self.mission.market_scope.biome)
        sharpe = float(
            record.get("sharpe_ratio") or record.get("sharpe")
            or record.get("fitness_score") or 0.0
        )
        win_rate = float(record.get("win_rate") or parameters.get("win_rate") or 0.0)
        if direction == "short":
            if biome != "crypto":
                self._log.info("Entry geblokkeerd: %s short alleen toegestaan in crypto biome", symbol)
                return
            allowed, reason = self._is_short_allowed_by_regime(regime, symbol=symbol)
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return
            allowed, reason = self._short_capacity_available(symbol)
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return
            allowed, reason = self._passes_entry_thresholds(
                symbol=symbol,
                strategy_type=strategy_type,
                sharpe=sharpe,
                win_rate=win_rate,
            )
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return

        if direction == "long":
            allowed, reason = self._is_entry_allowed_by_regime(
                regime, strategy_type=strategy_type, sharpe=sharpe, symbol=symbol
            )
            if not allowed:
                self._log.info("Entry geblokkeerd: %s", reason)
                return

        sig = {
            "symbol":        symbol,
            "current_price": price,
            "confidence":    float(record.get("fitness_score") or 0.7),
            "biome":         biome,
        }
        self._try_open_position(
            sig,
            regime=regime,
            side=direction,
            strategy_type=strategy_type if direction == "short" else None,
            sl_pct=_SHORT_SL_PCT if direction == "short" else None,
            tp_pct=_SHORT_TP_PCT if direction == "short" else None,
            sharpe=sharpe,
        )

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

                    symbol = str(payload.get("symbol") or "")

                    # Sla cross-biome signalen over (bijv. commodities-signalen in
                    # een crypto PaperAnt). Fail-open: geen biome veld → ga door naar
                    # de symbool-scope check hieronder.
                    signal_biome = payload.get("biome")
                    if signal_biome and signal_biome != self.mission.market_scope.biome:
                        self._processed_signals.add(signal_id)
                        self._log_biome_mismatch(symbol or "?", str(signal_biome))
                        continue

                    allowed_symbols = set(self.mission.market_scope.symbols or [])
                    if allowed_symbols and symbol and symbol not in allowed_symbols:
                        self._processed_signals.add(signal_id)
                        self._log.info(
                            "Scout-signaal gefilterd | symbol=%s reden=symbol_out_of_scope scope=%s",
                            symbol,
                            ",".join(sorted(allowed_symbols)),
                        )
                        continue

                    self._processed_signals.add(signal_id)

                    if self._is_stale_timestamp(payload.get("detected_at")):
                        self._log.debug(
                            "Stale scout-signaal %s overgeslagen (detected_at=%s)",
                            signal_id, payload.get("detected_at"),
                        )
                        continue

                    new_signals.append(payload)

            except OSError:
                self._log.warning("Kan scout-log niet lezen: %s", jsonl_path)

        return new_signals

    def _log_biome_mismatch(self, symbol: str, signal_biome: str) -> None:
        """Rate-limit cross-biome filter logs tot maximaal 1 regel per symbool/minuut."""
        key = symbol.upper() or "UNKNOWN"
        now = monotonic()
        last = self._biome_mismatch_log_times.get(key, 0.0)
        if now - last < _BIOME_MISMATCH_LOG_INTERVAL_SECONDS:
            return
        self._biome_mismatch_log_times[key] = now
        self._log.info(
            "Scout-signaal gefilterd | symbol=%s biome=%s reden=biome_mismatch expected=%s",
            key,
            signal_biome,
            self.mission.market_scope.biome,
        )

    # ------------------------------------------------------------------
    # Live prijs ophalen
    # ------------------------------------------------------------------

    def _fetch_price(self, symbol: str) -> float | None:
        """Haal de actuele marktprijs op via de BiomeAdapter (fail-closed).

        Commodity symbolen (NATGAS/COPPER/SILVER) worden via yfinance opgehaald
        omdat er geen live commodity adapter is.
        """
        if symbol in _COMMODITY_YFINANCE_TICKERS:
            return self._fetch_commodity_price(symbol)
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

    def _fetch_commodity_price(self, symbol: str) -> float | None:
        """Haal de actuele prijs voor een commodity op via yfinance (lazy import).

        Uitsluitend paper — geen live broker koppeling (fail-closed P2).
        """
        yf_ticker = _COMMODITY_YFINANCE_TICKERS.get(symbol)
        if not yf_ticker:
            return None
        try:
            import yfinance as yf  # lazy import — niet verplicht geïnstalleerd
            df = yf.Ticker(yf_ticker).history(period="5d", interval="1d", auto_adjust=True)
            if df is None or df.empty:
                self._log.warning("Geen yfinance prijs voor commodity %s (%s)", symbol, yf_ticker)
                return None
            close = float(df["Close"].iloc[-1])
            return close if close > 0 else None
        except ImportError:
            self._log.warning("yfinance niet geïnstalleerd — commodity prijs niet beschikbaar")
            return None
        except Exception:
            self._log.exception("Fout bij ophalen commodity prijs voor %s (%s)", symbol, yf_ticker)
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_trade_opened(
        self,
        position,
        signal_data: dict,
        *,
        strategy_type: str | None = None,
        sl_pct: float | None = None,
        tp_pct: float | None = None,
    ) -> None:
        """Log een trade_opened event naar ANT_LOGS/paper/{ant_id}.jsonl."""
        entry_fee_cost = round(position.entry_price * position.quantity * BROKER_FEE_PCT, 6)
        effective_entry = round(position.entry_price * (1.0 + BROKER_FEE_PCT), 8)
        biome = position.biome or ""
        self._write_log({
            "action":           "trade_opened",
            "position_id":      position.position_id,
            "symbol":           position.symbol,
            "biome":            biome,
            "asset_type":       biome if biome else None,
            "broker":           "paper_commodity" if biome == "commodity" else "paper",
            "side":             position.side.value,
            "entry_price":      position.entry_price,
            "effective_entry":  effective_entry,
            "broker_entry_fee": entry_fee_cost,
            "quantity":         position.quantity,
            "stop_loss":        position.stop_loss_price,
            "take_profit":      position.take_profit_price,
            "from_signal_id":   signal_data.get("signal_id"),
            "confidence":       signal_data.get("confidence"),
            "strategy_type":    strategy_type,
            "sl_pct":           sl_pct,
            "tp_pct":           tp_pct,
        })

    def _emit_trade_closed(self, position) -> None:
        """Log een trade_closed event met fee-gecorrigeerde PnL."""
        pnl_gross = position.realized_pnl() or 0.0
        entry_fee = position.entry_price * position.quantity * BROKER_FEE_PCT
        exit_fee  = (position.exit_price or 0.0) * position.quantity * BROKER_FEE_PCT
        fee_total = round(entry_fee + exit_fee, 6)
        pnl_net   = round(pnl_gross - fee_total, 4)

        entry_value = position.entry_price * position.quantity
        pnl_pct = round(pnl_net / entry_value * 100, 4) if entry_value > 0 else 0.0

        self._write_log({
            "action":            "trade_closed",
            "position_id":       position.position_id,
            "symbol":            position.symbol,
            "side":              position.side.value,
            "entry_price":       position.entry_price,
            "exit_price":        position.exit_price,
            "effective_exit":    round((position.exit_price or 0.0) * (1.0 - BROKER_FEE_PCT), 8),
            "quantity":          position.quantity,
            "exit_reason":       position.exit_reason,
            "broker_fee_cost":   fee_total,
            "realized_pnl_gross": round(pnl_gross, 4),
            "realized_pnl":      pnl_net,
            "pnl_pct":           pnl_pct,
        })
        self._log.info(
            "POSITIE GESLOTEN | %s via %s  pnl_gross=%.4f fee=%.4f pnl_net=%.4f (%.2f%%)",
            position.symbol, position.exit_reason, pnl_gross, fee_total, pnl_net, pnl_pct,
        )
        self._send_watchtower_feedback(position, pnl_pct, pnl_net)

    def _send_watchtower_feedback(self, position, pnl_pct: float, pnl_eur: float) -> None:
        """Fire-and-forget feedback naar Watchtower na trade close."""
        if self._watchtower_client is None:
            return

        reason = position.exit_reason or "unknown"
        if "stop_loss" in reason:
            exit_label = "SL"
        elif "take_profit" in reason:
            exit_label = "TP"
        elif "ttl" in reason:
            exit_label = "TTL"
        else:
            exit_label = "MANUAL"

        if position.closed_at and position.opened_at:
            duration_hours = round(
                (position.closed_at - position.opened_at).total_seconds() / 3600, 4
            )
        else:
            duration_hours = 0.0

        outcome = {
            "feedback_type": "TRADE_OUTCOME",
            "signal_id":    position.watchtower_signal_id,
            "asset":        position.symbol,
            "biome":        _watchtower_feedback_biome(position),
            "direction":    position.side.value.upper(),
            "entry_price":  float(position.entry_price),
            "exit_price":   float(position.exit_price or 0.0),
            "pnl_pct":      pnl_pct,
            "pnl_eur":      float(pnl_eur),
            "exit_reason":  exit_label,
            "duration_hours": duration_hours,
            "entry_time":   position.opened_at.isoformat() if position.opened_at else None,
            "exit_time":    position.closed_at.isoformat() if position.closed_at else None,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "strategy_type": getattr(position, "strategy_type", None),
            "open_positions_count": len(self._ledger.open_positions),
            "portfolio_heat": 0.0,
            "data_quality": "MEDIUM",
        }
        threading.Thread(
            target=self._watchtower_client.post_outcome,
            args=(outcome,),
            daemon=True,
            name=f"wt-fb-{position.position_id[:8]}",
        ).start()

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


def _watchtower_feedback_biome(position) -> str:
    """Map Colony biome strings naar het Watchtower feedback schema."""
    biome = str(getattr(position, "biome", "") or "").lower()
    symbol = str(getattr(position, "symbol", "") or "").upper()
    if biome == "crypto" or symbol.endswith(("-EUR", "-USD", "-BTC", "-USDT")):
        return "CRYPTO"
    return "EQUITIES"
