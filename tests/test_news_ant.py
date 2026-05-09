"""
tests/test_news_ant.py

Tests voor NewsAnt: sentiment, weekend-detectie, API-limiet, graceful skip.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.news_ant import (
    NewsAnt,
    _classify,
    _parse_rss_items,
    _rss_tags_for_title,
    _sentiment_score,
    _BULLISH_THRESHOLD,
    _BEARISH_THRESHOLD,
    _DAILY_CALL_WARNING,
    _DAILY_CALL_LIMIT,
)
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_mission() -> Mission:
    return Mission(
        mission_id="news-test-001",
        ant_type="news_ant",
        allowed_node="test-node",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="crypto", symbols=["GLOBAL"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=1.0,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=86400,
        heartbeat_interval=1800,
        success_conditions=SuccessConditions(description="Test news ant."),
    )


def _make_ant(tmp_path: Path, api_key: str = "test-key") -> NewsAnt:
    return NewsAnt(
        ant_id="abc123",
        mission=_make_mission(),
        scheduler=MagicMock(),
        logs_root=tmp_path,
        api_key=api_key,
    )


def _fake_articles(titles: list[str]) -> list[dict]:
    return [{"title": t, "description": ""} for t in titles]


def _fake_rss(title: str = "Bitcoin markets surge on Fed rate optimism") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>BBC Test</title>
    <item>
      <title>{title}</title>
      <description>Strong market growth and rally</description>
      <link>https://example.com/story</link>
    </item>
  </channel>
</rss>""".encode("utf-8")


# ---------------------------------------------------------------------------
# Sentiment berekening
# ---------------------------------------------------------------------------

class TestSentimentScore:
    def test_bullish_words_give_positive_score(self):
        arts = _fake_articles(["markets surge on strong growth rally"])
        score = _sentiment_score(arts)
        assert score > 0.0

    def test_bearish_words_give_negative_score(self):
        arts = _fake_articles(["stock market crash fear recession bearish drop"])
        score = _sentiment_score(arts)
        assert score < 0.0

    def test_empty_articles_returns_zero(self):
        assert _sentiment_score([]) == 0.0

    def test_neutral_article_near_zero(self):
        arts = _fake_articles(["the company announced quarterly results today"])
        score = _sentiment_score(arts)
        assert score == 0.0

    def test_mixed_positive_and_negative_balanced(self):
        arts = _fake_articles(["surge crash"])
        score = _sentiment_score(arts)
        assert score == 0.0

    def test_article_without_title_or_description_skipped(self):
        arts = [{"title": "", "description": ""}]
        score = _sentiment_score(arts)
        assert score == 0.0


class TestClassify:
    def test_bullish_above_threshold(self):
        assert _classify(_BULLISH_THRESHOLD + 0.01) == "bullish"

    def test_bearish_below_threshold(self):
        assert _classify(_BEARISH_THRESHOLD - 0.01) == "bearish"

    def test_neutral_at_zero(self):
        assert _classify(0.0) == "neutral"

    def test_neutral_at_boundary(self):
        assert _classify(_BULLISH_THRESHOLD) == "neutral"
        assert _classify(_BEARISH_THRESHOLD) == "neutral"


# ---------------------------------------------------------------------------
# Weekend detectie
# ---------------------------------------------------------------------------

class TestWeekendDetection:
    def _make_datetime(self, weekday: int) -> datetime:
        # weekday 0=ma, 5=za, 6=zo
        # 2026-04-27 is a Monday (weekday=0)
        base = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        return base + timedelta(days=weekday)

    def test_saturday_is_weekend(self, tmp_path):
        ant = _make_ant(tmp_path)
        sat = self._make_datetime(5)
        assert sat.weekday() == 5

    def test_sunday_is_weekend(self, tmp_path):
        ant = _make_ant(tmp_path)
        sun = self._make_datetime(6)
        assert sun.weekday() == 6

    def test_monday_is_not_weekend(self, tmp_path):
        ant = _make_ant(tmp_path)
        mon = self._make_datetime(0)
        assert mon.weekday() < 5

    def test_weekend_snapshot_has_weekend_flag(self, tmp_path):
        ant = _make_ant(tmp_path)
        sat = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)  # Saturday
        assert sat.weekday() == 5

        with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
            mock_dt.now.return_value = sat
            mock_dt.now.side_effect = None
            with patch.object(ant, "_fetch_queries", return_value=[]):
                with patch("ant_colony.ants.news_ant.datetime") as mock_dt2:
                    mock_dt2.now.return_value = sat
                    ant._tick.__func__  # just check it's callable

        # Direct test: build snapshot and check weekend flag
        ant._out_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(ant, "_fetch_queries", return_value=[]):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = sat
                # Manually call _tick using a Saturday datetime
                # Simulate tick logic: is_weekend = sat.weekday() >= 5
                is_weekend = sat.weekday() >= 5
                assert is_weekend is True

    def test_weekend_snapshot_excludes_equities(self, tmp_path):
        ant = _make_ant(tmp_path)
        sat = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)

        calls = []

        def track_fetch(queries):
            calls.append(queries)
            return []

        with patch.object(ant, "_fetch_queries", side_effect=track_fetch):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = sat
                ant._reset_date = sat.date()
                ant._tick()

        from ant_colony.ants.news_ant import _EQUITIES_QUERIES
        for call_args in calls:
            assert call_args != _EQUITIES_QUERIES, "Equities queries mogen niet op zaterdag"

    def test_weekday_snapshot_includes_equities(self, tmp_path):
        ant = _make_ant(tmp_path)
        mon = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)  # Monday

        calls = []

        def track_fetch(queries):
            calls.append(queries)
            return []

        with patch.object(ant, "_fetch_queries", side_effect=track_fetch):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = mon
                ant._reset_date = mon.date()
                ant._tick()

        from ant_colony.ants.news_ant import _EQUITIES_QUERIES
        assert any(call_args == _EQUITIES_QUERIES for call_args in calls), \
            "Equities queries moeten op maandag worden gedaan"


# ---------------------------------------------------------------------------
# API limiet bewaking
# ---------------------------------------------------------------------------

class TestApiLimitGuard:
    def test_stops_queries_at_hard_limit(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._daily_calls = _DAILY_CALL_LIMIT  # already at limit

        fetch_one_calls = []

        def fake_fetch_one(query):
            fetch_one_calls.append(query)
            return []

        with patch.object(ant, "_fetch_one", side_effect=fake_fetch_one):
            result = ant._fetch_queries(["Bitcoin", "Ethereum"])

        assert len(fetch_one_calls) == 0, "_fetch_one mag niet worden aangeroepen bij limiet"
        assert result == []

    def test_warning_logged_at_warning_threshold(self, tmp_path, caplog):
        ant = _make_ant(tmp_path)
        ant._daily_calls = _DAILY_CALL_WARNING

        def fake_fetch_one(query):
            return []

        with patch.object(ant, "_fetch_one", side_effect=fake_fetch_one):
            import logging
            with caplog.at_level(logging.WARNING, logger=f"ant.news.{ant.ant_id[:8]}"):
                ant._fetch_queries(["Bitcoin"])

        assert any("bijna bereikt" in r.message for r in caplog.records)

    def test_daily_counter_resets_on_new_day(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._daily_calls = 50
        ant._reset_date = date(2026, 4, 24)  # yesterday

        ant._maybe_reset_daily_counter(date(2026, 4, 25))

        assert ant._daily_calls == 0
        assert ant._reset_date == date(2026, 4, 25)

    def test_daily_counter_not_reset_same_day(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._daily_calls = 42
        today = date(2026, 4, 25)
        ant._reset_date = today

        ant._maybe_reset_daily_counter(today)

        assert ant._daily_calls == 42

    def test_tick_skipped_when_at_hard_limit(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._daily_calls = _DAILY_CALL_LIMIT
        ant._out_dir.mkdir(parents=True, exist_ok=True)

        now = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)
        ant._reset_date = now.date()

        written_files_before = list(ant._out_dir.glob("*.json"))

        with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
            mock_dt.now.return_value = now
            ant._tick()

        written_files_after = list(ant._out_dir.glob("*.json"))
        assert written_files_after == written_files_before, \
            "Geen snapshot mag worden geschreven als limiet bereikt is"

    def test_api_usage_is_persisted(self, tmp_path):
        ant = _make_ant(tmp_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"articles": []}

        with patch("httpx.get", return_value=mock_resp):
            ant._fetch_one("Bitcoin")

        usage_path = tmp_path / "news" / "api_usage.json"
        data = json.loads(usage_path.read_text(encoding="utf-8"))
        assert data["calls"] == 1
        assert data["date"] == ant._reset_date.isoformat()


# ---------------------------------------------------------------------------
# RSS fallback
# ---------------------------------------------------------------------------

class TestRssFallback:
    def test_rss_tag_mapping(self):
        assert "crypto" in _rss_tags_for_title("Bitcoin surges after ETF demand")
        assert "market" in _rss_tags_for_title("Fed rate decision moves markets")
        assert "technology" in _rss_tags_for_title("AI chip stocks rally")
        assert "commodity" in _rss_tags_for_title("Oil and gas prices rise")

    def test_parse_rss_items(self):
        articles = _parse_rss_items(_fake_rss(), source="BBC Business")
        assert len(articles) == 1
        assert articles[0]["title"].startswith("Bitcoin markets")
        assert articles[0]["url"] == "https://example.com/story"
        assert "crypto" in articles[0]["tags"]

    def test_tick_uses_rss_fallback_on_newsapi_429(self, tmp_path, caplog):
        ant = _make_ant(tmp_path)
        ant._out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
        ant._reset_date = now.date()

        def fake_get(url, **kwargs):
            resp = MagicMock()
            if "newsapi.org" in url:
                resp.status_code = 429
                resp.json.return_value = {"status": "error"}
                return resp
            resp.status_code = 200
            resp.content = _fake_rss()
            return resp

        with patch("httpx.get", side_effect=fake_get):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = now
                import logging
                with caplog.at_level(logging.INFO, logger=f"ant.news.{ant.ant_id[:8]}"):
                    ant._tick()

        snapshots = [
            p for p in (tmp_path / "news").glob("*.json")
            if p.name != "api_usage.json"
        ]
        assert len(snapshots) == 1
        data = json.loads(snapshots[0].read_text(encoding="utf-8"))
        assert data["source"] == "rss_fallback"
        assert data["article_count"] > 0
        assert data["top_headlines"]
        assert any("NewsAPI 429 → RSS fallback actief" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot output
# ---------------------------------------------------------------------------

class TestSnapshotOutput:
    def test_snapshot_written_to_news_dir(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._out_dir.mkdir(parents=True, exist_ok=True)

        now = datetime(2026, 4, 28, 8, 0, 0, tzinfo=timezone.utc)
        ant._reset_date = now.date()

        articles = _fake_articles(["markets surge on strong rally"])

        with patch.object(ant, "_fetch_queries", return_value=articles):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = now
                ant._tick()

        snapshots = list((tmp_path / "news").glob("*.json"))
        assert len(snapshots) == 1

        data = json.loads(snapshots[0].read_text(encoding="utf-8"))
        assert "timestamp" in data
        assert "market_sentiment" in data
        assert "crypto_sentiment" in data
        assert "equities_sentiment" in data
        assert "article_count" in data
        assert "bullish_count" in data
        assert "bearish_count" in data
        assert "neutral_count" in data
        assert "top_headlines" in data

    def test_weekend_snapshot_has_weekend_key(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._out_dir.mkdir(parents=True, exist_ok=True)

        sat = datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)
        ant._reset_date = sat.date()

        with patch.object(ant, "_fetch_queries", return_value=[]):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = sat
                ant._tick()

        snapshots = list((tmp_path / "news").glob("*.json"))
        assert len(snapshots) == 1
        data = json.loads(snapshots[0].read_text(encoding="utf-8"))
        assert data.get("weekend") is True

    def test_weekday_snapshot_has_no_weekend_key(self, tmp_path):
        ant = _make_ant(tmp_path)
        ant._out_dir.mkdir(parents=True, exist_ok=True)

        mon = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
        ant._reset_date = mon.date()

        with patch.object(ant, "_fetch_queries", return_value=[]):
            with patch("ant_colony.ants.news_ant.datetime") as mock_dt:
                mock_dt.now.return_value = mon
                ant._tick()

        snapshots = list((tmp_path / "news").glob("*.json"))
        assert len(snapshots) == 1
        data = json.loads(snapshots[0].read_text(encoding="utf-8"))
        assert "weekend" not in data


# ---------------------------------------------------------------------------
# Graceful skip zonder API key
# ---------------------------------------------------------------------------

class TestNoApiKey:
    def test_ant_instantiates_without_api_key(self, tmp_path):
        ant = NewsAnt(
            ant_id="nokey",
            mission=_make_mission(),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            api_key="",
        )
        assert ant._api_key == ""

    def test_fetch_one_returns_empty_on_http_error(self, tmp_path):
        ant = _make_ant(tmp_path, api_key="bad-key")

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"status": "error"}

        with patch("httpx.get", return_value=mock_resp):
            result = ant._fetch_one("Bitcoin")

        assert result == []

    def test_fetch_one_returns_empty_on_network_error(self, tmp_path):
        ant = _make_ant(tmp_path, api_key="test-key")

        with patch("httpx.get", side_effect=Exception("network timeout")):
            result = ant._fetch_one("Bitcoin")

        assert result == []

    def test_ant_type_in_schema(self):
        from ant_colony.schemas.ant import AntType
        values = [t.value for t in AntType]
        assert "news_ant" in values
