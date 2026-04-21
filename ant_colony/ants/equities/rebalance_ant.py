"""
ant_colony/ants/equities/rebalance_ant.py

RebalanceAnt — berekent doelgewichten op basis van DividendScoutAnt- en
VolatilityAnt-output en schrijft herbalanceer-orders.

Strategie: Defensief Dividend + Volatiliteit Hedge (Setup 3 uit CLAUDE.md)

Basis doelgewichten:
  70% dividend aristocrats  (DividendScoutAnt-kandidaten)
  20% laag-volatiliteit ETFs
  10% VIX hedge

Aanpassingen op basis van VIX-regime (VolatilityAnt-output):
  NORMAL:        basis gewichten
  HEDGE:         60% dividend / 20% low-vol / 20% hedge
  REDUCE_EQUITY: 45% dividend / 20% low-vol / 35% hedge

Herbalanceer triggers:
  - Kwartaal (elke ≥ 90 dagen)
  - VIX-regime veranderd t.o.v. de vorige rebalance

Output: ANT_LOGS/equities/rebalance/{ant_id}.jsonl — action = "rebalance_order"
  payload bevat: target_weights, dividend_symbols, low_vol_symbols,
                 vix_regime, trigger, rebalance_date

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed als beide bronnen niet beschikbaar zijn (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Laag-volatiliteit ETFs (vaste universe voor de 20% low-vol allocatie)
_LOW_VOL_ETFS: list[str] = ["USMV", "SPLV", "SPHD", "EFAV", "ACWV"]

# Basis doelgewichten (som = 1.0)
_BASE_WEIGHTS: dict[str, float] = {
    "dividend": 0.70,
    "low_vol":  0.20,
    "hedge":    0.10,
}

# Gecorrigeerde gewichten per VIX-regime
_WEIGHTS_BY_REGIME: dict[str, dict[str, float]] = {
    "NORMAL":        {"dividend": 0.70, "low_vol": 0.20, "hedge": 0.10},
    "HEDGE":         {"dividend": 0.60, "low_vol": 0.20, "hedge": 0.20},
    "REDUCE_EQUITY": {"dividend": 0.45, "low_vol": 0.20, "hedge": 0.35},
}

_QUARTERLY_DAYS = 90  # herbalanceer minimaal elke 90 dagen


class RebalanceAnt:
    """
    Leest DividendScoutAnt- en VolatilityAnt-output, bepaalt doelgewichten
    en schrijft herbalanceer-orders naar ANT_LOGS/equities/rebalance/.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry (niet actief gebruikt, voor conformiteit).
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

    # Rebalance-check elk uur; kwartaaltrigger zit in de tick-logica zelf.
    _TICK_INTERVAL: int = 3600

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry,
        logs_root: Path | None = None,
        **kwargs,
    ) -> None:
        self.ant_id         = ant_id
        self.mission        = mission
        self.scheduler      = scheduler
        self.biome_registry = biome_registry
        self.logs_root      = logs_root

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._last_tick_at: float = 0.0

        # State voor triggerlogica
        self._last_rebalance_date: date | None = None
        self._last_vix_regime: str             = ""

        self._log = logging.getLogger(f"ant.rebalance.{ant_id[:8]}")

        # Herstel vorige rebalance-state uit logs bij herstart
        self._restore_state()

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "RebalanceAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id, self.mission.ttl,
        )

        started_at = datetime.now(tz=timezone.utc)

        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info("TTL verlopen — afsluiten")
                    self._status = AntStatus.COMPLETED
                    break

                now_mono = time.monotonic()
                if now_mono - self._last_tick_at >= self._TICK_INTERVAL:
                    self._last_tick_at = now_mono
                    self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("RebalanceAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("RebalanceAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> dict | None:
        """
        Bepaal of herbalancering nodig is. Zo ja: bereken doelgewichten
        en schrijf een rebalance_order.
        Retourneert het order-dict of None (nuttig voor tests).
        """
        vix_regime       = self._load_latest_vix_regime()
        dividend_symbols = self._load_dividend_symbols()
        today            = date.today()

        trigger = self._determine_trigger(today, vix_regime)
        if trigger is None:
            self._last_action = f"tick:no_trigger vix={vix_regime or 'unknown'}"
            return None

        weights = _WEIGHTS_BY_REGIME.get(vix_regime or "NORMAL", _BASE_WEIGHTS)

        order = {
            "action":           "rebalance_order",
            "rebalance_date":   today.isoformat(),
            "trigger":          trigger,
            "vix_regime":       vix_regime or "NORMAL",
            "target_weights":   weights,
            "dividend_symbols": dividend_symbols,
            "low_vol_symbols":  _LOW_VOL_ETFS,
        }

        self._last_rebalance_date = today
        self._last_vix_regime     = vix_regime or "NORMAL"
        self._write_order(order)
        self._last_action = f"tick:rebalance trigger={trigger} vix={vix_regime}"

        self._log.info(
            "REBALANCE | trigger=%s vix=%s weights=%s",
            trigger, vix_regime,
            {k: f"{v:.0%}" for k, v in weights.items()},
        )
        return order

    def _determine_trigger(self, today: date, vix_regime: str | None) -> str | None:
        """
        Retourneer trigger-label als herbalancering nodig is, anders None.

        Triggers:
          "initial"        — eerste keer
          "quarterly"      — ≥ 90 dagen geleden
          "vix_regime_change" — VIX-regime veranderd t.o.v. vorige rebalance
        """
        if self._last_rebalance_date is None:
            return "initial"

        days_since = (today - self._last_rebalance_date).days
        if days_since >= _QUARTERLY_DAYS:
            return "quarterly"

        current_regime = vix_regime or "NORMAL"
        if current_regime != self._last_vix_regime:
            return "vix_regime_change"

        return None

    # ------------------------------------------------------------------
    # Input laden
    # ------------------------------------------------------------------

    def _load_latest_vix_regime(self) -> str | None:
        """
        Lees het meest recente VIX-regime uit ANT_LOGS/equities/volatility/*.jsonl.
        Retourneert regime-string of None als geen data.
        """
        if self.logs_root is None:
            return None

        vol_dir = self.logs_root / "equities" / "volatility"
        if not vol_dir.exists():
            return None

        latest_date  = ""
        latest_regime: str | None = None

        for path in vol_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "vix_signal":
                        continue
                    sig_date = str(payload.get("signal_date") or "")
                    if sig_date > latest_date:
                        latest_date   = sig_date
                        latest_regime = str(payload.get("regime") or "NORMAL")
            except OSError:
                pass

        return latest_regime

    def _load_dividend_symbols(self) -> list[str]:
        """
        Haal unieke dividend-kandidaten op uit ANT_LOGS/equities/dividend/*.jsonl.
        Retourneert lijst van symbolen (leeg als geen data).
        """
        if self.logs_root is None:
            return []

        div_dir = self.logs_root / "equities" / "dividend"
        if not div_dir.exists():
            return []

        symbols: set[str] = set()
        for path in div_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "dividend_candidate":
                        continue
                    sym = str(payload.get("symbol") or "").strip().upper()
                    if sym:
                        symbols.add(sym)
            except OSError:
                pass

        return sorted(symbols)

    # ------------------------------------------------------------------
    # State herstel bij herstart
    # ------------------------------------------------------------------

    def _restore_state(self) -> None:
        """
        Lees de meest recente rebalance_order uit ANT_LOGS/equities/rebalance/
        om _last_rebalance_date en _last_vix_regime te herstellen.
        """
        if self.logs_root is None:
            return

        rebalance_dir = self.logs_root / "equities" / "rebalance"
        if not rebalance_dir.exists():
            return

        latest_date_str = ""
        latest_payload: dict | None = None

        for path in rebalance_dir.glob("*.jsonl"):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if payload.get("action") != "rebalance_order":
                        continue
                    reb_date = str(payload.get("rebalance_date") or "")
                    if reb_date > latest_date_str:
                        latest_date_str = reb_date
                        latest_payload  = payload
            except OSError:
                pass

        if latest_payload is not None:
            try:
                self._last_rebalance_date = date.fromisoformat(latest_date_str)
            except ValueError:
                self._last_rebalance_date = None
            self._last_vix_regime = str(latest_payload.get("vix_regime") or "NORMAL")
            self._log.debug(
                "Vorige rebalance hersteld | date=%s regime=%s",
                self._last_rebalance_date, self._last_vix_regime,
            )

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_order(self, order: dict) -> None:
        """Schrijf rebalance_order naar ANT_LOGS/equities/rebalance/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload=order,
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "rebalance" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon rebalance-order niet naar disk schrijven: %s", log_path)

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
