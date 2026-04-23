"""
tests/test_activity_feed.py

Tests voor GET /api/activity endpoint en _read_activity_feed helper.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ant_colony.dashboard.api import (
    ColonyContext,
    _event_level,
    _read_activity_feed,
    _read_ant_dir_recent,
)
from ant_colony.dashboard.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client(ctx: ColonyContext) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=True)


def _now_iso(delta_seconds: int = 0) -> str:
    dt = datetime.now(tz=timezone.utc) + timedelta(seconds=delta_seconds)
    return dt.isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _ant_rec(ant_dir_path: Path, action: str, symbol: str = "BTC-EUR",
             delta_seconds: int = 0, level: str | None = None) -> dict:
    rec: dict = {
        "timestamp": _now_iso(delta_seconds),
        "payload": {"action": action, "symbol": symbol},
    }
    if level:
        rec["level"] = level
    return rec


def _queen_rec(delta_seconds: int = 0) -> dict:
    return {
        "timestamp": _now_iso(delta_seconds),
        "kapitaal_verhogen": ["paper:BTC-EUR"],
        "kapitaal_verlagen": [],
        "adviezen_gevolgd": ["paper:BTC-EUR win_rate=0.6"],
        "adviezen_genegeerd": [],
        "prioriteit_kandidaten": [],
        "deprioriteer_kandidaten": [],
        "allocatie_aanpassingen": {},
        "allocatie_toegepast": None,
    }


# ---------------------------------------------------------------------------
# _event_level tests
# ---------------------------------------------------------------------------

def test_event_level_default_is_info():
    assert _event_level({}) == "info"


def test_event_level_error():
    assert _event_level({"level": "ERROR"}) == "error"


def test_event_level_critical_maps_to_error():
    assert _event_level({"level": "critical"}) == "error"


def test_event_level_warning():
    assert _event_level({"level": "warning"}) == "warning"


def test_event_level_warning_uppercase():
    assert _event_level({"level": "WARNING"}) == "warning"


def test_event_level_info_explicit():
    assert _event_level({"level": "info"}) == "info"


# ---------------------------------------------------------------------------
# _read_ant_dir_recent tests
# ---------------------------------------------------------------------------

def test_read_ant_dir_recent_missing_dir():
    with tempfile.TemporaryDirectory() as tmp:
        result = _read_ant_dir_recent(Path(tmp), "scouts")
    assert result == []


def test_read_ant_dir_recent_reads_records():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "scouts"
        recs = [{"timestamp": _now_iso(), "payload": {"action": "signal_detected"}}]
        _write_jsonl(p / "scout_2026.jsonl", recs)
        result = _read_ant_dir_recent(Path(tmp), "scouts")
    assert len(result) == 1


def test_read_ant_dir_recent_skips_trades_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "paper"
        _write_jsonl(p / "paper_trades.jsonl", [{"timestamp": _now_iso(), "pnl": 1.0}])
        _write_jsonl(p / "paper_events.jsonl", [{"timestamp": _now_iso(), "payload": {"action": "trade_opened"}}])
        result = _read_ant_dir_recent(Path(tmp), "paper")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _read_activity_feed tests
# ---------------------------------------------------------------------------

def test_activity_feed_empty_logs_root():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        resp = _read_activity_feed(logs, 50, "all")
    assert resp.events == []
    assert resp.count == 0
    assert resp.filter == "all"


def test_activity_feed_single_scout_event():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        rec = _ant_rec(logs / "scouts", "signal_detected", delta_seconds=-10)
        _write_jsonl(logs / "scouts" / "s.jsonl", [rec])
        resp = _read_activity_feed(logs, 50, "all")
    assert resp.count == 1
    assert resp.events[0].ant_type == "scout"
    assert resp.events[0].action == "signal_detected"


def test_activity_feed_skips_older_than_24h():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat()
        rec = {"timestamp": old_ts, "payload": {"action": "signal_detected"}}
        _write_jsonl(logs / "scouts" / "s.jsonl", [rec])
        resp = _read_activity_feed(logs, 50, "all")
    assert resp.count == 0


def test_activity_feed_sorted_descending():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [
            {"timestamp": _now_iso(-300), "payload": {"action": "a1"}},
            {"timestamp": _now_iso(-100), "payload": {"action": "a2"}},
            {"timestamp": _now_iso(-500), "payload": {"action": "a3"}},
        ]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        resp = _read_activity_feed(logs, 50, "all")
    timestamps = [e.timestamp for e in resp.events]
    assert timestamps == sorted(timestamps, reverse=True)


def test_activity_feed_limit_respected():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [{"timestamp": _now_iso(-i * 10), "payload": {"action": "ev"}} for i in range(30)]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        resp = _read_activity_feed(logs, 10, "all")
    assert resp.count == 10
    assert len(resp.events) == 10


def test_activity_feed_filter_scout_only():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        scout_rec = {"timestamp": _now_iso(-10), "payload": {"action": "signal_detected"}}
        research_rec = {"timestamp": _now_iso(-20), "payload": {"action": "candidate_accepted"}}
        _write_jsonl(logs / "scouts" / "s.jsonl", [scout_rec])
        _write_jsonl(logs / "research" / "r.jsonl", [research_rec])
        resp = _read_activity_feed(logs, 50, "scout")
    assert all(e.ant_type == "scout" for e in resp.events)
    assert resp.count >= 1


def test_activity_feed_filter_research_only():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "scouts" / "s.jsonl", [{"timestamp": _now_iso(-10), "payload": {"action": "ev"}}])
        _write_jsonl(logs / "research" / "r.jsonl", [{"timestamp": _now_iso(-20), "payload": {"action": "candidate_accepted"}}])
        resp = _read_activity_feed(logs, 50, "research")
    assert all(e.ant_type == "research" for e in resp.events)


def test_activity_feed_filter_paper_only():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "paper" / "p.jsonl", [{"timestamp": _now_iso(-10), "payload": {"action": "trade_opened"}}])
        _write_jsonl(logs / "scouts" / "s.jsonl", [{"timestamp": _now_iso(-20), "payload": {"action": "ev"}}])
        resp = _read_activity_feed(logs, 50, "paper")
    assert all(e.ant_type == "paper" for e in resp.events)


def test_activity_feed_filter_errors_only():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [
            {"timestamp": _now_iso(-10), "level": "ERROR", "payload": {"action": "ev"}},
            {"timestamp": _now_iso(-20), "level": "info", "payload": {"action": "ev"}},
            {"timestamp": _now_iso(-30), "level": "warning", "payload": {"action": "ev"}},
        ]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        resp = _read_activity_feed(logs, 50, "errors")
    assert all(e.level in ("error", "warning") for e in resp.events)
    assert resp.count == 2


def test_activity_feed_filter_all_mixes_ants():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "scouts" / "s.jsonl", [{"timestamp": _now_iso(-10), "payload": {"action": "ev1"}}])
        _write_jsonl(logs / "research" / "r.jsonl", [{"timestamp": _now_iso(-20), "payload": {"action": "ev2"}}])
        _write_jsonl(logs / "paper" / "p.jsonl", [{"timestamp": _now_iso(-30), "payload": {"action": "ev3"}}])
        resp = _read_activity_feed(logs, 50, "all")
    ant_types = {e.ant_type for e in resp.events}
    assert len(ant_types) >= 3


def test_activity_feed_queen_records_included():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "queen" / "decisions.jsonl", [_queen_rec(delta_seconds=-10)])
        resp = _read_activity_feed(logs, 50, "all")
    queen_events = [e for e in resp.events if e.ant_type == "queen"]
    assert len(queen_events) == 1
    assert queen_events[0].action == "capital_increase"


def test_activity_feed_queen_filter():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "queen" / "decisions.jsonl", [_queen_rec(delta_seconds=-10)])
        _write_jsonl(logs / "scouts" / "s.jsonl", [{"timestamp": _now_iso(-20), "payload": {"action": "ev"}}])
        resp = _read_activity_feed(logs, 50, "queen")
    assert all(e.ant_type == "queen" for e in resp.events)
    assert resp.count >= 1


def test_activity_feed_cap_200():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [{"timestamp": _now_iso(-i), "payload": {"action": "ev"}} for i in range(250)]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        resp = _read_activity_feed(logs, 200, "all")
    assert resp.count <= 200


def test_activity_feed_fetched_at_set():
    with tempfile.TemporaryDirectory() as tmp:
        resp = _read_activity_feed(Path(tmp), 50, "all")
    assert resp.fetched_at is not None and len(resp.fetched_at) > 10


def test_activity_feed_count_matches_events():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [{"timestamp": _now_iso(-i * 5), "payload": {"action": "ev"}} for i in range(5)]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        resp = _read_activity_feed(logs, 50, "all")
    assert resp.count == len(resp.events)


def test_activity_feed_unknown_filter_falls_back_to_all():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        _write_jsonl(logs / "scouts" / "s.jsonl", [{"timestamp": _now_iso(-10), "payload": {"action": "ev"}}])
        resp = _read_activity_feed(logs, 50, "onbekend_type")
    assert resp.count >= 1


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

def test_endpoint_no_logs_root():
    ctx = ColonyContext(scheduler=None, queen=None, logs_root=None)
    client = _client(ctx)
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []
    assert data["count"] == 0
    assert "fetched_at" in data


def test_endpoint_default_limit_and_filter():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ColonyContext(scheduler=None, queen=None, logs_root=Path(tmp))
        client = _client(ctx)
        resp = client.get("/api/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert data["filter"] == "all"
    assert isinstance(data["events"], list)


def test_endpoint_with_events():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [{"timestamp": _now_iso(-i * 10), "payload": {"action": "signal_detected", "symbol": "ETH-EUR"}} for i in range(5)]
        _write_jsonl(logs / "scouts" / "s.jsonl", recs)
        ctx = ColonyContext(scheduler=None, queen=None, logs_root=logs)
        client = _client(ctx)
        resp = client.get("/api/activity?limit=50&filter=scout")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert all(e["ant_type"] == "scout" for e in data["events"])


def test_endpoint_limit_clamped():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ColonyContext(scheduler=None, queen=None, logs_root=Path(tmp))
        client = _client(ctx)
        resp = client.get("/api/activity?limit=999")
    assert resp.status_code == 200


def test_endpoint_filter_errors():
    with tempfile.TemporaryDirectory() as tmp:
        logs = Path(tmp)
        recs = [
            {"timestamp": _now_iso(-5), "level": "ERROR", "payload": {"action": "crash"}},
            {"timestamp": _now_iso(-10), "level": "info", "payload": {"action": "ok"}},
        ]
        _write_jsonl(logs / "audit" / "a.jsonl", recs)
        ctx = ColonyContext(scheduler=None, queen=None, logs_root=logs)
        client = _client(ctx)
        resp = client.get("/api/activity?filter=errors")
    data = resp.json()
    assert all(e["level"] in ("error", "warning") for e in data["events"])
    assert data["count"] == 1
