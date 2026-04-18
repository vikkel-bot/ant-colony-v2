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
    _API_RATE_LIMIT_SECS,
    _MIN_STARS,
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
        ant._last_api_call = 0.0  # skip rate limit sleep
        repo = make_repo(stars=_MIN_STARS)
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)
        assert repo["html_url"] in ant._seen_urls

    def test_high_star_repo_processed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
        repo = make_repo()
        ant._seen_urls.add(repo["html_url"])

        with patch("ant_colony.ants.ingestion_ant.httpx.get") as mock_get:
            ant._process_repo(repo)
        mock_get.assert_not_called()

    def test_second_tick_same_url_not_reprocessed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
        repo = make_repo()
        no_readme = make_http_response(status_code=404)

        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=no_readme):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)

        log_path = tmp_path / "ingestion" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_empty_readme_content_skipped(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
        items = [make_repo("u/r1"), make_repo("u/r2")]
        resp = make_http_response(json_data={"total_count": 2, "items": items})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == items

    def test_returns_empty_on_non_200(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        resp = make_http_response(status_code=403, json_data={})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == []

    def test_returns_empty_on_exception(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("network")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._search_github("trading strategy")
        assert result == []

    def test_correct_query_params(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
        resp = make_http_response(status_code=404)
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(make_repo())
        assert result is None

    def test_returns_none_on_exception(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=Exception("timeout")):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                result = ant._fetch_readme(make_repo())
        assert result is None

    def test_handles_newlines_in_base64(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
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
        # Simulate last call was 2 seconds ago
        ant._last_api_call = 9999.0  # high monotonic value

        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=9999.0 + 2.0):
            with patch("ant_colony.ants.ingestion_ant.time.sleep") as mock_sleep:
                ant._rate_limit()

        mock_sleep.assert_called_once()
        sleep_secs = mock_sleep.call_args[0][0]
        assert sleep_secs > 0
        assert sleep_secs <= _API_RATE_LIMIT_SECS

    def test_no_sleep_when_enough_time_passed(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0  # long ago

        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=_API_RATE_LIMIT_SECS + 1):
            with patch("ant_colony.ants.ingestion_ant.time.sleep") as mock_sleep:
                ant._rate_limit()

        mock_sleep.assert_not_called()

    def test_rate_limit_updates_last_call(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        new_time = 999.0
        with patch("ant_colony.ants.ingestion_ant.time.monotonic", return_value=new_time):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._rate_limit()
        assert ant._last_api_call == new_time


# ---------------------------------------------------------------------------
# 9. Log events
# ---------------------------------------------------------------------------


class TestLogEvents:
    def _run_full_ingest(self, tmp_path: Path) -> IngestionAnt:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
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
        ant._last_api_call = 0.0
        repo = make_repo()
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=readme_resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._process_repo(repo)  # should not crash

    def test_sequence_increments(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
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
    def test_tick_calls_all_search_terms(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        resp = make_http_response(json_data={"items": []})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp) as mock_get:
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()
        # One search call per term
        assert mock_get.call_count == len(_SEARCH_TERMS)

    def test_tick_multiple_repos_all_processed(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_api_call = 0.0
        repos = [make_repo(f"user/repo{i}", stars=50) for i in range(3)]
        search_resp = make_http_response(json_data={"items": repos})
        readme_resp = make_http_response(json_data={"content": encode_readme(_GOOD_README)})

        call_count = [0]
        def side_effect(url, **kwargs):
            if "search" in url:
                return search_resp
            return readme_resp

        with patch("ant_colony.ants.ingestion_ant.httpx.get", side_effect=side_effect):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()

        # 3 unique repos × 4 terms, but only first term yields 3 new repos.
        # Subsequent terms see same repos as duplicates (same URL).
        assert len(ant._seen_urls) == 3

    def test_tick_sets_last_action(self) -> None:
        ant = make_ant()
        ant._last_api_call = 0.0
        resp = make_http_response(json_data={"items": []})
        with patch("ant_colony.ants.ingestion_ant.httpx.get", return_value=resp):
            with patch("ant_colony.ants.ingestion_ant.time.sleep"):
                ant._tick()
        assert ant._last_action == "tick"
