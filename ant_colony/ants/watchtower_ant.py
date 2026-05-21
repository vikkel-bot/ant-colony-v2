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
_MIN_ENTRY_SCORE_COMMODITY = float(os.getenv("WATCHTOWER_MIN_ENTRY_SCORE_COMMODITY", "0.35"))
_MIN_CONFIDENCE_COMMODITY  = float(os.getenv("WATCHTOWER_MIN_CONFIDENCE_COMMODITY", "0.30"))

_MAX_SIGNAL_AGE_SECONDS = 2 * 3600
_MAX_DAILY_WATCHTOWER_ENTRIES = 10
_MAX_ASSET_ENTRY_INTERVAL_SECONDS = 24 * 3600
_CRYPTO_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "LINK", "LTC"})
_COMMODITY_ASSETS = frozenset({"BRENT", "WTI", "NATGAS", "COPPER", "SILVER", "GOLD"})
_VETO_RISK_KEYWORDS = ("macro_sensitive", "stale_news", "high_volatility", "cross_market")


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
        queen=None,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root
        self._client   = client
        self.queen     = queen

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
            self._write_veto(now, False, "watchtower_offline")
            self._send_heartbeat(now, "tick:offline")
            return

        received = len(signals)
        unique_signals, duplicate_count = self._dedupe_signals(signals)
        veto, veto_reason, veto_signal = self._evaluate_veto(unique_signals)
        self._write_veto(now, veto, veto_reason, veto_signal)
        filtered: list[dict] = []
        rejections: list[dict] = []
        for signal in unique_signals:
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
            asset = signal.get("asset") or signal.get("symbol") or ""
            biome_check = _infer_signal_biome(signal, str(asset).upper())
            min_score = _MIN_ENTRY_SCORE_COMMODITY if biome_check == "commodity" else _MIN_ENTRY_SCORE
            min_conf = _MIN_CONFIDENCE_COMMODITY if biome_check == "commodity" else _MIN_CONFIDENCE
            if entry_score < min_score:
                reasons.append("entry_score_below_threshold")
                reason_code = "score_too_low"
            if confidence < min_conf:
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
                self._register_queen_signal(signal, accepted=False, reason=reason_code or "asset_blocked")
            else:
                filtered.append(signal)

        self._log.info(
            "Watchtower poll | interval=%ds received_count=%d unique_count=%d "
            "duplicate_count=%d passed_filter_count=%d blocked_count=%d block_reasons=%s",
            _POLL_INTERVAL,
            received,
            len(unique_signals),
            duplicate_count,
            len(filtered),
            len(rejections),
            _reason_counts(rejections),
        )

        candidates, candidate_rejections = self._route_candidates(now, filtered)
        block_reasons = _reason_counts(rejections + candidate_rejections)
        self._write_filtered(now, rejections + candidate_rejections)

        # altijd schrijven — ook bij 0 gefilterd (voor dashboard stats)
        self._write_snapshot(
            now,
            received,
            len(unique_signals),
            duplicate_count,
            filtered,
            rejections,
            candidates_accepted=len(candidates),
            candidate_rejections=candidate_rejections,
            block_reasons=block_reasons,
        )
        self._send_heartbeat(now, f"tick:received={received} passed={len(filtered)}")

    def _dedupe_signals(self, signals: list[dict]) -> tuple[list[dict], int]:
        """Dedupliceer per poll voordat filters en daily limits tellen."""
        unique: list[dict] = []
        seen: set[tuple] = set()
        duplicates = 0
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            key = _signal_dedupe_key(signal)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            unique.append(signal)
        return unique, duplicates

    def _compute_veto(self, signal: dict) -> bool:
        """True = VETO (geen nieuwe entries). False = GO."""
        return self._veto_reason(signal) is not None

    def _evaluate_veto(self, signals: list[dict]) -> tuple[bool, str, dict | None]:
        """Bepaal de binaire Watchtower GO/NO-GO status voor deze poll."""
        for signal in signals:
            reason = self._veto_reason(signal)
            if reason:
                return True, reason, signal
        return False, "go", signals[0] if signals else None

    def _veto_reason(self, signal: dict) -> str | None:
        confidence = _safe_float(signal.get("confidence"))
        if confidence < 0.4:
            return f"confidence={confidence:.2f}"

        risk_flags = signal.get("risk_flags", "")
        if isinstance(risk_flags, (list, tuple, set)):
            risk_text = " ".join(str(flag) for flag in risk_flags)
        else:
            risk_text = str(risk_flags)
        risk_text = risk_text.lower()
        for keyword in _VETO_RISK_KEYWORDS:
            if keyword in risk_text:
                return f"risk_flag={keyword}"
        return None

    def _write_veto(
        self,
        now: datetime,
        veto: bool,
        reason: str,
        signal: dict | None = None,
    ) -> None:
        """Schrijf de Watchtower GO/NO-GO status voor Queen/PaperAnt."""
        if self.logs_root is None:
            return
        veto_path = Path(self.logs_root) / "queen" / "watchtower_veto.json"
        payload = {
            "veto": bool(veto),
            "reason": reason,
            "timestamp": now.isoformat(),
        }
        if signal:
            payload.update({
                "signal_id": signal.get("signal_id") or signal.get("id"),
                "asset": signal.get("asset") or signal.get("symbol"),
                "confidence": signal.get("confidence"),
                "entry_score": signal.get("entry_score"),
            })
        try:
            veto_path.parent.mkdir(parents=True, exist_ok=True)
            veto_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self._log.exception("Kon Watchtower veto-status niet schrijven: %s", veto_path)

    def _route_candidates(
        self,
        now: datetime,
        signals: list[dict],
    ) -> tuple[list[WatchtowerCandidate], list[dict]]:
        """Filter Watchtower-signalen naar paper-candidates voor de equities-pipeline."""
        candidates: list[WatchtowerCandidate] = []
        accepted_signals: list[dict] = []
        rejections: list[dict] = []
        open_symbols = self._read_open_equity_symbols()
        daily_limits = self._load_daily_limits(now)
        limits_changed = False

        for signal in signals:
            candidate, reason = self._build_candidate(signal, now, open_symbols, daily_limits)
            if candidate is None:
                rejection = self._candidate_rejection_record(signal, reason)
                rejections.append(rejection)
                self._register_queen_signal(signal, accepted=False, reason=reason)
                self._log.info(
                    "Watchtower signaal gefilterd | asset=%s reden=%s",
                    rejection.get("asset") or "UNKNOWN",
                    reason,
                )
                continue

            candidates.append(candidate)
            accepted_signals.append(signal)
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
            for signal in accepted_signals:
                self._register_queen_signal(signal, accepted=True)
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
        min_score = _MIN_ENTRY_SCORE_COMMODITY if biome == "commodity" else _MIN_ENTRY_SCORE
        if entry_score < min_score:
            return None, "score_too_low"

        confidence = _safe_float(signal.get("confidence"))
        min_conf = _MIN_CONFIDENCE_COMMODITY if biome == "commodity" else _MIN_CONFIDENCE
        if confidence < min_conf:
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

    def _register_queen_signal(self, signal: dict, *, accepted: bool, reason: str = "") -> None:
        """Meld Watchtower-flow bij Queen als die beschikbaar is."""
        if self.queen is None:
            return
        if _safe_float(signal.get("entry_score")) < 0.50:
            return
        try:
            payload = dict(signal)
            payload["queen_accepted"] = bool(accepted)
            if reason:
                payload["queen_rejection_reason"] = reason
            register = getattr(self.queen, "register_watchtower_signal", None)
            if callable(register):
                register(payload)
        except Exception:
            self._log.exception("Watchtower signaal kon niet bij Queen geregistreerd worden")

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
        unique_count: int,
        duplicate_count: int,
        filtered: list[dict],
        rejections: list[dict] | None = None,
        *,
        candidates_accepted: int = 0,
        candidate_rejections: list[dict] | None = None,
        block_reasons: dict[str, int] | None = None,
    ) -> None:
        """Schrijf poll-resultaat naar ANT_LOGS/watchtower/signals.jsonl."""
        if self._out_dir is None:
            return

        self._out_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp":     now.strftime("%Y-%m-%dT%H:%M:%S"),
            "poll_interval": _POLL_INTERVAL,
            "received":      total_received,
            "received_count": total_received,
            "unique_count":   unique_count,
            "duplicate_count": duplicate_count,
            "passed_filter": len(filtered),
            "passed_filter_count": len(filtered),
            "blocked_count": len(rejections or []) + len(candidate_rejections or []),
            "block_reasons": block_reasons or {},
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


def _reason_counts(rejections: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rejection in rejections:
        reason = str(
            rejection.get("rejection_reason")
            or rejection.get("detail_reason")
            or "unknown"
        )
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _signal_dedupe_key(signal: dict) -> tuple:
    asset = str(signal.get("asset") or signal.get("symbol") or "").upper().strip()
    direction = str(signal.get("direction") or "").lower().strip()
    window = str(
        signal.get("time_window")
        or signal.get("timeframe")
        or signal.get("window")
        or ""
    ).lower().strip()
    ts = _parse_signal_datetime(
        signal.get("published_at")
        or signal.get("created_at")
        or signal.get("timestamp")
        or signal.get("emitted_at")
    )
    hour_bucket = ts.strftime("%Y-%m-%dT%H") if ts else ""
    score = round(_safe_float(signal.get("entry_score")), 3)
    confidence = round(_safe_float(signal.get("confidence")), 3)
    if not asset and not direction and not hour_bucket:
        return ("id", str(signal.get("signal_id") or signal.get("id") or id(signal)))
    return (asset, direction, window, hour_bucket, score, confidence)


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
