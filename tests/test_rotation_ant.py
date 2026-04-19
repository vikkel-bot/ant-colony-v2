"""
tests/test_rotation_ant.py

Tests voor RotationAnt.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.equities.rotation_ant import (
    RotationAnt,
    _MIN_SHIFT,
    _TOP_N,
    _BOTTOM_N,
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
        ant_type="rotation_ant",
        allowed_node="pc2",
        allowed_actions=["read_data", "report"],
        market_scope=MarketScope(biome="equities", symbols=["XLK"]),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01, max_position_size=1.0,
            daily_loss_limit=1.0, stop_loss_required=False,
        ),
        ttl=3600,
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="rotation test"),
    )


def make_ant(logs_root=None, min_shift=_MIN_SHIFT) -> RotationAnt:
    return RotationAnt(
        ant_id=str(uuid.uuid4()),
        mission=make_mission(),
        scheduler=MagicMock(),
        biome_registry=MagicMock(spec=BiomeRegistry),
        logs_root=logs_root,
        min_shift=min_shift,
    )


def _ranking(symbols: list[str]) -> list[dict]:
    """Build a ranking list with the given symbols in order (rank 1 = first)."""
    return [
        {"rank": i + 1, "symbol": sym, "sector": sym, "score": 1.0 - i * 0.1,
         "r1m": 0.05, "r3m": 0.08, "r6m": 0.04}
        for i, sym in enumerate(symbols)
    ]


def write_momentum_ranking(rank_dir: Path, symbols: list[str], ranking_date: str) -> None:
    rank_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "payload": {
            "action":       "momentum_ranking",
            "ranking_date": ranking_date,
            "ranking":      _ranking(symbols),
        },
    }
    (rank_dir / "momentum.jsonl").open("a").write(json.dumps(record) + "\n")


def write_rotation_signal(
    rot_dir: Path,
    buys: list[str],
    sells: list[str],
    sig_date: str,
) -> None:
    rot_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "action_executed",
        "payload": {
            "action":   "rotation_signal",
            "signal_id": f"rotation-{sig_date}",
            "date":     sig_date,
            "buys":     buys,
            "sells":    sells,
            "shift_count": 3,
            "trigger":  "ranking_shift",
        },
    }
    (rot_dir / "rotation.jsonl").open("a").write(json.dumps(record) + "\n")


_ALL_11 = ["XLK", "XLF", "XLV", "XLI", "XLB", "XLP", "XLY", "XLU", "XLRE", "XLC", "XLE"]


# ---------------------------------------------------------------------------
# 1. Count shift
# ---------------------------------------------------------------------------


class TestCountShift:
    def test_no_change_zero_shift(self) -> None:
        shift = RotationAnt._count_shift(
            ["XLK", "XLF", "XLV"], ["XLK", "XLF", "XLV"],
            ["XLU", "XLRE", "XLE"], ["XLU", "XLRE", "XLE"],
        )
        assert shift == 0

    def test_one_new_top_one_shift(self) -> None:
        shift = RotationAnt._count_shift(
            ["XLK", "XLF", "XLV"], ["XLK", "XLF", "XLI"],
            ["XLU", "XLRE", "XLE"], ["XLU", "XLRE", "XLE"],
        )
        assert shift == 1

    def test_full_replacement_max_shift(self) -> None:
        shift = RotationAnt._count_shift(
            ["XLK", "XLF", "XLV"], ["XLI", "XLB", "XLP"],
            ["XLU", "XLRE", "XLE"], ["XLY", "XLC", "XLK"],
        )
        assert shift == 6  # 3 new top + 3 new bottom

    def test_empty_old_counts_all_as_new(self) -> None:
        shift = RotationAnt._count_shift(
            [], ["XLK", "XLF", "XLV"],
            [], ["XLU", "XLRE", "XLE"],
        )
        assert shift == 6

    def test_order_does_not_matter(self) -> None:
        shift_a = RotationAnt._count_shift(
            ["XLK", "XLF", "XLV"], ["XLV", "XLF", "XLK"],
            ["XLU", "XLRE", "XLE"], ["XLE", "XLRE", "XLU"],
        )
        assert shift_a == 0


# ---------------------------------------------------------------------------
# 2. Load latest ranking
# ---------------------------------------------------------------------------


class TestLoadLatestRanking:
    def test_no_logs_root_returns_empty(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._load_latest_ranking() == []

    def test_no_rank_dir_returns_empty(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._load_latest_ranking() == []

    def test_reads_ranking_from_file(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ranking = ant._load_latest_ranking()
        assert len(ranking) == len(_ALL_11)
        assert ranking[0]["symbol"] == _ALL_11[0]

    def test_returns_most_recent_date(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", ["XLK", "XLF", "XLV"], "2026-04-18")
        write_momentum_ranking(tmp_path / "momentum_rank", ["XLE", "XLI", "XLB"], "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ranking = ant._load_latest_ranking()
        assert ranking[0]["symbol"] == "XLE"

    def test_corrupt_lines_skipped(self, tmp_path: Path) -> None:
        rank_dir = tmp_path / "momentum_rank"
        rank_dir.mkdir()
        (rank_dir / "bad.jsonl").write_text("not valid json\n")
        ant = make_ant(logs_root=tmp_path)
        assert ant._load_latest_ranking() == []

    def test_ranking_sorted_by_rank(self, tmp_path: Path) -> None:
        symbols = list(reversed(_ALL_11))  # write reversed order
        write_momentum_ranking(tmp_path / "momentum_rank", symbols, "2026-04-19")
        ant     = make_ant(logs_root=tmp_path)
        ranking = ant._load_latest_ranking()
        ranks   = [r["rank"] for r in ranking]
        assert ranks == sorted(ranks)


# ---------------------------------------------------------------------------
# 3. Restore last rotation
# ---------------------------------------------------------------------------


class TestRestoreLastRotation:
    def test_no_rotation_dir_no_crash(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_top3 == []
        assert ant._last_bot3 == []

    def test_restores_buys_and_sells(self, tmp_path: Path) -> None:
        write_rotation_signal(
            tmp_path / "rotation",
            buys=["XLK", "XLF", "XLV"],
            sells=["XLU", "XLRE", "XLE"],
            sig_date="2026-04-18",
        )
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_top3 == ["XLK", "XLF", "XLV"]
        assert ant._last_bot3 == ["XLU", "XLRE", "XLE"]

    def test_restores_most_recent_signal(self, tmp_path: Path) -> None:
        write_rotation_signal(tmp_path / "rotation", ["A", "B", "C"], ["X", "Y", "Z"], "2026-04-17")
        write_rotation_signal(tmp_path / "rotation", ["D", "E", "F"], ["P", "Q", "R"], "2026-04-18")
        ant = make_ant(logs_root=tmp_path)
        assert ant._last_top3 == ["D", "E", "F"]

    def test_emitted_dates_populated(self, tmp_path: Path) -> None:
        write_rotation_signal(tmp_path / "rotation", ["XLK", "XLF", "XLV"],
                              ["XLU", "XLRE", "XLE"], "2026-04-18")
        ant = make_ant(logs_root=tmp_path)
        assert "2026-04-18" in ant._emitted_signal_dates


# ---------------------------------------------------------------------------
# 4. Tick
# ---------------------------------------------------------------------------


class TestTick:
    def test_initial_tick_emits_signal(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        signal = ant._tick()
        assert signal is not None
        assert signal["trigger"] == "initial"

    def test_initial_signal_has_top3_buys(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant    = make_ant(logs_root=tmp_path)
        signal = ant._tick()
        assert signal is not None
        assert signal["buys"] == _ALL_11[:_TOP_N]

    def test_initial_signal_has_bottom3_sells(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant    = make_ant(logs_root=tmp_path)
        signal = ant._tick()
        assert signal is not None
        assert signal["sells"] == _ALL_11[-_BOTTOM_N:]

    def test_no_ranking_returns_none(self, tmp_path: Path) -> None:
        ant = make_ant(logs_root=tmp_path)
        assert ant._tick() is None

    def test_same_day_dedup(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._tick() is None

    def test_small_shift_no_signal(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path, min_shift=_MIN_SHIFT)
        # Set previous rotation to same top-3/bottom-3 as current ranking
        ant._last_top3 = _ALL_11[:_TOP_N]
        ant._last_bot3 = _ALL_11[-_BOTTOM_N:]
        signal = ant._tick()
        assert signal is None

    def test_large_shift_emits_signal(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path, min_shift=2)
        # Previous rotation used completely different symbols
        ant._last_top3 = ["XLE", "XLI", "XLB"]
        ant._last_bot3 = ["XLK", "XLF", "XLV"]
        signal = ant._tick()
        assert signal is not None
        assert signal["trigger"] == "ranking_shift"

    def test_signal_id_format(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant    = make_ant(logs_root=tmp_path)
        signal = ant._tick()
        assert signal is not None
        today  = date.today().isoformat()
        assert signal["signal_id"] == f"rotation-{today}"

    def test_state_updated_after_emission(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        assert ant._last_top3 == _ALL_11[:_TOP_N]
        assert ant._last_bot3 == _ALL_11[-_BOTTOM_N:]

    def test_shift_count_in_signal(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path, min_shift=2)
        ant._last_top3 = ["XLE", "XLI", "XLB"]
        ant._last_bot3 = ["XLK", "XLF", "XLV"]
        signal = ant._tick()
        assert signal is not None
        assert signal["shift_count"] > 0

    def test_writes_log(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "rotation" / f"{ant.ant_id}.jsonl"
        assert log_path.exists()

    def test_log_has_rotation_signal_action(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path)
        ant._tick()
        log_path = tmp_path / "rotation" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        assert records[0]["payload"]["action"] == "rotation_signal"

    def test_no_logs_root_no_signal(self) -> None:
        ant = make_ant(logs_root=None)
        assert ant._tick() is None


# ---------------------------------------------------------------------------
# 5. Sequence in log
# ---------------------------------------------------------------------------


class TestLogSequence:
    def test_sequence_increments_across_ticks(self, tmp_path: Path) -> None:
        write_momentum_ranking(tmp_path / "momentum_rank", _ALL_11, "2026-04-19")
        ant = make_ant(logs_root=tmp_path, min_shift=0)
        # First tick
        ant._tick()
        # Force second tick by clearing dedup and updating ranking date
        ant._emitted_signal_dates.clear()
        ant._last_top3 = []
        write_momentum_ranking(tmp_path / "momentum_rank", list(reversed(_ALL_11)), "2026-04-20")
        ant._tick()
        log_path = tmp_path / "rotation" / f"{ant.ant_id}.jsonl"
        records  = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
        seqs     = [r["sequence"] for r in records]
        assert seqs == list(range(len(seqs)))


# ---------------------------------------------------------------------------
# 6. Heartbeat
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
# 7. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_ttl_expiry_returns_completed(self) -> None:
        ant = make_ant()
        ant._tick = MagicMock(return_value=None)
        with patch("ant_colony.ants.equities.rotation_ant.time.sleep"):
            with patch("ant_colony.ants.equities.rotation_ant.datetime") as mock_dt:
                start   = datetime(2026, 1, 1, tzinfo=timezone.utc)
                expired = start + timedelta(seconds=ant.mission.ttl + 1)
                mock_dt.now.side_effect = [start, start, expired]
                status  = ant.run()
        assert status == AntStatus.COMPLETED

    def test_keyboard_interrupt_returns_aborted(self) -> None:
        ant = make_ant()
        with patch("ant_colony.ants.equities.rotation_ant.time.sleep",
                   side_effect=KeyboardInterrupt):
            with patch("ant_colony.ants.equities.rotation_ant.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                status = ant.run()
        assert status == AntStatus.ABORTED

    def test_initial_status_is_idle(self) -> None:
        assert make_ant()._status == AntStatus.IDLE
