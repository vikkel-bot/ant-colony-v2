"""
tests/test_rebalance_ant.py

Tests voor RebalanceAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.rebalance_ant import (
    RebalanceAnt,
    _BASE_WEIGHTS,
    _LOW_VOL_ETFS,
    _QUARTERLY_DAYS,
    _WEIGHTS_BY_REGIME,
)
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mission() -> Mission:
    return Mission(
        mission_id=str(uuid.uuid4()),
        ant_type="rebalance_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["SPY"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="rebalance test"),
    )


def make_ant(logs_root: Path | None = None) -> RebalanceAnt:
    return RebalanceAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=MagicMock(spec=BiomeRegistry),
        logs_root=logs_root,
    )


def write_vix_signal(logs_root: Path, regime: str, signal_date: str, ant_id: str | None = None) -> None:
    vol_dir = logs_root / "equities" / "volatility"
    vol_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{ant_id or uuid.uuid4()}.jsonl"
    record = {
        "event_type": "action_executed",
        "source": ant_id or str(uuid.uuid4()),
        "sequence": 0,
        "payload": {
            "action": "vix_signal",
            "signal_date": signal_date,
            "vix_value": 30.0,
            "regime": regime,
            "severity": 0.6,
            "biome": "equities",
        },
    }
    (vol_dir / fname).write_text(json.dumps(record) + "\n", encoding="utf-8")


def write_dividend_candidate(logs_root: Path, symbol: str, ant_id: str | None = None) -> None:
    div_dir = logs_root / "equities" / "dividend"
    div_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{ant_id or uuid.uuid4()}.jsonl"
    record = {
        "event_type": "action_executed",
        "source": ant_id or str(uuid.uuid4()),
        "sequence": 0,
        "payload": {
            "action": "dividend_candidate",
            "symbol": symbol,
            "yield_pct": 3.5,
        },
    }
    path = div_dir / fname
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def write_rebalance_order(
    logs_root: Path,
    rebalance_date: str,
    vix_regime: str,
    ant_id: str | None = None,
) -> None:
    reb_dir = logs_root / "equities" / "rebalance"
    reb_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{ant_id or uuid.uuid4()}.jsonl"
    record = {
        "event_type": "action_executed",
        "source": ant_id or str(uuid.uuid4()),
        "sequence": 0,
        "payload": {
            "action": "rebalance_order",
            "rebalance_date": rebalance_date,
            "trigger": "initial",
            "vix_regime": vix_regime,
            "target_weights": _WEIGHTS_BY_REGIME[vix_regime],
            "dividend_symbols": ["AAPL"],
            "low_vol_symbols": _LOW_VOL_ETFS,
        },
    }
    (reb_dir / fname).write_text(json.dumps(record) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. _load_latest_vix_regime
# ---------------------------------------------------------------------------


class TestLoadLatestVixRegime:
    def test_returns_none_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._load_latest_vix_regime() is None

    def test_returns_none_when_vol_dir_missing(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._load_latest_vix_regime() is None

    def test_returns_regime_from_single_file(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_vix_signal(tmp_path, "HEDGE", "2026-04-18")
        assert ant._load_latest_vix_regime() == "HEDGE"

    def test_returns_latest_regime_across_multiple_dates(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        aid1 = str(uuid.uuid4())
        aid2 = str(uuid.uuid4())
        write_vix_signal(tmp_path, "NORMAL", "2026-03-01", aid1)
        write_vix_signal(tmp_path, "REDUCE_EQUITY", "2026-04-18", aid2)
        assert ant._load_latest_vix_regime() == "REDUCE_EQUITY"

    def test_ignores_non_vix_signal_actions(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        vol_dir = tmp_path / "equities" / "volatility"
        vol_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "other_action", "signal_date": "2026-04-18", "regime": "HEDGE"}}
        (vol_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert ant._load_latest_vix_regime() is None

    def test_ignores_malformed_json_lines(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        vol_dir = tmp_path / "equities" / "volatility"
        vol_dir.mkdir(parents=True, exist_ok=True)
        (vol_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        assert ant._load_latest_vix_regime() is None

    def test_returns_none_when_empty_files(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        vol_dir = tmp_path / "equities" / "volatility"
        vol_dir.mkdir(parents=True, exist_ok=True)
        (vol_dir / "empty.jsonl").write_text("", encoding="utf-8")
        assert ant._load_latest_vix_regime() is None

    def test_returns_latest_from_multiple_lines_same_file(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        vol_dir = tmp_path / "equities" / "volatility"
        vol_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"payload": {"action": "vix_signal", "signal_date": "2026-04-17", "regime": "NORMAL"}}),
            json.dumps({"payload": {"action": "vix_signal", "signal_date": "2026-04-19", "regime": "REDUCE_EQUITY"}}),
        ]
        (vol_dir / "multi.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert ant._load_latest_vix_regime() == "REDUCE_EQUITY"


# ---------------------------------------------------------------------------
# 2. _load_dividend_symbols
# ---------------------------------------------------------------------------


class TestLoadDividendSymbols:
    def test_returns_empty_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._load_dividend_symbols() == []

    def test_returns_empty_when_div_dir_missing(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._load_dividend_symbols() == []

    def test_returns_sorted_unique_symbols(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        write_dividend_candidate(tmp_path, "MSFT")
        write_dividend_candidate(tmp_path, "AAPL")
        write_dividend_candidate(tmp_path, "MSFT")  # duplicate
        symbols = ant._load_dividend_symbols()
        assert symbols == ["AAPL", "MSFT"]

    def test_ignores_non_dividend_candidate_actions(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        div_dir = tmp_path / "equities" / "dividend"
        div_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "other_action", "symbol": "AAPL"}}
        (div_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert ant._load_dividend_symbols() == []

    def test_symbols_uppercased(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        div_dir = tmp_path / "equities" / "dividend"
        div_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "dividend_candidate", "symbol": "aapl"}}
        (div_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert ant._load_dividend_symbols() == ["AAPL"]

    def test_ignores_empty_symbol(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        div_dir = tmp_path / "equities" / "dividend"
        div_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "dividend_candidate", "symbol": ""}}
        (div_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        assert ant._load_dividend_symbols() == []

    def test_ignores_malformed_lines(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        div_dir = tmp_path / "equities" / "dividend"
        div_dir.mkdir(parents=True, exist_ok=True)
        (div_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        assert ant._load_dividend_symbols() == []


# ---------------------------------------------------------------------------
# 3. _determine_trigger
# ---------------------------------------------------------------------------


class TestDetermineTrigger:
    def test_initial_when_no_previous_rebalance(self) -> None:
        ant = make_ant()
        assert ant._determine_trigger(date.today(), "NORMAL") == "initial"

    def test_quarterly_after_90_days(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=_QUARTERLY_DAYS)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), "NORMAL") == "quarterly"

    def test_quarterly_after_more_than_90_days(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=120)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), "NORMAL") == "quarterly"

    def test_no_trigger_within_90_days_same_regime(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), "NORMAL") is None

    def test_vix_regime_change_triggers_rebalance(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), "HEDGE") == "vix_regime_change"

    def test_vix_regime_change_to_reduce_equity(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "HEDGE"
        assert ant._determine_trigger(date.today(), "REDUCE_EQUITY") == "vix_regime_change"

    def test_none_regime_treated_as_normal(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), None) is None

    def test_quarterly_takes_precedence_over_regime_change(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=_QUARTERLY_DAYS)
        ant._last_vix_regime = "NORMAL"
        result = ant._determine_trigger(date.today(), "HEDGE")
        assert result == "quarterly"

    def test_exactly_89_days_no_quarterly_trigger(self) -> None:
        ant = make_ant()
        ant._last_rebalance_date = date.today() - timedelta(days=_QUARTERLY_DAYS - 1)
        ant._last_vix_regime = "NORMAL"
        assert ant._determine_trigger(date.today(), "NORMAL") is None


# ---------------------------------------------------------------------------
# 4. _restore_state
# ---------------------------------------------------------------------------


class TestRestoreState:
    def test_no_restore_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._last_rebalance_date is None
        assert ant._last_vix_regime == ""

    def test_no_restore_when_rebalance_dir_missing(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_rebalance_date is None

    def test_restores_date_and_regime(self, tmp_path: Path) -> None:
        write_rebalance_order(tmp_path, "2026-03-15", "HEDGE")
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_rebalance_date == date(2026, 3, 15)
        assert ant._last_vix_regime == "HEDGE"

    def test_restores_latest_date_when_multiple_orders(self, tmp_path: Path) -> None:
        aid1 = str(uuid.uuid4())
        aid2 = str(uuid.uuid4())
        write_rebalance_order(tmp_path, "2026-01-01", "NORMAL", aid1)
        write_rebalance_order(tmp_path, "2026-04-01", "REDUCE_EQUITY", aid2)
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_rebalance_date == date(2026, 4, 1)
        assert ant._last_vix_regime == "REDUCE_EQUITY"

    def test_ignores_non_rebalance_order_actions(self, tmp_path: Path) -> None:
        reb_dir = tmp_path / "equities" / "rebalance"
        reb_dir.mkdir(parents=True, exist_ok=True)
        record = {"payload": {"action": "other_action", "rebalance_date": "2026-04-18", "vix_regime": "HEDGE"}}
        (reb_dir / "x.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_rebalance_date is None

    def test_ignores_malformed_lines(self, tmp_path: Path) -> None:
        reb_dir = tmp_path / "equities" / "rebalance"
        reb_dir.mkdir(parents=True, exist_ok=True)
        (reb_dir / "bad.jsonl").write_text("not-json\n", encoding="utf-8")
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_rebalance_date is None


# ---------------------------------------------------------------------------
# 5. _tick
# ---------------------------------------------------------------------------


class TestTick:
    def test_initial_tick_returns_order(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None

    def test_order_has_required_fields(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        for field in ("action", "rebalance_date", "trigger", "vix_regime",
                      "target_weights", "dividend_symbols", "low_vol_symbols"):
            assert field in order, f"missing: {field}"

    def test_action_is_rebalance_order(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["action"] == "rebalance_order"

    def test_normal_regime_uses_base_weights(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["target_weights"] == _WEIGHTS_BY_REGIME["NORMAL"]

    def test_hedge_regime_uses_hedge_weights(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "HEDGE", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["target_weights"] == _WEIGHTS_BY_REGIME["HEDGE"]

    def test_reduce_equity_regime_uses_reduce_weights(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "REDUCE_EQUITY", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["target_weights"] == _WEIGHTS_BY_REGIME["REDUCE_EQUITY"]

    def test_no_vix_data_uses_normal_weights(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["vix_regime"] == "NORMAL"
        assert order["target_weights"] == _WEIGHTS_BY_REGIME["NORMAL"]

    def test_trigger_is_initial_on_first_tick(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["trigger"] == "initial"

    def test_no_trigger_within_90_days_same_regime(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        assert ant._tick() is None

    def test_quarterly_trigger_after_90_days(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._last_rebalance_date = date.today() - timedelta(days=_QUARTERLY_DAYS)
        ant._last_vix_regime = "NORMAL"
        order = ant._tick()
        assert order is not None
        assert order["trigger"] == "quarterly"

    def test_vix_regime_change_triggers_rebalance(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "HEDGE", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        order = ant._tick()
        assert order is not None
        assert order["trigger"] == "vix_regime_change"

    def test_dividend_symbols_included(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "NORMAL", "2026-04-19")
        write_dividend_candidate(tmp_path, "JNJ")
        write_dividend_candidate(tmp_path, "KO")
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert "JNJ" in order["dividend_symbols"]
        assert "KO" in order["dividend_symbols"]

    def test_low_vol_symbols_are_fixed_etfs(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        order = ant._tick()
        assert order is not None
        assert order["low_vol_symbols"] == _LOW_VOL_ETFS

    def test_last_rebalance_date_updated_after_tick(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._last_rebalance_date == date.today()

    def test_last_vix_regime_updated_after_tick(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "HEDGE", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._last_vix_regime == "HEDGE"

    def test_last_action_updated_after_tick(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._last_action != "init"

    def test_no_trigger_updates_last_action(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        ant._tick()
        assert "no_trigger" in ant._last_action


# ---------------------------------------------------------------------------
# 6. Log output
# ---------------------------------------------------------------------------


class TestLogOutput:
    def test_writes_log_file(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_rebalance_order_action(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["payload"]["action"] == "rebalance_order"

    def test_log_has_vix_regime(self, tmp_path: Path) -> None:
        write_vix_signal(tmp_path, "HEDGE", "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        payload = json.loads(log_path.read_text().splitlines()[0])["payload"]
        assert payload["vix_regime"] == "HEDGE"

    def test_log_has_target_weights(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        payload = json.loads(log_path.read_text().splitlines()[0])["payload"]
        assert "target_weights" in payload

    def test_no_log_when_logs_root_none(self) -> None:
        ant = make_ant(logs_root=None)
        ant._tick()  # must not crash

    def test_no_log_when_no_trigger(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._last_rebalance_date = date.today() - timedelta(days=10)
        ant._last_vix_regime = "NORMAL"
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        assert not log_path.exists()

    def test_sequence_increments_across_ticks(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        # Force another rebalance by simulating quarterly
        ant._last_rebalance_date = date.today() - timedelta(days=_QUARTERLY_DAYS)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))

    def test_rebalance_date_is_today(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "equities" / "rebalance" / f"{ant.ant_id}.jsonl"
        payload = json.loads(log_path.read_text().splitlines()[0])["payload"]
        assert payload["rebalance_date"] == date.today().isoformat()


# ---------------------------------------------------------------------------
# 7. Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_sent(self) -> None:
        ant = make_ant()
        ant._send_heartbeat()
        ant.scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_fail_does_not_raise(self) -> None:
        ant = make_ant()
        ant.scheduler.record_heartbeat.side_effect = RuntimeError("boom")
        ant._send_heartbeat()


# ---------------------------------------------------------------------------
# 8. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=None)
        with patch("ant_colony.ants.equities.rebalance_ant.time.sleep"):
            with patch("ant_colony.ants.equities.rebalance_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status  = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.rebalance_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.rebalance_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        assert make_ant()._status == AntStatus.IDLE
