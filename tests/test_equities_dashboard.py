"""
tests/test_equities_dashboard.py

Tests voor GET /api/equities/status endpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ant_colony.dashboard.api import ColonyContext, _read_equities_data
from ant_colony.dashboard.server import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")


def _client(ctx: ColonyContext) -> TestClient:
    return TestClient(create_app(ctx))


# ---------------------------------------------------------------------------
# 1. Lege context
# ---------------------------------------------------------------------------

class TestEquitiesEmpty:

    def test_returns_200(self):
        r = _client(ColonyContext()).get("/api/equities/status")
        assert r.status_code == 200

    def test_required_keys_present(self):
        d = _client(ColonyContext()).get("/api/equities/status").json()
        for key in ("enabled", "sector_long", "sector_neutral",
                    "piotroski_candidates", "breakout_signals", "dividend_candidates"):
            assert key in d

    def test_lists_empty_without_logs(self):
        d = _client(ColonyContext()).get("/api/equities/status").json()
        assert d["sector_long"] == []
        assert d["sector_neutral"] == []
        assert d["piotroski_candidates"] == []
        assert d["breakout_signals"] == []
        assert d["dividend_candidates"] == []

    def test_timestamps_none_without_logs(self):
        d = _client(ColonyContext()).get("/api/equities/status").json()
        assert d["last_sector_ts"] is None
        assert d["last_piotroski_ts"] is None
        assert d["last_breakout_ts"] is None
        assert d["last_dividend_ts"] is None

    def test_vix_none_without_logs(self):
        d = _client(ColonyContext()).get("/api/equities/status").json()
        assert d["vix_level"] is None
        assert d["vix_signal"] is None


# ---------------------------------------------------------------------------
# 2. EQUITIES_ENABLED toggle
# ---------------------------------------------------------------------------

class TestEquitiesEnabled:

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"EQUITIES_ENABLED": "false"}):
            d = _client(ColonyContext()).get("/api/equities/status").json()
            assert d["enabled"] is False

    def test_enabled_when_set_true(self):
        with patch.dict(os.environ, {"EQUITIES_ENABLED": "true"}):
            d = _client(ColonyContext()).get("/api/equities/status").json()
            assert d["enabled"] is True

    def test_enabled_case_insensitive(self):
        with patch.dict(os.environ, {"EQUITIES_ENABLED": "TRUE"}):
            d = _client(ColonyContext()).get("/api/equities/status").json()
            assert d["enabled"] is True


# ---------------------------------------------------------------------------
# 3. Sector rotatie
# ---------------------------------------------------------------------------

class TestSectorRotatie:

    def _write_sector_signals(self, log_dir: Path, entries: list[dict]) -> None:
        _write_jsonl(log_dir / "scouts" / "sector_ant.jsonl",
                     [{"payload": e} for e in entries])

    def test_long_signals_in_sector_long(self, tmp_path: Path):
        self._write_sector_signals(tmp_path, [{
            "action": "opportunity_detected",
            "symbol": "XLK", "sector_name": "Tech",
            "signal_type": "LONG", "momentum_rank": 1,
            "change_pct": 0.12, "emitted_at": _now_iso(),
            "current_price": 180.0, "confidence": 0.9,
            "signal_id": "sector-xlk",
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["sector_long"]) == 1
        assert d["sector_long"][0]["symbol"] == "XLK"
        assert d["sector_long"][0]["signal"] == "LONG"

    def test_neutral_signals_in_sector_neutral(self, tmp_path: Path):
        self._write_sector_signals(tmp_path, [{
            "action": "opportunity_detected",
            "symbol": "XLE", "sector_name": "Energy",
            "signal_type": "NEUTRAL", "momentum_rank": 5,
            "change_pct": -0.03, "emitted_at": _now_iso(),
            "current_price": 90.0, "confidence": 0.5,
            "signal_id": "sector-xle",
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["sector_neutral"]) == 1
        assert d["sector_neutral"][0]["signal"] == "NEUTRAL"

    def test_max_3_long_returned(self, tmp_path: Path):
        payloads = [
            {"action": "opportunity_detected", "symbol": f"XL{i}",
             "sector_name": f"Sector{i}", "signal_type": "LONG",
             "momentum_rank": i + 1, "change_pct": 0.1 - i * 0.02,
             "emitted_at": _now_iso(), "current_price": 100.0,
             "confidence": 0.9, "signal_id": f"sector-xl{i}"}
            for i in range(5)
        ]
        self._write_sector_signals(tmp_path, payloads)
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["sector_long"]) <= 3

    def test_last_sector_ts_populated(self, tmp_path: Path):
        now = _now_iso()
        self._write_sector_signals(tmp_path, [{
            "action": "opportunity_detected", "symbol": "XLK",
            "sector_name": "Tech", "signal_type": "LONG",
            "momentum_rank": 1, "change_pct": 0.1,
            "emitted_at": now, "current_price": 100.0,
            "confidence": 0.9, "signal_id": "s",
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["last_sector_ts"] is not None

    def test_wrong_action_ignored(self, tmp_path: Path):
        self._write_sector_signals(tmp_path, [{
            "action": "wrong_action", "symbol": "XLK",
            "signal_type": "LONG", "momentum_rank": 1,
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["sector_long"] == []


# ---------------------------------------------------------------------------
# 4. Piotroski kandidaten
# ---------------------------------------------------------------------------

class TestPiotroskiKandidaten:

    def test_candidates_read_from_log(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "piotroski" / "piot_ant.jsonl", [{
            "payload": {
                "action": "piotroski_candidate",
                "symbol": "AAPL", "f_score": 8,
                "evaluated_at": _now_iso(),
            }
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["piotroski_candidates"]) == 1
        assert d["piotroski_candidates"][0]["symbol"] == "AAPL"
        assert d["piotroski_candidates"][0]["f_score"] == 8

    def test_sorted_by_f_score_desc(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "piotroski" / "p.jsonl", [
            {"payload": {"action": "piotroski_candidate", "symbol": "AAPL",
                         "f_score": 7, "evaluated_at": _now_iso()}},
            {"payload": {"action": "piotroski_candidate", "symbol": "MSFT",
                         "f_score": 9, "evaluated_at": _now_iso()}},
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        scores = [c["f_score"] for c in d["piotroski_candidates"]]
        assert scores == sorted(scores, reverse=True)

    def test_dedup_keeps_latest(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "piotroski" / "p.jsonl", [
            {"payload": {"action": "piotroski_candidate", "symbol": "AAPL",
                         "f_score": 7, "evaluated_at": "2026-04-19T10:00:00+00:00"}},
            {"payload": {"action": "piotroski_candidate", "symbol": "AAPL",
                         "f_score": 9, "evaluated_at": "2026-04-20T10:00:00+00:00"}},
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["piotroski_candidates"]) == 1
        assert d["piotroski_candidates"][0]["f_score"] == 9


# ---------------------------------------------------------------------------
# 5. Breakout signalen
# ---------------------------------------------------------------------------

class TestBreakoutSignalen:

    def test_breakout_signals_read(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "breakout" / "b.jsonl", [{
            "payload": {
                "action": "breakout_signal",
                "symbol": "MSFT",
                "entry_price": 400.0, "sl_price": 368.0, "tp_price": 480.0,
                "distance_to_high": 0.02,
                "emitted_at": _now_iso(),
            }
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["breakout_signals"]) == 1
        sig = d["breakout_signals"][0]
        assert sig["symbol"] == "MSFT"
        assert sig["entry_price"] == pytest.approx(400.0)
        assert sig["sl_price"] == pytest.approx(368.0)
        assert sig["tp_price"] == pytest.approx(480.0)

    def test_last_breakout_ts_populated(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "breakout" / "b.jsonl", [{
            "payload": {
                "action": "breakout_signal", "symbol": "AAPL",
                "entry_price": 200.0, "sl_price": 184.0, "tp_price": 240.0,
                "distance_to_high": 0.01, "emitted_at": _now_iso(),
            }
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["last_breakout_ts"] is not None


# ---------------------------------------------------------------------------
# 6. Dividend kandidaten
# ---------------------------------------------------------------------------

class TestDividendKandidaten:

    def test_candidates_read_from_log(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "dividend" / "d.jsonl", [{
            "payload": {
                "action": "dividend_candidate",
                "symbol": "JNJ", "dividend_yield": 0.03,
                "consecutive_years": 60, "payout_ratio": 0.45,
                "vix_level": 18.5, "vix_signal": "NORMAL",
                "emitted_at": _now_iso(),
            }
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["dividend_candidates"]) == 1
        c = d["dividend_candidates"][0]
        assert c["symbol"] == "JNJ"
        assert c["consecutive_years"] == 60

    def test_top5_by_yield(self, tmp_path: Path):
        payloads = [
            {"payload": {
                "action": "dividend_candidate",
                "symbol": f"DIV{i}", "dividend_yield": (8 - i) * 0.01,
                "consecutive_years": 30, "payout_ratio": 0.5,
                "emitted_at": _now_iso(),
            }}
            for i in range(8)
        ]
        _write_jsonl(tmp_path / "equities" / "dividend" / "d.jsonl", payloads)
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert len(d["dividend_candidates"]) == 5
        yields = [c["dividend_yield"] for c in d["dividend_candidates"]]
        assert yields == sorted(yields, reverse=True)

    def test_vix_extracted(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "dividend" / "d.jsonl", [{
            "payload": {
                "action": "dividend_candidate",
                "symbol": "KO", "dividend_yield": 0.03,
                "consecutive_years": 40, "payout_ratio": 0.6,
                "vix_level": 27.3, "vix_signal": "HEDGE",
                "emitted_at": _now_iso(),
            }
        }])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/status").json()
        assert d["vix_level"] == pytest.approx(27.3, abs=0.1)
        assert d["vix_signal"] == "HEDGE"


# ---------------------------------------------------------------------------
# 7. _read_equities_data helper
# ---------------------------------------------------------------------------

class TestReadEquitiesData:

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        result = _read_equities_data(tmp_path)
        assert result["sector_long"] == []
        assert result["piotroski_candidates"] == []
        assert result["breakout_signals"] == []
        assert result["dividend_candidates"] == []

    def test_invalid_json_skipped(self, tmp_path: Path):
        p = tmp_path / "scouts" / "bad.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json\n")
        result = _read_equities_data(tmp_path)
        assert result["sector_long"] == []

    def test_empty_symbol_skipped(self, tmp_path: Path):
        _write_jsonl(tmp_path / "equities" / "piotroski" / "p.jsonl", [{
            "payload": {"action": "piotroski_candidate", "symbol": "", "f_score": 8,
                        "evaluated_at": _now_iso()}
        }])
        result = _read_equities_data(tmp_path)
        assert result["piotroski_candidates"] == []


# ---------------------------------------------------------------------------
# 8. Equities posities endpoint
# ---------------------------------------------------------------------------

class TestEquitiesPositionsEndpoint:

    def _registry_with_price(self, price: float) -> MagicMock:
        adapter = MagicMock()
        adapter.get_market_data.return_value = SimpleNamespace(close=price)
        registry = MagicMock()
        registry.list_biomes.return_value = ["equities"]
        registry.get.return_value = adapter
        return registry

    def test_returns_200_empty_positions(self):
        r = _client(ColonyContext()).get("/api/equities/positions")
        assert r.status_code == 200
        d = r.json()
        assert d["open_positions"] == []
        assert d["closed_positions"] == []
        assert d["summary"]["open_count"] == 0

    def test_pnl_calculated_for_open_equities_position(self, tmp_path: Path):
        _write_jsonl(tmp_path / "paper" / "eq-ant.jsonl", [{
            "timestamp": _now_iso(),
            "payload": {
                "action": "trade_opened",
                "position_id": "eq-1",
                "symbol": "AAPL",
                "biome": "equities",
                "side": "long",
                "entry_price": 100.0,
                "quantity": 2.0,
                "stop_loss": 92.0,
                "take_profit": 120.0,
                "trailing_stop_price": 95.0,
            },
        }])
        ctx = ColonyContext(
            logs_root=tmp_path,
            biome_registry=self._registry_with_price(110.0),
        )
        d = _client(ctx).get("/api/equities/positions").json()
        assert len(d["open_positions"]) == 1
        pos = d["open_positions"][0]
        assert pos["symbol"] == "AAPL"
        assert pos["current_price"] == pytest.approx(110.0)
        assert pos["pnl_pct"] == pytest.approx(10.0)
        assert pos["pnl_eur"] == pytest.approx(20.0)
        assert d["summary"]["total_invested_equities"] == pytest.approx(200.0)

    def test_pnl_uses_position_current_price_from_paper_ledger(self):
        opened = datetime.now(tz=timezone.utc)
        position = SimpleNamespace(
            position_id="eq-ledger-1",
            symbol="AAPL",
            biome="equities",
            side=SimpleNamespace(value="long"),
            entry_price=100.0,
            current_price=110.0,
            quantity=2.0,
            stop_loss_price=92.0,
            take_profit_price=120.0,
            peak_price=120.0,
            opened_at=opened,
        )
        ledger = SimpleNamespace(open_positions=[position])

        d = _client(ColonyContext(paper_ledgers=[ledger])).get("/api/equities/positions").json()

        pos = d["open_positions"][0]
        assert pos["current_price"] == pytest.approx(110.0)
        assert pos["trailing_stop_price"] == pytest.approx(114.0)
        assert pos["pnl_pct"] == pytest.approx(10.0)
        assert pos["pnl_eur"] == pytest.approx(20.0)

    def test_closed_positions_summary(self, tmp_path: Path):
        now = _now_iso()
        _write_jsonl(tmp_path / "paper" / "eq-ant.jsonl", [
            {
                "timestamp": now,
                "payload": {
                    "action": "trade_opened",
                    "position_id": "eq-1",
                    "symbol": "MSFT",
                    "biome": "equities",
                    "side": "long",
                    "entry_price": 100.0,
                    "quantity": 1.0,
                    "stop_loss": 92.0,
                    "take_profit": 120.0,
                    "trailing_stop_price": 95.0,
                },
            },
            {
                "timestamp": now,
                "payload": {
                    "action": "trade_closed",
                    "position_id": "eq-1",
                    "symbol": "MSFT",
                    "biome": "equities",
                    "side": "long",
                    "entry_price": 100.0,
                    "exit_price": 112.0,
                    "quantity": 1.0,
                    "exit_reason": "take_profit",
                    "trailing_stop_price": 104.0,
                    "realized_pnl": 12.0,
                    "pnl_pct": 12.0,
                    "trading_seconds": 3600,
                },
            },
        ])
        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/equities/positions").json()
        assert d["open_positions"] == []
        assert len(d["closed_positions"]) == 1
        assert d["closed_positions"][0]["exit_reason"] == "TP"
        assert d["summary"]["wins"] == 1
        assert d["summary"]["win_rate"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 9. Watchtower → Queen doorstroom endpoint
# ---------------------------------------------------------------------------

class TestWatchtowerReceivedEndpoint:

    def test_returns_200_empty(self):
        r = _client(ColonyContext()).get("/api/watchtower/signals/received")
        assert r.status_code == 200
        assert r.json()["signals"] == []

    def test_queen_acceptance_status_from_watchtower_log(self, tmp_path: Path):
        ts = _now_iso()
        _write_jsonl(tmp_path / "watchtower" / "signals.jsonl", [{
            "timestamp": ts,
            "received": 2,
            "passed_filter": 1,
            "signals": [{
                "id": "sig-aapl",
                "asset": "AAPL",
                "direction": "long",
                "timestamp": ts,
                "entry_score": 0.72,
                "confidence": 0.66,
            }],
            "rejections": [{
                "id": "sig-msft",
                "asset": "MSFT",
                "direction": "long",
                "timestamp": ts,
                "entry_score": 0.42,
                "confidence": 0.61,
                "rejection_reason": "score too low",
            }],
        }])

        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/watchtower/signals/received").json()
        by_asset = {s["asset"]: s for s in d["signals"]}
        assert by_asset["AAPL"]["queen_accepted"] is True
        assert by_asset["MSFT"]["queen_accepted"] is False
        assert by_asset["MSFT"]["rejection_reason"] == "score too low"
        assert d["summary"]["received_24h"] == 2
        assert d["summary"]["accepted_24h"] == 1
        assert d["summary"]["rejected_24h"] == 1
        assert d["summary"]["rejection_reasons"]["score"] == 1

    def test_queen_acceptance_status_from_candidate_log(self, tmp_path: Path):
        ts = _now_iso()
        _write_jsonl(tmp_path / "watchtower" / "candidates.jsonl", [{
            "timestamp": ts,
            "payload": {
                "action": "watchtower_candidate",
                "asset": "AAPL",
                "direction": "long",
                "entry_score": 0.72,
                "confidence": 0.66,
                "signal_id": "sig-aapl",
                "created_at": ts,
            },
        }])
        _write_jsonl(tmp_path / "watchtower" / "signals.jsonl", [{
            "timestamp": ts,
            "received": 2,
            "passed_filter": 2,
            "candidates_accepted": 1,
            "signals": [],
            "candidate_rejections": [{
                "id": "sig-btc",
                "asset": "BTC-EUR",
                "direction": "long",
                "timestamp": ts,
                "entry_score": 0.91,
                "confidence": 0.90,
                "rejection_reason": "crypto_confirmed_negative",
            }],
        }])

        d = _client(ColonyContext(logs_root=tmp_path)).get("/api/watchtower/signals/received").json()
        by_asset = {s["asset"]: s for s in d["signals"]}
        assert by_asset["AAPL"]["queen_accepted"] is True
        assert by_asset["BTC-EUR"]["queen_accepted"] is False
        assert d["summary"]["received_24h"] == 2
        assert d["summary"]["accepted_24h"] == 1
        assert d["summary"]["rejected_24h"] == 1
        assert d["summary"]["last_signal"]["asset"] in {"AAPL", "BTC-EUR"}
