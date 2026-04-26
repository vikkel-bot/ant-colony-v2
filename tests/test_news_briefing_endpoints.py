"""
tests/test_news_briefing_endpoints.py

Tests voor:
  GET /api/news/latest
  GET /api/equities/briefing
  Equities paper stats in /api/equities/status
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ant_colony.dashboard.api import (
    ColonyContext,
    _read_equities_paper_stats,
    _read_latest_briefing,
    _read_latest_news,
)
from ant_colony.dashboard.server import create_app


def _client(ctx: ColonyContext) -> TestClient:
    return TestClient(create_app(ctx))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _hours_ago_iso(h: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=h)).isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")


# ---------------------------------------------------------------------------
# GET /api/news/latest — lege context
# ---------------------------------------------------------------------------

class TestNewsLatestEmpty:

    def test_returns_200(self):
        r = _client(ColonyContext()).get("/api/news/latest")
        assert r.status_code == 200

    def test_available_false_without_logs(self):
        d = _client(ColonyContext()).get("/api/news/latest").json()
        assert d["available"] is False

    def test_available_false_with_empty_log_dir(self, tmp_path: Path):
        (tmp_path / "news").mkdir()
        ctx = ColonyContext(logs_root=tmp_path)
        d = _client(ctx).get("/api/news/latest").json()
        assert d["available"] is False


# ---------------------------------------------------------------------------
# GET /api/news/latest — met snapshot
# ---------------------------------------------------------------------------

class TestNewsLatestWithSnapshot:

    def _write_snapshot(self, news_dir: Path, payload: dict) -> None:
        news_dir.mkdir(parents=True, exist_ok=True)
        path = news_dir / "20240426-100000.json"
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_available_true_with_snapshot(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "bullish",
            "sentiment_score": 0.35,
            "crypto_sentiment": "neutral",
            "crypto_score": 0.05,
            "equities_sentiment": "bearish",
            "equities_score": -0.12,
            "top_headlines": ["Headline 1", "Headline 2", "Headline 3"],
            "article_count": 42,
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/news/latest").json()
        assert d["available"] is True

    def test_sentiment_fields_populated(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "bullish",
            "sentiment_score": 0.35,
            "crypto_sentiment": "neutral",
            "crypto_score": 0.05,
            "equities_sentiment": "bearish",
            "equities_score": -0.12,
            "top_headlines": ["A", "B"],
            "article_count": 10,
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/news/latest").json()
        assert d["market_sentiment"] == "bullish"
        assert abs(d["sentiment_score"] - 0.35) < 0.001
        assert d["crypto_sentiment"] == "neutral"
        assert d["equities_sentiment"] == "bearish"

    def test_top_headlines_max_3(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "neutral",
            "sentiment_score": 0.0,
            "top_headlines": ["A", "B", "C", "D", "E"],
            "article_count": 5,
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/news/latest").json()
        assert len(d["top_headlines"]) == 3

    def test_article_count_returned(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "neutral",
            "sentiment_score": 0.0,
            "top_headlines": [],
            "article_count": 77,
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/news/latest").json()
        assert d["article_count"] == 77

    def test_weekend_flag_present(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "neutral",
            "sentiment_score": 0.0,
            "top_headlines": [],
            "article_count": 0,
            "weekend": True,
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/news/latest").json()
        assert d["weekend"] is True

    def test_reader_function_directly(self, tmp_path: Path):
        self._write_snapshot(tmp_path / "news", {
            "timestamp": _now_iso(),
            "market_sentiment": "bearish",
            "sentiment_score": -0.2,
            "top_headlines": ["X"],
            "article_count": 3,
        })
        result = _read_latest_news(tmp_path)
        assert result["available"] is True
        assert result["market_sentiment"] == "bearish"

    def test_reader_returns_unavailable_for_missing_dir(self, tmp_path: Path):
        result = _read_latest_news(tmp_path)
        assert result["available"] is False


# ---------------------------------------------------------------------------
# GET /api/equities/briefing — lege context
# ---------------------------------------------------------------------------

class TestBriefingEmpty:

    def test_returns_200(self):
        r = _client(ColonyContext()).get("/api/equities/briefing")
        assert r.status_code == 200

    def test_available_false_without_logs(self):
        d = _client(ColonyContext()).get("/api/equities/briefing").json()
        assert d["available"] is False

    def test_available_false_missing_file(self, tmp_path: Path):
        (tmp_path / "queen").mkdir()
        ctx = ColonyContext(logs_root=tmp_path)
        d = _client(ctx).get("/api/equities/briefing").json()
        assert d["available"] is False


# ---------------------------------------------------------------------------
# GET /api/equities/briefing — met data
# ---------------------------------------------------------------------------

class TestBriefingWithData:

    def _write_briefing(self, queen_dir: Path, record: dict) -> None:
        queen_dir.mkdir(parents=True, exist_ok=True)
        with (queen_dir / "briefing.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_available_true_for_fresh_briefing(self, tmp_path: Path):
        self._write_briefing(tmp_path / "queen", {
            "timestamp": _now_iso(),
            "action": "market_opening_briefing",
            "market_sentiment": "bullish",
            "top_sectors": ["XLK", "XLE"],
            "rs_regime": "TRENDING",
            "headlines": ["H1", "H2"],
            "recommendation": "Markt opent bullish.",
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/briefing").json()
        assert d["available"] is True

    def test_fields_populated(self, tmp_path: Path):
        self._write_briefing(tmp_path / "queen", {
            "timestamp": _now_iso(),
            "action": "market_opening_briefing",
            "market_sentiment": "bearish",
            "top_sectors": ["XLU"],
            "rs_regime": "SIDEWAYS",
            "headlines": ["H1"],
            "recommendation": "Markt opent bearish.",
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/briefing").json()
        assert d["market_sentiment"] == "bearish"
        assert d["top_sectors"] == ["XLU"]
        assert d["rs_regime"] == "SIDEWAYS"
        assert d["recommendation"] == "Markt opent bearish."
        assert d["headlines"] == ["H1"]

    def test_age_hours_present(self, tmp_path: Path):
        self._write_briefing(tmp_path / "queen", {
            "timestamp": _hours_ago_iso(3),
            "action": "market_opening_briefing",
            "market_sentiment": "neutral",
            "top_sectors": [],
            "rs_regime": None,
            "headlines": [],
            "recommendation": "Neutraal.",
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/briefing").json()
        assert d["available"] is True
        assert d["age_hours"] is not None
        assert 2.9 < d["age_hours"] < 3.2

    def test_unavailable_if_older_than_24h(self, tmp_path: Path):
        self._write_briefing(tmp_path / "queen", {
            "timestamp": _hours_ago_iso(25),
            "action": "market_opening_briefing",
            "market_sentiment": "neutral",
            "top_sectors": [],
            "rs_regime": None,
            "headlines": [],
            "recommendation": "Oud.",
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/briefing").json()
        assert d["available"] is False

    def test_last_record_used_when_multiple(self, tmp_path: Path):
        queen_dir = tmp_path / "queen"
        self._write_briefing(queen_dir, {
            "timestamp": _hours_ago_iso(5),
            "action": "market_opening_briefing",
            "market_sentiment": "bearish",
            "top_sectors": [], "rs_regime": None,
            "headlines": [], "recommendation": "Oud.",
        })
        self._write_briefing(queen_dir, {
            "timestamp": _now_iso(),
            "action": "market_opening_briefing",
            "market_sentiment": "bullish",
            "top_sectors": ["XLK"], "rs_regime": "TRENDING",
            "headlines": ["Nieuw"], "recommendation": "Nieuwste.",
        })
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/briefing").json()
        assert d["market_sentiment"] == "bullish"

    def test_reader_function_directly(self, tmp_path: Path):
        (tmp_path / "queen").mkdir(parents=True, exist_ok=True)
        with (tmp_path / "queen" / "briefing.jsonl").open("w") as fh:
            fh.write(json.dumps({
                "timestamp": _now_iso(),
                "action": "market_opening_briefing",
                "market_sentiment": "bullish",
                "top_sectors": [], "rs_regime": "TRENDING",
                "headlines": [], "recommendation": "Test.",
            }) + "\n")
        result = _read_latest_briefing(tmp_path)
        assert result["available"] is True
        assert result["rs_regime"] == "TRENDING"


# ---------------------------------------------------------------------------
# EquitiesPaperAnt stats in /api/equities/status
# ---------------------------------------------------------------------------

class TestEquitiesPaperStats:

    def _paper_record(self, action: str, pos_id: str, pnl: float | None = None) -> dict:
        payload: dict = {
            "action": action,
            "position_id": pos_id,
            "biome": "equities",
            "symbol": "AAPL",
            "entry_price": 150.0,
            "quantity": 1.0,
            "stop_loss": 139.5,
            "take_profit": 300.0,
        }
        if action == "trade_closed" and pnl is not None:
            payload["realized_pnl"] = pnl
        return {"payload": payload}

    def test_paper_stats_defaults_no_logs(self):
        d = _client(ColonyContext()).get("/api/equities/status").json()
        assert d["eq_paper_open"] == 0
        assert d["eq_paper_total_pnl_eur"] is None
        assert d["eq_paper_winrate"] is None
        assert d["eq_paper_closed_count"] == 0

    def test_open_count_from_logs(self, tmp_path: Path):
        paper_dir = tmp_path / "paper"
        _write_jsonl(paper_dir / "eq_ant.jsonl", [
            self._paper_record("trade_opened", "pos-1"),
            self._paper_record("trade_opened", "pos-2"),
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["eq_paper_open"] == 2

    def test_closed_position_not_counted_as_open(self, tmp_path: Path):
        paper_dir = tmp_path / "paper"
        _write_jsonl(paper_dir / "eq_ant.jsonl", [
            self._paper_record("trade_opened", "pos-1"),
            self._paper_record("trade_closed", "pos-1", pnl=10.0),
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["eq_paper_open"] == 0

    def test_winrate_calculated(self, tmp_path: Path):
        paper_dir = tmp_path / "paper"
        _write_jsonl(paper_dir / "eq_ant.jsonl", [
            self._paper_record("trade_opened",  "pos-1"),
            self._paper_record("trade_closed",  "pos-1", pnl=15.0),
            self._paper_record("trade_opened",  "pos-2"),
            self._paper_record("trade_closed",  "pos-2", pnl=-5.0),
            self._paper_record("trade_opened",  "pos-3"),
            self._paper_record("trade_closed",  "pos-3", pnl=8.0),
            self._paper_record("trade_opened",  "pos-4"),
            self._paper_record("trade_closed",  "pos-4", pnl=-2.0),
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["eq_paper_closed_count"] == 4
        assert abs(d["eq_paper_winrate"] - 0.5) < 0.01
        assert abs(d["eq_paper_total_pnl_eur"] - 16.0) < 0.01

    def test_crypto_records_ignored(self, tmp_path: Path):
        paper_dir = tmp_path / "paper"
        _write_jsonl(paper_dir / "crypto_ant.jsonl", [
            {"payload": {"action": "trade_opened", "position_id": "c1",
                         "biome": "crypto", "symbol": "BTC-EUR",
                         "entry_price": 50000.0, "quantity": 0.01,
                         "stop_loss": 48000.0, "take_profit": 60000.0}},
            {"payload": {"action": "trade_closed", "position_id": "c1",
                         "biome": "crypto", "symbol": "BTC-EUR",
                         "realized_pnl": 50.0}},
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["eq_paper_open"] == 0
        assert d["eq_paper_closed_count"] == 0

    def test_reader_function_directly(self, tmp_path: Path):
        paper_dir = tmp_path / "paper"
        _write_jsonl(paper_dir / "eq_ant.jsonl", [
            self._paper_record("trade_opened", "pos-1"),
            self._paper_record("trade_closed", "pos-1", pnl=20.0),
        ])
        result = _read_equities_paper_stats(tmp_path)
        assert result["eq_paper_open"] == 0
        assert result["eq_paper_closed_count"] == 1
        assert result["eq_paper_winrate"] == 1.0
        assert abs(result["eq_paper_total_pnl_eur"] - 20.0) < 0.01
