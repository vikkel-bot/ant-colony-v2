"""
tests/test_ingestion_ant.py

Volledige coverage voor IngestionAnt.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ant_colony.ants.ingestion_ant import (
    IngestionAnt,
    _MIN_STARS,
    _RATE_LIMITS,
    _REDDIT_KEYWORDS,
    _REDDIT_TOP_URL,
    _DEVTO_ARTICLES_URL,
    _SEARCH_TERMS,
)
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.strategy_candidate import CandidateStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_README = """
# Momentum Trading Strategy
This bot generates buy signals when RSI is oversold (< 30) and price
crosses the EMA. The entry condition is triggered by an SMA crossover.
Exit: stop_loss at 2%, take_profit at 6%, trailing_stop option available.
"""

_BAD_README = """
# My Calculator App
Simple arithmetic utility. No trading logic here.
"""


def make_mission(biome: str = "crypto", symbols: list[str] | None = None) -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="ingestion_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "propose_candidate"],
        market_scope=MarketScope(biome=biome, symbols=symbols or ["BTC-EUR", "ETH-EUR"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="ingest test"),
    )


def make_ant(logs_root: Path | None = None) -> IngestionAnt:
    return IngestionAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        logs_root=logs_root,
    )


def reset_rate_limits(ant: IngestionAnt) -> None:
    """Zet alle per-bron rate-limit timers op 0 zodat tests niet hoeven te slapen."""
    ant._last_call = {"github": 0.0, "reddit": 0.0, "devto": 0.0}


def make_reddit_response(posts: list[dict] | None = None, status_code: int = 200) -> MagicMock:
    children = [{"data": p} for p in (posts or [])]
    return make_http_response(status_code=status_code, json_data={"data": {"children": children}})


def make_devto_response(articles: list[dict] | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = articles or []
    return resp


def make_reddit_post(
    title: str = "My backtest strategy edge",
    selftext: str = "Uses RSI crossover for entry, stop_loss for exit.",
    url: str = "https://example.com/post",
    permalink: str = "/r/algotrading/comments/abc/my_post/",
    score: int = 42,
) -> dict:
    return {
        "title":     title,
        "selftext":  selftext,
        "url":       url,
        "permalink": permalink,
        "score":     score,
    }


def make_devto_article(
    title: str = "A trading bot strategy with backtest",
    description: str = "Momentum entry with stop_loss exit.",
    url: str = "https://dev.to/user/article-123",
    reactions: int = 10,
) -> dict:
    return {
        "title":                   title,
        "description":             description,
        "url":                     url,
        "positive_reactions_count": reactions,
    }


def make_repo(
    full_name: str = "user/trading-bot",
    stars: int = 50,
    description: str = "A momentum trading bot",
    language: str = "Python",
) -> dict:
    return {
        "full_name":        full_name,
        "html_url":         f"https://github.com/{full_name}",
        "stargazers_count": stars,
        "description":      description,
        "language":         language,
    }


def make_http_response(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


def encode_readme(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# ---------------------------------------------------------------------------
# 1. Kwaliteitsfilter — sterren
# ---------------------------------------------------------------------------


class TestStarFilter:
    def test_low_star_repo_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        repo = make_repo(stars=_MIN_STARS - 1)
        # If processed, it would try to fetch README — should not happen
        with patch("ant_colony.ants.ingestion_ant.httpx.get") as mock_get:
            ant._process_repo(repo)
        mock_get.assert_not_called()
        assert len(ant._seen_urls) == 0

    def test_exact_min_stars_processed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)  # skip rate limit sleep
        repo = make_repo(stars=_MIN_STARS)
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)
        assert repo["html_url"] in ant._seen_urls

    def test_high_star_repo_processed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo(stars=1000)
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)
        assert repo["html_url"] in ant._seen_urls


# ---------------------------------------------------------------------------
# 2. Kwaliteitsfilter — duplicaten
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_url_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo()
        ant._seen_urls.add(repo["html_url"])

        with patch("ant_colony.ants.ingestion_ant.httpx.get") as mock_get:
            ant._process_repo(repo)
        mock_get.assert_not_called()

    def test_second_tick_same_url_not_reprocessed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo()
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})

        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)
                call_count_first = readme_resp.json.call_count

        # Second call: already in _seen_urls
        with patch("ant_colony.ants.ingestion_ant.httpx.get") as mock_get2:
            ant._process_repo(repo)
        mock_get2.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Kwaliteitsfilter — README ontbreekt
# ---------------------------------------------------------------------------


class TestReadmeFilter:
    def test_no_readme_http_404_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo()
        no_readme = make_http_response(status_code=404)

        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=no_readme):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)

        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_empty_readme_content_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo()
        empty_readme = make_http_response(json_data={"content": ""})

        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=empty_readme):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)

        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()


# ---------------------------------------------------------------------------
# 4. Strategie-extractie
# ---------------------------------------------------------------------------


class TestStrategyExtraction:
    def test_good_readme_returns_result(self) -> None:
        ant = make_ant()
        result = ant._extract_strategy(_GOOD_README, make_repo())
        assert result is not None
        assert len(result["entry_keywords"]) > 0
        assert len(result["exit_keywords"]) > 0

    def test_bad_readme_returns_none(self) -> None:
        ant = make_ant()
        result = ant._extract_strategy(_BAD_README, make_repo())
        assert result is None

    def test_only_entry_keywords_returns_none(self) -> None:
        readme = "This bot uses RSI and SMA crossover to enter positions."
        ant = make_ant()
        result = ant._extract_strategy(readme, make_repo())
        assert result is None

    def test_only_exit_keywords_returns_none(self) -> None:
        readme = "Set stop_loss and take_profit levels manually."
        ant = make_ant()
        result = ant._extract_strategy(readme, make_repo())
        assert result is None

    def test_entry_keywords_detected(self) -> None:
        ant = make_ant()
        result = ant._extract_strategy(_GOOD_README, make_repo())
        assert result is not None
        found = {kw.lower() for kw in result["entry_keywords"]}
        assert "rsi" in found or "ema" in found or "sma" in found

    def test_exit_keywords_detected(self) -> None:
        ant = make_ant()
        result = ant._extract_strategy(_GOOD_README, make_repo())
        assert result is not None
        found = {kw.lower() for kw in result["exit_keywords"]}
        assert "stop_loss" in found or "take_profit" in found

    def test_description_included(self) -> None:
        repo = make_repo(description="Awesome momentum bot")
        ant = make_ant()
        result = ant._extract_strategy(_GOOD_README, repo)
        assert result is not None
        assert result["description"] == "Awesome momentum bot"

    def test_case_insensitive(self) -> None:
        readme = "Uses RSI AND SMA Crossover for ENTRY. STOP_LOSS and TAKE_PROFIT for exit."
        ant = make_ant()
        result = ant._extract_strategy(readme, make_repo())
        assert result is not None


# ---------------------------------------------------------------------------
# 5. Kandidaat bouwen
# ---------------------------------------------------------------------------


class TestCandidateBuild:
    def _good_extracted(self) -> dict:
        return {
            "entry_keywords": ["rsi", "sma"],
            "exit_keywords":  ["stop_loss", "take_profit"],
            "description":    "Momentum strategy",
        }

    def test_candidate_has_ingested_status(self) -> None:
        ant = make_ant()
        candidate = ant._build_candidate(make_repo(), self._good_extracted())
        assert candidate is not None
        assert candidate.status == CandidateStatus.INGESTED

    def test_candidate_has_source_github(self) -> None:
        ant = make_ant()
        candidate = ant._build_candidate(make_repo(), self._good_extracted())
        assert candidate.source == "github"

    def test_candidate_source_url_set(self) -> None:
        repo = make_repo(full_name="user/repo")
        ant = make_ant()
        candidate = ant._build_candidate(repo, self._good_extracted())
        assert candidate.source_url == "https://github.com/user/repo"

    def test_candidate_has_provenance(self) -> None:
        ant = make_ant()
        candidate = ant._build_candidate(make_repo(), self._good_extracted())
        assert candidate is not None
        assert len(candidate.provenance) == 1
        assert candidate.provenance[0].action == "ingested"
        assert candidate.provenance[0].actor == ant.ant_id

    def test_candidate_provenance_has_url(self) -> None:
        repo = make_repo()
        ant = make_ant()
        candidate = ant._build_candidate(repo, self._good_extracted())
        assert candidate is not None
        assert candidate.provenance[0].details["url"] == repo["html_url"]

    def test_candidate_entry_conditions_set(self) -> None:
        ant = make_ant()
        candidate = ant._build_candidate(make_repo(), self._good_extracted())
        assert candidate is not None
        assert "keywords" in candidate.entry_conditions
        assert len(candidate.entry_conditions["keywords"]) > 0

    def test_candidate_exit_conditions_set(self) -> None:
        ant = make_ant()
        candidate = ant._build_candidate(make_repo(), self._good_extracted())
        assert candidate is not None
        assert "keywords" in candidate.exit_conditions
        assert len(candidate.exit_conditions["keywords"]) > 0

    def test_candidate_id_deterministic_per_url(self) -> None:
        repo = make_repo()
        ant = make_ant()
        c1 = ant._build_candidate(repo, self._good_extracted())
        c2 = ant._build_candidate(repo, self._good_extracted())
        assert c1 is not None and c2 is not None
        assert c1.candidate_id == c2.candidate_id

    def test_candidate_stars_in_parameters(self) -> None:
        repo = make_repo(stars=123)
        ant = make_ant()
        candidate = ant._build_candidate(repo, self._good_extracted())
        assert candidate is not None
        assert candidate.parameters["stars"] == 123

    def test_missing_exit_conditions_filtered_upstream(self) -> None:
        # _extract_strategy returns None when no exit keywords found,
        # so _build_candidate is never called with empty exit_keywords.
        # Verify _extract_strategy correctly blocks the call.
        ant = make_ant()
        readme_no_exit = "Uses RSI and SMA crossover to generate a buy signal. No close conditions provided."
        result = ant._extract_strategy(readme_no_exit, make_repo())
        assert result is None


# ---------------------------------------------------------------------------
# 6. GitHub search
# ---------------------------------------------------------------------------


class TestGitHubSearch:
    def test_returns_items_on_200(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        items = [make_repo("u/r1"), make_repo("u/r2")]
        resp = make_http_response(json_data={"total_count": 2, "items": items})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == items

    def test_returns_empty_on_non_200(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        resp = make_http_response(status_code=403, json_data={})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("network")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == []

    def test_correct_query_params(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        resp = make_http_response(json_data={"items": []})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp) as mock_get:
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._search_github("mean reversion")
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["q"] == "mean reversion"
        assert call_kwargs[1]["params"]["per_page"] == 10


# ---------------------------------------------------------------------------
# 7. README fetch
# ---------------------------------------------------------------------------


class TestReadmeFetch:
    def test_returns_decoded_text(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        repo = make_repo()
        b64 = encode_readme("buy signal rsi stop_loss")
        resp = make_http_response(json_data={"content": b64})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(repo)
        assert result is not None
        assert "buy signal" in result

    def test_returns_none_on_404(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        resp = make_http_response(status_code=404)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(make_repo())
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("timeout")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(make_repo())
        assert result is None

    def test_handles_newlines_in_base64(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        raw = encode_readme("entry rsi sma stop_loss take_profit")
        # Insert newlines like GitHub does (every 60 chars)
        chunked = "\n".join(raw[i:i+60] for i in range(0, len(raw), 60)) + "\n"
        resp = make_http_response(json_data={"content": chunked})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(make_repo())
        assert result is not None
        assert "entry" in result


# ---------------------------------------------------------------------------
# 8. Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_sleeps_when_called_too_quickly(self) -> None:
        ant = make_ant()
        github_limit = _RATE_LIMITS["github"]
        ant._last_call["github"] = 9999.0  # recent call

        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=9999.0 + 2.0):
            with patch("ant_colony.ants.ingestion_ant.time.sleep") as mock_sleep:
                ant._rate_limit("github")

        mock_sleep.assert_called_once()
        sleep_secs = mock_sleep.call_args[0][0]
        assert sleep_secs > 0
        assert sleep_secs <= github_limit

    def test_no_sleep_when_enough_time_passed(self) -> None:
        ant = make_ant()
        github_limit = _RATE_LIMITS["github"]
        ant._last_call["github"] = 0.0

        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=github_limit + 1):
            with patch("ant_colony.ants.ingestion_ant.time.sleep") as mock_sleep:
                ant._rate_limit("github")

        mock_sleep.assert_not_called()

    def test_rate_limit_updates_last_call(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        new_time = 999.0
        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=new_time):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._rate_limit("github")
        assert ant._last_call["github"] == new_time

    def test_per_source_limits_independent(self) -> None:
        assert _RATE_LIMITS["reddit"] == 30.0
        assert _RATE_LIMITS["devto"] == 30.0
        assert _RATE_LIMITS["github"] == 10.0

    def test_reddit_rate_limit_applied(self) -> None:
        ant = make_ant()
        ant._last_call["reddit"] = 9999.0

        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=9999.0 + 5.0):
            with patch("ant_colony.ants.ingestion_ant.time.sleep") as mock_sleep:
                ant._rate_limit("reddit")

        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == pytest.approx(25.0, abs=0.1)


# ---------------------------------------------------------------------------
# 9. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def _run_full_ingest(self, tmp_path: Path) -> IngestionAnt:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo = make_repo()
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)
        return ant

    def test_log_file_created(self, tmp_path: Path) -> None:
        ant = self._run_full_ingest(tmp_path)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_candidate_ingested_action(self, tmp_path: Path) -> None:
        ant = self._run_full_ingest(tmp_path)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        actions = [r["payload"]["action"] for r in records]
        assert "candidate_ingested" in actions

    def test_log_record_has_required_fields(self, tmp_path: Path) -> None:
        ant = self._run_full_ingest(tmp_path)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload = next(r["payload"] for r in records if r["payload"]["action"] == "candidate_ingested")
        for field in ("candidate_id", "name", "source_url", "status",
                      "entry_keywords", "exit_keywords", "provenance"):
            assert field in payload, f"Missing field: {field}"

    def test_log_status_is_ingested(self, tmp_path: Path) -> None:
        ant = self._run_full_ingest(tmp_path)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload = next(r["payload"] for r in records if r["payload"]["action"] == "candidate_ingested")
        assert payload["status"] == "ingested"

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        reset_rate_limits(ant)
        repo = make_repo()
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)  # should not crash

    def test_sequence_increments(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repo1 = make_repo("u/repo1")
        repo2 = make_repo("u/repo2", stars=100)
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo1)
                ant._process_repo(repo2)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))


# ---------------------------------------------------------------------------
# 10. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent_on_send_heartbeat(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_contains_ant_id(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        hb = ant.scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()  # must not propagate


# ---------------------------------------------------------------------------
# 11. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock()  # prevent real HTTP calls consuming datetime mock

        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            with patch("ant_colony.ants.ingestion_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]

                status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()

        with patch("ant_colony.ants.ingestion_ant.time.sleep", side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.ingestion_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_exception_in_tick_returns_aborted(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(side_effect=RuntimeError("tick boom"))

        with patch("ant_colony.ants.ingestion_ant.time.sleep", side_effect=Exception("stop")):
            with patch("ant_colony.ants.ingestion_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()

        assert status == AntStatus.ABORTED

    def test_final_heartbeat_sent_on_exit(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock()  # prevent real HTTP calls

        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            with patch("ant_colony.ants.ingestion_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                ant.run()

        assert ant.scheduler.record_heartbeat.call_count >= 1


# ---------------------------------------------------------------------------
# 12. Tick — meerdere zoektermen
# ---------------------------------------------------------------------------


class TestTick:
    def _empty_responses(self, url: str, **kwargs) -> MagicMock:
        """Universele mock die voor elke bron een lege response retourneert."""
        if "api.github.com" in url:
            return make_http_response(json_data={"items": []})
        if "reddit.com" in url:
            return make_reddit_response(posts=[])
        if "dev.to" in url:
            return make_devto_response(articles=[])
        return make_http_response(json_data={})

    def test_tick_calls_all_search_terms(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=self._empty_responses) as mock_get:
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()
        # len(_SEARCH_TERMS) GitHub calls + 1 Reddit + 1 Dev.to
        assert mock_get.call_count == len(_SEARCH_TERMS) + 2

    def test_tick_multiple_repos_all_processed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        repos = [make_repo(f"user/repo{i}", stars=50) for i in range(3)]
        search_resp = make_http_response(json_data={"items": repos})
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})

        def side_effect(url, **kwargs):
            if "search" in url:
                return search_resp
            if "readme" in url.lower() or "api.github.com/repos" in url:
                return readme_resp
            if "reddit.com" in url:
                return make_reddit_response(posts=[])
            if "dev.to" in url:
                return make_devto_response(articles=[])
            return readme_resp

        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=side_effect):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()

        # 3 unique repos × N terms, but only first term yields 3 new repos.
        assert len(ant._seen_urls) == 3

    def test_tick_sets_last_action(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=self._empty_responses):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()
        assert ant._last_action == "tick"


# ---------------------------------------------------------------------------
# 13. Reddit — fetch en verwerking
# ---------------------------------------------------------------------------


class TestRedditFetch:
    def test_returns_posts_on_200(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        posts = [make_reddit_post()]
        resp = make_reddit_response(posts=posts)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_reddit()
        assert len(result) == 1
        assert result[0]["title"] == posts[0]["title"]

    def test_returns_empty_on_429(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=make_reddit_response(status_code=429)):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_reddit()
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("timeout")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_reddit()
        assert result == []

    def test_post_without_keywords_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        post = make_reddit_post(title="Hello world", selftext="Nothing trading related")
        ant._process_reddit_post(post)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_post_with_keywords_ingested(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        post = make_reddit_post()  # contains "backtest" and "strategy"
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_reddit_post(post)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_reddit_candidate_has_correct_source(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        post = make_reddit_post()
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_reddit_post(post)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload = records[0]["payload"]
        assert payload["provenance"][0]["details"]["source"] == "reddit"

    def test_reddit_duplicate_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        reset_rate_limits(ant)
        post = make_reddit_post()
        ant._seen_urls.add(post["url"])
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_reddit_post(post)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_reddit_candidate_id_deterministic(self) -> None:
        ant = make_ant()
        post = make_reddit_post()
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            extracted = ant._extract_strategy(
                (post["title"] + " " + post["selftext"]).lower(),
                {"description": post["title"]},
            )
            c1 = ant._build_text_candidate("reddit", post["url"], post["title"], post["title"], extracted, {})
            c2 = ant._build_text_candidate("reddit", post["url"], post["title"], post["title"], extracted, {})
        assert c1 is not None and c2 is not None
        assert c1.candidate_id == c2.candidate_id

    def test_reddit_post_no_strategy_keywords_in_text_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        post = make_reddit_post(
            title="Amazing market discussion",
            selftext="Buy the dip! No entry/exit logic provided.",
        )
        ant._process_reddit_post(post)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()


# ---------------------------------------------------------------------------
# 14. Dev.to — fetch en verwerking
# ---------------------------------------------------------------------------


class TestDevtoFetch:
    def test_returns_articles_on_200(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        articles = [make_devto_article()]
        resp = make_devto_response(articles=articles)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_devto()
        assert len(result) == 1
        assert result[0]["title"] == articles[0]["title"]

    def test_returns_empty_on_429(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=make_devto_response(status_code=429)):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_devto()
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        ant = make_ant()
        reset_rate_limits(ant)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("net")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_devto()
        assert result == []

    def test_article_without_keywords_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article(title="Python basics", description="Learn Python fast")
        ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_article_with_keywords_ingested(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article()  # contains "bot" and "backtest"
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_devto_candidate_has_correct_source(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article()
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        payload = records[0]["payload"]
        assert payload["provenance"][0]["details"]["source"] == "devto"

    def test_devto_duplicate_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article()
        ant._seen_urls.add(article["url"])
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_devto_article_missing_url_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article(url="")
        ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_devto_candidate_id_deterministic(self) -> None:
        ant = make_ant()
        article = make_devto_article()
        extracted = ant._extract_strategy(
            (article["title"] + " " + article["description"]).lower(),
            {"description": article["description"]},
        )
        c1 = ant._build_text_candidate("devto", article["url"], article["title"], article["description"], extracted, {})
        c2 = ant._build_text_candidate("devto", article["url"], article["title"], article["description"], extracted, {})
        assert c1 is not None and c2 is not None
        assert c1.candidate_id == c2.candidate_id

    def test_devto_provenance_has_url(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        article = make_devto_article()
        with patch("ant_colony.ants.ingestion_ant.time.sleep"):
            ant._process_devto_article(article)
        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["payload"]["provenance"][0]["details"]["url"] == article["url"]
