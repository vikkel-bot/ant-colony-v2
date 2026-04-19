"""
ant_colony/colony/promoter.py

StrategyPromoter — beoordeelt RESEARCH-kandidaten en promoveert ze naar APPROVED.

Verantwoordelijkheden:
  - Leest ANT_LOGS/research/*.jsonl en ANT_LOGS/strategy/*.jsonl
  - Filtert kandidaten met status RESEARCH die voldoen aan hoge criteria
    (sharpe_ratio > 0.7, win_rate > 0.55, total_trades >= 20)
  - Promoveert via Queen (twee stappen: RESEARCH→PAPER, PAPER→APPROVED)
  - Schrijft APPROVED kandidaten naar ANT_LOGS/approved/{candidate_id}.jsonl
  - Dedupliceert op candidate_id (_seen_candidate_ids)

Regels:
  - Geen orders — alleen promotie (P1)
  - Fail-closed — negeert fouten (P2)
  - Queen is de enige promotie-autoriteit (P1)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ant_colony.queen.queen import Queen
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions
from ant_colony.schemas.strategy_candidate import CandidateStatus, StrategyCandidate

_SHARPE_THRESHOLD    = 0.7
_WIN_RATE_THRESHOLD  = 0.55
_MIN_TRADES          = 20

_PAPER_TTL           = 1_209_600          # 14 dagen in seconden
_PAPER_CAPITAL       = 100.0              # EUR per kandidaat
_PAPER_HEARTBEAT     = 300                # 5 minuten
_PAPER_ALLOWED_ACTIONS = ["read_data", "paper_execute", "report"]

logger = logging.getLogger(__name__)


class StrategyPromoter:
    """
    Beoordeelt RESEARCH-kandidaten en promoveert ze naar APPROVED via de Queen.

    Args:
        queen:      Queen-instantie (promotie-autoriteit).
        logs_root:  Pad naar ANT_LOGS. None = geen disk-I/O (tests).
    """

    def __init__(
        self,
        queen: Queen,
        logs_root: Path | None = None,
        node_id: str = "pc2",
    ) -> None:
        self._queen     = queen
        self._logs_root = logs_root
        self._node_id   = node_id
        self._seen_candidate_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """
        Één promotiecyclus: lees logs, beoordeel, promoveer.

        Fail-closed: negeert alle exceptions.
        """
        try:
            self._process_candidates()
        except Exception:
            logger.exception("Onverwachte fout in StrategyPromoter.tick()")

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _process_candidates(self) -> None:
        if self._logs_root is None:
            return

        for source_dir in ("research", "strategy"):
            dir_path = self._logs_root / source_dir
            if not dir_path.exists():
                continue
            for path in sorted(dir_path.glob("*.jsonl")):
                self._process_file(path, source_dir)

    def _process_file(self, path: Path, source_dir: str) -> None:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue

                candidate_dict = self._extract_candidate_dict(raw, source_dir)
                if candidate_dict is None:
                    continue

                try:
                    candidate = StrategyCandidate(**candidate_dict)
                except Exception:
                    logger.debug("Kan kandidaat niet parsen uit %s", path)
                    continue

                if candidate.candidate_id in self._seen_candidate_ids:
                    continue
                self._seen_candidate_ids.add(candidate.candidate_id)

                if candidate.status != CandidateStatus.RESEARCH:
                    continue

                if not self._meets_criteria(candidate):
                    continue

                self._promote(candidate)

        except OSError:
            logger.warning("Kan bestand niet lezen: %s", path)

    def _extract_candidate_dict(self, raw: dict, source_dir: str) -> dict | None:
        """Haal de kandidaat-dict op uit het log-record (AuditEvent wrapper of raw JSON)."""
        if source_dir == "strategy":
            # strategy logs: AuditEvent met payload.action == "variant_emitted"
            payload = raw.get("payload") or {}
            if payload.get("action") != "variant_emitted":
                return None
            # payload bevat de velden van StrategyCandidate
            return {k: v for k, v in payload.items() if k != "action"}
        else:
            # research logs: raw StrategyCandidate JSON
            if "candidate_id" not in raw:
                return None
            return raw

    def _meets_criteria(self, candidate: StrategyCandidate) -> bool:
        """Controleer of de kandidaat voldoet aan de promotie-criteria."""
        bt = candidate.backtest_results
        if bt is None:
            return False

        sharpe      = getattr(bt, "sharpe_ratio", None) or 0.0
        win_rate    = getattr(bt, "win_rate", None) or 0.0
        total_trades = getattr(bt, "total_trades", None) or 0

        passes = (
            sharpe      >= _SHARPE_THRESHOLD
            and win_rate    >= _WIN_RATE_THRESHOLD
            and total_trades >= _MIN_TRADES
        )

        if not passes:
            logger.debug(
                "Kandidaat %s voldoet niet | sharpe=%.3f win_rate=%.3f trades=%d",
                candidate.candidate_id, sharpe, win_rate, total_trades,
            )
        return passes

    def _promote(self, candidate: StrategyCandidate) -> None:
        """Promoveer RESEARCH → PAPER → APPROVED via de Queen en schrijf naar disk."""
        # Stap 1: RESEARCH → PAPER
        result1 = self._queen.promote_candidate(candidate, CandidateStatus.PAPER)
        if not result1.accepted or result1.candidate is None:
            logger.info(
                "Promotie RESEARCH→PAPER geweigerd voor %s: %s",
                candidate.candidate_id, result1.rejection_reason,
            )
            return

        # Stap 1b: automatisch paper_ant missie aanmaken
        self._issue_paper_mission(result1.candidate)

        # Stap 2: PAPER → APPROVED
        result2 = self._queen.promote_candidate(result1.candidate, CandidateStatus.APPROVED)
        if not result2.accepted or result2.candidate is None:
            logger.info(
                "Promotie PAPER→APPROVED geweigerd voor %s: %s",
                candidate.candidate_id, result2.rejection_reason,
            )
            return

        approved = result2.candidate
        logger.info(
            "Kandidaat APPROVED: %s | sharpe=%.3f",
            approved.candidate_id,
            (approved.backtest_results.sharpe_ratio if approved.backtest_results else 0.0),
        )
        self._write_approved(approved)

    def _issue_paper_mission(self, candidate: StrategyCandidate) -> None:
        """Geef automatisch een paper_ant missie uit voor de gegeven kandidaat."""
        ts  = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        mid = f"paper-auto-{candidate.candidate_id[:8]}-{ts}"

        market_scope_raw = candidate.market_scope or {}
        symbol = market_scope_raw.get("symbol") or ""
        symbols = [symbol] if symbol else list(
            market_scope_raw.get("symbols") or ["BTC-EUR"]
        )
        biome = candidate.biome or "crypto"

        try:
            mission = Mission(
                mission_id=mid,
                ant_type="paper_ant",
                allowed_node=self._node_id,
                allowed_actions=_PAPER_ALLOWED_ACTIONS,
                market_scope=MarketScope(biome=biome, symbols=symbols),
                capital_limit=_PAPER_CAPITAL,
                risk_limits=RiskLimits(
                    max_drawdown_pct=0.20,
                    max_position_size=_PAPER_CAPITAL,
                    daily_loss_limit=50.0,
                    stop_loss_required=True,
                ),
                ttl=_PAPER_TTL,
                heartbeat_interval=_PAPER_HEARTBEAT,
                success_conditions=SuccessConditions(
                    description=f"Paper test voor kandidaat {candidate.candidate_id[:8]}"
                ),
            )
        except Exception:
            logger.exception(
                "Kan paper missie niet bouwen voor kandidaat %s", candidate.candidate_id
            )
            return

        result = self._queen.issue_mission(mission)
        if result.accepted:
            logger.info(
                "Paper missie aangemaakt | mission_id=%s candidate=%s symbols=%s ttl=%dd capital=€%.0f",
                mid, candidate.candidate_id[:8], symbols, _PAPER_TTL // 86400, _PAPER_CAPITAL,
            )
        else:
            logger.warning(
                "Paper missie geweigerd | mission_id=%s candidate=%s reden=%s",
                mid, candidate.candidate_id[:8], result.rejection_reason,
            )

    def _write_approved(self, candidate: StrategyCandidate) -> None:
        """Schrijf APPROVED kandidaat naar ANT_LOGS/approved/{candidate_id}.jsonl."""
        if self._logs_root is None:
            return

        approved_dir = self._logs_root / "approved"
        try:
            approved_dir.mkdir(parents=True, exist_ok=True)
            log_path = approved_dir / f"{candidate.candidate_id}.jsonl"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(candidate.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            logger.exception("Kon approved kandidaat niet schrijven: %s", candidate.candidate_id)
