"""
Portfolio benchmark calculator for ANT COLONY v2.

Read-only analysis. It uses the fixed equity audit results from the 3-year
walk-forward run and does not touch live trading logic, thresholds, routing, or
positions.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


BENCHMARK_CAGR = 0.106
BOOST_LEVELS = (0.0, 0.10, 0.20, 0.30)


@dataclass(frozen=True)
class EdgeCombination:
    asset: str
    strategy: str
    expectancy_r: float
    profit_factor: float
    observed_trades_3yr: int
    cagr: float
    max_drawdown: float
    status: str = "POSITIVE_EDGE"
    expectancy_weight: float = 1.0

    @property
    def combo_id(self) -> str:
        return f"{self.asset}:{self.strategy}"

    @property
    def trades_per_year(self) -> float:
        return self.observed_trades_3yr / 3.0

    @property
    def adjusted_status(self) -> str:
        return "NEEDS_VALIDATION" if self.status == "MARGINAL" else self.status


POSITIVE_EDGE_COMBINATIONS: tuple[EdgeCombination, ...] = (
    # The source audit reported these as trades/year. Store the implied 3-year
    # counts so the shared calculation can apply observed_trades_3yr / 3.
    EdgeCombination("JNJ", "momentum long", 0.2738, 2.3068, 33, 0.030, 0.023),
    EdgeCombination("GLD", "momentum long", 0.2575, 1.7445, 36, 0.031, 0.043),
    EdgeCombination("XLK", "momentum long", 0.2476, 1.7778, 45, 0.037, 0.059),
    EdgeCombination("AAPL", "momentum long", 0.2346, 1.6338, 43, 0.033, 0.033),
    EdgeCombination("XLY", "momentum long", 0.2069, 1.6162, 37, 0.025, 0.039),
    EdgeCombination("QQQ", "momentum long", 0.2003, 1.6710, 39, 0.026, 0.070),
    EdgeCombination("XLF", "bb_lower_approach", 0.1708, 1.3164, 33, 0.018, 0.046),
    EdgeCombination("XLI", "momentum long", 0.1176, 1.4047, 34, 0.013, 0.028),
    EdgeCombination("XLU", "momentum long", 0.0880, 1.2835, 34, 0.010, 0.028),
)


MARGINAL_COMBINATIONS: tuple[EdgeCombination, ...] = (
    # The source audit reported marginal trade counts as raw 3-year counts.
    EdgeCombination("XLE", "rsi_based", 0.5540, 2.3296, 23, 0.0425, 0.0238, "MARGINAL", 0.5),
    EdgeCombination("XLF", "bollinger_bands", 0.4316, 2.1832, 27, 0.0388, 0.0332, "MARGINAL", 0.5),
    EdgeCombination("XLF", "mean_reversion", 0.4316, 2.1832, 27, 0.0388, 0.0332, "MARGINAL", 0.5),
    EdgeCombination("XLF", "rsi_based", 0.3871, 2.1169, 16, 0.0205, 0.0166, "MARGINAL", 0.5),
    EdgeCombination("GLD", "bb_lower_approach", 0.2723, 1.6303, 22, 0.0196, 0.0500, "MARGINAL", 0.5),
    EdgeCombination("MSFT", "bollinger_bands", 0.2003, 1.3300, 25, 0.0159, 0.0375, "MARGINAL", 0.5),
    EdgeCombination("MSFT", "mean_reversion", 0.2003, 1.3300, 25, 0.0159, 0.0375, "MARGINAL", 0.5),
)


CONFIRMED_NEGATIVE = (
    "XLRE momentum",
    "XLU bb_lower",
    "XLK bb_lower",
    "MSFT momentum",
    "XLB momentum",
    "MSFT rsi_based",
    "XLRE bb_lower",
    "XLP bb_lower",
    "AAPL bb_lower",
    "JNJ bb_lower",
    "XLY bb_lower",
    "XLB bb_lower",
)


def per_combo_metrics(combo: EdgeCombination, *, risk_per_trade: float, watchtower_boost: float) -> dict[str, float]:
    adjusted_expectancy = combo.expectancy_r * combo.expectancy_weight * (1.0 + watchtower_boost)
    gross_return_per_trade = risk_per_trade * adjusted_expectancy
    linear = combo.trades_per_year * gross_return_per_trade
    compound = (1.0 + gross_return_per_trade) ** combo.trades_per_year - 1.0
    return {
        "trades_per_year": combo.trades_per_year,
        "adjusted_expectancy_r": adjusted_expectancy,
        "gross_return_per_trade": gross_return_per_trade,
        "annual_return_linear": linear,
        "annual_return_compound": compound,
    }


def evaluate_subset(
    subset: tuple[EdgeCombination, ...],
    *,
    risk_per_trade: float,
    max_drawdown_limit: float,
    watchtower_boost: float,
) -> dict[str, object]:
    combo_metrics = [per_combo_metrics(c, risk_per_trade=risk_per_trade, watchtower_boost=watchtower_boost) for c in subset]
    linear = sum(m["annual_return_linear"] for m in combo_metrics)
    compound = math.prod(1.0 + m["annual_return_compound"] for m in combo_metrics) - 1.0 if combo_metrics else 0.0
    trades_per_year = sum(m["trades_per_year"] for m in combo_metrics)
    capital_utilization = trades_per_year * risk_per_trade
    max_dd = max((c.max_drawdown for c in subset), default=0.0)
    n = len(subset)
    independent_dd = max_dd * (math.sqrt(n) / n) if n else 0.0
    conservative_dd = max_dd
    verdict = feasibility_verdict(
        annual_return=linear,
        max_drawdown=conservative_dd,
        max_drawdown_limit=max_drawdown_limit,
        capital_utilization=capital_utilization,
    )
    marginal_ids = [c.combo_id for c in subset if c.status == "MARGINAL"]
    return {
        "assets_included": ";".join(c.combo_id for c in subset) or "NONE",
        "marginal_included": ";".join(marginal_ids),
        "n_combinations": n,
        "annual_return_linear": round(linear, 6),
        "annual_return_compound": round(compound, 6),
        "compound_divergence_flag": abs(compound - linear) > 0.02,
        "max_drawdown_conservative": round(conservative_dd, 6),
        "max_drawdown_independent": round(independent_dd, 6),
        "trades_per_year": round(trades_per_year, 3),
        "capital_utilization": round(capital_utilization, 6),
        "watchtower_boost": watchtower_boost,
        "feasibility_verdict": verdict,
        "contains_needs_validation": bool(marginal_ids),
    }


def feasibility_verdict(
    *,
    annual_return: float,
    max_drawdown: float,
    max_drawdown_limit: float,
    capital_utilization: float,
) -> str:
    if annual_return >= BENCHMARK_CAGR and max_drawdown <= max_drawdown_limit and capital_utilization <= 1.0:
        return "BENCHMARK_ACHIEVABLE"
    if annual_return >= BENCHMARK_CAGR / 2 and max_drawdown <= max_drawdown_limit:
        return "BENCHMARK_PARTIAL"
    return "BENCHMARK_UNREACHABLE"


def search_portfolios(
    combinations: tuple[EdgeCombination, ...],
    *,
    risk_per_trade: float,
    max_drawdown_limit: float,
    boosts: Iterable[float],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for boost in boosts:
        for size in range(0, len(combinations) + 1):
            for subset in itertools.combinations(combinations, size):
                rows.append(
                    evaluate_subset(
                        subset,
                        risk_per_trade=risk_per_trade,
                        max_drawdown_limit=max_drawdown_limit,
                        watchtower_boost=boost,
                    )
                )
    return rows


def select_best(rows: list[dict[str, object]], *, boost: float, require_deployable: bool = True) -> dict[str, object]:
    candidates = [r for r in rows if float(r["watchtower_boost"]) == boost]
    if require_deployable:
        deployable = [
            r for r in candidates
            if float(r["capital_utilization"]) <= 1.0 and r["feasibility_verdict"] != "BENCHMARK_UNREACHABLE"
        ]
        if deployable:
            candidates = deployable
    return max(candidates, key=lambda r: (float(r["annual_return_linear"]), -float(r["max_drawdown_conservative"])))


def build_report(
    *,
    rows: list[dict[str, object]],
    include_marginal: bool,
    risk_per_trade: float,
    max_drawdown_limit: float,
    capital: float,
    watchtower_boosts: tuple[float, ...],
) -> str:
    best_by_boost = [select_best(rows, boost=boost) for boost in watchtower_boosts]
    positive_only_rows = [row for row in rows if not row["contains_needs_validation"]]
    positive_only_by_boost = [select_best(positive_only_rows, boost=boost) for boost in watchtower_boosts]
    best_baseline = select_best(rows, boost=0.0)
    positive_only_baseline = select_best(positive_only_rows, boost=0.0)
    positive_only_achievable = positive_only_baseline["feasibility_verdict"] == "BENCHMARK_ACHIEVABLE"
    recommendation = recommendation_for(
        best_by_boost,
        include_marginal,
        positive_only_achievable=positive_only_achievable,
    )
    benchmark_abs = capital * BENCHMARK_CAGR
    best_return = float(best_baseline["annual_return_linear"])
    minimum_capital_for_current_abs = benchmark_abs / best_return if best_return > 0 else math.inf
    minimum_capital_per_trade = capital * risk_per_trade
    simultaneous_positions = math.floor(capital / (capital * risk_per_trade * 10.0)) if capital > 0 and risk_per_trade > 0 else 0
    marginal_used = sorted({item for row in best_by_boost for item in str(row.get("marginal_included") or "").split(";") if item})

    lines = [
        "# Portfolio Benchmark Report",
        "",
        "## Assumptions",
        f"- Benchmark CAGR: `{BENCHMARK_CAGR:.1%}`",
        f"- Risk per trade: `{risk_per_trade:.2%}`",
        f"- Max drawdown limit: `{max_drawdown_limit:.1%}`",
        f"- Capital: `EUR {capital:.2f}`",
        f"- Include marginal: `{include_marginal}`",
        "- Crypto excluded: all tested crypto strategies are CONFIRMED_NEGATIVE.",
        "- POSITIVE_EDGE input trades/year are converted to implied 3-year observed counts internally.",
        "- MARGINAL input trades are treated as raw 3-year counts and discounted to 50% expectancy.",
        "- Portfolio return assumes strategy independence and additive risk budgets.",
        "- Drawdown verdict uses the conservative correlated-bucket estimate, not the optimistic independent estimate.",
        "- Marginal combinations are included at 50% expectancy and flagged NEEDS_VALIDATION.",
        "",
        "## Watchtower Sensitivity",
        "|watchtower_boost|best_subset|expected_cagr_linear|expected_cagr_compound|max_dd_conservative|capital_utilization|verdict|",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in best_by_boost:
        lines.append(
            "|{boost:.0%}|{subset}|{linear:.2%}|{compound:.2%}|{dd:.2%}|{util:.2%}|{verdict}|".format(
                boost=float(row["watchtower_boost"]),
                subset=row["assets_included"],
                linear=float(row["annual_return_linear"]),
                compound=float(row["annual_return_compound"]),
                dd=float(row["max_drawdown_conservative"]),
                util=float(row["capital_utilization"]),
                verdict=row["feasibility_verdict"],
            )
        )
    lines.extend([
        "",
        "## Feasibility",
        f"- Best baseline portfolio: `{best_baseline['assets_included']}`",
        f"- Baseline expected CAGR linear: `{float(best_baseline['annual_return_linear']):.2%}`",
        f"- Baseline expected CAGR compound: `{float(best_baseline['annual_return_compound']):.2%}`",
        f"- Conservative max drawdown: `{float(best_baseline['max_drawdown_conservative']):.2%}`",
        f"- Capital utilization: `{float(best_baseline['capital_utilization']):.2%}`",
        f"- Compound divergence flag (>2 percentage points): `{best_baseline['compound_divergence_flag']}`",
        f"- Baseline verdict: `{best_baseline['feasibility_verdict']}`",
        "",
        "## Marginal Sensitivity",
    ])
    if include_marginal:
        lines.append("- Marginal combinations in best sensitivity portfolios: " + (", ".join(marginal_used) if marginal_used else "none"))
        lines.append(f"- Best POSITIVE_EDGE-only baseline: `{positive_only_baseline['assets_included']}`")
        lines.append(f"- POSITIVE_EDGE-only baseline expected CAGR: `{float(positive_only_baseline['annual_return_linear']):.2%}`")
        lines.append(f"- POSITIVE_EDGE-only baseline verdict: `{positive_only_baseline['feasibility_verdict']}`")
        if positive_only_achievable:
            lines.append("- Marginal combinations are not required to cross the benchmark at baseline assumptions.")
        else:
            lines.append("- Marginal combinations may be needed, but remain NEEDS_VALIDATION until >=30 out-of-sample trades.")
    else:
        lines.append("- Marginal combinations excluded. Re-run with `--include-marginal` for validation sensitivity.")

    lines.extend([
        "",
        "## Capital Sizing",
        f"- Minimum capital per trade at current capital: `EUR {minimum_capital_per_trade:.2f}`",
        f"- Simultaneous positions possible by provided formula: `{simultaneous_positions}`",
        f"- Benchmark absolute return on current capital: `EUR {benchmark_abs:.2f}/year`",
        f"- Minimum capital to generate that absolute EUR target at baseline portfolio return: `EUR {minimum_capital_for_current_abs:.2f}`",
        f"- Current EUR {capital:.2f} sufficient by return-rate math: `{best_return >= BENCHMARK_CAGR}`",
        "",
        "## Confirmed Negative Exclusions",
        ", ".join(CONFIRMED_NEGATIVE),
        "",
        "## Recommendation",
        recommendation,
    ])
    if recommendation == "BENCHMARK_UNREACHABLE":
        lines.append("Fundamental strategy research required before deployment. Current edge is insufficient to meet the 10.6% benchmark at acceptable risk.")
    elif recommendation == "PROCEED_WITH_CANARY":
        lines.append("Proceed to PC2 log validation and canary deployment sizing; keep crypto disabled.")
    else:
        lines.append("Needs more edge or validation before full benchmark-target deployment.")
    return "\n".join(lines) + "\n"


def recommendation_for(
    best_by_boost: list[dict[str, object]],
    include_marginal: bool,
    *,
    positive_only_achievable: bool = False,
) -> str:
    if positive_only_achievable:
        return "PROCEED_WITH_CANARY"
    if any(r["feasibility_verdict"] == "BENCHMARK_ACHIEVABLE" for r in best_by_boost):
        return "PROCEED_WITH_CANARY" if not include_marginal else "PROCEED_WITH_CANARY_PENDING_VALIDATION"
    if best_by_boost[-1]["feasibility_verdict"] == "BENCHMARK_UNREACHABLE":
        return "BENCHMARK_UNREACHABLE"
    return "NEEDS_MORE_EDGE"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()}) or ["empty"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_calculator(
    *,
    output_dir: Path,
    risk_per_trade: float,
    max_drawdown_limit: float,
    watchtower_boost: float,
    include_marginal: bool,
    capital: float,
) -> dict[str, object]:
    boosts = tuple(sorted(set((*BOOST_LEVELS, watchtower_boost))))
    combinations = POSITIVE_EDGE_COMBINATIONS + (MARGINAL_COMBINATIONS if include_marginal else ())
    rows = search_portfolios(
        combinations,
        risk_per_trade=risk_per_trade,
        max_drawdown_limit=max_drawdown_limit,
        boosts=boosts,
    )
    sensitivity_rows = [
        {
            "watchtower_boost": row["watchtower_boost"],
            "portfolio_subset": row["assets_included"],
            "expected_cagr": row["annual_return_compound"],
            "expected_cagr_linear": row["annual_return_linear"],
            "feasibility_verdict": row["feasibility_verdict"],
        }
        for row in rows
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "portfolio_benchmark_matrix.csv", rows)
    write_csv(output_dir / "portfolio_sensitivity_chart_data.csv", sensitivity_rows)
    report = build_report(
        rows=rows,
        include_marginal=include_marginal,
        risk_per_trade=risk_per_trade,
        max_drawdown_limit=max_drawdown_limit,
        capital=capital,
        watchtower_boosts=boosts,
    )
    (output_dir / "portfolio_benchmark_report.md").write_text(report, encoding="utf-8")
    best_baseline = select_best(rows, boost=0.0)
    return {
        "output_dir": str(output_dir),
        "rows": len(rows),
        "include_marginal": include_marginal,
        "best_baseline_subset": best_baseline["assets_included"],
        "best_baseline_return": best_baseline["annual_return_linear"],
        "best_baseline_verdict": best_baseline["feasibility_verdict"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only equity portfolio benchmark calculator.")
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--max-drawdown-limit", type=float, default=0.20)
    parser.add_argument("--watchtower-boost", type=float, default=0.0)
    parser.add_argument("--include-marginal", action="store_true")
    parser.add_argument("--capital", type=float, default=1638.0)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("logs") / "portfolio_benchmark" / stamp


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else default_output_dir().resolve()
    manifest = run_calculator(
        output_dir=output_dir,
        risk_per_trade=args.risk_per_trade,
        max_drawdown_limit=args.max_drawdown_limit,
        watchtower_boost=args.watchtower_boost,
        include_marginal=args.include_marginal,
        capital=args.capital,
    )
    print("Portfolio benchmark calculator afgerond")
    print(f"Output map       : {manifest['output_dir']}")
    print(f"Matrix rows      : {manifest['rows']}")
    print(f"Best baseline    : {manifest['best_baseline_subset']}")
    print(f"Baseline return  : {float(manifest['best_baseline_return']):.2%}")
    print(f"Baseline verdict : {manifest['best_baseline_verdict']}")
    print(f"Rapport          : {Path(manifest['output_dir']) / 'portfolio_benchmark_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
