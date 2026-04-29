from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from ant_colony.ants.watchtower_backtest_ant import WatchtowerBacktestAnt
from ant_colony.lab.backtester import OHLCVBar
from ant_colony.lab.watchtower_backtester import (
    WatchtowerBacktestAssumptions,
    WatchtowerSignal,
    WatchtowerSignalBacktester,
    append_watchtower_export,
    load_watchtower_signals,
)
from ant_colony.schemas.mission import MarketScope, Mission, RiskLimits, SuccessConditions


def _ts(hour: int) -> datetime:
    return datetime(2026, 4, 27, hour, 0, tzinfo=timezone.utc)


def _bar(hour: int, open_: float, high: float, low: float, close: float) -> OHLCVBar:
    return OHLCVBar(
        timestamp=_ts(hour),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def _signal(
    signal_id: str = "sig-1",
    symbol: str = "BTC-EUR",
    hour: int = 10,
    linked_assets: tuple[str, ...] = (),
) -> WatchtowerSignal:
    return WatchtowerSignal(
        signal_id=signal_id,
        symbol=symbol,
        direction="long",
        timestamp=_ts(hour),
        entry_score=0.8,
        confidence=0.7,
        asset_class="crypto" if "-" in symbol else "equities",
        source_field="crypto" if "-" in symbol else "equities",
        linked_assets=linked_assets,
        raw={},
    )


def _assumptions(**overrides) -> WatchtowerBacktestAssumptions:
    base = {
        "fee_pct_per_side": 0.0,
        "slippage_pct": 0.0,
        "default_take_profit_pct": 0.03,
        "default_stop_loss_pct": 0.02,
        "max_bars_held": 3,
        "allow_short": True,
    }
    base.update(overrides)
    return WatchtowerBacktestAssumptions(**base)


class TestWatchtowerSignalBacktester:
    def test_enters_on_next_bar_after_signal_no_lookahead(self):
        bars = [
            _bar(10, 50.0, 1000.0, 1.0, 500.0),   # should be ignored for entry
            _bar(11, 100.0, 102.0, 99.0, 101.0),
            _bar(12, 101.0, 104.0, 100.0, 103.0),
        ]

        report = WatchtowerSignalBacktester().run(
            [_signal(hour=10)],
            {"BTC-EUR": bars},
            _assumptions(),
        )

        assert report.total_trades == 1
        trade = report.trades[0]
        assert trade.entry_timestamp == _ts(11)
        assert trade.entry_price == 100.0
        assert trade.exit_reason == "take_profit"

    def test_fees_and_slippage_reduce_net_return(self):
        bars = [
            _bar(11, 100.0, 106.0, 99.0, 104.0),
            _bar(12, 104.0, 105.0, 103.0, 104.0),
        ]

        report = WatchtowerSignalBacktester().run(
            [_signal(hour=10)],
            {"BTC-EUR": bars},
            WatchtowerBacktestAssumptions(
                fee_pct_per_side=0.0025,
                slippage_pct=0.001,
                default_take_profit_pct=0.03,
                default_stop_loss_pct=0.02,
                max_bars_held=2,
            ),
        )

        trade = report.trades[0]
        assert trade.fee_cost > 0
        assert trade.net_return_pct < 0.03
        assert trade.net_pnl < trade.gross_pnl

    def test_same_bar_tp_and_sl_uses_pessimistic_stop_loss(self):
        bars = [
            _bar(11, 100.0, 104.0, 97.0, 101.0),
        ]

        report = WatchtowerSignalBacktester().run(
            [_signal(hour=10)],
            {"BTC-EUR": bars},
            _assumptions(default_take_profit_pct=0.03, default_stop_loss_pct=0.02),
        )

        assert report.trades[0].exit_reason == "stop_loss"
        assert report.trades[0].net_return_pct < 0

    def test_short_signals_skipped_unless_enabled(self):
        short_signal = WatchtowerSignal(
            signal_id="short-1",
            symbol="AAPL",
            direction="short",
            timestamp=_ts(10),
            asset_class="equities",
            source_field="equities",
        )
        bars = [_bar(11, 100.0, 101.0, 95.0, 97.0)]

        report = WatchtowerSignalBacktester().run(
            [short_signal],
            {"AAPL": bars},
            _assumptions(allow_short=False),
        )

        assert report.total_trades == 0
        assert report.skipped_signals == 1
        assert report.skipped_by_reason == {"short_disabled": 1}

    def test_cross_field_stats_uses_linked_assets(self):
        bars = [_bar(11, 100.0, 104.0, 99.0, 103.0)]
        signal = _signal(symbol="AAPL", linked_assets=("BTC-EUR",))

        report = WatchtowerSignalBacktester().run(
            [signal],
            {"AAPL": bars},
            _assumptions(),
        )

        assert report.cross_field["signals_with_linked_assets"] == 1
        assert report.cross_field["trades_with_linked_assets"] == 1
        assert report.by_asset_class["equities"]["trade_count"] == 1

    def test_skip_reasons_explain_missing_trades(self):
        no_bar_signal = _signal(signal_id="no-bars", symbol="SOL-EUR", hour=10)
        no_next_signal = _signal(signal_id="no-next", symbol="BTC-EUR", hour=12)
        concurrency_a = _signal(signal_id="conc-a", symbol="ETH-EUR", hour=10)
        concurrency_b = _signal(signal_id="conc-b", symbol="ETH-BTC", hour=10)
        bars = {
            "BTC-EUR": [_bar(11, 100.0, 101.0, 99.0, 100.0)],
            "ETH-EUR": [
                _bar(11, 100.0, 102.0, 99.0, 101.0),
                _bar(12, 101.0, 102.0, 100.0, 101.0),
            ],
            "ETH-BTC": [_bar(11, 100.0, 104.0, 99.0, 103.0)],
        }

        report = WatchtowerSignalBacktester().run(
            [no_bar_signal, no_next_signal, concurrency_a, concurrency_b],
            bars,
            _assumptions(max_bars_held=3, max_concurrent_positions=1),
        )

        assert report.total_trades == 1
        assert report.skipped_by_reason == {
            "max_concurrent_positions": 1,
            "no_bars": 1,
            "no_next_bar_after_signal": 1,
        }


class TestWatchtowerSignalLoader:
    def test_loads_signals_from_watchtower_snapshot_log(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "received": 1,
            "passed_filter": 1,
            "signals": [
                {
                    "signal_id": "wt-1",
                    "asset": "BTC-EUR",
                    "direction": "LONG",
                    "entry_score": 0.9,
                    "confidence": 0.8,
                    "asset_class": "crypto",
                    "linked_assets": ["QQQ"],
                }
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        signals = load_watchtower_signals(tmp_path)

        assert len(signals) == 1
        assert signals[0].symbol == "BTC-EUR"
        assert signals[0].linked_assets == ("QQQ",)

    def test_appends_watchtower_export_as_replay_log(self, tmp_path):
        packet = {
            "generated_at": "2026-04-27T10:00:00+00:00",
            "count": 1,
            "filters": {"asset_class": "crypto"},
            "signals": [
                {
                    "signal_id": "wt-export-1",
                    "asset": "BTC-EUR",
                    "direction": "long",
                    "timestamp": "2026-04-27T10:00:00+00:00",
                    "asset_class": "crypto",
                }
            ],
        }

        path = append_watchtower_export(tmp_path, packet)
        signals = load_watchtower_signals(tmp_path)

        assert path == tmp_path / "watchtower" / "signals.jsonl"
        assert len(signals) == 1
        assert signals[0].signal_id == "wt-export-1"

    def test_loader_deduplicates_signal_ids_across_exports(self, tmp_path):
        packet = {
            "generated_at": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "dup-1",
                    "asset": "BTC-EUR",
                    "direction": "long",
                    "timestamp": "2026-04-27T10:00:00+00:00",
                }
            ],
        }
        append_watchtower_export(tmp_path, packet)
        append_watchtower_export(tmp_path, packet)

        signals = load_watchtower_signals(tmp_path)

        assert len(signals) == 1
        assert signals[0].signal_id == "dup-1"

    def test_loader_can_replay_neutral_signals_as_long(self, tmp_path):
        packet = {
            "generated_at": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "neutral-1",
                    "asset": "BTC-EUR",
                    "direction": "neutral",
                    "timestamp": "2026-04-27T10:00:00+00:00",
                }
            ],
        }
        append_watchtower_export(tmp_path, packet)

        assert load_watchtower_signals(tmp_path) == []
        signals = load_watchtower_signals(tmp_path, include_neutral_as_long=True)

        assert len(signals) == 1
        assert signals[0].direction == "long"
        assert signals[0].raw["original_direction"] == "neutral"


class TestWatchtowerBacktestAnt:
    def _mission(self) -> Mission:
        return Mission(
            mission_id="wt-bt-001",
            ant_type="watchtower_backtest_ant",
            allowed_node="node-1",
            allowed_actions=["read_data", "backtest", "report"],
            market_scope=MarketScope(biome="crypto", symbols=["BTC-EUR"]),
            capital_limit=0.0,
            risk_limits=RiskLimits(
                max_drawdown_pct=1.0,
                max_position_size=1.0,
                daily_loss_limit=1.0,
                stop_loss_required=False,
            ),
            ttl=3600,
            heartbeat_interval=60,
            success_conditions=SuccessConditions(description="Replay Watchtower signals."),
        )

    def test_run_once_writes_backtest_report(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "wt-1",
                    "asset": "BTC-EUR",
                    "direction": "LONG",
                    "entry_score": 0.9,
                    "confidence": 0.8,
                    "asset_class": "crypto",
                }
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        candles = [
            SimpleNamespace(timestamp=_ts(11), open=100.0, high=104.0, low=99.0, close=103.0, volume=1.0),
        ]
        adapter = MagicMock()
        adapter.get_candles.return_value = candles
        registry = MagicMock()
        registry.get.return_value = adapter

        ant = WatchtowerBacktestAnt(
            ant_id="wtbt-123",
            mission=self._mission(),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            biome_registry=registry,
            assumptions=_assumptions(),
        )

        report = ant.run_once()

        assert report["total_trades"] == 1
        files = list((tmp_path / "backtests").glob("watchtower_backtest_*.json"))
        assert len(files) == 1
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        assert saved["total_trades"] == 1
        assert report["report_path"].endswith(".json")

    def test_run_once_reports_seed_signal_source(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "seed-wt-1",
                    "asset": "BTC-EUR",
                    "direction": "LONG",
                    "entry_score": 0.9,
                    "confidence": 0.8,
                    "asset_class": "crypto",
                }
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        adapter = MagicMock()
        adapter.get_candles.return_value = [
            SimpleNamespace(timestamp=_ts(11), open=100.0, high=104.0, low=99.0, close=103.0, volume=1.0),
        ]
        registry = MagicMock()
        registry.get.return_value = adapter

        ant = WatchtowerBacktestAnt(
            ant_id="wtbt-seed",
            mission=self._mission(),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            biome_registry=registry,
            assumptions=_assumptions(),
            signal_source="seed",
        )

        report = ant.run_once()

        assert report["signal_source"] == "seed"
        assert report["total_trades"] == 1

    def test_run_once_filters_degraded_seed_market_quality(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "seed-historical",
                    "asset": "BTC-EUR",
                    "direction": "LONG",
                    "timestamp": "2026-04-27T10:00:00+00:00",
                    "entry_score": 0.9,
                    "asset_class": "crypto",
                    "seed_market_quality": "historical",
                },
                {
                    "signal_id": "seed-degraded",
                    "asset": "BTC-EUR",
                    "direction": "LONG",
                    "timestamp": "2026-04-27T11:00:00+00:00",
                    "entry_score": 0.9,
                    "asset_class": "crypto",
                    "seed_market_quality": "degraded",
                },
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        adapter = MagicMock()
        adapter.get_candles.return_value = [
            SimpleNamespace(timestamp=_ts(11), open=100.0, high=104.0, low=99.0, close=103.0, volume=1.0),
            SimpleNamespace(timestamp=_ts(12), open=103.0, high=107.0, low=102.0, close=106.0, volume=1.0),
        ]
        registry = MagicMock()
        registry.get.return_value = adapter

        ant = WatchtowerBacktestAnt(
            ant_id="wtbt-quality",
            mission=self._mission(),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            biome_registry=registry,
            assumptions=_assumptions(max_concurrent_positions=3),
            signal_source="seed",
            min_market_quality="historical",
        )

        report = ant.run_once()

        assert report["min_market_quality"] == "historical"
        assert report["total_signals"] == 1
        assert report["trades"][0]["signal_id"] == "seed-historical"

    def test_run_once_can_include_neutral_signals(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "seed-neutral",
                    "asset": "BTC-EUR",
                    "direction": "neutral",
                    "timestamp": "2026-04-27T10:00:00+00:00",
                    "entry_score": 0.9,
                    "asset_class": "crypto",
                    "seed_market_quality": "historical",
                },
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        adapter = MagicMock()
        adapter.get_candles.return_value = [
            SimpleNamespace(timestamp=_ts(11), open=100.0, high=104.0, low=99.0, close=103.0, volume=1.0),
        ]
        registry = MagicMock()
        registry.get.return_value = adapter

        ant = WatchtowerBacktestAnt(
            ant_id="wtbt-neutral",
            mission=self._mission(),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            biome_registry=registry,
            assumptions=_assumptions(),
            include_neutral=True,
        )

        report = ant.run_once()

        assert report["include_neutral"] is True
        assert report["total_trades"] == 1
        assert report["trades"][0]["direction"] == "long"

    def test_eth_btc_ratio_uses_synthetic_candles(self, tmp_path):
        log_dir = tmp_path / "watchtower"
        log_dir.mkdir()
        record = {
            "timestamp": "2026-04-27T10:00:00+00:00",
            "signals": [
                {
                    "signal_id": "eth-btc-1",
                    "asset": "ETH-BTC",
                    "direction": "LONG",
                    "entry_score": 0.9,
                    "confidence": 0.8,
                    "asset_class": "crypto",
                }
            ],
        }
        (log_dir / "signals.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

        def candles(symbol, timeframe, limit):
            if symbol == "ETH-EUR":
                return [
                    SimpleNamespace(timestamp=_ts(11), open=2000, high=2080, low=1980, close=2050, volume=1.0),
                    SimpleNamespace(timestamp=_ts(12), open=2050, high=2200, low=2040, close=2180, volume=1.0),
                ]
            if symbol == "BTC-EUR":
                return [
                    SimpleNamespace(timestamp=_ts(11), open=50000, high=50500, low=49500, close=50000, volume=1.0),
                    SimpleNamespace(timestamp=_ts(12), open=50000, high=50200, low=49800, close=50000, volume=1.0),
                ]
            return []

        adapter = MagicMock()
        adapter.get_candles.side_effect = candles
        registry = MagicMock()
        registry.get.return_value = adapter

        ant = WatchtowerBacktestAnt(
            ant_id="wtbt-ethbtc",
            mission=Mission(
                mission_id="wt-bt-ethbtc",
                ant_type="watchtower_backtest_ant",
                allowed_node="node-1",
                allowed_actions=["read_data", "backtest", "report"],
                market_scope=MarketScope(biome="crypto", symbols=["ETH-BTC"]),
                capital_limit=0.0,
                risk_limits=RiskLimits(
                    max_drawdown_pct=1.0,
                    max_position_size=1.0,
                    daily_loss_limit=1.0,
                    stop_loss_required=False,
                ),
                ttl=3600,
                heartbeat_interval=60,
                success_conditions=SuccessConditions(description="Replay ETH/BTC ratio."),
            ),
            scheduler=MagicMock(),
            logs_root=tmp_path,
            biome_registry=registry,
            assumptions=_assumptions(),
        )

        report = ant.run_once()

        assert report["total_trades"] == 1
        assert report["trades"][0]["symbol"] == "ETH-BTC"
