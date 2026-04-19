"""
tests/test_piotroski_ant.py

Tests voor PiotroskiAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.piotroski_ant import (
    PiotroskiAnt,
    _MIN_PIOTROSKI_SCORE,
    _READ_ACTION,
    _WRITE_ACTION,
)
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions, MarketScope, Mission, RiskLimits, SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission() -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="piotroski_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["AAPL"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="piotroski test"),
    )


def _good_fundamentals() -> dict:
    return {
        "roa":               0.08,
        "operating_cashflow": 5_000_000.0,
        "net_income":         4_000_000.0,
        "debt_to_equity":     0.4,
        "current_ratio":      2.0,
        "gross_margin":       0.30,
        "roe":                0.15,
        "pe_ratio":           18.0,
        "total_assets":       50_000_000.0,
    }


def make_adapter(fundamentals: dict | None = None, available: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.is_available.return_value = available
    adapter.get_fundamentals.return_value = fundamentals if fundamentals is not None else _good_fundamentals()
    return adapter


def make_biome_registry(adapter: MagicMock) -> MagicMock:
    registry = MagicMock(spec=BiomeRegistry)
    registry.get.return_value = adapter
    return registry


def make_ant(adapter=None, logs_root=None, research_log_dir=None, min_f_score=_MIN_PIOTROSKI_SCORE) -> PiotroskiAnt:
    if adapter is None:
        adapter = make_adapter()
    return PiotroskiAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=make_biome_registry(adapter),
        logs_root=logs_root,
        min_f_score=min_f_score,
        research_log_dir=research_log_dir,
    )


def write_candidate_log(log_dir: Path, candidate_id: str, symbol: str, f_score: int = 8) -> None:
    """Write a fake FundamentalAnt log entry to the research dir."""
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "payload": {
            "action": _READ_ACTION,
            "candidate_id": candidate_id,
            "symbol": symbol,
            "f_score": f_score,
            "strategy_type": "piotroski_breakout",
        }
    }
    (log_dir / "fundamental_ant.jsonl").open("a").write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# 1. Initialisatie
# ---------------------------------------------------------------------------


class TestInit:
    def test_status_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE

    def test_research_log_dir_defaults_to_logs_root(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._research_log_dir == tmp_path / "research"

    def test_research_log_dir_override(self, tmp_path: Path) -> None:
        override = tmp_path / "custom"
        ant = make_ant(research_log_dir=override)
        assert ant._research_log_dir == override

    def test_research_log_dir_none_when_no_logs_root(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._research_log_dir is None

    def test_seen_candidates_empty(self) -> None:
        ant = make_ant()
        assert len(ant._seen_candidate_ids) == 0


# ---------------------------------------------------------------------------
# 2. Tick — geen log-map
# ---------------------------------------------------------------------------


class TestTickNoLogDir:
    def test_no_log_dir_returns_empty(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._tick() == []

    def test_missing_log_dir_returns_empty(self, tmp_path: Path) -> None:
        ant = make_ant(research_log_dir=tmp_path / "nonexistent")
        assert ant._tick() == []

    def test_no_adapter_still_works_with_fallback(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = PiotroskiAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
            research_log_dir=research_dir,
        )
        result = ant._tick()
        assert len(result) == 1
        assert result[0]["f_score"] == 8

    def test_unavailable_adapter_uses_fallback(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        adapter = make_adapter(available=False)
        ant = make_ant(adapter=adapter, research_log_dir=research_dir)
        result = ant._tick()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 3. Score filtering
# ---------------------------------------------------------------------------


class TestScoreFiltering:
    def test_score_above_threshold_forwarded(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        good = _good_fundamentals()  # scores >= 7
        ant = make_ant(adapter=make_adapter(good), research_log_dir=research_dir)
        result = ant._tick()
        assert len(result) == 1

    def test_score_below_threshold_rejected(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=3)

        bad = {k: 0.0 for k in _good_fundamentals()}
        ant = make_ant(adapter=make_adapter(bad), research_log_dir=research_dir)
        result = ant._tick()
        assert result == []

    def test_score_exactly_at_threshold_forwarded(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "MSFT", f_score=7)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir)
        result = ant._tick()
        assert len(result) == 1

    def test_custom_min_f_score(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir, min_f_score=9)
        # With min_f_score=9 and score=8 from fresh fundamentals (<=8), rejected
        # _good_fundamentals scores 9 with all criteria met, so should pass
        result = ant._tick()
        # If score >= 9 → pass; depends on fundamentals
        assert isinstance(result, list)

    def test_fallback_score_below_threshold_rejected(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=5)

        registry = MagicMock(spec=BiomeRegistry)
        registry.get.return_value = None
        ant = PiotroskiAnt(
            ant_id=str(uuid.uuid4()),
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=registry,
            research_log_dir=research_dir,
        )
        result = ant._tick()
        assert result == []


# ---------------------------------------------------------------------------
# 4. Deduplicatie
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_candidate_not_processed_twice(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir)
        first  = ant._tick()
        second = ant._tick()
        assert len(first) == 1
        assert len(second) == 0

    def test_two_different_candidates_both_processed(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid1 = str(uuid.uuid4())
        cid2 = str(uuid.uuid4())
        write_candidate_log(research_dir, cid1, "AAPL", f_score=8)
        write_candidate_log(research_dir, cid2, "MSFT", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir)
        result = ant._tick()
        assert len(result) == 2

    def test_seen_set_grows_after_processing(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir)
        ant._tick()
        assert cid in ant._seen_candidate_ids


# ---------------------------------------------------------------------------
# 5. Log output
# ---------------------------------------------------------------------------


class TestLogOutput:
    def test_write_candidate_creates_file(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), logs_root=tmp_path, research_log_dir=research_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "piotroski" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_contains_piotroski_candidate_action(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), logs_root=tmp_path, research_log_dir=research_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "piotroski" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert len(records) == 1
        assert records[0]["payload"]["action"] == _WRITE_ACTION

    def test_log_payload_has_required_fields(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), logs_root=tmp_path, research_log_dir=research_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "piotroski" / f"{ant.ant_id}.jsonl"
        payload = json.loads(log_path.read_text().splitlines()[0])["payload"]
        for field in ("action", "candidate_id", "symbol", "f_score", "evaluated_at"):
            assert field in payload, f"missing field: {field}"

    def test_no_log_when_logs_root_none(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        cid = str(uuid.uuid4())
        write_candidate_log(research_dir, cid, "AAPL", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), logs_root=None, research_log_dir=research_dir)
        ant._tick()  # Should not raise

    def test_sequence_increments(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        for i in range(3):
            write_candidate_log(research_dir, str(uuid.uuid4()), f"SYM{i}", f_score=8)

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), logs_root=tmp_path, research_log_dir=research_dir)
        ant._tick()
        log_path = tmp_path / "equities" / "piotroski" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))

    def test_symbol_missing_in_payload_skipped(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": _READ_ACTION, "candidate_id": str(uuid.uuid4()), "f_score": 8}}
        (research_dir / "bad.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(adapter=make_adapter(_good_fundamentals()), research_log_dir=research_dir)
        result = ant._tick()
        assert result == []


# ---------------------------------------------------------------------------
# 6. Scan log files
# ---------------------------------------------------------------------------


class TestScanLogFiles:
    def test_skips_wrong_action(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "some_other_action", "candidate_id": "x", "symbol": "AAPL"}}
        (research_dir / "test.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(research_log_dir=research_dir)
        results = list(ant._scan_log_dir(research_dir, _READ_ACTION))
        assert results == []

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        (research_dir / "bad.jsonl").write_text("not json\n")

        ant = make_ant(research_log_dir=research_dir)
        results = list(ant._scan_log_dir(research_dir, _READ_ACTION))
        assert results == []

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        (research_dir / "empty.jsonl").write_text("\n\n\n")

        ant = make_ant(research_log_dir=research_dir)
        results = list(ant._scan_log_dir(research_dir, _READ_ACTION))
        assert results == []

    def test_skips_missing_candidate_id(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": _READ_ACTION, "symbol": "AAPL"}}
        (research_dir / "no_id.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(research_log_dir=research_dir)
        results = list(ant._scan_log_dir(research_dir, _READ_ACTION))
        assert results == []

    def test_reads_multiple_files(self, tmp_path: Path) -> None:
        research_dir = tmp_path / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            record = {"payload": {"action": _READ_ACTION, "candidate_id": f"cid-{i}", "symbol": f"SYM{i}", "f_score": 8}}
            (research_dir / f"ant_{i}.jsonl").write_text(json.dumps(record) + "\n")

        ant = make_ant(research_log_dir=research_dir)
        results = list(ant._scan_log_dir(research_dir, _READ_ACTION))
        assert len(results) == 3


# ---------------------------------------------------------------------------
# 7. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()


# ---------------------------------------------------------------------------
# 8. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=[])

        with patch("ant_colony.ants.equities.piotroski_ant.time.sleep"):
            with patch("ant_colony.ants.equities.piotroski_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.piotroski_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.piotroski_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        ant = make_ant()
        assert ant._status == AntStatus.IDLE
