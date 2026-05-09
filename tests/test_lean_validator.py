from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ant_colony.research.lean_validator import LeanValidator


def test_validate_returns_unavailable_when_lean_missing(tmp_path: Path) -> None:
    validator = LeanValidator(tmp_path, lean_executable="definitely-missing-lean")

    result = validator.validate(
        {
            "candidate_id": "cand-lean-missing",
            "biome": "equities",
            "market_scope": {"symbol": "AAPL"},
            "entry_conditions": {"direction": "long"},
            "exit_conditions": {"take_profit_pct": 0.06, "stop_loss_pct": 0.03},
        }
    )

    assert result["lean_status"] == "unavailable"
    assert result["lean_sharpe"] is None
    assert (tmp_path / "lean" / "cand-lean-missing.json").exists()


def test_parse_lean_statistics_json(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "result.json").write_text(
        json.dumps(
            {
                "Statistics": {
                    "Sharpe Ratio": "1.23",
                    "Drawdown": "4.5%",
                    "Win Rate": "62%",
                    "Total Trades": "31",
                }
            }
        ),
        encoding="utf-8",
    )
    validator = LeanValidator(tmp_path)

    metrics = validator._parse_output(output_dir)

    assert metrics is not None
    assert metrics["lean_sharpe"] == 1.23
    assert metrics["lean_max_drawdown"] == 0.045
    assert metrics["lean_win_rate"] == 0.62
    assert metrics["lean_trades"] == 31


def test_validate_runs_lean_and_writes_passed_result(tmp_path: Path, monkeypatch) -> None:
    validator = LeanValidator(tmp_path, lean_executable="lean")

    monkeypatch.setattr(
        "ant_colony.research.lean_validator.shutil.which",
        lambda _cmd: "C:/fake/tool.exe",
    )
    monkeypatch.setattr(validator, "_run_quick", lambda _cmd: (True, "Docker version ok"))

    def fake_run(command, **kwargs):
        project_dir = Path(command[-1])
        output_dir = project_dir / "backtests" / "unit-test"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "result.json").write_text(
            json.dumps(
                {
                    "statistics": {
                        "Sharpe Ratio": "0.88",
                        "Max Drawdown": "3%",
                        "Win Rate": "55%",
                        "Total Trades": 18,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert kwargs["timeout"] == 120
        assert command[:2] == ["C:/fake/tool.exe", "backtest"]
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("ant_colony.research.lean_validator.subprocess.run", fake_run)

    result = validator.validate(
        {
            "candidate_id": "cand-lean-ok",
            "biome": "equities",
            "market_scope": {"symbol": "AAPL"},
            "entry_conditions": {"direction": "long"},
            "exit_conditions": {"take_profit_pct": 0.06, "stop_loss_pct": 0.03},
        }
    )

    assert result["lean_status"] == "passed"
    assert result["lean_sharpe"] == 0.88
    stored = json.loads((tmp_path / "lean" / "cand-lean-ok.json").read_text(encoding="utf-8"))
    assert stored["lean_status"] == "passed"


def test_prepare_project_uses_sma_template_and_candidate_parameters(tmp_path: Path) -> None:
    validator = LeanValidator(tmp_path)

    project_dir = validator._prepare_project({
        "candidate_id": "cand-template",
        "market_scope": {"symbol": "BTC-EUR"},
        "parameters": {"short_period": 12, "long_period": 34},
    })

    assert (project_dir / "main.py").exists()
    config = json.loads((project_dir / "config.json").read_text(encoding="utf-8"))
    assert config["parameters"]["symbol"] == "BTCEUR"
    assert config["parameters"]["short_period"] == "12"
    assert config["parameters"]["long_period"] == "34"
