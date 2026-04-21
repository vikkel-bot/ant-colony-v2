"""
ant_colony/ants/equities/volatility_ant.py

VolatilityAnt — monitort de VIX dagelijks en emitteert marktspanning-signalen.

Strategie: Defensief Dividend + Volatiliteit Hedge (Setup 3 uit CLAUDE.md)

VIX-signalen:
  VIX <= _VIX_HEDGE_THRESHOLD (25)  → "NORMAL"          (normale allocatie)
  VIX >  _VIX_HEDGE_THRESHOLD (25)  → "HEDGE"           (verhoog hedge-positie)
  VIX >  _VIX_REDUCE_THRESHOLD (35) → "REDUCE_EQUITY"   (verklein equities significant)

Eén signaal per dag (dedup op datum). Het signaal bevat de ruwe VIX-waarde,
het regime-label en een severity-score (0.0–1.0 genormaliseerd op 0–50 VIX).

Output: ANT_LOGS/equities/volatility/{ant_id}.jsonl — action = "vix_signal"
  vix_value    Actuele VIX-slotkoers
  regime       "NORMAL" | "HEDGE" | "REDUCE_EQUITY"
  severity     Genormaliseerde score 0.0–1.0
  signal_date  ISO-datum van het signaal
  biome        biome_id uit de missie

RebalanceAnt leest dit format automatisch.

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten: geen signaal als VIX onleesbaar is (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

_VIX_SYMBOL          = "^VIX"
_VIX_HEDGE_THRESHOLD   = 25.0   # boven dit niveau: HEDGE
_VIX_REDUCE_THRESHOLD  = 35.0   # boven dit niveau: REDUCE_EQUITY
_VIX_SEVERITY_MAX      = 50.0   # VIX-waarde die overeenkomt met severity = 1.0

_REGIME_NORMAL        = "NORMAL"
_REGIME_HEDGE         = "HEDGE"
_REGIME_REDUCE_EQUITY = "REDUCE_EQUITY"


class VolatilityAnt:
    """
    Monitort VIX dagelijks en emitteert marktspanning-signalen naar
    ANT_LOGS/equities/volatility/{ant_id}.jsonl.

    Args:
        ant_id:          Unieke identifier (UUID-string).
        mission:         Toegewezen Mission.
        scheduler:       ColonyScheduler voor heartbeat-registratie.
        biome_registry:  BiomeRegistry met YahooFinanceAdapter geregistreerd.
        logs_root:       Pad naar ANT_LOGS. None = geen disk-logging.
    """

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
        self._emitted_dates: set[str] = set()

        self._log = logging.getLogger(f"ant.volatility.{ant_id[:8]}")

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "VolatilityAnt gestart | mission=%s ttl=%ds hedge_at=%.0f reduce_at=%.0f",
            self.mission.mission_id, self.mission.ttl,
            _VIX_HEDGE_THRESHOLD, _VIX_REDUCE_THRESHOLD,
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

                self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("VolatilityAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("VolatilityAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> dict | None:
        """
        Lees VIX, bepaal regime, emiteer signaal indien nieuw voor vandaag.
        Retourneert signaal-dict of None (nuttig voor tests).
        """
        today = date.today().isoformat()
        if today in self._emitted_dates:
            self._last_action = "tick:dedup"
            return None

        vix = self._get_vix()
        if vix is None:
            self._log.warning("VIX niet beschikbaar — tick overgeslagen (fail-closed)")
            self._last_action = "tick:no_vix"
            return None

        regime   = self._classify_vix(vix)
        severity = min(vix / _VIX_SEVERITY_MAX, 1.0)

        signal = {
            "action":      "vix_signal",
            "signal_date": today,
            "vix_value":   round(vix, 2),
            "regime":      regime,
            "severity":    round(severity, 4),
            "biome":       self.mission.market_scope.biome,
        }

        self._emitted_dates.add(today)
        self._write_signal(signal)
        self._last_action = f"tick:vix={vix:.1f} regime={regime}"

        self._log.info(
            "VIX SIGNAAL | vix=%.1f regime=%s severity=%.2f",
            vix, regime, severity,
        )
        return signal

    # ------------------------------------------------------------------
    # VIX ophalen en classificeren
    # ------------------------------------------------------------------

    def _get_vix(self) -> float | None:
        """
        Haal actuele VIX-slotkoers op via de adapter.
        Retourneert None bij iedere fout (fail-closed).
        """
        try:
            adapter = self.biome_registry.get(self.mission.market_scope.biome)
            if adapter is None or not adapter.is_available():
                return None

            get_candles_fn = getattr(adapter, "get_candles", None)
            if get_candles_fn is None:
                return None

            candles = get_candles_fn(_VIX_SYMBOL, period="5d", interval="1d")
            if not candles:
                return None
            return candles[-1].close
        except Exception:
            self._log.exception("VIX ophalen mislukt")
            return None

    @staticmethod
    def _classify_vix(vix: float) -> str:
        """Retourneer het regime-label op basis van de VIX-waarde."""
        if vix > _VIX_REDUCE_THRESHOLD:
            return _REGIME_REDUCE_EQUITY
        if vix > _VIX_HEDGE_THRESHOLD:
            return _REGIME_HEDGE
        return _REGIME_NORMAL

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_signal(self, signal: dict) -> None:
        """Schrijf vix_signal naar ANT_LOGS/equities/volatility/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload=signal,
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "volatility" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon VIX-signaal niet naar disk schrijven: %s", log_path)

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
