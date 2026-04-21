"""
ant_colony/ants/equities/dividend_scout_ant.py

DividendScoutAnt — screent Dividend Aristocrats op dividendkwaliteit
en geeft een hedge-signaal af bij hoge marktspanning (VIX > 25).

Strategie: Defensief Dividend + Volatiliteit Hedge (Setup 3)
  - 70% dividend aristocrats (25+ jaar dividendgroei)
  - 20% laag-volatiliteit ETFs
  - 10% VIX hedge bij hoge marktspanning
  - Herbalanceer kwartaals

Kandidaatcriteria:
  - dividend_yield > 2%
  - consecutive_years > 25
  - payout_ratio < 80% (duurzaam dividend)

VIX-logica:
  - VIX <= 25: normale allocatie
  - VIX > 25: verhoog hedge-signaal (flag voor Queen)

Regels:
  - Plaatst geen orders (P1)
  - Fail-closed bij API-fouten (P2)
  - Alle state in het object (P7)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.biome.adapters.yahoo_finance_adapter import YahooFinanceAdapter
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission

# Dividend Aristocrats — S&P500-aandelen met 25+ jaar aaneengesloten dividendgroei
_DIVIDEND_ARISTOCRATS: list[str] = [
    "ABT", "ABBV", "AFL", "APD", "ATO", "ADP", "BDX", "CAT", "CB",
    "CVX", "CHD", "CINF", "CTAS", "CLX", "KO", "CL", "ED", "DOV",
    "ECL", "EMR", "ESS", "EXPD", "FAST", "FRT", "GPC", "GWW", "HRL",
    "ITW", "IBM", "JNJ", "KMB", "LOW", "MKC", "MCD", "MDT", "MMM",
    "NUE", "O",   "PH",  "PNR", "PCAR", "PEP", "PPG", "PG", "SHW",
    "SPGI", "SWK", "SYY", "TGT", "WMT", "GD", "XOM",
]

_VIX_SYMBOL     = "^VIX"
_VIX_THRESHOLD  = 25.0
_MIN_YIELD      = 0.02    # 2%
_MAX_YIELD      = 0.15    # 15% — hogere waarden zijn yfinance data-artefacten
_MIN_YEARS      = 25
_MAX_PAYOUT     = 0.80    # 80%
_API_DELAY_SECS = 2.0


class DividendScoutAnt:
    """
    Screent Dividend Aristocrats en geeft een VIX-hedge-signaal.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission.
        scheduler: ColonyScheduler voor heartbeat-registratie.
        adapter:   YahooFinanceAdapter instantie (injectable voor tests).
        logs_root: Pad naar ANT_LOGS. None = geen disk-logging.
    """

    # Fundamentele data verandert dagelijks; elke uur scannen is voldoende.
    _TICK_INTERVAL: int = 3600

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        biome_registry: BiomeRegistry | None = None,
        adapter: YahooFinanceAdapter | None = None,
        logs_root: Path | None = None,
        **kwargs,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root

        # Explicit adapter wins; then try registry; then create standalone YF adapter.
        if adapter is not None:
            self.adapter: YahooFinanceAdapter = adapter
        elif biome_registry is not None:
            _reg = biome_registry.get("equities")
            self.adapter = _reg if isinstance(_reg, YahooFinanceAdapter) else YahooFinanceAdapter()
        else:
            self.adapter = YahooFinanceAdapter()

        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"
        self._last_tick_at: float = 0.0
        self._log = logging.getLogger(f"ant.dividend_scout.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "equities" / "dividend"
            log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """Blokkerende tick-loop. Retourneert AntStatus bij afsluiting."""
        self._status = AntStatus.RUNNING
        self._log.info(
            "DividendScoutAnt gestart | mission=%s ttl=%ds watchlist=%d",
            self.mission.mission_id,
            self.mission.ttl,
            len(_DIVIDEND_ARISTOCRATS),
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
            self._log.info("DividendScoutAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("DividendScoutAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> dict:
        """
        Screen Dividend Aristocrats + haal VIX op.
        Retourneert dict met candidates en vix_signal (ook gebruikt door tests).
        """
        vix       = self._get_vix()
        hedge     = vix > _VIX_THRESHOLD if vix is not None else False
        candidates = []

        for symbol in _DIVIDEND_ARISTOCRATS:
            try:
                result = self._screen_symbol(symbol)
                if result is not None:
                    candidates.append(result)
                    self._write_candidate(result, vix=vix, hedge_active=hedge)
                    self._log.info(
                        "KANDIDAAT | %s  yield=%.2f%%  years=%d  payout=%.0f%%",
                        symbol,
                        result["dividend_yield"] * 100,
                        result["consecutive_years"],
                        result["payout_ratio"] * 100,
                    )
                time.sleep(_API_DELAY_SECS)
            except Exception:
                self._log.exception("Screening mislukt voor %s — overgeslagen", symbol)

        vix_signal = "HEDGE" if hedge else "NORMAL"
        if hedge:
            self._log.warning("VIX=%.1f > %.0f — hedge-signaal actief", vix, _VIX_THRESHOLD)
        else:
            self._log.info("VIX=%.1f — normale allocatie", vix or 0.0)

        self._last_action = f"tick:{len(candidates)} kandidaten vix={vix_signal}"
        return {"candidates": candidates, "vix": vix, "vix_signal": vix_signal}

    def _screen_symbol(self, symbol: str) -> dict | None:
        """Screen één Dividend Aristocrat. Retourneert kandidaat-dict of None."""
        info = self.adapter.get_dividend_info(symbol)

        yield_val   = info.get("dividend_yield", 0.0)
        years       = info.get("consecutive_years", 0)
        payout      = info.get("payout_ratio", 0.0)

        if yield_val <= _MIN_YIELD:
            self._log.debug("%s: yield %.2f%% <= %.0f%% — overgeslagen", symbol, yield_val * 100, _MIN_YIELD * 100)
            return None
        if yield_val > _MAX_YIELD:
            self._log.warning(
                "%s: yield %.2f%% > %.0f%% — waarschijnlijk data-artefact, overgeslagen",
                symbol, yield_val * 100, _MAX_YIELD * 100,
            )
            return None
        if years <= _MIN_YEARS:
            self._log.debug("%s: %d jaren <= %d — overgeslagen", symbol, years, _MIN_YEARS)
            return None
        if payout > _MAX_PAYOUT:
            self._log.debug("%s: payout %.0f%% > %.0f%% — overgeslagen", symbol, payout * 100, _MAX_PAYOUT * 100)
            return None

        return {
            "symbol":           symbol,
            "dividend_yield":   round(yield_val, 6),
            "consecutive_years": years,
            "payout_ratio":     round(payout, 6),
            "screened_at":      datetime.now(tz=timezone.utc).isoformat(),
        }

    def _get_vix(self) -> float | None:
        """Haal huidige VIX-waarde op. Retourneert None bij fout."""
        try:
            candles = self.adapter.get_candles(_VIX_SYMBOL, period="5d", interval="1d")
            if not candles:
                return None
            return candles[-1].close
        except Exception:
            self._log.exception("VIX ophalen mislukt")
            return None

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(
        self,
        candidate: dict,
        vix: float | None,
        hedge_active: bool,
    ) -> None:
        """Schrijf kandidaat naar ANT_LOGS/equities/dividend/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":           "dividend_candidate",
                "symbol":           candidate["symbol"],
                "dividend_yield":   candidate["dividend_yield"],
                "consecutive_years": candidate["consecutive_years"],
                "payout_ratio":     candidate["payout_ratio"],
                "vix":              vix,
                "hedge_active":     hedge_active,
                "screened_at":      candidate["screened_at"],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "equities" / "dividend" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet naar disk schrijven: %s", log_path)

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
