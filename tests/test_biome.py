"""
tests/test_biome.py

Fase 5 — Multi-biome scaffolding.

Test-opzet:
  - TestMarketData           snapshot, is_stale, is_valid_price
  - TestAccountState         equity property, is_stale
  - TestBiomeAdapterProtocol isinstance() check via runtime_checkable
  - TestBiomeRegistry        register, unregister, get, list, hot-swap
  - TestBiomeConfig          velden, validators, factory methods
  - TestQueenBiomeCapital    set_biome_capital, limits, allocated, available,
                             issue_mission met biome-check
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ant_colony.biome import AccountState, BiomeAdapter, BiomeRegistry, MarketData
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.queen import MissionIssueResult, MissionRejectionReason, Queen
from ant_colony.schemas.biome import BiomeConfig, BiomeRiskProfile, ExecutionConstraints
from ant_colony.schemas.mission import (
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)
from ant_colony.schemas.node import Node, NodeStatus, RuntimePaths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_market_data(
    symbol: str = "BTC-EUR",
    biome_id: str = "crypto",
    age_seconds: float = 0.0,
    close: float = 30_000.0,
) -> MarketData:
    ts = _now() - timedelta(seconds=age_seconds)
    return MarketData(
        symbol=symbol,
        timeframe="1h",
        timestamp=ts,
        open=close * 0.99,
        high=close * 1.01,
        low=close * 0.98,
        close=close,
        volume=100.0,
        biome_id=biome_id,
    )


def _make_account_state(
    biome_id: str = "crypto",
    balance: float = 5_000.0,
    positions_value: float = 2_000.0,
    age_seconds: float = 0.0,
) -> AccountState:
    ts = _now() - timedelta(seconds=age_seconds)
    return AccountState(
        biome_id=biome_id,
        balance=balance,
        positions_value=positions_value,
        timestamp=ts,
    )


def _make_scheduler(tmp_path: Path) -> ColonyScheduler:
    return ColonyScheduler(logs_root=tmp_path, tick_interval=5)


def _make_queen(scheduler: ColonyScheduler, capital: float = 50_000.0) -> Queen:
    queen = Queen(capital_total=capital, scheduler=scheduler)
    # Registreer standaard node die overeenkomt met _make_mission() defaults
    queen.register_node(Node(
        node_id="node-1",
        hostname="host-node-1",
        allowed_biomes=["crypto", "equities"],
        allowed_ant_types=["research_ant"],
        heartbeat_interval=60,
        runtime_paths=RuntimePaths(output="/out", live="/live", logs="/logs"),
    ))
    return queen


def _make_mission(
    mission_id: str = "m-001",
    capital: float = 5_000.0,
    biome: str = "crypto",
) -> Mission:
    return Mission(
        mission_id=mission_id,
        ant_type="research_ant",
        allowed_node="node-1",
        allowed_actions=["read"],
        market_scope=MarketScope(biome=biome, symbols=["BTC-EUR"]),
        capital_limit=capital,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.10,
            max_position_size=500.0,
            daily_loss_limit=200.0,
        ),
        ttl=86_400,
        heartbeat_interval=60,
        success_conditions=SuccessConditions(description="test"),
    )


# ---------------------------------------------------------------------------
# Stub adapter voor protocol-tests
# ---------------------------------------------------------------------------

class StubAdapter:
    """Minimale duck-type implementatie van BiomeAdapter."""

    def __init__(self, biome_id: str, available: bool = True) -> None:
        self._biome_id = biome_id
        self._available = available

    @property
    def biome_id(self) -> str:
        return self._biome_id

    def is_available(self) -> bool:
        return self._available

    def get_market_data(self, symbol: str, timeframe: str) -> MarketData | None:
        return _make_market_data(symbol=symbol, biome_id=self._biome_id)

    def get_account_state(self) -> AccountState | None:
        return _make_account_state(biome_id=self._biome_id)

    def place_order(self, order):
        return None

    def get_positions(self):
        return []


# ---------------------------------------------------------------------------
# TestMarketData
# ---------------------------------------------------------------------------

class TestMarketData:
    def test_fresh_data_is_not_stale(self):
        data = _make_market_data(age_seconds=0.0)
        assert not data.is_stale()

    def test_data_older_than_default_threshold_is_stale(self):
        data = _make_market_data(age_seconds=301.0)
        assert data.is_stale()

    def test_data_well_within_threshold_is_not_stale(self):
        # age << max_age_seconds → not stale (strictly greater check)
        data = _make_market_data(age_seconds=10.0)
        assert not data.is_stale(max_age_seconds=3600.0)

    def test_custom_max_age(self):
        data = _make_market_data(age_seconds=10.0)
        assert data.is_stale(max_age_seconds=5.0)
        assert not data.is_stale(max_age_seconds=60.0)

    def test_is_valid_price_true_for_positive_prices(self):
        data = _make_market_data(close=30_000.0)
        assert data.is_valid_price

    def test_is_valid_price_false_when_close_is_zero(self):
        data = _make_market_data(close=0.0)
        assert not data.is_valid_price

    def test_is_valid_price_false_when_close_is_negative(self):
        data = _make_market_data(close=-1.0)
        assert not data.is_valid_price

    def test_biome_id_stored(self):
        data = _make_market_data(biome_id="equities")
        assert data.biome_id == "equities"

    def test_symbol_stored(self):
        data = _make_market_data(symbol="AAPL")
        assert data.symbol == "AAPL"


# ---------------------------------------------------------------------------
# TestAccountState
# ---------------------------------------------------------------------------

class TestAccountState:
    def test_equity_is_balance_plus_positions(self):
        state = _make_account_state(balance=5_000.0, positions_value=2_000.0)
        assert state.equity == 7_000.0

    def test_equity_with_zero_positions(self):
        state = _make_account_state(balance=10_000.0, positions_value=0.0)
        assert state.equity == 10_000.0

    def test_fresh_state_is_not_stale(self):
        state = _make_account_state(age_seconds=0.0)
        assert not state.is_stale()

    def test_state_older_than_default_threshold_is_stale(self):
        state = _make_account_state(age_seconds=61.0)
        assert state.is_stale()

    def test_custom_max_age(self):
        state = _make_account_state(age_seconds=30.0)
        assert state.is_stale(max_age_seconds=10.0)
        assert not state.is_stale(max_age_seconds=60.0)

    def test_default_currency_is_eur(self):
        state = _make_account_state()
        assert state.currency == "EUR"

    def test_biome_id_stored(self):
        state = _make_account_state(biome_id="equities")
        assert state.biome_id == "equities"


# ---------------------------------------------------------------------------
# TestBiomeAdapterProtocol
# ---------------------------------------------------------------------------

class TestBiomeAdapterProtocol:
    def test_stub_satisfies_protocol(self):
        adapter = StubAdapter("crypto")
        assert isinstance(adapter, BiomeAdapter)

    def test_object_without_biome_id_fails_protocol(self):
        class Incomplete:
            def is_available(self): return True
            def get_market_data(self, s, t): return None
            def get_account_state(self): return None

        assert not isinstance(Incomplete(), BiomeAdapter)

    def test_object_without_is_available_fails_protocol(self):
        class Incomplete:
            @property
            def biome_id(self): return "x"
            def get_market_data(self, s, t): return None
            def get_account_state(self): return None

        assert not isinstance(Incomplete(), BiomeAdapter)

    def test_adapter_returns_market_data(self):
        adapter = StubAdapter("crypto")
        data = adapter.get_market_data("BTC-EUR", "1h")
        assert data is not None
        assert data.symbol == "BTC-EUR"

    def test_adapter_returns_account_state(self):
        adapter = StubAdapter("crypto")
        state = adapter.get_account_state()
        assert state is not None
        assert state.biome_id == "crypto"

    def test_adapter_is_available(self):
        adapter = StubAdapter("crypto", available=True)
        assert adapter.is_available()

    def test_unavailable_adapter(self):
        adapter = StubAdapter("crypto", available=False)
        assert not adapter.is_available()


# ---------------------------------------------------------------------------
# TestBiomeRegistry
# ---------------------------------------------------------------------------

class TestBiomeRegistry:
    def test_empty_registry_has_count_zero(self):
        registry = BiomeRegistry()
        assert registry.count == 0

    def test_register_increases_count(self):
        registry = BiomeRegistry()
        registry.register(StubAdapter("crypto"))
        assert registry.count == 1

    def test_get_returns_registered_adapter(self):
        registry = BiomeRegistry()
        adapter = StubAdapter("crypto")
        registry.register(adapter)
        assert registry.get("crypto") is adapter

    def test_get_unknown_returns_none(self):
        registry = BiomeRegistry()
        assert registry.get("unknown") is None

    def test_is_registered_true_after_register(self):
        registry = BiomeRegistry()
        registry.register(StubAdapter("crypto"))
        assert registry.is_registered("crypto")

    def test_is_registered_false_before_register(self):
        registry = BiomeRegistry()
        assert not registry.is_registered("crypto")

    def test_unregister_removes_adapter(self):
        registry = BiomeRegistry()
        registry.register(StubAdapter("crypto"))
        registry.unregister("crypto")
        assert registry.get("crypto") is None
        assert registry.count == 0

    def test_unregister_unknown_is_idempotent(self):
        registry = BiomeRegistry()
        registry.unregister("nonexistent")  # should not raise
        assert registry.count == 0

    def test_register_overwrites_existing(self):
        registry = BiomeRegistry()
        adapter1 = StubAdapter("crypto")
        adapter2 = StubAdapter("crypto")
        registry.register(adapter1)
        registry.register(adapter2)
        assert registry.get("crypto") is adapter2
        assert registry.count == 1

    def test_list_biomes_returns_sorted(self):
        registry = BiomeRegistry()
        registry.register(StubAdapter("equities"))
        registry.register(StubAdapter("crypto"))
        registry.register(StubAdapter("commodities"))
        assert registry.list_biomes() == ["commodities", "crypto", "equities"]

    def test_list_biomes_empty_when_no_adapters(self):
        registry = BiomeRegistry()
        assert registry.list_biomes() == []

    def test_empty_biome_id_raises(self):
        registry = BiomeRegistry()
        adapter = StubAdapter("")
        with pytest.raises(ValueError, match="biome_id"):
            registry.register(adapter)

    def test_multiple_biomes_independent(self):
        registry = BiomeRegistry()
        crypto = StubAdapter("crypto")
        equities = StubAdapter("equities")
        registry.register(crypto)
        registry.register(equities)
        assert registry.get("crypto") is crypto
        assert registry.get("equities") is equities
        assert registry.count == 2


# ---------------------------------------------------------------------------
# TestBiomeConfig
# ---------------------------------------------------------------------------

class TestBiomeConfig:
    def test_crypto_factory_biome_id(self):
        cfg = BiomeConfig.crypto()
        assert cfg.biome_id == "crypto"

    def test_crypto_factory_is_active(self):
        cfg = BiomeConfig.crypto()
        assert cfg.is_active

    def test_crypto_factory_has_symbols(self):
        cfg = BiomeConfig.crypto()
        assert len(cfg.symbols) >= 1

    def test_crypto_factory_custom_symbols(self):
        cfg = BiomeConfig.crypto(symbols=["ETH-EUR"])
        assert cfg.symbols == ["ETH-EUR"]

    def test_equities_factory_biome_id(self):
        cfg = BiomeConfig.equities()
        assert cfg.biome_id == "equities"

    def test_commodities_factory_biome_id(self):
        cfg = BiomeConfig.commodities()
        assert cfg.biome_id == "commodities"

    def test_biome_id_must_be_lowercase(self):
        with pytest.raises(ValueError, match="lowercase"):
            BiomeConfig(
                biome_id="Crypto",
                display_name="x",
                symbols=["BTC-EUR"],
                risk_profile=BiomeRiskProfile(
                    max_position_pct=0.1, min_trade_size=10.0, max_daily_trades=5
                ),
                execution_constraints=ExecutionConstraints(
                    min_lot_size=0.001, price_precision=2
                ),
            )

    def test_biome_id_must_not_contain_spaces(self):
        with pytest.raises(ValueError, match="spaces"):
            BiomeConfig(
                biome_id="my biome",
                display_name="x",
                symbols=["BTC-EUR"],
                risk_profile=BiomeRiskProfile(
                    max_position_pct=0.1, min_trade_size=10.0, max_daily_trades=5
                ),
                execution_constraints=ExecutionConstraints(
                    min_lot_size=0.001, price_precision=2
                ),
            )

    def test_risk_profile_max_position_pct_bounds(self):
        with pytest.raises(ValueError):
            BiomeRiskProfile(max_position_pct=0.0, min_trade_size=10.0, max_daily_trades=1)
        with pytest.raises(ValueError):
            BiomeRiskProfile(max_position_pct=1.1, min_trade_size=10.0, max_daily_trades=1)

    def test_execution_constraints_default_trading_hours(self):
        ec = ExecutionConstraints(min_lot_size=0.01, price_precision=2)
        assert ec.trading_hours_utc == ["00:00-23:59"]

    def test_crypto_trading_hours_24_7(self):
        cfg = BiomeConfig.crypto()
        assert cfg.execution_constraints.trading_hours_utc == ["00:00-23:59"]

    def test_equities_trading_hours_limited(self):
        cfg = BiomeConfig.equities()
        assert cfg.execution_constraints.trading_hours_utc != ["00:00-23:59"]


# ---------------------------------------------------------------------------
# TestQueenBiomeCapital
# ---------------------------------------------------------------------------

class TestQueenBiomeCapital:
    def test_biome_limit_none_before_set(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        assert queen.biome_capital_limit("crypto") is None

    def test_set_biome_capital_stores_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        assert queen.biome_capital_limit("crypto") == 10_000.0

    def test_set_biome_capital_negative_raises(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        with pytest.raises(ValueError, match=">= 0"):
            queen.set_biome_capital("crypto", -1.0)

    def test_set_biome_capital_exceeds_colony_total_raises(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=10_000.0)
        with pytest.raises(ValueError, match="capital_total"):
            queen.set_biome_capital("crypto", 20_000.0)

    def test_set_biome_capital_equal_to_colony_total_ok(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=10_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        assert queen.biome_capital_limit("crypto") == 10_000.0

    def test_biome_allocated_zero_before_missions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        queen.set_biome_capital("crypto", 5_000.0)
        assert queen.biome_capital_allocated("crypto") == 0.0

    def test_biome_allocated_increases_after_issue(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 20_000.0)
        queen.issue_mission(_make_mission("m1", capital=8_000.0, biome="crypto"))
        assert queen.biome_capital_allocated("crypto") == 8_000.0

    def test_biome_allocated_only_counts_matching_biome(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.issue_mission(_make_mission("m1", capital=5_000.0, biome="crypto"))
        queen.issue_mission(_make_mission("m2", capital=4_000.0, biome="equities"))
        assert queen.biome_capital_allocated("crypto") == 5_000.0
        assert queen.biome_capital_allocated("equities") == 4_000.0

    def test_biome_available_none_when_no_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched)
        assert queen.biome_capital_available("crypto") is None

    def test_biome_available_equals_limit_before_missions(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        assert queen.biome_capital_available("crypto") == 10_000.0

    def test_biome_available_decreases_after_issue(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        queen.issue_mission(_make_mission("m1", capital=3_000.0, biome="crypto"))
        assert queen.biome_capital_available("crypto") == 7_000.0

    def test_biome_available_restored_after_revoke(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        queen.issue_mission(_make_mission("m1", capital=3_000.0, biome="crypto"))
        queen.revoke_mission("m1")
        assert queen.biome_capital_available("crypto") == 10_000.0

    def test_biome_available_never_negative(self, tmp_path):
        # Biome limit lowered after missions are issued (hypothetical) — floor at 0
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        queen.issue_mission(_make_mission("m1", capital=10_000.0, biome="crypto"))
        # Manually lower limit after the fact (bypass via internal dict for test)
        queen._biome_limits["crypto"] = 5_000.0
        assert queen.biome_capital_available("crypto") == 0.0

    def test_issue_mission_accepted_within_biome_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        result = queen.issue_mission(_make_mission("m1", capital=5_000.0, biome="crypto"))
        assert result.accepted

    def test_issue_mission_rejected_when_biome_limit_exceeded(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 3_000.0)
        result = queen.issue_mission(_make_mission("m1", capital=5_000.0, biome="crypto"))
        assert not result.accepted
        assert result.rejection_reason == MissionRejectionReason.BIOME_CAPITAL_EXCEEDED

    def test_biome_rejection_reason_in_result(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 1_000.0)
        result = queen.issue_mission(_make_mission("m1", capital=2_000.0, biome="crypto"))
        assert result.rejection_reason == MissionRejectionReason.BIOME_CAPITAL_EXCEEDED
        assert "biome_capital_available" in result.rejection_detail

    def test_unconstrained_biome_passes_biome_check(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        # No set_biome_capital call — biome is unconstrained
        result = queen.issue_mission(_make_mission("m1", capital=10_000.0, biome="equities"))
        assert result.accepted

    def test_colony_capital_check_still_applies_without_biome_limit(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=5_000.0)
        result = queen.issue_mission(_make_mission("m1", capital=10_000.0, biome="equities"))
        assert not result.accepted
        assert result.rejection_reason == MissionRejectionReason.CAPITAL_EXCEEDED

    def test_biome_check_before_colony_check_not_needed(self, tmp_path):
        # Colony check fires first (duplicate check). Biome check must not shadow duplicate.
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 10_000.0)
        queen.issue_mission(_make_mission("m1", capital=3_000.0, biome="crypto"))
        result = queen.issue_mission(_make_mission("m1", capital=3_000.0, biome="crypto"))
        assert result.rejection_reason == MissionRejectionReason.DUPLICATE_MISSION_ID

    def test_multiple_biome_limits_independent(self, tmp_path):
        sched = _make_scheduler(tmp_path)
        queen = _make_queen(sched, capital=50_000.0)
        queen.set_biome_capital("crypto", 5_000.0)
        queen.set_biome_capital("equities", 8_000.0)
        queen.issue_mission(_make_mission("m1", capital=4_000.0, biome="crypto"))
        assert queen.biome_capital_available("crypto") == 1_000.0
        assert queen.biome_capital_available("equities") == 8_000.0
