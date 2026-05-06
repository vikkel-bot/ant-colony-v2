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
from ant_colony.dashboard.api import (
    ColonyContext, create_router, _parse_ts,
    _compute_sl_tp_progress, _read_open_positions_from_logs,
    _build_event_summary, _parse_queen_decision, _read_queen_decisions,
)
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

    def test_live_eur_balance_from_crypto_adapter(self):
        from ant_colony.biome.biome_adapter import AccountState
        account = AccountState(
            biome_id="crypto", balance=80.64, positions_value=0.0,
            timestamp=datetime.now(tz=timezone.utc),
        )
        adapter = MagicMock()
        adapter.is_available.return_value = True
        adapter.get_account_state.return_value = account
        registry = MagicMock()
        registry.get.return_value = adapter
        ctx = ColonyContext(
            queen=_make_queen(),
            scheduler=_make_scheduler(),
            biome_registry=registry,
            broker_names={"crypto": "Bitvavo"},
        )
        r = _client(ctx).get("/api/metrics")
        d = r.json()
        assert d["live_eur_balance"] == pytest.approx(80.64)
        assert d["live_eur_source"] == "Bitvavo"

    def test_live_eur_balance_none_when_no_registry(self):
        ctx = ColonyContext(queen=_make_queen(), scheduler=_make_scheduler())
        r = _client(ctx).get("/api/metrics")
        d = r.json()
        assert d["live_eur_balance"] is None
        assert d["live_eur_source"] is None

    def test_live_eur_balance_none_when_adapter_raises(self):
        adapter = MagicMock()
        adapter.get_account_state.side_effect = RuntimeError("API down")
        registry = MagicMock()
        registry.get.return_value = adapter
        ctx = ColonyContext(
            queen=_make_queen(),
            scheduler=_make_scheduler(),
            biome_registry=registry,
        )
        r = _client(ctx).get("/api/metrics")
        d = r.json()
        assert d["live_eur_balance"] is None


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
# TestBrokersDataAge — last_updated / data_age_seconds in BrokerEntry
# ---------------------------------------------------------------------------

class TestBrokersDataAge:

    def _make_registry(self, *, available: bool, balance: float = 100.0) -> MagicMock:
        from ant_colony.biome.biome_adapter import AccountState
        account = AccountState(
            biome_id="crypto", balance=balance, positions_value=0.0,
            timestamp=datetime.now(tz=timezone.utc),
        )
        adapter = MagicMock()
        adapter.is_available.return_value = available
        adapter.get_account_state.return_value = account if available else None
        registry = MagicMock()
        registry.get.return_value = adapter
        return registry

    def test_connected_broker_has_last_updated(self):
        queen = _make_queen()
        queen.set_biome_capital("crypto", 10_000.0)
        _issue(queen, _make_mission())
        registry = self._make_registry(available=True)
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler(), biome_registry=registry)
        r = _client(ctx).get("/api/brokers")
        b = r.json()["brokers"][0]
        assert b["last_updated"] is not None
        assert b["data_age_seconds"] == pytest.approx(0.0)

    def test_disconnected_broker_has_no_last_updated(self):
        queen = _make_queen()
        queen.set_biome_capital("crypto", 10_000.0)
        _issue(queen, _make_mission())
        registry = self._make_registry(available=False)
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler(), biome_registry=registry)
        r = _client(ctx).get("/api/brokers")
        b = r.json()["brokers"][0]
        assert b["last_updated"] is None
        assert b["data_age_seconds"] is None

    def test_response_has_fetched_at(self):
        queen = _make_queen()
        queen.set_biome_capital("crypto", 10_000.0)
        ctx = ColonyContext(queen=queen, scheduler=_make_scheduler())
        r = _client(ctx).get("/api/brokers")
        assert "fetched_at" in r.json()
        assert r.json()["fetched_at"] != ""

    def test_empty_queen_returns_fetched_at(self):
        r = _client(ColonyContext()).get("/api/brokers")
        assert r.json()["fetched_at"] != ""


# ---------------------------------------------------------------------------
# TestCapitalEndpoint
# ---------------------------------------------------------------------------

class TestCapitalEndpoint:

    def _make_registry(
        self,
        balance: float = 80.0,
        holdings: float = 500.0,
        available: bool = True,
    ) -> MagicMock:
        from ant_colony.biome.biome_adapter import AccountState, LivePosition
        account = AccountState(
            biome_id="crypto", balance=balance, positions_value=holdings,
            timestamp=datetime.now(tz=timezone.utc),
        )
        pos = MagicMock(spec=LivePosition)
        pos.market_value = holdings
        adapter = MagicMock()
        adapter.is_available.return_value = available
        adapter.get_account_state.return_value = account if available else None
        adapter.get_positions.return_value = [pos] if available and holdings > 0 else []
        registry = MagicMock()
        registry.list_biomes.return_value = ["crypto"]
        registry.get.return_value = adapter
        return registry

    def test_empty_registry_returns_zeros(self):
        r = _client(ColonyContext()).get("/api/capital")
        d = r.json()
        assert d["total_eur"] == 0.0
        assert d["available_eur"] == 0.0
        assert d["holdings_eur"] == 0.0
        assert d["brokers"] == []
        assert "fetched_at" in d

    def test_connected_adapter_sums_balance_and_holdings(self):
        registry = self._make_registry(balance=80.0, holdings=500.0)
        ctx = ColonyContext(biome_registry=registry, broker_names={"crypto": "Bitvavo"})
        r = _client(ctx).get("/api/capital")
        d = r.json()
        assert d["total_eur"] == pytest.approx(580.0)
        assert d["available_eur"] == pytest.approx(80.0)
        assert d["holdings_eur"] == pytest.approx(500.0)
        assert len(d["brokers"]) == 1
        b = d["brokers"][0]
        assert b["name"] == "Bitvavo"
        assert b["status"] == "connected"
        assert b["total_eur"] == pytest.approx(580.0)
        assert b["available_eur"] == pytest.approx(80.0)
        assert b["holdings_eur"] == pytest.approx(500.0)

    def test_connected_broker_data_age_is_zero(self):
        registry = self._make_registry(balance=100.0, holdings=0.0)
        ctx = ColonyContext(biome_registry=registry)
        r = _client(ctx).get("/api/capital")
        b = r.json()["brokers"][0]
        assert b["data_age_seconds"] == pytest.approx(0.0)
        assert b["last_updated"] is not None

    def test_disconnected_adapter_status_and_no_data_age(self):
        registry = self._make_registry(balance=0.0, holdings=0.0, available=False)
        ctx = ColonyContext(biome_registry=registry)
        r = _client(ctx).get("/api/capital")
        d = r.json()
        assert d["total_eur"] == pytest.approx(0.0)
        b = d["brokers"][0]
        assert b["status"] == "disconnected"
        assert b["last_updated"] is None
        assert b["data_age_seconds"] is None

    def test_adapter_exception_treated_as_disconnected(self):
        adapter = MagicMock()
        adapter.is_available.side_effect = RuntimeError("timeout")
        registry = MagicMock()
        registry.list_biomes.return_value = ["crypto"]
        registry.get.return_value = adapter
        ctx = ColonyContext(biome_registry=registry)
        r = _client(ctx).get("/api/capital")
        assert r.status_code == 200
        assert r.json()["total_eur"] == pytest.approx(0.0)

    def test_response_has_fetched_at(self):
        registry = self._make_registry()
        ctx = ColonyContext(biome_registry=registry)
        r = _client(ctx).get("/api/capital")
        assert r.json()["fetched_at"] != ""

    def test_multiple_brokers_sum_correctly(self):
        from ant_colony.biome.biome_adapter import AccountState
        def _make_adapter(balance: float, holdings: float) -> MagicMock:
            acc = AccountState(
                biome_id="x", balance=balance, positions_value=holdings,
                timestamp=datetime.now(tz=timezone.utc),
            )
            pos = MagicMock()
            pos.market_value = holdings
            a = MagicMock()
            a.is_available.return_value = True
            a.get_account_state.return_value = acc
            a.get_positions.return_value = [pos] if holdings > 0 else []
            return a

        registry = MagicMock()
        registry.list_biomes.return_value = ["crypto", "equities"]
        registry.get.side_effect = lambda b: (
            _make_adapter(80.0, 500.0) if b == "crypto" else _make_adapter(1000.0, 200.0)
        )
        ctx = ColonyContext(biome_registry=registry)
        r = _client(ctx).get("/api/capital")
        d = r.json()
        assert d["total_eur"] == pytest.approx(1780.0)
        assert d["available_eur"] == pytest.approx(1080.0)
        assert d["holdings_eur"] == pytest.approx(700.0)
        assert len(d["brokers"]) == 2


# ---------------------------------------------------------------------------
# TestSlTpProgress — unit tests voor de helper
# ---------------------------------------------------------------------------

class TestSlTpProgress:

    def test_long_at_sl_returns_zero(self):
        assert _compute_sl_tp_progress("long", 90.0, sl=90.0, tp=110.0) == pytest.approx(0.0)

    def test_long_at_tp_returns_one(self):
        assert _compute_sl_tp_progress("long", 110.0, sl=90.0, tp=110.0) == pytest.approx(1.0)

    def test_long_at_entry_symmetric_is_half(self):
        # entry = 100, SL = 90, TP = 110 → (100-90)/(110-90) = 0.5
        assert _compute_sl_tp_progress("long", 100.0, sl=90.0, tp=110.0) == pytest.approx(0.5)

    def test_short_at_sl_returns_zero(self):
        # SHORT: SL is above entry, TP is below
        assert _compute_sl_tp_progress("short", 110.0, sl=110.0, tp=90.0) == pytest.approx(0.0)

    def test_short_at_tp_returns_one(self):
        assert _compute_sl_tp_progress("short", 90.0, sl=110.0, tp=90.0) == pytest.approx(1.0)

    def test_short_at_entry_symmetric_is_half(self):
        # entry = 100, SL = 110, TP = 90 → (110-100)/(110-90) = 0.5
        assert _compute_sl_tp_progress("short", 100.0, sl=110.0, tp=90.0) == pytest.approx(0.5)

    def test_none_current_returns_none(self):
        assert _compute_sl_tp_progress("long", None, sl=90.0, tp=110.0) is None

    def test_clamped_below_zero(self):
        # price below SL → clamped to 0.0
        assert _compute_sl_tp_progress("long", 80.0, sl=90.0, tp=110.0) == pytest.approx(0.0)

    def test_clamped_above_one(self):
        # price above TP → clamped to 1.0
        assert _compute_sl_tp_progress("long", 120.0, sl=90.0, tp=110.0) == pytest.approx(1.0)

    def test_zero_range_returns_none(self):
        # sl == tp → invalid
        assert _compute_sl_tp_progress("long", 100.0, sl=100.0, tp=100.0) is None


# ---------------------------------------------------------------------------
# TestReadOpenPositionsFromLogs
# ---------------------------------------------------------------------------

class TestReadOpenPositionsFromLogs:

    def _write_event(self, path: Path, action: str, payload: dict,
                     source: str = "ant-1", ts: str | None = None) -> None:
        ts = ts or datetime.now(tz=timezone.utc).isoformat()
        record = {"timestamp": ts, "source": source, "payload": {"action": action, **payload}}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_no_paper_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _read_open_positions_from_logs(Path(tmp))
        assert result == []

    def test_opened_without_closed_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {
                "position_id": "pos-1", "symbol": "BTC-EUR", "biome": "crypto",
                "side": "long", "entry_price": 100.0, "quantity": 0.01,
                "stop_loss": 90.0, "take_profit": 120.0, "strategy_type": "sma",
            })
            result = _read_open_positions_from_logs(Path(tmp))
        assert len(result) == 1
        assert result[0]["position_id"] == "pos-1"
        assert result[0]["symbol"] == "BTC-EUR"

    def test_opened_and_closed_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {"position_id": "pos-1", "symbol": "BTC-EUR"})
            self._write_event(p, "trade_closed", {"position_id": "pos-1"})
            result = _read_open_positions_from_logs(Path(tmp))
        assert result == []

    def test_zombie_position_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            old_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat()
            self._write_event(p, "trade_opened", {
                "position_id": "old-pos", "symbol": "ETH-EUR",
            }, ts=old_ts)
            result = _read_open_positions_from_logs(Path(tmp))
        assert result == []


# ---------------------------------------------------------------------------
# TestPositionsEndpoint
# ---------------------------------------------------------------------------

class TestPositionsEndpoint:

    def _write_event(self, path: Path, action: str, payload: dict,
                     source: str = "ant-1", ts: str | None = None) -> None:
        ts = ts or datetime.now(tz=timezone.utc).isoformat()
        record = {"timestamp": ts, "source": source, "payload": {"action": action, **payload}}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def _make_adapter(self, close_price: float) -> MagicMock:
        from ant_colony.biome.biome_adapter import MarketData
        md = MagicMock(spec=MarketData)
        md.close = close_price
        adapter = MagicMock()
        adapter.get_market_data.return_value = md
        return adapter

    def test_empty_when_no_logs_root(self):
        r = _client(ColonyContext()).get("/api/positions")
        d = r.json()
        assert d["count"] == 0
        assert d["positions"] == []
        assert "fetched_at" in d

    def test_empty_when_no_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/positions")
        assert r.json()["count"] == 0

    def test_pnl_calculated_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {
                "position_id": "pos-1", "symbol": "BTC-EUR", "biome": "crypto",
                "side": "long", "entry_price": 100.0, "quantity": 1.0,
                "stop_loss": 90.0, "take_profit": 120.0,
            })
            adapter = self._make_adapter(close_price=110.0)
            registry = MagicMock()
            registry.list_biomes.return_value = ["crypto"]
            registry.get.return_value = adapter
            ctx = ColonyContext(logs_root=Path(tmp), biome_registry=registry)
            r = _client(ctx).get("/api/positions")
        d = r.json()
        assert d["count"] == 1
        pos = d["positions"][0]
        assert pos["current_price"] == pytest.approx(110.0)
        assert pos["pnl_eur"]       == pytest.approx(10.0)
        assert pos["pnl_pct"]       == pytest.approx(10.0)
        assert d["total_pnl_eur"]   == pytest.approx(10.0)

    def test_sl_tp_progress_at_entry(self):
        # entry=100, SL=90, TP=120 → current=100 → progress=(100-90)/(120-90)=0.333
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {
                "position_id": "pos-1", "symbol": "BTC-EUR", "biome": "crypto",
                "side": "long", "entry_price": 100.0, "quantity": 1.0,
                "stop_loss": 90.0, "take_profit": 120.0,
            })
            adapter = self._make_adapter(close_price=100.0)
            registry = MagicMock()
            registry.list_biomes.return_value = ["crypto"]
            registry.get.return_value = adapter
            ctx = ColonyContext(logs_root=Path(tmp), biome_registry=registry)
            r = _client(ctx).get("/api/positions")
        pos = r.json()["positions"][0]
        assert pos["sl_tp_progress"] == pytest.approx(10/30, rel=1e-3)

    def test_no_live_price_returns_null_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {
                "position_id": "pos-1", "symbol": "BTC-EUR", "biome": "crypto",
                "side": "long", "entry_price": 100.0, "quantity": 1.0,
                "stop_loss": 90.0, "take_profit": 120.0,
            })
            ctx = ColonyContext(logs_root=Path(tmp))   # geen registry
            r = _client(ctx).get("/api/positions")
        pos = r.json()["positions"][0]
        assert pos["current_price"]  is None
        assert pos["pnl_eur"]        is None
        assert pos["pnl_pct"]        is None
        assert pos["sl_tp_progress"] is None
        assert r.json()["total_pnl_eur"] is None

    def test_multiple_positions_summed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            for i, (entry, price) in enumerate([(100.0, 110.0), (200.0, 210.0)]):
                self._write_event(p, "trade_opened", {
                    "position_id": f"pos-{i}", "symbol": "BTC-EUR", "biome": "crypto",
                    "side": "long", "entry_price": entry, "quantity": 1.0,
                    "stop_loss": entry * 0.9, "take_profit": entry * 1.2,
                })
            adapter = MagicMock()
            from ant_colony.biome.biome_adapter import MarketData
            # Return different price per call
            adapter.get_market_data.side_effect = [
                MagicMock(spec=MarketData, close=110.0),
                MagicMock(spec=MarketData, close=210.0),
            ]
            registry = MagicMock()
            registry.list_biomes.return_value = ["crypto"]
            registry.get.return_value = adapter
            ctx = ColonyContext(logs_root=Path(tmp), biome_registry=registry)
            r = _client(ctx).get("/api/positions")
        d = r.json()
        assert d["count"] == 2
        # pnl: 10 + 10 = 20
        assert d["total_pnl_eur"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# TestBuildEventSummary — unit tests voor de helper
# ---------------------------------------------------------------------------

class TestBuildEventSummary:

    def test_trade_opened(self):
        s = _build_event_summary("trade_opened", {
            "symbol": "BTC-EUR", "side": "long",
            "quantity": 0.00075, "entry_price": 66402.0,
        })
        assert "BTC-EUR" in s
        assert "LONG" in s
        assert "66402" in s

    def test_trade_closed(self):
        s = _build_event_summary("trade_closed", {
            "symbol": "ETH-EUR", "exit_reason": "stop_loss", "realized_pnl": 0.54,
        })
        assert "ETH-EUR" in s
        assert "stop_loss" in s
        assert "+0.54" in s

    def test_trade_closed_negative_pnl(self):
        s = _build_event_summary("trade_closed", {
            "symbol": "BTC-EUR", "exit_reason": "stop_loss", "realized_pnl": -2.10,
        })
        assert "-2.10" in s

    def test_candidate_accepted(self):
        s = _build_event_summary("candidate_accepted", {
            "symbol": "BTC-EUR", "sharpe": 0.245, "win_rate": 0.587,
        })
        assert "sharpe=0.245" in s
        assert "win_rate=0.587" in s

    def test_opportunity_detected(self):
        s = _build_event_summary("opportunity_detected", {
            "symbol": "ETH-EUR", "confidence": 0.83,
        })
        assert "ETH-EUR" in s
        assert "confidence=0.83" in s

    def test_repo_ingested(self):
        s = _build_event_summary("repo_ingested", {
            "repo": "user/repo-name", "stars": 142,
        })
        assert "user/repo-name" in s
        assert "stars=142" in s

    def test_pnl_summary(self):
        s = _build_event_summary("pnl_summary", {
            "trade_count": 5, "total_realized_pnl": 3.12, "win_rate": 0.60,
        })
        assert "5 closed" in s
        assert "+3.12" in s
        assert "winrate=60%" in s

    def test_unknown_action_returns_action_or_symbol(self):
        s = _build_event_summary("some_unknown_action", {"symbol": "BTC-EUR"})
        assert "BTC-EUR" in s

    def test_unknown_action_no_symbol_returns_action(self):
        s = _build_event_summary("heartbeat_sent", {})
        assert s == "heartbeat sent"


# ---------------------------------------------------------------------------
# TestAntEventsEndpoint
# ---------------------------------------------------------------------------

class TestAntEventsEndpoint:

    def _write_event(self, path: Path, action: str, payload: dict,
                     source: str = "ant-1", ts: str | None = None) -> None:
        ts = ts or datetime.now(tz=timezone.utc).isoformat()
        record = {
            "timestamp": ts,
            "source": source,
            "payload": {"action": action, **payload},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def test_empty_when_no_logs_root(self):
        r = _client(ColonyContext()).get("/api/ants/research/events")
        assert r.status_code == 200
        d = r.json()
        assert d["count"] == 0
        assert d["events"] == []
        assert d["ant_type"] == "research"

    def test_unknown_ant_type_returns_404(self):
        r = _client(ColonyContext()).get("/api/ants/nonexistent/events")
        assert r.status_code == 404

    def test_returns_events_for_known_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "research" / "ant-1.jsonl"
            for i in range(3):
                self._write_event(p, "candidate_accepted", {
                    "symbol": "BTC-EUR", "sharpe": 0.3 + i * 0.1,
                })
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/research/events")
        d = r.json()
        assert d["count"] == 3
        assert d["ant_type"] == "research"
        for ev in d["events"]:
            assert "action" in ev
            assert "summary" in ev
            assert "payload" in ev
            assert "timestamp" in ev

    def test_limit_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "research" / "ant-1.jsonl"
            for i in range(10):
                self._write_event(p, "candidate_accepted", {"symbol": "BTC-EUR"})
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/research/events?limit=5")
        d = r.json()
        assert d["count"] == 5
        assert d["has_more"] is True

    def test_events_sorted_descending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "scouts" / "ant-1.jsonl"
            now = datetime.now(tz=timezone.utc)
            for delta in [60, 30, 10]:   # oldest first in file
                ts = (now - timedelta(seconds=delta)).isoformat()
                self._write_event(p, "signal_detected", {"symbol": "BTC-EUR"}, ts=ts)
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/scout/events")
        events = r.json()["events"]
        assert len(events) == 3
        # Newest first → smallest delta first
        ts0 = events[0]["timestamp"]
        ts1 = events[1]["timestamp"]
        ts2 = events[2]["timestamp"]
        assert ts0 > ts1 > ts2

    def test_events_older_than_24h_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "research" / "ant-1.jsonl"
            old_ts  = (datetime.now(tz=timezone.utc) - timedelta(hours=25)).isoformat()
            new_ts  = datetime.now(tz=timezone.utc).isoformat()
            self._write_event(p, "candidate_accepted", {"symbol": "OLD"}, ts=old_ts)
            self._write_event(p, "candidate_accepted", {"symbol": "NEW"}, ts=new_ts)
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/research/events")
        d = r.json()
        assert d["count"] == 1
        assert d["events"][0]["payload"]["symbol"] == "NEW"

    def test_has_more_false_when_all_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "research" / "ant-1.jsonl"
            for _ in range(3):
                self._write_event(p, "candidate_accepted", {"symbol": "BTC-EUR"})
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/research/events?limit=10")
        assert r.json()["has_more"] is False

    def test_summary_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "paper" / "ant-1.jsonl"
            self._write_event(p, "trade_opened", {
                "symbol": "ETH-EUR", "side": "long",
                "entry_price": 3000.0, "quantity": 0.01,
            })
            ctx = ColonyContext(logs_root=Path(tmp))
            r = _client(ctx).get("/api/ants/paper/events")
        ev = r.json()["events"][0]
        assert "ETH-EUR" in ev["summary"]
        assert ev["action"] == "trade_opened"


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
        data = r.json()
        assert data["status"] == "ok"
        assert data["uptime_seconds"] >= 0
        assert data["agents"] == 0

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
