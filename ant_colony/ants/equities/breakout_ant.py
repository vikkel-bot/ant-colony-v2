"""
ant_colony/ants/equities/breakout_ant.py

BreakoutAnt — leest PiotroskiAnt-kandidaten, controleert of het aandeel
een 52-weeks high breakout bevestigt, en emitteert entry-signalen met
stop-loss (8%) en take-profit (20%) niveaus.

Pipeline positie: FundamentalAnt → PiotroskiAnt → **BreakoutAnt**

Werkwijze per tick:
  1. Scan ANT_LOGS/equities/piotroski/*.jsonl voor nieuwe piotroski_candidate entries.
  2. Per kandidaat: haal 1 jaar dagelijkse OHLCV op via de equities-adapter.
  3. Bereken 52-weeks high over alle bars.
  4. Breakout bevestigd als: (high_52w - current_close) / high_52w <= breakout_margin (5%).
  5. Bij bevestiging: emit breakout_signal naar ANT_LOGS/equities/breakout/{ant_id}.jsonl.
     Signal bevat: entry_price, sl_price (−8%), tp_price (+20%), high_52w.
  6. Dedupliceer op candidate_id — één signaal per kandidaat per dag.

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_READ_ACTION     = "piotroski_candidate"  # PiotroskiAnt output
_WRITE_ACTION    = "breakout_signal"      # eigen output
_BREAKOUT_MARGIN = 0.05   # prijs binnen 5% van 52-weeks high
_TP_PCT          = 0.20   # 20% take-profit
_SL_PCT          = 0.08   # 8% stop-loss
_MIN_CANDLES     = 2


class BreakoutAnt:
    """
    Controleert PiotroskiAnt-kandidaten op 52-weeks high breakout
    en emitteert entry-signalen.

    Args:
        ant_id:            Unieke identifier.
        mission:           Toegewezen Mission.
        scheduler:         ColonyScheduler voor heartbeat-registratie.
        biome_registry:    BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:         Pad naar ANT_LOGS. None = geen disk-logging.
        piotroski_log_dir: Override van de te lezen PiotroskiAnt log-map.
                           Standaard: logs_root/equities/piotroski/.
        breakout_margin:   Maximale afstand tot 52w-high om als breakout te tellen.
        tp_pct:            Take-profit fractie (standaard 0.20 = 20%).
        sl_pct:            Stop-loss fractie (standaard 0.08 = 8%).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        piotroski_log_dir: Path | None = None,
        breakout_margin: float = _BREAKOUT_MARGIN,
        tp_pct: float = _TP_PCT,
        sl_pct: float = _SL_PCT,
        **kwargs,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root
        self._breakout_margin = breakout_margin
        self._tp_pct          = tp_pct
        self._sl_pct          = sl_pct

        if piotroski_log_dir is not None:
            self._piotroski_log_dir = piotroski_log_dir
        elif logs_root is not None:
            self._piotroski_log_dir = logs_root / "equities" / "piotroski"
        else:
            self._piotroski_log_dir = None

        self._seen_candidate_ids: set[str] = set()
        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._log = logging.getLogger(f"ant.breakout.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "BreakoutAnt gestart | mission=%s ttl=%ds margin=%.0f%% sl=%.0f%% tp=%.0f%%",
            self.mission.mission_id, self.mission.ttl,
            self._breakout_margin * 100, self._sl_pct * 100, self._tp_pct * 100,
        )

        started_at     = datetime.now(tz=timezone.utc)
        last_heartbeat = started_at

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info("TTL verlopen — afsluiten")
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                if (now - last_heartbeat).total_seconds() >= self.mission.heartbeat_interval:
                    self._send_heartbeat()
                    last_heartbeat = datetime.now(tz=timezone.utc)

                time.sleep(self.mission.heartbeat_interval)

        except KeyboardInterrupt:
            self._log.info("BreakoutAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            self._send_heartbeat()
            self._log.info("BreakoutAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> list[dict]:
        """
        Scan PiotroskiAnt-logs, controleer breakout per kandidaat.
        Retourneert lijst van emitteerde signalen (nuttig voor tests).
        """
        if self._piotroski_log_dir is None or not self._piotroski_log_dir.exists():
            self._log.debug("Geen piotroski log-map beschikbaar — tick overgeslagen")
            self._last_action = "tick:no_log_dir"
            return []

        adapter = self.biome_registry.get(self.mission.market_scope.biome)
        if adapter is None or not adapter.is_available():
            self._log.warning("Geen equities adapter beschikbaar")
            self._last_action = "tick:no_adapter"
            return []

        get_candles_fn = getattr(adapter, "get_candles", None)
        if get_candles_fn is None:
            self._log.warning("Adapter mist get_candles() — BreakoutAnt vereist YahooFinanceAdapter")
            self._last_action = "tick:no_get_candles"
            return []

        signals = []
        for candidate_id, payload in self._scan_log_dir(self._piotroski_log_dir, _READ_ACTION):
            try:
                signal = self._check_breakout(candidate_id, payload, get_candles_fn)
                if signal is not None:
                    signals.append(signal)
                    self._write_signal(signal)
                    self._log.info(
                        "BREAKOUT SIGNAL | %s  entry=%.2f  sl=%.2f  tp=%.2f",
                        signal["symbol"],
                        signal["entry_price"],
                        signal["sl_price"],
                        signal["tp_price"],
                    )
            except Exception:
                self._log.exception("Breakout-check mislukt voor candidate_id=%s", candidate_id)

        self._last_action = f"tick:{len(signals)} signalen"
        return signals

    def _check_breakout(
        self,
        candidate_id: str,
        payload: dict,
        get_candles_fn,
    ) -> dict | None:
        """
        Controleer of het symbool een 52-weeks high breakout bevestigt.

        Breakout conditie: (high_52w - current_price) / high_52w <= breakout_margin

        Retourneert signal-dict of None als geen breakout.
        """
        symbol = payload.get("symbol", "")
        if not symbol:
            return None

        signal_id = f"breakout-{symbol.lower()}-{date.today().isoformat()}"
        if signal_id in self._seen_candidate_ids:
            return None

        candles = get_candles_fn(symbol, period="1y", interval="1d")
        if len(candles) < _MIN_CANDLES:
            self._log.debug("%s: onvoldoende candles (%d) voor breakout-check", symbol, len(candles))
            return None

        current_price = candles[-1].close
        high_52w      = max(c.high for c in candles)

        if high_52w <= 0 or current_price <= 0:
            return None

        distance = (high_52w - current_price) / high_52w
        if distance > self._breakout_margin:
            self._log.debug(
                "%s: afstand tot 52w-high = %.1f%% > %.0f%% — geen breakout",
                symbol, distance * 100, self._breakout_margin * 100,
            )
            return None

        sl_price = round(current_price * (1 - self._sl_pct), 4)
        tp_price = round(current_price * (1 + self._tp_pct), 4)

        self._seen_candidate_ids.add(signal_id)
        return {
            "signal_id":         signal_id,
            "candidate_id":      candidate_id,
            "symbol":            symbol,
            "entry_price":       round(current_price, 4),
            "sl_price":          sl_price,
            "tp_price":          tp_price,
            "sl_pct":            self._sl_pct,
            "tp_pct":            self._tp_pct,
            "high_52w":          round(high_52w, 4),
            "distance_to_high":  round(distance, 6),
            "breakout_confirmed": True,
            "emitted_at":        datetime.now(tz=timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Log scanning
    # ------------------------------------------------------------------

    def _scan_log_dir(self, log_dir: Path, action: str):
        """Yield (candidate_id, payload) voor alle nieuwe entries met het gegeven action."""
        try:
            for path in sorted(log_dir.glob("*.jsonl")):
                yield from self._scan_log_file(path, action)
        except Exception:
            self._log.exception("Log-scan mislukt voor %s", log_dir)

    def _scan_log_file(self, path: Path, action: str):
        """Yield (candidate_id, payload) voor nieuwe entries in één .jsonl bestand."""
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record  = json.loads(line)
                        payload = record.get("payload", {})
                        if payload.get("action") != action:
                            continue
                        candidate_id = payload.get("candidate_id", "")
                        if not candidate_id or candidate_id in self._seen_candidate_ids:
                            continue
                        yield candidate_id, payload
                    except (json.JSONDecodeError, AttributeError):
                        continue
        except OSError:
            self._log.exception("Kan log-bestand niet lezen: %s", path)

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_signal(self, signal: dict) -> None:
        """Schrijf breakout_signal naar ANT_LOGS/equities/breakout/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":            _WRITE_ACTION,
                "signal_id":         signal["signal_id"],
                "candidate_id":      signal["candidate_id"],
                "symbol":            signal["symbol"],
                "entry_price":       signal["entry_price"],
                "sl_price":          signal["sl_price"],
                "tp_price":          signal["tp_price"],
                "sl_pct":            signal["sl_pct"],
                "tp_pct":            signal["tp_pct"],
                "high_52w":          signal["high_52w"],
                "distance_to_high":  signal["distance_to_high"],
                "breakout_confirmed": signal["breakout_confirmed"],
                "emitted_at":        signal["emitted_at"],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "breakout" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon signaal niet naar disk schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
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
