"""
ant_colony/ants/watchtower_ant.py

WatchtowerAnt — pollt Watchtower entry-intelligence en filtert bruikbare signalen.

Verantwoordelijkheden:
  1. Poll Watchtower elke WATCHTOWER_POLL_INTERVAL seconden (standaard 300s).
  2. Filter signalen: entry_score >= min, confidence >= min, geen HIGH_RISK.
  3. Schrijf gefilterde signalen naar ANT_LOGS/watchtower/signals.jsonl.
  4. Route high-confidence niet-crypto signalen naar candidates.jsonl.
  5. Log hoeveel signalen ontvangen, gefilterd en naar Queen/Paper doorgestuurd zijn.
  6. Log "Watchtower offline, degrading gracefully" elke poll als offline.

Regels:
  - Geen live orders; alleen candidate-intake voor de bestaande paper pipeline.
  - Gooit nooit een exception naar buiten.
  - Als Watchtower offline: colony draait gewoon door.
  - Één JSONL-regel per poll (append-only).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.clients.watchtower_client import WatchtowerClient
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.heartbeat import Heartbeat
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Constanten (overschrijfbaar via env vars)
# ---------------------------------------------------------------------------

_POLL_INTERVAL  = int(os.getenv("WATCHTOWER_POLL_INTERVAL", "300"))
_MIN_ENTRY_SCORE = float(os.getenv("WATCHTOWER_MIN_ENTRY_SCORE", "0.6"))
_MIN_CONFIDENCE  = float(os.getenv("WATCHTOWER_MIN_CONFIDENCE", "0.5"))

_MAX_SIGNAL_AGE_SECONDS = 2 * 3600
_MAX_DAILY_WATCHTOWER_ENTRIES = 3
_MAX_ASSET_ENTRY_INTERVAL_SECONDS = 24 * 3600
_CRYPTO_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "LINK", "LTC"})
_COMMODITY_ASSETS = frozenset({"BRENT", "WTI", "NATGAS", "COPPER", "SILVER", "GOLD"})


@dataclass(frozen=True)
class WatchtowerCandidate:
    """Kandidaat die WatchtowerAnt overdraagt aan de bestaande paper intake."""

    asset: str
    biome: str
    direction: str
    entry_score: float
    confidence: float
    signal_id: str
    source: str
    created_at: datetime
    reason: str

    def to_payload(self) -> dict:
        return {
            "action": "watchtower_candidate",
            "asset": self.asset,
            "symbol": self.asset,
            "biome": self.biome,
            "direction": self.direction,
            "entry_score": self.entry_score,
            "confidence": self.confidence,
            "signal_id": self.signal_id,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "reason": self.reason,
        }


class WatchtowerAnt:
    """
    Entry-intelligence monitor die Watchtower pollt en signalen filtert.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission (capital_limit=0, observatie-only).
        scheduler: ColonyScheduler voor heartbeats.
        logs_root: Pad naar ANT_LOGS root directory.
        client:    WatchtowerClient instantie.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None,
        client: WatchtowerClient,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root
        self._client   = client

        self._log = logging.getLogger(f"ant.watchtower.{ant_id[:8]}")
        self._out_dir = (Path(logs_root) / "watchtower") if logs_root else None

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Hoofdlus: poll elke WATCHTOWER_POLL_INTERVAL seconden. Blokkeert indefinitely."""
        if self._out_dir is not None:
            self._out_dir.mkdir(parents=True, exist_ok=True)

        self._log.info(
            "WatchtowerAnt gestart | ant_id=%s  interval=%ds  url=%s",
            self.ant_id, _POLL_INTERVAL, self._client.base_url,
        )
        while True:
            try:
                self._tick()
            except Exception:
                self._log.exception("WatchtowerAnt tick onverwachte fout — gaat door")
            time.sleep(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Interne methoden
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        signals = self._client.get_signals()

        # Offline detectie: get_signals() zet last_get_succeeded=False bij fout
        if not self._client.last_get_succeeded:
            self._log.warning(
                "Watchtower offline, degrading gracefully | url=%s",
                self._client.base_url,
            )
            self._send_heartbeat(now, "tick:offline")
            return

        received = len(signals)
        filtered: list[dict] = []
        rejections: list[dict] = []
        for signal in signals:
            asset = signal.get("asset") or signal.get("symbol")
            direction = signal.get("direction")
            entry_score = _safe_float(signal.get("entry_score"))
            confidence = _safe_float(signal.get("confidence"))
            self._log.info(
                "Watchtower signaal ontvangen | asset=%s direction=%s score=%.3f confidence=%.3f",
                asset or "UNKNOWN",
                direction or "UNKNOWN",
                entry_score,
                confidence,
            )
            reasons: list[str] = []
            reason_code: str | None = None
            if entry_score < _MIN_ENTRY_SCORE:
                reasons.append("entry_score_below_threshold")
                reason_code = "score_too_low"
            if confidence < _MIN_CONFIDENCE:
                reasons.append("confidence_below_threshold")
                reason_code = reason_code or "score_too_low"
            if "HIGH_RISK" in signal.get("risk_flags", []):
                reasons.append("risk_flag_high_risk")
                reason_code = "high_risk"

            if reasons:
                rejections.append({
                    "signal_id": signal.get("signal_id") or signal.get("id"),
                    "asset": asset,
                    "direction": direction,
                    "timestamp": signal.get("timestamp") or signal.get("created_at"),
                    "entry_score": signal.get("entry_score"),
                    "confidence": signal.get("confidence"),
                    "risk_flags": signal.get("risk_flags", []),
                    "rejection_reason": reason_code or "asset_blocked",
                    "detail_reason": "; ".join(reasons),
                })
            else:
                filtered.append(signal)

        self._log.info(
            "Watchtower poll | interval=%ds  ontvangen=%d  door_filter=%d  afgewezen=%d",
            _POLL_INTERVAL, received, len(filtered), len(rejections),
        )

        candidates, candidate_rejections = self._route_candidates(now, filtered)
        self._write_filtered(now, rejections + candidate_rejections)

        # altijd schrijven — ook bij 0 gefilterd (voor dashboard stats)
        self._write_snapshot(
            now,
            received,
            filtered,
            rejections,
            candidates_accepted=len(candidates),
            candidate_rejections=candidate_rejections,
        )
        self._send_heartbeat(now, f"tick:received={received} passed={len(filtered)}")

    def _route_candidates(
        self,
        now: datetime,
        signals: list[dict],
    ) -> tuple[list[WatchtowerCandidate], list[dict]]:
        """Filter Watchtower-signalen naar paper-candidates voor de equities-pipeline."""
        candidates: list[WatchtowerCandidate] = []
        rejections: list[dict] = []
        open_symbols = self._read_open_equity_symbols()
        daily_limits = self._load_daily_limits(now)
        limits_changed = False

        for signal in signals:
            candidate, reason = self._build_candidate(signal, now, open_symbols, daily_limits)
            if candidate is None:
                rejection = self._candidate_rejection_record(signal, reason)
                rejections.append(rejection)
                self._log.info(
                    "Watchtower signaal gefilterd | asset=%s reden=%s",
                    rejection.get("asset") or "UNKNOWN",
                    reason,
                )
                continue

            candidates.append(candidate)
            self._record_daily_accept(candidate.asset, now, daily_limits)
            limits_changed = True
            self._log.info(
                "Watchtower kandidaat doorgestuurd | asset=%s score=%.3f conf=%.3f",
                candidate.asset,
                candidate.entry_score,
                candidate.confidence,
            )

        if candidates:
            self._write_candidates(now, candidates)
        if limits_changed:
            self._save_daily_limits(daily_limits)

        return candidates, rejections

    def _build_candidate(
        self,
        signal: dict,
        now: datetime,
        open_symbols: set[str],
        daily_limits: dict,
    ) -> tuple[WatchtowerCandidate | None, str]:
        asset = str(signal.get("asset") or signal.get("symbol") or "").upper().strip()
        if not asset:
            return None, "asset_blocked"

        biome = _infer_signal_biome(signal, asset)
        if biome == "crypto":
            return None, "biome_mismatch"

        direction = str(signal.get("direction") or "").lower().strip()
        if direction != "long":
            return None, "asset_blocked"

        entry_score = _safe_float(signal.get("entry_score"))
        if entry_score < _MIN_ENTRY_SCORE:
            return None, "score_too_low"

        confidence = _safe_float(signal.get("confidence"))
        if confidence < _MIN_CONFIDENCE:
            return None, "score_too_low"

        signal_id = str(signal.get("signal_id") or signal.get("id") or "").strip()
        if not signal_id:
            return None, "asset_blocked"

        created_at = _parse_signal_datetime(
            signal.get("created_at") or signal.get("timestamp") or signal.get("emitted_at")
        )
        if created_at is None:
            return None, "asset_blocked"
        if (now - created_at).total_seconds() > _MAX_SIGNAL_AGE_SECONDS:
            return None, "asset_blocked"
        if created_at > now.replace(microsecond=999999):
            return None, "asset_blocked"

        if asset in open_symbols:
            return None, "asset_blocked"

        rate_reason = self._daily_limit_rejection(asset, now, daily_limits)
        if rate_reason:
            return None, rate_reason

        return WatchtowerCandidate(
            asset=asset,
            biome=biome,
            direction="long",
            entry_score=entry_score,
            confidence=confidence,
            signal_id=signal_id,
            source="watchtower",
            created_at=created_at,
            reason=str(signal.get("reason") or ""),
        ), ""

    def _candidate_rejection_record(self, signal: dict, reason: str) -> dict:
        return {
            "signal_id": signal.get("signal_id") or signal.get("id"),
            "asset": signal.get("asset") or signal.get("symbol"),
            "biome": _infer_signal_biome(signal, str(signal.get("asset") or signal.get("symbol") or "")),
            "direction": signal.get("direction"),
            "timestamp": signal.get("timestamp") or signal.get("created_at"),
            "entry_score": signal.get("entry_score"),
            "confidence": signal.get("confidence"),
            "risk_flags": signal.get("risk_flags", []),
            "rejection_reason": _coarse_rejection_reason(reason),
            "detail_reason": reason,
        }

    def _write_filtered(self, now: datetime, rejections: list[dict]) -> None:
        """Schrijf per gefilterd Watchtower-signaal een append-only auditregel."""
        if self._out_dir is None or not rejections:
            return
        self._out_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._out_dir / "filtered.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                for rejection in rejections:
                    record = {
                        "timestamp": now.isoformat(),
                        "source": self.ant_id,
                        "payload": {
                            "action": "watchtower_signal_filtered",
                            **rejection,
                        },
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._log.exception("Kon watchtower filtered log niet schrijven: %s", log_path)

    def _write_candidates(self, now: datetime, candidates: list[WatchtowerCandidate]) -> None:
        if self._out_dir is None:
            return
        self._out_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._out_dir / "candidates.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                for candidate in candidates:
                    record = {
                        "timestamp": now.isoformat(),
                        "source": self.ant_id,
                        "payload": candidate.to_payload(),
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._log.exception("Kon watchtower candidates niet schrijven: %s", log_path)

    def _read_open_equity_symbols(self) -> set[str]:
        if self.logs_root is None:
            return set()
        paper_dir = Path(self.logs_root) / "paper"
        if not paper_dir.exists():
            return set()

        opened: dict[str, str] = {}
        closed: set[str] = set()
        for path in sorted(paper_dir.glob("*.jsonl")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") or {}
                    action = payload.get("action")
                    pos_id = str(payload.get("position_id") or "")
                    if action == "trade_opened" and str(payload.get("biome") or "") == "equities":
                        symbol = str(payload.get("symbol") or "").upper().strip()
                        if symbol:
                            opened[pos_id or f"symbol:{symbol}"] = symbol
                    elif action == "trade_closed" and pos_id:
                        closed.add(pos_id)
            except OSError:
                continue

        return {symbol for pos_id, symbol in opened.items() if pos_id not in closed}

    def _daily_limits_path(self) -> Path | None:
        if self._out_dir is None:
            return None
        return self._out_dir / "daily_limits.json"

    def _load_daily_limits(self, now: datetime) -> dict:
        today = now.date().isoformat()
        default = {"date": today, "total": 0, "assets": {}}
        path = self._daily_limits_path()
        if path is None or not path.exists():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(data, dict):
            return default
        assets = data.get("assets")
        if not isinstance(assets, dict):
            assets = {}
        if data.get("date") != today:
            return {"date": today, "total": 0, "assets": assets}
        return {
            "date": today,
            "total": int(data.get("total") or 0),
            "assets": assets,
        }

    def _save_daily_limits(self, data: dict) -> None:
        path = self._daily_limits_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self._log.exception("Kon watchtower daily limits niet schrijven: %s", path)

    def _daily_limit_rejection(self, asset: str, now: datetime, data: dict) -> str | None:
        if int(data.get("total") or 0) >= _MAX_DAILY_WATCHTOWER_ENTRIES:
            return "daily_limit_total"
        asset_info = (data.get("assets") or {}).get(asset) or {}
        last_accepted = _parse_signal_datetime(asset_info.get("last_accepted_at"))
        if last_accepted is not None:
            age = (now - last_accepted).total_seconds()
            if 0 <= age < _MAX_ASSET_ENTRY_INTERVAL_SECONDS:
                return "daily_limit_asset_24h"
        return None

    def _record_daily_accept(self, asset: str, now: datetime, data: dict) -> None:
        assets = data.setdefault("assets", {})
        info = dict(assets.get(asset) or {})
        info["count"] = int(info.get("count") or 0) + 1
        info["last_accepted_at"] = now.isoformat()
        assets[asset] = info
        data["total"] = int(data.get("total") or 0) + 1

    def _write_snapshot(
        self,
        now: datetime,
        total_received: int,
        filtered: list[dict],
        rejections: list[dict] | None = None,
        *,
        candidates_accepted: int = 0,
        candidate_rejections: list[dict] | None = None,
    ) -> None:
        """Schrijf poll-resultaat naar ANT_LOGS/watchtower/signals.jsonl."""
        if self._out_dir is None:
            return

        self._out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp":     now.strftime("%Y-%m-%dT%H:%M:%S"),
            "poll_interval": _POLL_INTERVAL,
            "received":      total_received,
            "passed_filter": len(filtered),
            "signals":       filtered,
            "rejections":    rejections or [],
            "candidates_accepted": candidates_accepted,
            "candidate_rejections": candidate_rejections or [],
        }
        log_path = self._out_dir / "signals.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self._log.exception("Kon watchtower snapshot niet schrijven: %s", log_path)

    def _send_heartbeat(self, now: datetime, last_action: str) -> None:
        try:
            self.scheduler.record_heartbeat(
                Heartbeat(
                    ant_id=self.ant_id,
                    mission_id=self.mission.mission_id,
                    node_id=self.mission.allowed_node,
                    timestamp=now,
                    budget_used=0.0,
                    last_action=last_action,
                )
            )
        except Exception:
            self._log.exception("WatchtowerAnt heartbeat mislukt — poll-loop gaat door")


def _safe_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _infer_signal_biome(signal: dict, asset: str) -> str:
    """Leid het doel-biome af uit Watchtower metadata zonder nieuwe trading paths."""
    for key in ("biome", "asset_class", "source_field"):
        value = str(signal.get(key) or "").lower().strip()
        if value in {"crypto", "cryptocurrency"}:
            return "crypto"
        if value in {"equity", "equities", "stock", "stocks"}:
            return "equities"
        if value in {"commodity", "commodities"}:
            return "commodity"

    asset = asset.upper().strip()
    if asset in _COMMODITY_ASSETS:
        return "commodity"
    if asset in _CRYPTO_ASSETS or asset.endswith(("-EUR", "-USD", "-BTC", "-USDT")):
        return "crypto"
    return "equities"


def _coarse_rejection_reason(reason: str | None) -> str:
    """Normaliseer filterredenen voor het append-only filtered.jsonl dashboardspoor."""
    reason = (reason or "").lower()
    if "high_risk" in reason or "risk_flag" in reason:
        return "high_risk"
    if "biome" in reason:
        return "biome_mismatch"
    if "score" in reason or "confidence" in reason:
        return "score_too_low"
    return "asset_blocked"


def _parse_signal_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
