"""
tests/test_claude_ant.py

Tests voor ClaudeAnt — Anthropic API integratie.

Scenarios:
  1.  Geen API key → AntStatus.ABORTED direct bij run()
  2.  Rate limit respecteren: geen tweede call binnen 300s
  3.  Geen research kandidaten → geen API call
  4.  Top-5 selectie op basis van sharpe (hoogste eerst)
  5.  Correct JSON response → variant naar ingestion geschreven
  6.  Markdown code block in response → correct geparsed
  7.  Ongeldige JSON response → geen crash, geen ingestion
  8.  Lege response → geen crash
  9.  Claude log geschreven na succesvolle analyse
  10. Ingestion log bevat candidate_ingested event na succesvolle analyse
  11. logs_root=None → geen crash
  12. _parse_response — correcte JSON array
  13. _parse_response — JSON in markdown code block
  14. _parse_response — lege string retourneert []
  15. Cost logging aanwezig in claude log
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.claude_ant import ClaudeAnt
from ant_colony.schemas.ant import AntStatus
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


def make_mission() -> Mission:
    return Mission(
        mission_id="m-claude-test-001",
        ant_type="claude_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="crypto", symbols=[_SYMBOL]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="claude test"),
        abort_conditions=AbortConditions(),
    )


def make_ant(tmp_path: Path, api_key: str = "test-key-abc") -> ClaudeAnt:
    return ClaudeAnt(
        ant_id="ant-claude-test-0001",
        mission=make_mission(),
        scheduler=MagicMock(),
        logs_root=tmp_path,
        api_key=api_key,
    )


def write_research_record(tmp_path: Path, sharpe: float = 0.8, candidate_id: str | None = None) -> str:
    cid = candidate_id or str(uuid.uuid4())
    research_dir = tmp_path / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "timestamp":  "2024-01-01T12:00:00+00:00",
        "source":     "ant-research-001",
        "payload": {
            "action":        "candidate_accepted",
            "candidate_id":  cid,
            "symbol":        _SYMBOL,
            "sharpe":        sharpe,
            "win_rate":      0.6,
            "strategy_type": "sma_crossover",
            "grade":         "A" if sharpe > 0.5 else "B",
            "best_regime":   "bull",
        },
    }
    log_file = research_dir / "ant-research-001.jsonl"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return cid


def make_mock_anthropic_response(response_text: str) -> MagicMock:
    """Maak een mock Anthropic message response."""
    content_block = MagicMock()
    content_block.text = response_text
    msg = MagicMock()
    msg.content = [content_block]
    msg.usage.input_tokens  = 500
    msg.usage.output_tokens = 200
    return msg


_VALID_ANALYSIS_JSON = json.dumps([
    {
        "candidate_id": "test-cand-001",
        "rationale": "Momentum strategie werkt in trending markten",
        "failure_modes": "Faalt in sideways markten",
        "confidence": 7,
        "improved_variant": {
            "entry_keywords": ["momentum", "rsi"],
            "take_profit_pct": 0.08,
            "stop_loss_pct": 0.04,
            "logic_summary": "Verbeterde momentum strategie met RSI filter",
        },
    }
])


def read_ingestion_records(tmp_path: Path, ant_id: str) -> list[dict]:
    path = tmp_path / "ingestion" / f"{ant_id}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def read_claude_log(tmp_path: Path, ant_id: str) -> list[dict]:
    path = tmp_path / "claude" / f"{ant_id}.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_api_key_aborts_immediately(tmp_path: Path) -> None:
    """Geen API key → run() retourneert ABORTED."""
    from ant_colony.schemas.ant import AntStatus
    ant    = make_ant(tmp_path, api_key="")
    status = ant.run()
    assert status == AntStatus.ABORTED


def test_rate_limit_respected(tmp_path: Path) -> None:
    """Tweede tick binnen 300s → geen extra API call."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    call_count = 0

    def mock_analyse(candidates):
        nonlocal call_count
        call_count += 1

    ant._analyse_with_claude = mock_analyse
    ant._last_api_call = __import__("time").monotonic()   # gesimuleerde recente call

    ant._tick()
    assert call_count == 0   # rate limit blokkeert tweede call


def test_no_research_candidates_no_api_call(tmp_path: Path) -> None:
    """Geen research logs → geen API call."""
    ant = make_ant(tmp_path)

    call_count = 0
    def mock_analyse(candidates):
        nonlocal call_count
        call_count += 1

    ant._analyse_with_claude = mock_analyse
    ant._tick()
    assert call_count == 0


def test_top3_selection_by_sharpe(tmp_path: Path) -> None:
    """Kandidaten gesorteerd op sharpe, top 3 geselecteerd."""
    ant = make_ant(tmp_path)
    for i in range(8):
        write_research_record(tmp_path, sharpe=float(i) * 0.1)

    candidates = ant._read_top_candidates()
    assert len(candidates) == 3
    # Hoogste sharpe eerst
    assert candidates[0]["sharpe"] >= candidates[1]["sharpe"]


def test_valid_response_writes_ingestion(tmp_path: Path) -> None:
    """Geldige JSON response → variant geschreven naar ingestion."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response(_VALID_ANALYSIS_JSON)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg

        ant._analyse_with_claude(ant._read_top_candidates())

    records = read_ingestion_records(tmp_path, ant.ant_id)
    assert len(records) == 1
    assert records[0]["payload"]["action"] == "candidate_ingested"


def test_markdown_code_block_in_response(tmp_path: Path) -> None:
    """Response in markdown code block → correct geparsed."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    wrapped = f"```json\n{_VALID_ANALYSIS_JSON}\n```"
    mock_msg = make_mock_anthropic_response(wrapped)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    records = read_ingestion_records(tmp_path, ant.ant_id)
    assert len(records) == 1


def test_invalid_json_response_no_crash(tmp_path: Path) -> None:
    """Ongeldige JSON response → geen crash, geen ingestion."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response("dit is geen json {{{")

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    records = read_ingestion_records(tmp_path, ant.ant_id)
    assert len(records) == 0


def test_empty_response_no_crash(tmp_path: Path) -> None:
    """Lege response → geen crash."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response("")

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())


def test_claude_log_written_after_analysis(tmp_path: Path) -> None:
    """Claude log geschreven na succesvolle analyse."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response(_VALID_ANALYSIS_JSON)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    logs = read_claude_log(tmp_path, ant.ant_id)
    assert len(logs) == 1
    assert logs[0]["payload"]["action"] == "claude_analysis_complete"


def test_cost_logged_in_claude_log(tmp_path: Path) -> None:
    """Kosten aanwezig in claude log."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response(_VALID_ANALYSIS_JSON)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    logs = read_claude_log(tmp_path, ant.ant_id)
    payload = logs[0]["payload"]
    assert "cost_usd"       in payload
    assert "total_cost_usd" in payload
    assert "input_tokens"   in payload
    assert "output_tokens"  in payload


def test_logs_root_none_no_crash() -> None:
    """logs_root=None → geen crash."""
    ant = ClaudeAnt(
        ant_id="ant-claude-none",
        mission=make_mission(),
        scheduler=MagicMock(),
        logs_root=None,
        api_key="test-key",
    )
    ant._process_input_files = MagicMock()   # noop
    ant._tick()  # should not raise


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------

def test_parse_response_valid_json_array(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    result = ant._parse_response(_VALID_ANALYSIS_JSON)
    assert len(result) == 1
    assert result[0]["confidence"] == 7


def test_parse_response_markdown_block(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    wrapped = f"```json\n{_VALID_ANALYSIS_JSON}\n```"
    result  = ant._parse_response(wrapped)
    assert len(result) == 1


def test_parse_response_empty_string(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    assert ant._parse_response("") == []


def test_parse_response_invalid_json(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    assert ant._parse_response("niet geldig json {{{{") == []


def test_parse_response_dict_wrapped_in_list(tmp_path: Path) -> None:
    ant = make_ant(tmp_path)
    single = json.dumps({"candidate_id": "x", "confidence": 5, "improved_variant": {}})
    result = ant._parse_response(single)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Budget tracking tests
# ---------------------------------------------------------------------------

def _write_cost_record(tmp_path: Path, cost_eur: float, year: int, month: int) -> None:
    """Schrijf een kostenregel naar ANT_LOGS/claude/costs.jsonl."""
    costs_path = tmp_path / "claude" / "costs.jsonl"
    costs_path.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime(year, month, 1, tzinfo=timezone.utc).isoformat()
    record = {"timestamp": ts, "ant_id": "test", "cost_eur": cost_eur, "month_total": cost_eur, "budget_eur": 10.0}
    with costs_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def test_load_month_costs_empty(tmp_path: Path) -> None:
    """Geen costs.jsonl → 0.0."""
    ant = make_ant(tmp_path)
    assert ant._load_month_costs() == 0.0


def test_load_month_costs_current_month(tmp_path: Path) -> None:
    """Kosten in huidige maand worden opgeteld."""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    _write_cost_record(tmp_path, 2.50, now.year, now.month)
    _write_cost_record(tmp_path, 1.00, now.year, now.month)
    ant = make_ant(tmp_path)
    assert abs(ant._month_cost_eur - 3.50) < 1e-6


def test_load_month_costs_ignores_other_months(tmp_path: Path) -> None:
    """Kosten uit andere maanden worden genegeerd."""
    from datetime import datetime, timezone
    now = datetime.now(tz=timezone.utc)
    prev_month = 12 if now.month == 1 else now.month - 1
    prev_year  = now.year - 1 if now.month == 1 else now.year
    _write_cost_record(tmp_path, 9.99, prev_year, prev_month)
    ant = make_ant(tmp_path)
    assert ant._month_cost_eur == 0.0


def test_append_cost_record_creates_file(tmp_path: Path) -> None:
    """_append_cost_record schrijft naar costs.jsonl."""
    ant = make_ant(tmp_path)
    ant._append_cost_record(0.05)
    costs_path = tmp_path / "claude" / "costs.jsonl"
    assert costs_path.exists()
    rec = json.loads(costs_path.read_text(encoding="utf-8").strip())
    assert rec["cost_eur"] == 0.05


def test_rate_limit_fast_below_budget_warn(tmp_path: Path) -> None:
    """Onder 80% budget → rate limit = 300s."""
    ant = make_ant(tmp_path)
    ant._month_cost_eur = 7.9   # 79% van €10
    assert ant._rate_limit == 300.0


def test_rate_limit_slow_at_budget_warn(tmp_path: Path) -> None:
    """≥80% budget → rate limit = 3600s."""
    ant = make_ant(tmp_path)
    ant._month_cost_eur = 8.0   # 80% van €10
    assert ant._rate_limit == 3600.0


def test_budget_exceeded_sets_aborted(tmp_path: Path) -> None:
    """Bij 100% budget → _tick() zet status op ABORTED."""
    ant = make_ant(tmp_path)
    ant._status = AntStatus.RUNNING
    ant._month_cost_eur = 10.0   # 100% van €10
    ant._tick()
    assert ant._status == AntStatus.ABORTED


def test_budget_exceeded_writes_log(tmp_path: Path) -> None:
    """Bij budget overschrijding wordt BUDGET_EXCEEDED gelogd."""
    ant = make_ant(tmp_path)
    ant._status = AntStatus.RUNNING
    ant._month_cost_eur = 10.0
    ant._tick()
    logs = read_claude_log(tmp_path, ant.ant_id)
    assert any(r.get("payload", {}).get("action") == "BUDGET_EXCEEDED" for r in logs)


def test_cost_appended_after_api_call(tmp_path: Path) -> None:
    """Na een succesvolle API call wordt kostenregel geschreven naar costs.jsonl."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response(_VALID_ANALYSIS_JSON)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    costs_path = tmp_path / "claude" / "costs.jsonl"
    assert costs_path.exists()


def test_budget_info_in_claude_log(tmp_path: Path) -> None:
    """claude log bevat month_cost_eur en budget_eur na analyse."""
    ant = make_ant(tmp_path)
    write_research_record(tmp_path)

    mock_msg = make_mock_anthropic_response(_VALID_ANALYSIS_JSON)

    with patch("anthropic.Anthropic") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_msg
        ant._analyse_with_claude(ant._read_top_candidates())

    logs = read_claude_log(tmp_path, ant.ant_id)
    payload = logs[0]["payload"]
    assert "month_cost_eur" in payload
    assert "budget_eur"     in payload
