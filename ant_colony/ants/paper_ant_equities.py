"""
ant_colony/ants/paper_ant_equities.py

EquitiesPaperAnt — paper trading voor equities met trailing stop en dynamische exit.

Verantwoordelijkheden:
  1. Leest signalen uit vier bronnen:
       - ANT_LOGS/scouts/*.jsonl          (SectorScoutAnt: biome=equities)
       - ANT_LOGS/equities/breakout/      (BreakoutAnt: breakout_signal)
       - ANT_LOGS/equities/dividend/      (DividendScoutAnt: dividend_candidate)
       - ANT_LOGS/watchtower/candidates.jsonl (WatchtowerAnt: watchtower_candidate)
  2. Opent LONG paper posities (max 1 per symbool, max 10 tegelijk).
     Kapitaal per trade: 10% van beschikbaar kapitaal.
  3. Exit-logica (P3: exit vóór entry, elke tick):
       a. Trailing stop: sluit als current_price < peak_price * (1 - TRAILING_STOP_PCT)
       b. Harde SL: sluit als current_price < entry_price * (1 - HARD_SL_PCT)
       c. Momentum-exit: sector_scout-posities sluiten pas na 4h hold en 3 misses
       d. TTL noodrem: 365 dagen voor equity momentum, 14 dagen voor Watchtower
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
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.ants._market_signal import read_latest_market_signal
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
_SECTOR_RANKING_ENTRY_LIMIT = 3   # SectorScoutAnt emitteert LONG voor top-3

# NYSE / IBKR reguliere handelsuren in Amsterdam-tijd
_AMS_TZ             = ZoneInfo("Europe/Amsterdam")
_MARKET_OPEN_HOUR   = 15          # 15:30 AMS
_MARKET_OPEN_MIN    = 30
_MARKET_CLOSE_HOUR  = 22          # 22:00 AMS
_MARKET_CLOSE_MIN   = 0

_TRADING_SECONDS_PER_DAY = int(6.5 * 3600)           # 6h30 = 23400 s


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


_SECONDS_PER_DAY          = 24 * 3600
_EQUITY_MAX_TTL_DAYS      = _env_int("EQUITY_MAX_TTL_DAYS", 365)
_TTL_TRADING_DAYS         = _EQUITY_MAX_TTL_DAYS  # backward-compatible export voor tests/dashboard
_TTL_TRADING_SECONDS      = _EQUITY_MAX_TTL_DAYS * _SECONDS_PER_DAY
_WATCHTOWER_TTL_DAYS      = 14
_WATCHTOWER_TTL_SECONDS   = _WATCHTOWER_TTL_DAYS * _SECONDS_PER_DAY
_MOMENTUM_EXIT_CONSECUTIVE_TICKS = _env_int(
    "EQUITY_MOMENTUM_EXIT_CONSECUTIVE_TICKS",
    3,
    minimum=3,
)
_MOMENTUM_EXIT_MIN_HOLD_SECONDS = 4 * 3600
_MOMENTUM_EXIT_COOLDOWN_SECONDS = 24 * 3600
_MOMENTUM_EXIT_TYPE       = "EXIT_MOMENTUM_LOST"
_BIOME_MISMATCH_LOG_INTERVAL_SECONDS = 60.0

_CONFIDENCE_THRESHOLD    = 0.6
_WATCHTOWER_POSITION_SCALE = min(
    1.0,
    max(0.0, float(os.getenv("WATCHTOWER_POSITION_SCALE", "0.5"))),
)


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
        self._seen_watchtower_ids: set[str] = set()

        # Open posities: tracking voor dedup-guard (naast ledger)
        self._open_symbols: set[str] = set()

        # Bijhouden van handelstijd per positie voor TTL-noodrem
        # position_id → geaccumuleerde handelsseconden
        self._trading_seconds: dict[str, float] = {}

        # Peak_price per positie — wordt ook hersteld vanuit logs
        self._peak_prices: dict[str, float] = {}

        # Bron + momentum-exit state per positie
        self._position_sources: dict[str, str] = {}
        self._momentum_miss_counts: dict[str, int] = {}
        self._momentum_exit_pending: set[str] = set()
        self._momentum_cooldowns: dict[str, datetime] = {}
        self._biome_mismatch_log_times: dict[str, float] = {}

        # Ledger-herstel bij herstart
        self._restore_from_logs()

        if self._open_symbols:
            self._log.info(
                "EquitiesPaperAnt herstart gedetecteerd — %d positie(s) hersteld in ledger",
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
        open_before = len(self._ledger.open_positions)
        stats = {
            "scout": 0,
            "research": 0,
            "approved": 0,
            "filtered": 0,
        }
        if in_market:
            scout_stats = self._process_scout_signals()
            stats["scout"] += scout_stats["received"]
            stats["filtered"] += scout_stats["filtered"]
            stats["approved"] += scout_stats["opened"]

            for processor in (
                self._process_breakout_signals,
                self._process_dividend_candidates,
                self._process_watchtower_candidates,
            ):
                result = processor()
                stats["approved"] += result["opened"]
                stats["filtered"] += result["filtered"]
        else:
            self._log.info(
                "EqPaper markt gesloten — entries overgeslagen | regime=%s open_posities=%d",
                self._latest_equity_regime(),
                len(self._ledger.open_positions),
            )

        opened = max(0, len(self._ledger.open_positions) - open_before)
        self._log.info(
            "paper tick | regime=%s scout=%d research=%d approved=%d filtered=%s opened=%d",
            self._latest_equity_regime(),
            stats["scout"],
            stats["research"],
            stats["approved"],
            stats["filtered"],
            opened,
        )
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
        Evalueer alle open posities op trailing stop, harde SL, momentum-exit en TTL.

        advance_trading_time: True als de markt open is — telt 1 seconde op bij
        alle open posities zodat handelsseconden voor dashboard/logs bijgehouden worden.
        """
        sector_top5 = self._latest_sector_scout_top_symbols(limit=5)
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

            # --- Exit-condities ---
            exit_type: str | None = None
            exit_price = price

            trailing_stop_price = peak * (1.0 - _TRAILING_STOP_PCT)
            hard_sl_price       = entry * (1.0 - _HARD_SL_PCT)

            if price < hard_sl_price:
                exit_type = "hard_stop_loss"
            elif price < trailing_stop_price:
                exit_type = "trailing_stop"
            elif (
                pos_id in self._momentum_exit_pending
                and self._position_age_seconds(position) >= _MOMENTUM_EXIT_MIN_HOLD_SECONDS
            ):
                exit_type = _MOMENTUM_EXIT_TYPE
            elif self._position_age_seconds(position) >= self._position_ttl_seconds(position):
                exit_type = "ttl_trading_days"

            if exit_type is not None:
                self._close_position(position, exit_price, exit_type)
            else:
                self._update_momentum_exit_state(position, sector_top5)

    def _position_age_seconds(self, position: PaperPosition) -> float:
        opened_at = position.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(tz=timezone.utc) - opened_at).total_seconds())

    def _position_ttl_seconds(self, position: PaperPosition) -> int:
        source = self._position_sources.get(position.position_id, "")
        if source == "watchtower" or position.watchtower_signal_id:
            return _WATCHTOWER_TTL_SECONDS
        return _TTL_TRADING_SECONDS

    def _position_ttl_seconds_for_source(
        self,
        source: str,
        watchtower_signal_id: str | None = None,
    ) -> int:
        if source == "watchtower" or watchtower_signal_id:
            return _WATCHTOWER_TTL_SECONDS
        return _TTL_TRADING_SECONDS

    def _update_momentum_exit_state(
        self,
        position: PaperPosition,
        sector_top5: set[str] | None,
    ) -> None:
        pos_id = position.position_id
        if not self._is_momentum_exit_eligible(position):
            self._momentum_miss_counts.pop(pos_id, None)
            self._momentum_exit_pending.discard(pos_id)
            return
        if sector_top5 is None:
            return

        symbol = position.symbol.upper()
        if symbol in sector_top5:
            self._momentum_miss_counts.pop(pos_id, None)
            self._momentum_exit_pending.discard(pos_id)
            return

        misses = self._momentum_miss_counts.get(pos_id, 0) + 1
        self._momentum_miss_counts[pos_id] = misses
        if misses >= _MOMENTUM_EXIT_CONSECUTIVE_TICKS and pos_id not in self._momentum_exit_pending:
            age_seconds = self._position_age_seconds(position)
            if age_seconds < _MOMENTUM_EXIT_MIN_HOLD_SECONDS:
                self._log.info(
                    "Momentum-exit uitgesteld | %s age=%.0fs min_hold=%ds ticks=%d",
                    symbol,
                    age_seconds,
                    _MOMENTUM_EXIT_MIN_HOLD_SECONDS,
                    misses,
                )
                return
            self._momentum_exit_pending.add(pos_id)
            self._log.warning(
                "Momentum-exit gemarkeerd | %s niet in sector_scout top5 ticks=%d",
                symbol,
                misses,
            )

    def _is_momentum_exit_eligible(self, position: PaperPosition) -> bool:
        return self._position_sources.get(position.position_id, "sector_scout") == "sector_scout"

    def _set_momentum_cooldown(self, symbol: str, closed_at: datetime | None = None) -> None:
        symbol = symbol.upper().strip()
        if not symbol:
            return
        closed_at = closed_at or datetime.now(tz=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        self._momentum_cooldowns[symbol] = closed_at + timedelta(
            seconds=_MOMENTUM_EXIT_COOLDOWN_SECONDS
        )

    def _momentum_cooldown_until(self, symbol: str) -> datetime | None:
        symbol = symbol.upper().strip()
        cooldown_until = self._momentum_cooldowns.get(symbol)
        if cooldown_until is None:
            return None
        now = datetime.now(tz=timezone.utc)
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        if cooldown_until <= now:
            self._momentum_cooldowns.pop(symbol, None)
            return None
        return cooldown_until

    def _latest_sector_scout_ranking_payload(self) -> dict | None:
        """Lees de meest recente sector_scout ranking snapshot payload."""
        if self.logs_root is None:
            return None
        ranking_dir = self.logs_root / "equities" / "sector_scout"
        if not ranking_dir.exists():
            return None

        latest_ts: datetime | None = None
        latest_payload: dict | None = None
        for path in sorted(ranking_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") or {}
                    if payload.get("action") != "sector_ranking":
                        continue
                    ranking = payload.get("ranking") or []
                    if not isinstance(ranking, list):
                        continue
                    ts = _parse_iso_datetime(record.get("timestamp"))
                    if latest_ts is None or (ts is not None and ts >= latest_ts):
                        latest_ts = ts or latest_ts
                        latest_payload = payload
            except OSError:
                self._log.warning("Kan sector_scout ranking niet lezen: %s", path)

        return latest_payload

    def _latest_sector_scout_top_symbols(self, *, limit: int = 5) -> set[str] | None:
        """Lees de meest recente sector_scout ranking snapshot en retourneer top-N symbolen."""
        latest_payload = self._latest_sector_scout_ranking_payload()
        if not latest_payload:
            return None
        latest_ranking = latest_payload.get("ranking") or []
        if not latest_ranking:
            return None
        ordered = sorted(latest_ranking, key=lambda row: int(row.get("rank") or 999))
        return {
            str(row.get("symbol") or "").upper()
            for row in ordered[:limit]
            if row.get("symbol")
        }

    def _close_position(
        self, position: PaperPosition, exit_price: float, exit_type: str
    ) -> None:
        """Sluit een positie en log het event."""
        closed = position.model_copy(update={
            "status":     PositionStatus.CLOSED_STOP_LOSS
                          if exit_type in ("hard_stop_loss", "trailing_stop")
                          else PositionStatus.CLOSED_MANUAL
                          if exit_type == _MOMENTUM_EXIT_TYPE
                          else PositionStatus.CLOSED_TTL,
            "exit_price": exit_price,
            "closed_at":  datetime.now(tz=timezone.utc),
            "exit_reason": exit_type,
        })
        if exit_type == _MOMENTUM_EXIT_TYPE:
            self._set_momentum_cooldown(position.symbol, closed.closed_at)
        self._open_symbols.discard(position.symbol)
        self._trading_seconds.pop(position.position_id, None)
        self._peak_prices.pop(position.position_id, None)
        self._position_sources.pop(position.position_id, None)
        self._momentum_miss_counts.pop(position.position_id, None)
        self._momentum_exit_pending.discard(position.position_id)
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

    def _process_scout_signals(self) -> dict[str, int]:
        """Verwerk opportunity_detected signalen met biome=equities uit scouts/."""
        stats = {"received": 0, "filtered": 0, "opened": 0}
        if self.logs_root is None:
            return stats
        scout_dir = self.logs_root / "scouts"

        # Bearish-filter: bij negatief nieuws alleen top-1 sector (rank=1) toestaan
        mkt = read_latest_market_signal(self.logs_root)
        news_bearish = (mkt or {}).get("news_sentiment") == "bearish"

        if scout_dir.exists():
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
                        stats["received"] += 1

                        symbol = str(payload.get("symbol") or "").upper()
                        if not symbol:
                            stats["filtered"] += 1
                            self._log_equity_evaluation("", "FILTER", "missing_symbol")
                            continue

                        confidence = float(payload.get("confidence") or 0.0)
                        if confidence < _CONFIDENCE_THRESHOLD:
                            stats["filtered"] += 1
                            self._log_equity_evaluation(
                                symbol,
                                "FILTER",
                                f"confidence_below_threshold:{confidence:.2f}",
                            )
                            continue

                        momentum_rank = int(payload.get("momentum_rank") or 0)
                        if news_bearish and momentum_rank > 1:
                            stats["filtered"] += 1
                            self._log_equity_evaluation(
                                symbol,
                                "FILTER",
                                f"bearish_news_rank:{momentum_rank}",
                            )
                            continue

                        if self._try_open_position(symbol, source="sector_scout"):
                            stats["opened"] += 1
                        else:
                            stats["filtered"] += 1

                except OSError:
                    self._log.warning("Kan scout-log niet lezen: %s", path)

        if stats["received"] == 0:
            ranking_stats = self._process_sector_ranking_entries(news_bearish=news_bearish)
            for key in stats:
                stats[key] += ranking_stats[key]
        return stats

    def _process_sector_ranking_entries(self, *, news_bearish: bool) -> dict[str, int]:
        """Fallback-intake: gebruik nieuwste sector_ranking snapshot als entrybron."""
        stats = {"received": 0, "filtered": 0, "opened": 0}
        payload = self._latest_sector_scout_ranking_payload()
        if not payload:
            self._log.debug("EqPaper sector_scout ranking niet gevonden")
            return stats

        ranking = payload.get("ranking") or []
        if not isinstance(ranking, list) or not ranking:
            self._log.debug("EqPaper sector_scout ranking leeg of ongeldig")
            return stats

        ordered = sorted(ranking, key=lambda row: int(row.get("rank") or 999))
        top_symbols = [str(row.get("symbol") or "").upper() for row in ordered[:_SECTOR_RANKING_ENTRY_LIMIT]]
        self._log.info(
            "EqPaper sector_scout ranking ontvangen | items=%d top3=%s",
            len(ordered),
            ",".join(symbol for symbol in top_symbols if symbol),
        )

        ranking_date = str(payload.get("ranking_date") or datetime.now(tz=timezone.utc).date().isoformat())
        for row in ordered[:_SECTOR_RANKING_ENTRY_LIMIT]:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                stats["filtered"] += 1
                self._log_equity_evaluation("", "FILTER", "ranking_missing_symbol")
                continue
            signal_state = str(row.get("signal") or "").upper()
            rank = int(row.get("rank") or 999)
            if signal_state != "LONG" or rank > _SECTOR_RANKING_ENTRY_LIMIT:
                continue

            sig_id = f"sector-ranking-{symbol.lower()}-{ranking_date}"
            if sig_id in self._seen_scout_ids:
                continue
            self._seen_scout_ids.add(sig_id)
            stats["received"] += 1

            if news_bearish and rank > 1:
                stats["filtered"] += 1
                self._log_equity_evaluation(symbol, "FILTER", f"bearish_news_rank:{rank}")
                continue

            if self._try_open_position(symbol, source="sector_scout"):
                stats["opened"] += 1
            else:
                stats["filtered"] += 1
        return stats

    def _process_breakout_signals(self) -> dict[str, int]:
        """Verwerk breakout_signal records uit equities/breakout/."""
        stats = {"received": 0, "filtered": 0, "opened": 0}
        if self.logs_root is None:
            return stats
        breakout_dir = self.logs_root / "equities" / "breakout"
        if not breakout_dir.exists():
            return stats

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
                    stats["received"] += 1

                    symbol = str(payload.get("symbol") or "").upper()
                    if not symbol:
                        stats["filtered"] += 1
                        self._log_equity_evaluation("", "FILTER", "breakout_missing_symbol")
                        continue

                    if self._try_open_position(symbol, source="breakout_ant"):
                        stats["opened"] += 1
                    else:
                        stats["filtered"] += 1

            except OSError:
                self._log.warning("Kan breakout-log niet lezen: %s", path)
        return stats

    def _process_dividend_candidates(self) -> dict[str, int]:
        """Verwerk dividend_candidate records uit equities/dividend/."""
        stats = {"received": 0, "filtered": 0, "opened": 0}
        if self.logs_root is None:
            return stats
        dividend_dir = self.logs_root / "equities" / "dividend"
        if not dividend_dir.exists():
            return stats

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
                    stats["received"] += 1

                    symbol = str(payload.get("symbol") or "").upper()
                    if not symbol:
                        stats["filtered"] += 1
                        self._log_equity_evaluation("", "FILTER", "dividend_missing_symbol")
                        continue

                    if self._try_open_position(symbol, source="dividend_scout"):
                        stats["opened"] += 1
                    else:
                        stats["filtered"] += 1

            except OSError:
                self._log.warning("Kan dividend-log niet lezen: %s", path)
        return stats

    def _process_watchtower_candidates(self) -> dict[str, int]:
        """Verwerk door WatchtowerAnt geaccepteerde equity-candidates."""
        stats = {"received": 0, "filtered": 0, "opened": 0}
        if self.logs_root is None:
            return stats
        candidate_path = self.logs_root / "watchtower" / "candidates.jsonl"
        if not candidate_path.exists():
            return stats

        try:
            lines = candidate_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            self._log.warning("Kan Watchtower candidate-log niet lezen: %s", candidate_path)
            return stats

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict):
                payload = record if isinstance(record, dict) else {}
            if payload.get("action") != "watchtower_candidate":
                continue

            sig_id = str(payload.get("signal_id") or "")
            if not sig_id or sig_id in self._seen_watchtower_ids:
                continue
            self._seen_watchtower_ids.add(sig_id)
            stats["received"] += 1

            biome = str(payload.get("biome") or "equities").lower()
            if biome != "equities":
                stats["filtered"] += 1
                self._log_equity_evaluation(
                    str(payload.get("symbol") or payload.get("asset") or "").upper(),
                    "FILTER",
                    f"biome_mismatch:{biome or 'missing'}",
                )
                continue

            direction = str(payload.get("direction") or "").lower()
            if direction != "long":
                stats["filtered"] += 1
                self._log_equity_evaluation(
                    str(payload.get("symbol") or payload.get("asset") or "").upper(),
                    "FILTER",
                    f"watchtower_direction:{direction or 'missing'}",
                )
                continue

            symbol = str(payload.get("symbol") or payload.get("asset") or "").upper()
            if not symbol:
                stats["filtered"] += 1
                self._log_equity_evaluation("", "FILTER", "watchtower_missing_symbol")
                continue

            if self._try_open_position(
                symbol,
                source="watchtower",
                watchtower_signal_id=sig_id,
                position_scale=_WATCHTOWER_POSITION_SCALE,
            ):
                stats["opened"] += 1
            else:
                stats["filtered"] += 1
        return stats

    # ------------------------------------------------------------------
    # Positie openen
    # ------------------------------------------------------------------

    def _try_open_position(
        self,
        symbol: str,
        *,
        source: str,
        watchtower_signal_id: str | None = None,
        position_scale: float = 1.0,
    ) -> bool:
        """Probeer een LONG positie te openen voor het gegeven symbool."""
        symbol = symbol.upper()
        if symbol in self._open_symbols:
            self._log_equity_evaluation(symbol, "FILTER", "already_open")
            return False

        cooldown_until = self._momentum_cooldown_until(symbol)
        if cooldown_until is not None:
            self._log_equity_evaluation(
                symbol,
                "FILTER",
                f"momentum_lost_cooldown_until:{cooldown_until.isoformat()}",
            )
            return False

        if len(self._ledger.open_positions) >= _MAX_OPEN_POSITIONS:
            self._log_equity_evaluation(symbol, "FILTER", "max_positions_reached")
            return False

        price = self._fetch_price(symbol)
        if price is None or price <= 0:
            self._log_equity_evaluation(symbol, "FILTER", "no_live_price")
            return False

        capital_available = self._ledger.capital_available
        capital_fraction  = _TRADE_CAPITAL_FRACTION
        hard_sl_pct       = _HARD_SL_PCT
        position_scale = min(1.0, max(0.0, float(position_scale or 0.0)))

        # Market signal: aanpassing op positiegrootte en harde SL (trailing stop ongewijzigd)
        mkt = read_latest_market_signal(self.logs_root) if self.logs_root else None
        if mkt is not None:
            capital_fraction *= float(mkt.get("position_size_mult") or 1.0)
            hard_sl_pct      *= float(mkt.get("sl_mult") or 1.0)
            if mkt.get("combined_signal") not in (None, "normal"):
                self._log.debug(
                    "market_signal aanpassing | %s signal=%s pos_mult=%.2f sl_mult=%.2f",
                    symbol, mkt.get("combined_signal"),
                    float(mkt.get("position_size_mult") or 1.0),
                    float(mkt.get("sl_mult") or 1.0),
                )

        capital_fraction *= position_scale
        capital_per_trade = capital_available * capital_fraction
        if capital_per_trade <= 0:
            self._log_equity_evaluation(symbol, "FILTER", "no_capital_available")
            return False

        quantity = capital_per_trade / price
        sl_price = price * (1.0 - hard_sl_pct)
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
                ttl=self._position_ttl_seconds_for_source(source, watchtower_signal_id),
                current_price=price,
                peak_price=price,
                opened_at=datetime.now(tz=timezone.utc),
                watchtower_signal_id=watchtower_signal_id,
            )
        except Exception:
            self._log.exception("Kan PaperPosition niet aanmaken voor %s", symbol)
            self._log_equity_evaluation(symbol, "FILTER", "position_create_error")
            return False

        self._open_symbols.add(symbol)
        self._peak_prices[position.position_id] = price
        self._trading_seconds[position.position_id] = 0.0
        self._position_sources[position.position_id] = source
        self._ledger.record_opened(position)
        self._emit_trade_opened(position, source=source, position_scale=position_scale)
        self._last_action = f"trade_opened:{symbol}"

        self._log.info(
            "POSITIE GEOPEND | %s LONG %.6f @ %.4f  SL=%.4f  trailing=5%%  bron=%s",
            symbol, quantity, price, sl_price, source,
        )
        self._log_equity_evaluation(symbol, "OPEN", source)
        return True

    def _latest_equity_regime(self) -> str:
        if self.logs_root is None:
            return "UNKNOWN"
        try:
            from ant_colony.ants.equities.rs_regime_ant import read_latest_rs_regime

            payload = read_latest_rs_regime(self.logs_root)
            if payload and payload.get("regime"):
                return str(payload["regime"]).upper()
        except Exception:
            self._log.debug("Kan RS-regime niet lezen voor EqPaper logging", exc_info=True)
        return "UNKNOWN"

    def _log_equity_evaluation(self, symbol: str, decision: str, reason: str) -> None:
        if reason.startswith("biome_mismatch"):
            key = (symbol or "UNKNOWN").upper()
            now = time.monotonic()
            last = self._biome_mismatch_log_times.get(key, 0.0)
            if now - last < _BIOME_MISMATCH_LOG_INTERVAL_SECONDS:
                return
            self._biome_mismatch_log_times[key] = now
        self._log.info(
            "EqPaper evaluatie | symbol=%s regime=%s kapitaal=%.2f open_posities=%d max_posities=%d beslissing=%s reden=%s",
            symbol or "UNKNOWN",
            self._latest_equity_regime(),
            self._ledger.capital_available,
            len(self._ledger.open_positions),
            _MAX_OPEN_POSITIONS,
            decision,
            reason,
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
        momentum_closed_ts: dict[str, datetime] = {}

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
                        if (
                            payload.get("exit_type") == _MOMENTUM_EXIT_TYPE
                            or payload.get("exit_reason") == _MOMENTUM_EXIT_TYPE
                        ):
                            closed_at = _parse_iso_datetime(record.get("timestamp"))
                            if closed_at is None:
                                closed_at = datetime.now(tz=timezone.utc)
                            momentum_closed_ts[pos_id] = closed_at

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
            self._position_sources[pos_id] = str(
                payload.get("source")
                or ("watchtower" if payload.get("watchtower_signal_id") else "sector_scout")
            )
            restored += 1

        if restored:
            self._log.info("%d equities positie(s) hersteld in ledger", restored)

        self._restore_momentum_cooldowns(opened, momentum_closed_ts)

    def _restore_momentum_cooldowns(
        self,
        opened: dict[str, dict],
        momentum_closed_ts: dict[str, datetime],
    ) -> None:
        """Herstel recente MOMENTUM_LOST cooldowns uit append-only paper logs."""
        if not momentum_closed_ts:
            return
        now = datetime.now(tz=timezone.utc)
        restored = 0
        for pos_id, closed_at in momentum_closed_ts.items():
            opened_payload = opened.get(pos_id) or {}
            symbol = str(opened_payload.get("symbol") or "").upper().strip()
            if not symbol:
                continue
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            cooldown_until = closed_at + timedelta(seconds=_MOMENTUM_EXIT_COOLDOWN_SECONDS)
            if cooldown_until <= now:
                continue
            existing = self._momentum_cooldowns.get(symbol)
            if existing is None or cooldown_until > existing:
                self._momentum_cooldowns[symbol] = cooldown_until
                restored += 1
        if restored:
            self._log.info("%d momentum cooldown(s) hersteld uit logs", restored)

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
                ttl=self._position_ttl_seconds_for_source(
                    str(payload.get("source") or ""),
                    payload.get("watchtower_signal_id"),
                ),
                current_price=entry,
                peak_price=entry,
                opened_at=opened_at,
                watchtower_signal_id=payload.get("watchtower_signal_id"),
            )
        except Exception:
            self._log.warning(
                "Kan eq-positie %s niet reconstrueren — herstel overgeslagen", pos_id
            )
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _emit_trade_opened(
        self,
        position: PaperPosition,
        *,
        source: str,
        position_scale: float = 1.0,
    ) -> None:
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
            "position_scale":       position_scale,
            "ttl_seconds":          position.ttl,
            "ttl_days":             round(position.ttl / _SECONDS_PER_DAY, 4),
            "watchtower_signal_id": position.watchtower_signal_id,
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
        self._send_watchtower_feedback(position, exit_type, pnl_pct, pnl_net)

    def _send_watchtower_feedback(
        self,
        position: PaperPosition,
        exit_type: str,
        pnl_pct: float,
        pnl_eur: float,
    ) -> None:
        """Fire-and-forget feedback naar Watchtower na trade close."""
        if self._watchtower_client is None or not position.watchtower_signal_id:
            return

        _map = {
            "hard_stop_loss":  "SL",
            "trailing_stop":   "TRAILING",
            "ttl_trading_days": "TTL",
            _MOMENTUM_EXIT_TYPE: "MANUAL",
        }
        exit_label = _map.get(exit_type, "MANUAL")

        if position.closed_at and position.opened_at:
            duration_hours = round(
                (position.closed_at - position.opened_at).total_seconds() / 3600, 4
            )
        else:
            duration_hours = 0.0

        closed_at = position.closed_at or datetime.now(timezone.utc)

        outcome = {
            "feedback_type": "TRADE_OUTCOME",
            "signal_id":    position.watchtower_signal_id,
            "asset":        position.symbol,
            "biome":        "EQUITIES",
            "direction":    position.side.value.upper(),
            "entry_price":  float(position.entry_price),
            "exit_price":   float(position.exit_price or 0.0),
            "pnl_pct":      pnl_pct,
            "pnl_eur":      float(pnl_eur),
            "exit_reason":  exit_label,
            "duration_hours": duration_hours,
            "entry_time":   position.opened_at.isoformat() if position.opened_at else None,
            "exit_time":    closed_at.isoformat(),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "strategy_type": "watchtower" if position.watchtower_signal_id else "equities_paper",
            "open_positions_count": len(self._ledger.open_positions),
            "portfolio_heat": 0.0,
            "data_quality": "MEDIUM",
        }

        def _post_feedback() -> None:
            try:
                self._watchtower_client.post_outcome(outcome)
            except Exception:
                self._log.warning(
                    "Watchtower feedback mislukt — positie sluiting blijft geldig | signal_id=%s",
                    position.watchtower_signal_id,
                )

        threading.Thread(
            target=_post_feedback,
            daemon=True,
            name=f"wt-fb-eq-{position.position_id[:8]}",
        ).start()

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

def _parse_iso_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
