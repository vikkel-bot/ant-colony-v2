"""
tests/test_dashboard_api.py

Tests voor het Colony Dashboard — api.py, server.py en ColonyContext.

Dekt:
  - Alle 8 endpoints met lege context (nul-data, geen crash)
  - Alle 8 endpoints met gevulde Queen + Scheduler context
  - KillSwitch: operator_confirm vereist, niveau-validatie, delegatie aan Queen
  - Ticker: JSONL-bestanden lezen, sorteren, laatste N teruggeven
  - Performance: PnL berekend uit trade-log bestanden
  - server.py: create_app retourneert werkende FastAPI app
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler, ColonyStatus
from ant_colony.dashboard.api import ColonyContext, create_router, _parse_ts
from ant_colony.dashboard.server import create_app
from ant_colony.queen.queen import Queen
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions
from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runtime_paths() -> RuntimePaths:
    return RuntimePaths(output="C:/tmp/out", live="C:/tmp/live", logs="C:/tmp/logs")


def _make_node(node_id: str = "node-1", biomes: list[str] | None = None) -> Node:
    return Node(
        node_id=node_id,
        hostname="test-host",
        status=NodeStatus.ACTIVE,
        allowed_biomes=biomes or ["crypto"],
        allowed_ant_types=["execution_ant", "paper_ant", "scout_ant"],
        heartbeat_interval=30,
        runtime_paths=_make_runtime_paths(),
    )


def _make_scheduler(status: ColonyStatus = ColonyStatus.RUNNING) -> ColonyScheduler:
    s = MagicMock(spec=ColonyScheduler)
    s.status = status
    return s


def _make_queen(capital: float = 50_000.0) -> Queen:
    queen = Queen(capital_total=capital, scheduler=_make_scheduler())
    queen.register_node(_make_node())
    return queen


def _make_mission(
    mission_id: str = "m-001",
    ant_type: str = "paper_ant",
    biome: str = "crypto",
    capital: float = 5_000.0,
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type=ant_type,
        allowed_node="node-1",
        allowed_actions=["paper_trade"],
        market_scope=MarketScope(biome=biome, symbols=["BTC-EUR"]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.1,
            max_position_size=2_000.0,
            daily_loss_limit=500.0,
        ),
        ttl=3600,
        heartbeat_interval=30,
        success_conditions=SuccessConditions(description="test"),
    )


def _issue(queen: Queen, mission: Mission) -> None:
    result = queen.issue_mission(mission)
    assert result.accepted, f"issue_mission failed: {result.rejection_reason}"


def _client(ctx: ColonyContext | None = None) -> TestClient:
    return TestClient(create_app(ctx))


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# TestEmptyContext — alle endpoints draaien zonder crash op lege context
# ---------------------------------------------------------------------------

class TestEmptyContext:

    def setup_method(self):
        self.client = _client(ColonyContext())

    def test_status_empty(self):
        r = self.client.get("/api/status")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "UNKNOWN"
        assert d["last_tick"] is None

    def test_metrics_empty(self):
        r = self.client.get("/api/metrics")
        assert r.status_code == 200
        d = r.json()
        assert d["capital_total"] == 0.0
        assert d["active_ants"] == 0

    def test_performance_empty(self):
        r = self.client.get("/api/performance")
        assert r.status_code == 200
        d = r.json()
        assert d["day"] == 0.0
        assert d["alltime"] == 0.0

    def test_biomes_empty(self):
        r = self.client.get("/api/biomes")
        assert r.status_code == 200
        assert r.json()["biomes"] == []

    def test_ants_empty(self):
        r = self.client.get("/api/ants")
        assert r.status_code == 200
        assert r.json()["ants"] == []

    def test_brokers_empty(self):
        r = self.client.get("/api/brokers")
        assert r.status_code == 200
        assert r.json()["brokers"] == []

    def test_ticker_empty(self):
        r = self.client.get("/api/ticker")
        assert r.status_code == 200
        assert r.json()["events"] == []

    def test_killswitch_no_colony(self):
        r = self.client.post("/api/killswitch", json={"level": 3, "operator_confirm": True})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# TestStatusEndpoint
# ---------------------------------------------------------------------------

class TestStatusEndpoint:

    def test_running_status(self):
        scheduler = _make_scheduler(ColonyStatus.RUNNING)
        ctx = ColonyContext(scheduler=scheduler)
        client = _client(ctx)
        r = client.get("/api/status")
        assert r.json()["status"] == "RUNNING"

    def test_halted_status(self):
        scheduler = _make_scheduler(ColonyStatus.HALTED)
        ctx = ColonyContext(scheduler=scheduler)
        client = _client(ctx)
        r = client.get("/api/status")
        assert r.json()["status"] == "HALTED"

    def test_server_time_present(self):
        ctx = ColonyContext(scheduler=_make_scheduler())
        r = _client(ctx).get("/api/status")
        assert r.json()["server_time"] is not None

    def test_last_tick_from_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            log_file = logs / "colony" / "scheduler.jsonl"
            _write_jsonl(log_file, [{"timestamp": _now_iso(), "event_type": "tick"}])
            ctx = ColonyContext(scheduler=_make_scheduler(), logs_root=logs)
            r = _client(ctx).get("/api/status")
            assert r.json()["last_tick"] is not None
            assert r.json()["seconds_ago"] is not None
            assert r.json()["seconds_ago"] >= 0

    def test_no_log_file_last_tick_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ColonyContext(scheduler=_make_scheduler(), logs_root=Path(tmp))
            r = _client(ctx).get("/api/status")
            assert r.json()["last_tick"] is None


# ---------------------------------------------------------------------------
# TestMetricsEndpoint
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:

    def test_capital_total(self):
        queen = _make_queen(capital=75_000.0)
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/metrics")
        assert r.json()["capital_total"] == 75_000.0

    def test_active_ants_count(self):
        queen = _make_queen()
        _issue(queen, _make_mission("m-1"))
        _issue(queen, _make_mission("m-2"))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/metrics")
        assert r.json()["active_ants"] == 2

    def test_capital_allocated(self):
        queen = _make_queen(capital=50_000.0)
        _issue(queen, _make_mission("m-1", capital=5_000.0))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/metrics")
        assert r.json()["capital_allocated"] == 5_000.0
        assert r.json()["capital_available"] == 45_000.0

    def test_utilization_pct(self):
        queen = _make_queen(capital=10_000.0)
        _issue(queen, _make_mission("m-1", capital=2_500.0))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/metrics")
        assert r.json()["utilization_pct"] == 25.0


# ---------------------------------------------------------------------------
# TestPerformanceEndpoint
# ---------------------------------------------------------------------------

class TestPerformanceEndpoint:

    def test_alltime_from_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            trades = [
                {"realized_pnl": 100.0, "closed_at": _now_iso()},
                {"realized_pnl":  50.0, "closed_at": _now_iso()},
                {"realized_pnl": -20.0, "closed_at": _now_iso()},
            ]
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", trades)
            ctx = ColonyContext(logs_root=logs)
            r = _client(ctx).get("/api/performance")
            assert r.json()["alltime"] == pytest.approx(130.0)

    def test_old_trade_excluded_from_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=2)).isoformat()
            new_ts = _now_iso()
            trades = [
                {"realized_pnl": 200.0, "closed_at": old_ts},
                {"realized_pnl":  50.0, "closed_at": new_ts},
            ]
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", trades)
            ctx = ColonyContext(logs_root=logs)
            r = _client(ctx).get("/api/performance")
            d = r.json()
            assert d["day"] == pytest.approx(50.0)
            assert d["alltime"] == pytest.approx(250.0)

    def test_no_trades_all_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/performance")
            d = r.json()
            assert d["day"] == 0.0
            assert d["alltime"] == 0.0


# ---------------------------------------------------------------------------
# TestBiomesEndpoint
# ---------------------------------------------------------------------------

class TestBiomesEndpoint:

    def test_biome_entry_fields(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        _issue(queen, _make_mission("m-1", capital=5_000.0))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/biomes")
        biomes = r.json()["biomes"]
        assert len(biomes) == 1
        b = biomes[0]
        assert b["biome_id"] == "crypto"
        assert b["limit"] == 20_000.0
        assert b["allocated"] == 5_000.0
        assert b["available"] == 15_000.0

    def test_biome_fraction(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        _issue(queen, _make_mission("m-1", capital=2_500.0))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/biomes")
        b = r.json()["biomes"][0]
        assert b["fraction"] == pytest.approx(0.25)

    def test_no_biome_limits_empty(self):
        queen = _make_queen()
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/biomes")
        assert r.json()["biomes"] == []


# ---------------------------------------------------------------------------
# TestAntsEndpoint
# ---------------------------------------------------------------------------

class TestAntsEndpoint:

    def test_ant_fields(self):
        queen = _make_queen()
        _issue(queen, _make_mission("m-1", ant_type="paper_ant"))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/ants")
        ants = r.json()["ants"]
        assert len(ants) == 1
        a = ants[0]
        assert a["ant_type"] == "paper_ant"
        assert a["node_id"] == "node-1"
        assert a["biome"] == "crypto"
        assert a["is_live"] is False

    def test_execution_ant_is_live(self):
        queen = _make_queen()
        _issue(queen, _make_mission("m-1", ant_type="execution_ant"))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/ants")
        assert r.json()["ants"][0]["is_live"] is True

    def test_ttl_remaining_present(self):
        queen = _make_queen()
        _issue(queen, _make_mission("m-1"))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/ants")
        ttl = r.json()["ants"][0]["ttl_remaining"]
        assert ttl is not None
        assert 0 <= ttl <= 3600

    def test_multiple_ants(self):
        queen = _make_queen()
        _issue(queen, _make_mission("m-1", ant_type="paper_ant"))
        _issue(queen, _make_mission("m-2", ant_type="scout_ant"))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/ants")
        assert len(r.json()["ants"]) == 2


# ---------------------------------------------------------------------------
# TestBrokersEndpoint
# ---------------------------------------------------------------------------

class TestBrokersEndpoint:

    def test_broker_connected_when_allocated(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        _issue(queen, _make_mission("m-1", capital=5_000.0))
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/brokers")
        b = r.json()["brokers"][0]
        assert b["status"] == "connected"
        assert b["capital_deployed"] == 5_000.0

    def test_broker_standby_when_not_allocated(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        # No missions issued → nothing allocated
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/brokers")
        b = r.json()["brokers"][0]
        assert b["status"] == "standby"

    def test_custom_broker_name(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        ctx = ColonyContext(
            queen=queen,
            scheduler=_make_scheduler(),
            broker_names={"crypto": "Bitvavo Pro"},
        )
        r = _client(ctx).get("/api/brokers")
        assert r.json()["brokers"][0]["name"] == "Bitvavo Pro"

    def test_default_broker_name_crypto(self):
        queen = _make_queen(capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/brokers")
        assert r.json()["brokers"][0]["name"] == "Bitvavo"


# ---------------------------------------------------------------------------
# TestTickerEndpoint
# ---------------------------------------------------------------------------

class TestTickerEndpoint:

    def test_reads_jsonl_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            events = [
                {"event_type": "mission_issued", "source": "queen",
                 "timestamp": _now_iso(), "mission_id": "m-1", "payload": {}},
                {"event_type": "action_executed", "source": "live_gate",
                 "timestamp": _now_iso(), "mission_id": "m-1", "payload": {"symbol": "BTC-EUR"}},
            ]
            _write_jsonl(logs / "missions" / "m-1.jsonl", events)
            ctx = ColonyContext(logs_root=logs)
            r = _client(ctx).get("/api/ticker")
            assert len(r.json()["events"]) == 2

    def test_returns_last_n_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            now = datetime.now(tz=timezone.utc)
            events = [
                {"event_type": f"ev_{i}", "source": "test",
                 "timestamp": (now + timedelta(seconds=i)).isoformat(),
                 "mission_id": None, "payload": {}}
                for i in range(25)
            ]
            _write_jsonl(logs / "colony" / "test.jsonl", events)
            ctx = ColonyContext(logs_root=logs)
            r = _client(ctx).get("/api/ticker")
            result = r.json()["events"]
            assert len(result) == 20
            # Most recent last
            assert result[-1]["event_type"] == "ev_24"

    def test_empty_logs_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ticker")
            assert r.json()["events"] == []

    def test_malformed_line_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            log_file = logs / "colony" / "bad.jsonl"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("w") as fh:
                fh.write("NOT JSON\n")
                fh.write(json.dumps({
                    "event_type": "ok", "source": "test",
                    "timestamp": _now_iso(), "mission_id": None, "payload": {}
                }) + "\n")
            ctx = ColonyContext(logs_root=logs)
            r = _client(ctx).get("/api/ticker")
            events = r.json()["events"]
            assert len(events) == 1
            assert events[0]["event_type"] == "ok"


# ---------------------------------------------------------------------------
# TestKillSwitchEndpoint
# ---------------------------------------------------------------------------

class TestKillSwitchEndpoint:

    def test_requires_operator_confirm(self):
        queen = _make_queen()
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).post("/api/killswitch", json={"level": 3, "operator_confirm": False})
        assert r.status_code == 400

    def test_invalid_level_rejected(self):
        queen = _make_queen()
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).post("/api/killswitch", json={"level": 99, "operator_confirm": True})
        assert r.status_code == 400

    def test_level_3_activates_colony_kill(self):
        queen = _make_queen()
        scheduler = _make_scheduler()
        ctx = ColonyContext(queen=queen, scheduler=scheduler)
        with patch.object(queen, "kill_switch") as mock_ks:
            r = _client(ctx).post("/api/killswitch", json={"level": 3, "operator_confirm": True})
            assert r.status_code == 200
            assert r.json()["executed"] is True
            mock_ks.assert_called_once()
            from ant_colony.colony.scheduler.colony_scheduler import KillLevel
            args = mock_ks.call_args
            assert args.kwargs.get("level") == KillLevel.COLONY or args.args[0] == KillLevel.COLONY

    def test_level_1_passes_scope(self):
        queen = _make_queen()
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        with patch.object(queen, "kill_switch") as mock_ks:
            r = _client(ctx).post("/api/killswitch",
                json={"level": 1, "scope": "ant-abc", "operator_confirm": True})
            assert r.status_code == 200
            mock_ks.assert_called_once()

    def test_response_contains_message(self):
        queen = _make_queen()
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        with patch.object(queen, "kill_switch"):
            r = _client(ctx).post("/api/killswitch", json={"level": 2, "operator_confirm": True})
            assert "message" in r.json()
            assert r.json()["level"] == 2


# ---------------------------------------------------------------------------
# TestServerApp
# ---------------------------------------------------------------------------

class TestServerApp:

    def test_root_serves_html(self):
        ctx = ColonyContext()
        r = _client(ctx).get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "ANT COLONY" in r.text

    def test_health_endpoint(self):
        r = _client(ColonyContext()).get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_create_app_none_context(self):
        # create_app(None) must not crash — uses empty ColonyContext
        app = create_app(None)
        client = TestClient(app)
        assert client.get("/api/status").status_code == 200


# ---------------------------------------------------------------------------
# TestParseTs (helper)
# ---------------------------------------------------------------------------

class TestParseTs:

    def test_valid_iso_utc(self):
        ts = "2026-04-16T12:34:56+00:00"
        dt = _parse_ts(ts)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_datetime_gets_utc(self):
        dt = _parse_ts("2026-04-16T12:34:56")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_returns_none(self):
        assert _parse_ts(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_ts("") is None

    def test_garbage_returns_none(self):
        assert _parse_ts("not-a-date") is None


# ---------------------------------------------------------------------------
# TestPerformanceChartEndpoint
# ---------------------------------------------------------------------------

class TestPerformanceChartEndpoint:

    def _write_trade(self, logs: Path, pnl: float, days_ago: float = 0.0,
                     filename: str = "m-1_trades.jsonl") -> None:
        ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
        _write_jsonl(logs / "paper" / filename, [{"realized_pnl": pnl, "closed_at": ts}])

    def test_no_logs_root_empty_response(self) -> None:
        r = _client(ColonyContext()).get("/api/performance/chart")
        assert r.status_code == 200
        d = r.json()
        assert d["points"] == []
        assert d["total_pnl"] == 0.0
        assert d["dag"] == 0.0
        assert d["alltime"] == 0.0

    def test_response_has_all_period_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 10.0, "closed_at": _now_iso()},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            d = r.json()
            for key in ("dag", "week", "maand", "jaar", "alltime", "curve_label", "total_pnl", "points"):
                assert key in d, f"missing key: {key}"

    def test_single_day_trade_alltime_curve(self) -> None:
        """Minder dan 2 dagdata → curve_label='alltime'."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 50.0, "closed_at": _now_iso()},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            d = r.json()
            assert d["curve_label"] == "alltime"

    def test_two_day_trades_today_curve(self) -> None:
        """2 of meer dagdata → curve_label='vandaag'."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 10.0, "closed_at": _now_iso()},
                {"realized_pnl": 20.0, "closed_at": _now_iso()},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            d = r.json()
            assert d["curve_label"] == "vandaag"

    def test_period_totals_correct(self) -> None:
        """Periode-totalen kloppen: dag telt alleen vandaag, alltime alles."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            now_ts  = _now_iso()
            old_ts  = (datetime.now(tz=timezone.utc) - timedelta(days=5)).isoformat()
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 100.0, "closed_at": now_ts},
                {"realized_pnl":  50.0, "closed_at": old_ts},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            d = r.json()
            assert d["alltime"] == pytest.approx(150.0)
            assert d["dag"]     == pytest.approx(100.0)
            assert d["week"]    == pytest.approx(150.0)   # beide binnen 7 dagen

    def test_alltime_curve_uses_all_historical_trades(self) -> None:
        """Alltime curve bevat ook historische trades van buiten vandaag."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 200.0, "closed_at": old_ts},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            d = r.json()
            assert d["curve_label"] == "alltime"
            assert d["total_pnl"]   == pytest.approx(200.0)
            assert len(d["points"])  >= 2   # startpunt + 1 trade

    def test_chart_starts_at_zero(self) -> None:
        """Eerste punt in de curve heeft pnl=0 (startpunt)."""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            _write_jsonl(logs / "paper" / "m-1_trades.jsonl", [
                {"realized_pnl": 40.0, "closed_at": _now_iso()},
                {"realized_pnl": 20.0, "closed_at": _now_iso()},
            ])
            r = _client(ColonyContext(logs_root=logs)).get("/api/performance/chart")
            points = r.json()["points"]
            assert points[0]["pnl"] == pytest.approx(0.0)

    def test_no_trades_at_all_empty_points(self) -> None:
        """Geen trades → lege puntenreeks, alle periodes 0."""
        with tempfile.TemporaryDirectory() as tmp:
            r = _client(ColonyContext(logs_root=Path(tmp))).get("/api/performance/chart")
            d = r.json()
            assert d["points"]  == []
            assert d["total_pnl"] == 0.0
            assert d["dag"]     == 0.0
            assert d["alltime"] == 0.0
