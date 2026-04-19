"""
tests/test_operator_ant.py

Tests voor OperatorAnt — menselijke input → ingestion kandidaat.

Scenarios:
  1.  URL input → candidate geschreven naar ingestion log
  2.  Tekst input met keywords → candidate geschreven
  3.  URL duplicaat (zelfde URL in ingestion) → processed met "al bekend"
  4.  Keyword duplicaat (>= 3 overlap) → processed met "al bekend"
  5.  Lege content → overgeslagen zonder crash
  6.  logs_root=None → geen crash
  7.  Ongeldig JSON in input bestand → overgeslagen zonder crash
  8.  Verwerkt bestand nogmaals aangeboden → niet opnieuw verwerkt (_seen_input_files)
  9.  Bevestigingsbestand geschreven naar operator/processed/
  10. Code input → type correct bepaald uit bestandsnaam
  11. _type_from_filename helper
  12. _extract_url helper
  13. _extract_keywords helper
  14. Meerdere input bestanden → elk apart verwerkt
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.ants.operator_ant import (
    OperatorAnt,
    _extract_keywords,
    _extract_url,
    _type_from_filename,
)
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "BTC-EUR"
_BIOME  = "crypto"


def make_mission() -> Mission:
    return Mission(
        mission_id="m-operator-test-001",
        ant_type="operator_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome=_BIOME, symbols=[_SYMBOL]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="operator test"),
        abort_conditions=AbortConditions(),
    )


def make_ant(tmp_path: Path) -> OperatorAnt:
    return OperatorAnt(
        ant_id="ant-operator-test-0001",
        mission=make_mission(),
        scheduler=MagicMock(),
        logs_root=tmp_path,
    )


def write_input_file(
    tmp_path: Path,
    *,
    input_type: str = "url",
    content: str = "https://github.com/example/trading-bot",
    filename: str | None = None,
) -> Path:
    input_dir = tmp_path / "operator" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    fname = filename or f"{ts}_{input_type}.json"
    path  = input_dir / fname
    path.write_text(json.dumps({"type": input_type, "content": content}), encoding="utf-8")
    return path


def write_ingestion_event(
    tmp_path: Path,
    *,
    candidate_id: str | None = None,
    source_url: str = "",
    entry_keywords: list[str] | None = None,
) -> None:
    cid = candidate_id or str(uuid.uuid4())
    ingestion_dir = tmp_path / "ingestion"
    ingestion_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "timestamp":  datetime.now(tz=timezone.utc).isoformat(),
        "source":     "ingestion-ant-001",
        "payload": {
            "action":         "candidate_ingested",
            "candidate_id":   cid,
            "source_url":     source_url,
            "entry_keywords": entry_keywords or ["rsi", "sma", "crossover", "bollinger"],
            "status":         "ingested",
        },
    }
    log_file = ingestion_dir / "ingestion-ant-001.jsonl"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_ingestion_log(tmp_path: Path) -> list[dict]:
    ingestion_dir = tmp_path / "ingestion"
    if not ingestion_dir.exists():
        return []
    records = []
    for path in ingestion_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def read_operator_log(tmp_path: Path) -> list[dict]:
    op_dir = tmp_path / "operator"
    if not op_dir.exists():
        return []
    records = []
    for path in op_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def read_processed(tmp_path: Path) -> list[dict]:
    processed_dir = tmp_path / "operator" / "processed"
    if not processed_dir.exists():
        return []
    results = []
    for path in processed_dir.glob("*.json"):
        results.append(json.loads(path.read_text(encoding="utf-8")))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_url_input_writes_ingestion_candidate(tmp_path: Path) -> None:
    """URL input → candidate geschreven naar ingestion log."""
    ant = make_ant(tmp_path)
    write_input_file(tmp_path, input_type="url", content="https://github.com/example/bot")

    ant._process_input_files()

    records = read_ingestion_log(tmp_path)
    # filter only from this ant
    operator_records = [r for r in records if r.get("source") == ant.ant_id]
    assert len(operator_records) == 1
    assert operator_records[0]["payload"]["action"] == "candidate_ingested"


def test_text_input_with_keywords_writes_candidate(tmp_path: Path) -> None:
    """Tekst input met keywords → candidate geschreven."""
    ant = make_ant(tmp_path)
    content = "RSI momentum crossover strategy with stop loss take profit exit signals"
    write_input_file(tmp_path, input_type="text", content=content)

    ant._process_input_files()

    records = read_ingestion_log(tmp_path)
    op_recs = [r for r in records if r.get("source") == ant.ant_id]
    assert len(op_recs) == 1
    payload = op_recs[0]["payload"]
    assert payload["action"] == "candidate_ingested"
    assert payload["entry_keywords"]  # keywords extracted


def test_url_duplicate_not_re_ingested(tmp_path: Path) -> None:
    """Zelfde URL al in ingestion → geen nieuwe kandidaat, processed met 'al bekend'."""
    url = "https://github.com/example/existing-bot"
    write_ingestion_event(tmp_path, source_url=url)

    ant = make_ant(tmp_path)
    write_input_file(tmp_path, input_type="url", content=url)
    ant._process_input_files()

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 0

    processed = read_processed(tmp_path)
    assert len(processed) == 1
    assert processed[0]["result"] == "duplicate"
    assert "al bekend" in processed[0]["message"]


def test_keyword_duplicate_not_re_ingested(tmp_path: Path) -> None:
    """Keyword overlap >= 3 → geen nieuwe kandidaat."""
    write_ingestion_event(
        tmp_path,
        entry_keywords=["rsi", "sma", "crossover", "bollinger", "momentum"],
    )

    ant = make_ant(tmp_path)
    content = "A strategy using RSI, SMA crossover and Bollinger bands for entry signals"
    write_input_file(tmp_path, input_type="text", content=content)
    ant._process_input_files()

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 0


def test_empty_content_skipped_no_crash(tmp_path: Path) -> None:
    """Leeg content veld → overgeslagen zonder crash."""
    ant = make_ant(tmp_path)
    input_dir = tmp_path / "operator" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / "20240101T000000_text.json"
    path.write_text(json.dumps({"type": "text", "content": ""}), encoding="utf-8")

    ant._process_input_files()  # should not raise

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 0


def test_logs_root_none_no_crash() -> None:
    """logs_root=None → geen crash."""
    ant = OperatorAnt(
        ant_id="ant-op-none",
        mission=make_mission(),
        scheduler=MagicMock(),
        logs_root=None,
    )
    ant._process_input_files()  # should not raise


def test_invalid_json_skipped(tmp_path: Path) -> None:
    """Ongeldig JSON in input bestand → overgeslagen."""
    ant = make_ant(tmp_path)
    input_dir = tmp_path / "operator" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / "20240101T000000_text.json"
    path.write_text("dit is geen json {{{{", encoding="utf-8")

    ant._process_input_files()  # should not raise


def test_seen_file_not_reprocessed(tmp_path: Path) -> None:
    """Zelfde bestand twee keer aangeboden → niet opnieuw verwerkt."""
    ant = make_ant(tmp_path)
    write_input_file(tmp_path, input_type="url", content="https://github.com/foo/bar")

    ant._process_input_files()
    ant._process_input_files()

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 1


def test_processed_confirmation_written(tmp_path: Path) -> None:
    """Altijd een bevestigingsbestand naar operator/processed/."""
    ant = make_ant(tmp_path)
    write_input_file(tmp_path, input_type="url", content="https://github.com/new/bot")

    ant._process_input_files()

    processed = read_processed(tmp_path)
    assert len(processed) == 1
    assert processed[0]["result"] == "accepted"
    assert "candidate_id" in processed[0]


def test_code_type_from_filename(tmp_path: Path) -> None:
    """Type 'code' correct bepaald uit bestandsnaam."""
    ant = make_ant(tmp_path)
    content = "def strategy(): pass  # RSI crossover with stop loss take profit exit"
    write_input_file(tmp_path, input_type="code", content=content, filename="20240101T000000_code.json")

    ant._process_input_files()

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 1
    assert op_recs[0]["payload"]["name"].startswith("operator:")


def test_multiple_inputs_each_processed(tmp_path: Path) -> None:
    """Meerdere input bestanden → elk apart verwerkt."""
    ant = make_ant(tmp_path)
    for i in range(3):
        write_input_file(
            tmp_path,
            input_type="url",
            content=f"https://github.com/example/bot-{i}",
            filename=f"2024010{i}T000000_url.json",
        )

    ant._process_input_files()

    op_recs = [r for r in read_ingestion_log(tmp_path) if r.get("source") == ant.ant_id]
    assert len(op_recs) == 3


def test_operator_log_written(tmp_path: Path) -> None:
    """Eigen activiteitslog geschreven naar operator/{ant_id}.jsonl."""
    ant = make_ant(tmp_path)
    write_input_file(tmp_path, input_type="url", content="https://github.com/foo/baz")

    ant._process_input_files()

    op_logs = read_operator_log(tmp_path)
    assert any(
        r.get("payload", {}).get("action") == "operator_input_processed"
        for r in op_logs
    )


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_type_from_filename_url() -> None:
    assert _type_from_filename("20240101T000000_url.json") == "url"


def test_type_from_filename_code() -> None:
    assert _type_from_filename("20240101T000000_code.json") == "code"


def test_type_from_filename_unknown_defaults_text() -> None:
    assert _type_from_filename("something_unknown.json") == "text"


def test_extract_url_from_sentence() -> None:
    url = _extract_url("Check out https://github.com/foo/bar for details")
    assert url == "https://github.com/foo/bar"


def test_extract_url_bare() -> None:
    url = _extract_url("https://github.com/foo/bar")
    assert url == "https://github.com/foo/bar"


def test_extract_keywords_finds_known_terms() -> None:
    text = "RSI momentum crossover with stop loss and take profit exit signals"
    kws = _extract_keywords(text)
    assert "rsi" in kws
    assert "momentum" in kws
    assert "crossover" in kws
    assert "stop loss" in kws
