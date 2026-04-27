"""
tests/test_watchtower_client.py

Tests voor WatchtowerClient: signalen ophalen, feedback posten, health check.
Colony moet altijd doordraaien als Watchtower offline of onbereikbaar is.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ant_colony.clients.watchtower_client import WatchtowerClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(url: str = "http://127.0.0.1:8011", timeout: int = 5) -> WatchtowerClient:
    return WatchtowerClient(base_url=url, timeout=timeout)


def _mock_response(status_code: int, json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# get_signals — online
# ---------------------------------------------------------------------------

class TestGetSignals:
    def test_returns_list_when_online(self):
        signals = [
            {
                "signal_id": "sig-001",
                "asset": "ASML",
                "direction": "LONG",
                "entry_score": 0.8,
                "confidence": 0.7,
                "risk_flags": [],
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            }
        ]
        client = _make_client()
        with patch("httpx.get", return_value=_mock_response(200, signals)):
            result = client.get_signals(limit=10)
        assert result == signals
        assert client.last_get_succeeded is True

    def test_returns_empty_when_offline(self):
        client = _make_client()
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            result = client.get_signals()
        assert result == []
        assert client.last_get_succeeded is False

    def test_returns_empty_on_timeout(self):
        client = _make_client()
        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            result = client.get_signals()
        assert result == []
        assert client.last_get_succeeded is False

    def test_returns_empty_on_http_error(self):
        client = _make_client()
        with patch("httpx.get", return_value=_mock_response(500)):
            result = client.get_signals()
        assert result == []
        assert client.last_get_succeeded is False

    def test_returns_empty_when_response_not_list(self):
        client = _make_client()
        with patch("httpx.get", return_value=_mock_response(200, {"error": "bad"})):
            result = client.get_signals()
        assert result == []
        assert client.last_get_succeeded is True

    def test_no_exception_propagates(self):
        """Kritieke eis: NOOIT een exception gooien naar buiten."""
        client = _make_client()
        with patch("httpx.get", side_effect=RuntimeError("unexpected")):
            result = client.get_signals()
        assert result == []

    def test_passes_limit_param(self):
        captured = {}

        def fake_get(url, params=None, timeout=None):
            captured["params"] = params
            return _mock_response(200, [])

        client = _make_client()
        with patch("httpx.get", side_effect=fake_get):
            client.get_signals(limit=25)

        assert captured["params"] == {"limit": 25}


# ---------------------------------------------------------------------------
# post_outcome — fire-and-forget
# ---------------------------------------------------------------------------

class TestPostOutcome:
    _outcome = {
        "signal_id": None,
        "asset": "BTC-EUR",
        "direction": "LONG",
        "entry_price": 60000.0,
        "exit_price": 61000.0,
        "pnl_pct": 1.67,
        "exit_reason": "TP",
        "duration_hours": 2.5,
        "timestamp": "2026-04-27T12:00:00+00:00",
    }

    def test_returns_true_on_success(self):
        client = _make_client()
        with patch("httpx.post", return_value=_mock_response(200)):
            result = client.post_outcome(self._outcome)
        assert result is True

    def test_returns_false_on_connect_error(self):
        client = _make_client()
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            result = client.post_outcome(self._outcome)
        assert result is False

    def test_returns_false_on_timeout(self):
        client = _make_client()
        with patch("httpx.post", side_effect=httpx.TimeoutException("timeout")):
            result = client.post_outcome(self._outcome)
        assert result is False

    def test_returns_false_on_http_error(self):
        client = _make_client()
        with patch("httpx.post", return_value=_mock_response(422)):
            result = client.post_outcome(self._outcome)
        assert result is False

    def test_no_exception_propagates(self):
        """Kritieke eis: NOOIT blokkeren of exception gooien naar buiten."""
        client = _make_client()
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            result = client.post_outcome(self._outcome)
        assert result is False

    def test_posts_to_correct_endpoint(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"]  = url
            captured["json"] = json
            return _mock_response(200)

        client = _make_client(url="http://localhost:9999")
        with patch("httpx.post", side_effect=fake_post):
            client.post_outcome(self._outcome)

        assert captured["url"] == "http://localhost:9999/outcomes/evaluate"
        assert captured["json"]["asset"] == "BTC-EUR"

    def test_accepts_none_signal_id(self):
        """Non-Watchtower trades hebben signal_id=None — moet altijd werken."""
        outcome = {**self._outcome, "signal_id": None}
        client = _make_client()
        with patch("httpx.post", return_value=_mock_response(200)):
            result = client.post_outcome(outcome)
        assert result is True


# ---------------------------------------------------------------------------
# is_healthy — sync startup check
# ---------------------------------------------------------------------------

class TestIsHealthy:
    def test_returns_true_when_200(self):
        client = _make_client()
        with patch("httpx.get", return_value=_mock_response(200)):
            assert client.is_healthy() is True

    def test_returns_false_when_non_200(self):
        client = _make_client()
        with patch("httpx.get", return_value=_mock_response(503)):
            assert client.is_healthy() is False

    def test_returns_false_on_connect_error(self):
        client = _make_client()
        with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
            assert client.is_healthy() is False

    def test_returns_false_on_timeout(self):
        client = _make_client()
        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            assert client.is_healthy() is False

    def test_no_exception_propagates(self):
        client = _make_client()
        with patch("httpx.get", side_effect=RuntimeError("boom")):
            assert client.is_healthy() is False

    def test_uses_correct_url(self):
        captured = {}

        def fake_get(url, timeout=None):
            captured["url"] = url
            return _mock_response(200)

        client = _make_client(url="http://wt.internal:8011")
        with patch("httpx.get", side_effect=fake_get):
            client.is_healthy()

        assert captured["url"] == "http://wt.internal:8011/health"


# ---------------------------------------------------------------------------
# Configuratie via env vars
# ---------------------------------------------------------------------------

class TestConfig:
    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("WATCHTOWER_URL", raising=False)
        client = WatchtowerClient()
        assert client.base_url == "http://127.0.0.1:8011"

    def test_url_from_env(self, monkeypatch):
        monkeypatch.setenv("WATCHTOWER_URL", "http://custom:9999")
        client = WatchtowerClient()
        assert client.base_url == "http://custom:9999"

    def test_default_timeout(self, monkeypatch):
        monkeypatch.delenv("WATCHTOWER_TIMEOUT", raising=False)
        client = WatchtowerClient()
        assert client.timeout == 5

    def test_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("WATCHTOWER_TIMEOUT", "10")
        client = WatchtowerClient()
        assert client.timeout == 10

    def test_trailing_slash_stripped(self):
        client = WatchtowerClient(base_url="http://127.0.0.1:8011/")
        assert not client.base_url.endswith("/")
