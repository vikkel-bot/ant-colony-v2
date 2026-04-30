from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path


def _load_module():
    path = Path.cwd() / "scripts" / "portfolio_benchmark_calculator.py"
    spec = importlib.util.spec_from_file_location("portfolio_benchmark_calculator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _local_tmp(name: str) -> Path:
    path = Path.cwd() / ".codex_test_tmp" / f"{name}_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def test_positive_edge_portfolio_can_reach_benchmark() -> None:
    calc = _load_module()
    rows = calc.search_portfolios(
        calc.POSITIVE_EDGE_COMBINATIONS,
        risk_per_trade=0.01,
        max_drawdown_limit=0.20,
        boosts=(0.0,),
    )
    best = calc.select_best(rows, boost=0.0)
    assert best["feasibility_verdict"] == "BENCHMARK_ACHIEVABLE"
    assert float(best["annual_return_linear"]) >= 0.106
    assert float(best["max_drawdown_conservative"]) <= 0.20
    assert float(best["capital_utilization"]) <= 1.0


def test_marginal_combinations_are_half_weighted() -> None:
    calc = _load_module()
    combo = calc.MARGINAL_COMBINATIONS[0]
    metrics = calc.per_combo_metrics(combo, risk_per_trade=0.01, watchtower_boost=0.0)
    expected = combo.expectancy_r * 0.5 * 0.01 * combo.trades_per_year
    assert metrics["annual_return_linear"] == expected


def test_run_calculator_writes_outputs() -> None:
    calc = _load_module()
    output_dir = _local_tmp("portfolio_benchmark")
    manifest = calc.run_calculator(
        output_dir=output_dir,
        risk_per_trade=0.01,
        max_drawdown_limit=0.20,
        watchtower_boost=0.0,
        include_marginal=False,
        capital=1638.0,
    )
    assert manifest["rows"] == 512 * 4
    assert (output_dir / "portfolio_benchmark_report.md").exists()
    assert (output_dir / "portfolio_benchmark_matrix.csv").exists()
    assert (output_dir / "portfolio_sensitivity_chart_data.csv").exists()
