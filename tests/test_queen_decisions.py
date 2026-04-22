"""
tests/test_queen_decisions.py

Tests voor GET /api/queen/decisions endpoint en _parse_queen_decision helper.
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
    _parse_queen_decision,
    _read_queen_decisions,
)
from ant_colony.dashboard.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client(ctx: ColonyContext) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=True)


def _queen_record(
    timestamp: str,
    verhogen: list | None = None,
    verlagen: list | None = None,
    gevolgd: list | None = None,
    genegeerd: list | None = None,
    prioriteit: list | None = None,
    deprio: list | None = None,
    alloc: dict | None = None,
) -> dict:
    return {
        "timestamp": timestamp,
        "kapitaal_verhogen": verhogen or [],
        "kapitaal_verlagen": verlagen or [],
        "adviezen_gevolgd": gevolgd or [],
        "adviezen_genegeerd": genegeerd or [],
        "prioriteit_kandidaten": prioriteit or [],
        "deprioriteer_kandidaten": deprio or [],
        "allocatie_aanpassingen": alloc or {},
        "allocatie_toegepast": None,
    }


def _write_decisions(tmp: Path, records: list[dict]) -> None:
    d = tmp / "queen"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "decisions.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _old_iso(days: int = 8) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# GET /api/queen/decisions — via HTTP client
# ---------------------------------------------------------------------------

class TestQueenDecisionsEndpoint:

    def test_no_logs_root_returns_empty(self):
        r = _client(ColonyContext()).get("/api/queen/decisions")
        assert r.status_code == 200
        d = r.json()
        assert d["decisions"] == []
        assert d["count"] == 0
        assert d["has_more"] is False

    def test_no_decisions_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions")
            d = r.json()
            assert d["decisions"] == []
            assert d["count"] == 0

    def test_returns_decisions_sorted_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(tz=timezone.utc)
            older = (now - timedelta(hours=2)).isoformat()
            newer = (now - timedelta(hours=1)).isoformat()
            _write_decisions(Path(tmp), [
                _queen_record(older, gevolgd=["eerste"]),
                _queen_record(newer, gevolgd=["tweede"]),
            ])
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions")
            d = r.json()
            assert d["count"] == 2
            assert d["decisions"][0]["timestamp"] == newer
            assert d["decisions"][1]["timestamp"] == older

    def test_limit_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(tz=timezone.utc)
            records = [
                _queen_record((now - timedelta(hours=i)).isoformat(), gevolgd=[f"item {i}"])
                for i in range(8)
            ]
            _write_decisions(Path(tmp), records)
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions?limit=5")
            d = r.json()
            assert d["count"] == 5
            assert len(d["decisions"]) == 5

    def test_has_more_true_when_more_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(tz=timezone.utc)
            records = [
                _queen_record((now - timedelta(hours=i)).isoformat(), gevolgd=[f"item {i}"])
                for i in range(6)
            ]
            _write_decisions(Path(tmp), records)
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions?limit=5")
            d = r.json()
            assert d["has_more"] is True

    def test_has_more_false_when_all_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [_queen_record(_now_iso(), gevolgd=["ok"])]
            _write_decisions(Path(tmp), records)
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions?limit=10")
            d = r.json()
            assert d["has_more"] is False

    def test_old_records_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_decisions(Path(tmp), [
                _queen_record(_old_iso(8), gevolgd=["oud"]),
                _queen_record(_now_iso(), gevolgd=["recent"]),
            ])
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions")
            d = r.json()
            assert d["count"] == 1
            assert d["decisions"][0]["summary"] == "recent"

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            qdir = Path(tmp) / "queen"
            qdir.mkdir()
            with (qdir / "decisions.jsonl").open("w") as fh:
                fh.write("GEEN_JSON\n")
                fh.write(json.dumps(_queen_record(_now_iso(), gevolgd=["geldig"])) + "\n")
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions")
            d = r.json()
            assert d["count"] == 1

    def test_response_schema_fields_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_decisions(Path(tmp), [_queen_record(_now_iso(), verlagen=["m1"])])
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/queen/decisions")
            d = r.json()
            assert d["count"] == 1
            entry = d["decisions"][0]
            for field in ("timestamp", "decision_type", "mission_id", "title", "summary", "payload"):
                assert field in entry, f"field {field!r} ontbreekt"


# ---------------------------------------------------------------------------
# _parse_queen_decision() — unit tests
# ---------------------------------------------------------------------------

class TestParseQueenDecision:

    def test_capital_increase_type(self):
        rec = _queen_record(_now_iso(), verhogen=["mission-abc"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "capital_increase"
        assert "Kapitaal" in e.title

    def test_capital_decrease_type(self):
        rec = _queen_record(_now_iso(), verlagen=["mission-xyz"], gevolgd=["paper:ETH-EUR win_rate=10%"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "capital_decrease"
        assert "Kapitaal" in e.title
        assert "paper:ETH-EUR" in e.summary

    def test_mission_promote_type(self):
        rec = _queen_record(_now_iso(), prioriteit=["candidate-1"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "mission_promote"

    def test_mission_demote_type(self):
        rec = _queen_record(_now_iso(), deprio=["candidate-bad"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "mission_demote"

    def test_regime_change_type(self):
        rec = _queen_record(_now_iso(), alloc={"crypto": 0.6})
        e = _parse_queen_decision(rec)
        assert e.decision_type == "regime_change"

    def test_strategy_select_type(self):
        rec = _queen_record(_now_iso(), gevolgd=["Aanpassing doorgevoerd"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "strategy_select"
        assert "Aanpassingen doorgevoerd" == e.title

    def test_mission_pause_type_when_only_genegeerd(self):
        rec = _queen_record(_now_iso(), genegeerd=["onvoldoende bewijs"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "mission_pause"

    def test_neutral_type_when_all_empty(self):
        rec = _queen_record(_now_iso())
        e = _parse_queen_decision(rec)
        assert e.decision_type == "neutral"
        assert e.title == "Neutraal"

    def test_mission_id_set_for_capital_decrease(self):
        rec = _queen_record(_now_iso(), verlagen=["paper-crypto-ETH"])
        e = _parse_queen_decision(rec)
        assert e.mission_id == "paper-crypto-ETH"

    def test_mission_id_none_for_neutral(self):
        rec = _queen_record(_now_iso())
        e = _parse_queen_decision(rec)
        assert e.mission_id is None

    def test_summary_max_200_chars(self):
        long_text = "x" * 300
        rec = _queen_record(_now_iso(), gevolgd=[long_text])
        e = _parse_queen_decision(rec)
        assert len(e.summary) <= 200

    def test_payload_is_raw_record(self):
        rec = _queen_record(_now_iso(), gevolgd=["test"])
        e = _parse_queen_decision(rec)
        assert e.payload == rec

    def test_timestamp_preserved(self):
        ts = _now_iso()
        rec = _queen_record(ts)
        e = _parse_queen_decision(rec)
        assert e.timestamp == ts

    def test_multiple_verhogen_count_in_title(self):
        rec = _queen_record(_now_iso(), verhogen=["m1", "m2", "m3"])
        e = _parse_queen_decision(rec)
        assert "3" in e.title

    def test_gevolgd_as_summary_for_capital_decrease(self):
        rec = _queen_record(
            _now_iso(),
            verlagen=["paper-m1"],
            gevolgd=["paper:BTC-EUR win_rate=0.00%"],
        )
        e = _parse_queen_decision(rec)
        assert "paper:BTC-EUR" in e.summary

    def test_capital_increase_priority_over_verlagen(self):
        # verhogen heeft prioriteit boven verlagen
        rec = _queen_record(_now_iso(), verhogen=["m1"], verlagen=["m2"])
        e = _parse_queen_decision(rec)
        assert e.decision_type == "capital_increase"


# ---------------------------------------------------------------------------
# _read_queen_decisions() — helper unit tests
# ---------------------------------------------------------------------------

class TestReadQueenDecisions:

    def test_no_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _read_queen_decisions(Path(tmp), 10)
            assert res.decisions == []
            assert res.count == 0
            assert res.has_more is False

    def test_limit_5_from_10_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(tz=timezone.utc)
            records = [
                _queen_record((now - timedelta(hours=i)).isoformat(), gevolgd=[f"r{i}"])
                for i in range(10)
            ]
            _write_decisions(Path(tmp), records)
            res = _read_queen_decisions(Path(tmp), 5)
            assert res.count == 5
            assert res.has_more is True

    def test_sorted_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(tz=timezone.utc)
            _write_decisions(Path(tmp), [
                _queen_record((now - timedelta(hours=3)).isoformat(), gevolgd=["derde"]),
                _queen_record((now - timedelta(hours=1)).isoformat(), gevolgd=["eerste"]),
                _queen_record((now - timedelta(hours=2)).isoformat(), gevolgd=["tweede"]),
            ])
            res = _read_queen_decisions(Path(tmp), 10)
            assert res.count == 3
            assert "eerste" in res.decisions[0].summary
            assert "tweede" in res.decisions[1].summary
            assert "derde"  in res.decisions[2].summary

    def test_7_day_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_decisions(Path(tmp), [
                _queen_record(_old_iso(10), gevolgd=["te oud"]),
                _queen_record(_now_iso(), gevolgd=["recent"]),
            ])
            res = _read_queen_decisions(Path(tmp), 10)
            assert res.count == 1
            assert "recent" in res.decisions[0].summary

    def test_exactly_at_7_day_boundary_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = (datetime.now(tz=timezone.utc) - timedelta(days=7, minutes=1)).isoformat()
            _write_decisions(Path(tmp), [_queen_record(ts, gevolgd=["net te oud"])])
            res = _read_queen_decisions(Path(tmp), 10)
            assert res.count == 0

    def test_has_more_false_when_fewer_than_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_decisions(Path(tmp), [_queen_record(_now_iso(), gevolgd=["een"])])
            res = _read_queen_decisions(Path(tmp), 10)
            assert res.has_more is False
