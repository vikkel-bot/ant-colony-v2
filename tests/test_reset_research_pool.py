"""
Tests voor scripts/reset_research_pool.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from reset_research_pool import (
    DEFAULT_CUTOFF,
    reset_research_pool,
    restore_research_pool,
)


def _record(
    *,
    candidate_id: str,
    timestamp: str | None,
    strategy_type: str,
) -> dict:
    record = {
        "event_type": "action_executed",
        "source": "research-ant-test",
        "payload": {
            "action": "candidate_accepted",
            "candidate_id": candidate_id,
            "strategy_type": strategy_type,
            "symbol": "BTC-EUR",
            "sharpe": 1.0,
        },
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_reset_archives_old_research_records_and_keeps_current(tmp_path: Path) -> None:
    research_path = tmp_path / "research" / "research-ant.jsonl"
    old_record = _record(
        candidate_id="old-rsi",
        timestamp="2026-05-20T23:59:00+00:00",
        strategy_type="rsi_based",
    )
    current_record = _record(
        candidate_id="current-squeeze",
        timestamp="2026-05-21T00:00:00+00:00",
        strategy_type="volatility_squeeze",
    )
    _write_jsonl(research_path, [old_record, current_record])

    result = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, yes=True)

    assert result.archived_records == 1
    assert result.current_records == 1
    active_records = _read_jsonl(research_path)
    assert [r["payload"]["candidate_id"] for r in active_records] == ["current-squeeze"]

    archived_files = list((tmp_path / "archive").rglob("research-ant.jsonl"))
    assert len(archived_files) == 1
    archived_records = _read_jsonl(archived_files[0])
    assert [r["payload"]["candidate_id"] for r in archived_records] == ["old-rsi"]


def test_reset_is_idempotent_after_old_records_are_moved(tmp_path: Path) -> None:
    research_path = tmp_path / "research" / "research-ant.jsonl"
    _write_jsonl(
        research_path,
        [
            _record(
                candidate_id="old-rsi",
                timestamp="2026-05-20T12:00:00+00:00",
                strategy_type="rsi_based",
            ),
            _record(
                candidate_id="current-mean",
                timestamp="2026-05-22T12:00:00+00:00",
                strategy_type="mean_reversion",
            ),
        ],
    )

    first = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, yes=True)
    second = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, yes=True)

    assert first.archived_records == 1
    assert second.archived_records == 0
    assert len(list((tmp_path / "archive").rglob("research-ant.jsonl"))) == 1
    active_records = _read_jsonl(research_path)
    assert [r["payload"]["candidate_id"] for r in active_records] == ["current-mean"]


def test_reset_keeps_records_without_timestamp_fail_closed(tmp_path: Path) -> None:
    research_path = tmp_path / "research" / "research-ant.jsonl"
    unknown_ts_record = _record(
        candidate_id="unknown-ts",
        timestamp=None,
        strategy_type="volatility_squeeze",
    )
    _write_jsonl(research_path, [unknown_ts_record])

    result = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, yes=True)

    assert result.archived_records == 0
    assert result.current_records == 1
    assert _read_jsonl(research_path)[0]["payload"]["candidate_id"] == "unknown-ts"
    assert not (tmp_path / "archive").exists()


def test_reset_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    research_path = tmp_path / "research" / "research-ant.jsonl"
    old_record = _record(
        candidate_id="old-rsi",
        timestamp="2026-05-20T12:00:00+00:00",
        strategy_type="rsi_based",
    )
    _write_jsonl(research_path, [old_record])
    before = research_path.read_text(encoding="utf-8")

    result = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, dry_run=True)

    assert result.archived_records == 1
    assert research_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "archive").exists()


def test_restore_research_pool_reverses_archive_without_duplicates(tmp_path: Path) -> None:
    research_path = tmp_path / "research" / "research-ant.jsonl"
    _write_jsonl(
        research_path,
        [
            _record(
                candidate_id="old-rsi",
                timestamp="2026-05-20T12:00:00+00:00",
                strategy_type="rsi_based",
            ),
            _record(
                candidate_id="current-mean",
                timestamp="2026-05-22T12:00:00+00:00",
                strategy_type="mean_reversion",
            ),
        ],
    )
    reset_result = reset_research_pool(tmp_path, cutoff=DEFAULT_CUTOFF, yes=True)
    assert reset_result.archive_dir is not None

    first_restore = restore_research_pool(tmp_path, reset_result.archive_dir, yes=True)
    second_restore = restore_research_pool(tmp_path, reset_result.archive_dir, yes=True)

    assert first_restore.restored_records == 1
    assert second_restore.restored_records == 0
    restored_ids = [r["payload"]["candidate_id"] for r in _read_jsonl(research_path)]
    assert sorted(restored_ids) == ["current-mean", "old-rsi"]
