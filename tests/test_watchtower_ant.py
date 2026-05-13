"""
tests/test_watchtower_ant.py

Tests voor WatchtowerAnt: signaalfiltering, offline-detectie, JSONL-schrijven.
Colony moet altijd doordraaien als Watchtower offline is.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ant_colony.ants.watchtower_ant import WatchtowerAnt
from ant_colony.clients.watchtower_client import WatchtowerClient
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_mission() -> Mission:
    return Mission(
        mission_id="wt-test-001",
        ant_type="watchtower_ant",
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
        heartbeat_interval=300,
        success_conditions=SuccessConditions(description="Test watchtower ant."),
    )


def _make_client(signals: list[dict] | None = None, healthy: bool = True) -> WatchtowerClient:
    """Maak een mock WatchtowerClient."""
    client = MagicMock(spec=WatchtowerClient)
    client.base_url = "http://127.0.0.1:8011"
    client.last_get_succeeded = True if healthy else False
    client.get_signals.return_value = signals or []
    client.is_healthy.return_value = healthy
    return client


def _make_ant(
    tmp_path: Path,
    client: WatchtowerClient | None = None,
    queen=None,
) -> WatchtowerAnt:
    if client is None:
        client = _make_client()
    return WatchtowerAnt(
        ant_id="wt-abc123",
        mission=_make_mission(),
        scheduler=MagicMock(),
        logs_root=tmp_path,
        client=client,
        queen=queen,
    )


# ---------------------------------------------------------------------------
# Signaalfiltering
# ---------------------------------------------------------------------------

class TestSignalFilter:
    def _signals(self) -> list[dict]:
        return [
            {
                "signal_id": "s1",
                "asset": "ASML",
                "direction": "LONG",
                "entry_score": 0.85,
                "confidence": 0.75,
                "risk_flags": [],
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            },
            {
                "signal_id": "s2",
                "asset": "NVDA",
                "direction": "LONG",
                "entry_score": 0.4,       # onder drempel
                "confidence": 0.7,
                "risk_flags": [],
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            },
            {
                "signal_id": "s3",
                "asset": "META",
                "direction": "SHORT",
                "entry_score": 0.9,
                "confidence": 0.3,        # confidence te laag
                "risk_flags": [],
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            },
            {
                "signal_id": "s4",
                "asset": "TSLA",
                "direction": "LONG",
                "entry_score": 0.7,
                "confidence": 0.6,
                "risk_flags": ["HIGH_RISK"],  # gefilterd
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            },
            {
                "signal_id": "s5",
                "asset": "AAPL",
                "direction": "LONG",
                "entry_score": 0.65,
                "confidence": 0.55,
                "risk_flags": [],
                "linked_assets": [],
                "timestamp": "2026-04-27T10:00:00",
            },
        ]

    def test_only_passing_signals_written(self, tmp_path):
        client = _make_client(signals=self._signals())
        # Simuleer successful get_signals (last_get_succeeded=True)
        client.last_get_succeeded = True

        def mock_get_signals(limit=50):
            client.last_get_succeeded = True
            return self._signals()

        client.get_signals.side_effect = mock_get_signals

        ant = _make_ant(tmp_path, client)
        ant._tick()

        log_path = tmp_path / "watchtower" / "signals.jsonl"
        assert log_path.exists()
        rec = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert rec["received"] == 5
        assert rec["passed_filter"] == 2   # s1 en s5 passeren filter
        assert rec["unique_count"] == 5
        assert rec["duplicate_count"] == 0
        ids = {s["signal_id"] for s in rec["signals"]}
        assert ids == {"s1", "s5"}

    def test_duplicate_signals_are_deduped_before_filters(self, tmp_path):
        signal = self._signals()[0]
        duplicate = dict(signal, signal_id="different-id-same-content")
        client = _make_client(signals=[signal, duplicate])

        ant = _make_ant(tmp_path, client)
        ant._tick()

        rec = json.loads((tmp_path / "watchtower" / "signals.jsonl").read_text())
        assert rec["received"] == 2
        assert rec["unique_count"] == 1
        assert rec["duplicate_count"] == 1
        assert rec["passed_filter"] == 1

    def test_entry_score_boundary(self, tmp_path):
        """entry_score exact op drempel (0.6) moet door het filter."""
        sig = {
            "signal_id": "exact",
            "asset": "X",
            "direction": "LONG",
            "entry_score": 0.6,
            "confidence": 0.5,
            "risk_flags": [],
            "linked_assets": [],
            "timestamp": "2026-04-27T10:00:00",
        }
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [sig]

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        rec = json.loads((tmp_path / "watchtower" / "signals.jsonl").read_text())
        assert rec["passed_filter"] == 1

    def test_high_risk_flag_blocks_signal(self, tmp_path):
        sig = {
            "signal_id": "risky",
            "asset": "X",
            "direction": "LONG",
            "entry_score": 0.99,
            "confidence": 0.99,
            "risk_flags": ["HIGH_RISK", "ILLIQUID"],
            "linked_assets": [],
            "timestamp": "2026-04-27T10:00:00",
        }
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [sig]

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        rec = json.loads((tmp_path / "watchtower" / "signals.jsonl").read_text())
        assert rec["passed_filter"] == 0
        assert rec["rejections"][0]["rejection_reason"] == "high_risk"
        assert rec["rejections"][0]["detail_reason"] == "risk_flag_high_risk"

    def test_zero_passed_filter_still_writes_snapshot(self, tmp_path):
        """Als geen signalen door het filter komen maar Watchtower online is, schrijft toch snapshot (voor stats)."""
        sig_low = {
            "signal_id": "low",
            "asset": "X",
            "direction": "LONG",
            "entry_score": 0.1,
            "confidence": 0.1,
            "risk_flags": [],
            "linked_assets": [],
            "timestamp": "2026-04-27T10:00:00",
        }
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [sig_low]

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        log_path = tmp_path / "watchtower" / "signals.jsonl"
        assert log_path.exists()
        rec = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert rec["received"] == 1
        assert rec["passed_filter"] == 0
        assert rec["rejections"][0]["rejection_reason"] == "score_too_low"
        assert rec["rejections"][0]["detail_reason"] == (
            "entry_score_below_threshold; confidence_below_threshold"
        )
        assert rec["signals"] == []


# ---------------------------------------------------------------------------
# Offline-detectie
# ---------------------------------------------------------------------------

class TestOffline:
    def test_offline_does_not_raise(self, tmp_path):
        """Als Watchtower offline is, moet _tick() gewoon doordraaien."""
        client = _make_client(healthy=False)

        def mock_get(limit=50):
            client.last_get_succeeded = False
            return []

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()   # moet GEEN exception gooien

    def test_offline_nothing_written_to_disk(self, tmp_path):
        client = _make_client(healthy=False)

        def mock_get(limit=50):
            client.last_get_succeeded = False
            return []

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        assert not (tmp_path / "watchtower" / "signals.jsonl").exists()

    def test_online_but_empty_signals_logs_zero(self, tmp_path):
        """Watchtower online maar geen signalen — schrijft snapshot met 0 ontvangen."""
        client = _make_client(healthy=True, signals=[])

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return []

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        log_path = tmp_path / "watchtower" / "signals.jsonl"
        assert log_path.exists()
        rec = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert rec["received"] == 0
        assert rec["passed_filter"] == 0

    def test_tick_sends_scheduler_heartbeat(self, tmp_path):
        client = _make_client(healthy=True, signals=[])

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return []

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)

        ant._tick()

        ant.scheduler.record_heartbeat.assert_called_once()
        heartbeat = ant.scheduler.record_heartbeat.call_args.args[0]
        assert heartbeat.ant_id == ant.ant_id
        assert heartbeat.last_action == "tick:received=0 passed=0"


# ---------------------------------------------------------------------------
# JSONL-schrijven
# ---------------------------------------------------------------------------

class TestJSONLWriting:
    def _good_signal(self, idx: int = 1) -> dict:
        return {
            "signal_id": f"sig-{idx:03d}",
            "asset": "ASML",
            "direction": "LONG",
            "entry_score": 0.8,
            "confidence": 0.7,
            "risk_flags": [],
            "linked_assets": [],
            "timestamp": "2026-04-27T10:00:00",
        }

    def test_snapshot_format(self, tmp_path):
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [self._good_signal()]

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()

        log_path = tmp_path / "watchtower" / "signals.jsonl"
        rec = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert "timestamp" in rec
        assert "received" in rec
        assert "poll_interval" in rec
        assert "passed_filter" in rec
        assert "signals" in rec
        assert isinstance(rec["signals"], list)

    def test_multiple_ticks_append(self, tmp_path):
        """Elke tick voegt een nieuwe regel toe aan het JSONL-bestand."""
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [self._good_signal()]

        client.get_signals.side_effect = mock_get
        ant = _make_ant(tmp_path, client)
        ant._tick()
        ant._tick()

        log_path = tmp_path / "watchtower" / "signals.jsonl"
        lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2

    def test_logs_root_none_does_not_crash(self):
        """Als logs_root=None: geen file-operaties, geen crash."""
        client = _make_client()

        def mock_get(limit=50):
            client.last_get_succeeded = True
            return [{"signal_id": "x", "asset": "A", "direction": "LONG",
                     "entry_score": 0.9, "confidence": 0.9, "risk_flags": [],
                     "linked_assets": [], "timestamp": "2026-04-27T10:00:00"}]

        client.get_signals.side_effect = mock_get
        ant = WatchtowerAnt(
            ant_id="wt-test",
            mission=_make_mission(),
            scheduler=MagicMock(),
            logs_root=None,
            client=client,
        )
        ant._tick()   # mag niet crashen


# ---------------------------------------------------------------------------
# Watchtower → Queen candidate consumer
# ---------------------------------------------------------------------------

class TestWatchtowerCandidateConsumer:
    def _signal(
        self,
        *,
        signal_id: str = "wt-001",
        asset: str = "AAPL",
        direction: str = "long",
        entry_score: float = 0.72,
        confidence: float = 0.66,
    ) -> dict:
        ts = datetime.now(tz=timezone.utc).isoformat()
        return {
            "signal_id": signal_id,
            "asset": asset,
            "direction": direction,
            "entry_score": entry_score,
            "confidence": confidence,
            "risk_flags": [],
            "timestamp": ts,
            "created_at": ts,
            "reason": "fresh positive Watchtower signal",
        }

    def _read_snapshot(self, tmp_path: Path) -> dict:
        return json.loads((tmp_path / "watchtower" / "signals.jsonl").read_text())

    def _read_candidates(self, tmp_path: Path) -> list[dict]:
        path = tmp_path / "watchtower" / "candidates.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_high_quality_positive_edge_signal_is_queued(self, tmp_path):
        client = _make_client(signals=[self._signal(asset="AAPL")])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        candidates = self._read_candidates(tmp_path)
        payload = candidates[0]["payload"]
        assert payload["action"] == "watchtower_candidate"
        assert payload["asset"] == "AAPL"
        assert payload["direction"] == "long"
        assert payload["signal_id"] == "wt-001"
        rec = self._read_snapshot(tmp_path)
        assert rec["candidates_accepted"] == 1
        assert rec["candidate_rejections"] == []

    def test_accepted_signal_is_registered_with_queen(self, tmp_path):
        queen = MagicMock()
        client = _make_client(signals=[self._signal(asset="AAPL")])
        ant = _make_ant(tmp_path, client, queen=queen)

        ant._tick()

        queen.register_watchtower_signal.assert_called_once()
        payload = queen.register_watchtower_signal.call_args.args[0]
        assert payload["asset"] == "AAPL"
        assert payload["queen_accepted"] is True

    def test_candidate_rejection_is_registered_with_queen(self, tmp_path):
        queen = MagicMock()
        client = _make_client(signals=[
            self._signal(asset="BTC-EUR", entry_score=0.95, confidence=0.95),
        ])
        ant = _make_ant(tmp_path, client, queen=queen)

        ant._tick()

        queen.register_watchtower_signal.assert_called_once()
        payload = queen.register_watchtower_signal.call_args.args[0]
        assert payload["asset"] == "BTC-EUR"
        assert payload["queen_accepted"] is False
        assert payload["queen_rejection_reason"] == "biome_mismatch"

    def test_below_poll_threshold_is_logged_to_filtered_file(self, tmp_path):
        client = _make_client(signals=[
            self._signal(asset="AAPL", entry_score=0.59, confidence=0.70),
        ])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        rec = self._read_snapshot(tmp_path)
        assert rec["candidates_accepted"] == 0
        assert rec["rejections"][0]["rejection_reason"] == "score_too_low"
        filtered = json.loads((tmp_path / "watchtower" / "filtered.jsonl").read_text())
        assert filtered["payload"]["rejection_reason"] == "score_too_low"
        assert not (tmp_path / "watchtower" / "candidates.jsonl").exists()

    def test_crypto_signal_is_always_rejected(self, tmp_path):
        client = _make_client(signals=[
            self._signal(asset="BTC-EUR", entry_score=0.95, confidence=0.95),
        ])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        rec = self._read_snapshot(tmp_path)
        assert rec["candidates_accepted"] == 0
        assert rec["candidate_rejections"][0]["rejection_reason"] == "biome_mismatch"

    def test_commodity_signal_is_queued_for_existing_intake(self, tmp_path):
        client = _make_client(signals=[
            self._signal(asset="NATGAS", entry_score=0.72, confidence=0.66),
        ])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        candidates = self._read_candidates(tmp_path)
        payload = candidates[0]["payload"]
        assert payload["asset"] == "NATGAS"
        assert payload["biome"] == "commodity"

    def test_daily_limit_blocks_after_ten_entries(self, tmp_path):
        client = _make_client(signals=[
            self._signal(signal_id="s-jnj",  asset="JNJ"),
            self._signal(signal_id="s-gld",  asset="GLD"),
            self._signal(signal_id="s-xlk",  asset="XLK"),
            self._signal(signal_id="s-aapl", asset="AAPL"),
            self._signal(signal_id="s-msft", asset="MSFT"),
            self._signal(signal_id="s-amzn", asset="AMZN"),
            self._signal(signal_id="s-tsla", asset="TSLA"),
            self._signal(signal_id="s-nvda", asset="NVDA"),
            self._signal(signal_id="s-meta", asset="META"),
            self._signal(signal_id="s-googl", asset="GOOGL"),
            self._signal(signal_id="s-nflx", asset="NFLX"),
        ])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        candidates = self._read_candidates(tmp_path)
        assert len(candidates) == 10
        rec = self._read_snapshot(tmp_path)
        reasons = [r["detail_reason"] for r in rec["candidate_rejections"]]
        assert "daily_limit_total" in reasons

    def test_duplicate_asset_within_24h_is_skipped(self, tmp_path):
        client = _make_client(signals=[
            self._signal(signal_id="s-aapl-1", asset="AAPL"),
            self._signal(signal_id="s-aapl-2", asset="AAPL", entry_score=0.86),
        ])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        candidates = self._read_candidates(tmp_path)
        assert len(candidates) == 1
        rec = self._read_snapshot(tmp_path)
        assert rec["candidate_rejections"][0]["detail_reason"] == "daily_limit_asset_24h"

    def test_deduped_signals_do_not_pollute_daily_limit(self, tmp_path):
        sig = self._signal(signal_id="s-aapl-1", asset="AAPL")
        dup = dict(sig, signal_id="s-aapl-dup")
        client = _make_client(signals=[sig, dup])
        ant = _make_ant(tmp_path, client)

        ant._tick()

        candidates = self._read_candidates(tmp_path)
        assert len(candidates) == 1
        rec = self._read_snapshot(tmp_path)
        assert rec["duplicate_count"] == 1
        assert rec["candidate_rejections"] == []


# ---------------------------------------------------------------------------
# Feedback hook integratie (PaperPosition → watchtower_signal_id)
# ---------------------------------------------------------------------------

class TestWatchtowerSignalId:
    def test_paper_position_has_watchtower_signal_id_field(self):
        """PaperPosition moet watchtower_signal_id als optioneel veld hebben."""
        from datetime import datetime, timezone
        from ant_colony.exit_chain.position import PaperPosition, PositionSide

        pos = PaperPosition(
            position_id="test-pos-001",
            symbol="BTC-EUR",
            biome="crypto",
            mission_id="mission-001",
            ant_id="ant-001",
            side=PositionSide.LONG,
            entry_price=50000.0,
            quantity=0.01,
            stop_loss_price=49000.0,
            take_profit_price=51500.0,
            ttl=86400,
            current_price=50000.0,
            peak_price=50000.0,
        )
        assert pos.watchtower_signal_id is None

    def test_paper_position_accepts_signal_id(self):
        from ant_colony.exit_chain.position import PaperPosition, PositionSide

        pos = PaperPosition(
            position_id="test-pos-002",
            symbol="BTC-EUR",
            biome="crypto",
            mission_id="mission-001",
            ant_id="ant-001",
            side=PositionSide.LONG,
            entry_price=50000.0,
            quantity=0.01,
            stop_loss_price=49000.0,
            take_profit_price=51500.0,
            ttl=86400,
            current_price=50000.0,
            peak_price=50000.0,
            watchtower_signal_id="wt-signal-abc",
        )
        assert pos.watchtower_signal_id == "wt-signal-abc"
