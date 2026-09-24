"""Kostenmodel — versioned, artefact-gebaseerd.

Leest een BEVROREN artefact (docs/COST_MODEL_*.json) dat uit orderboek-snapshots
is gebouwd met scripts/build_cost_model.py. Dit bestand haalt zelf geen data op
en berekent niets opnieuw: een geregistreerde toets verwijst naar een
artefactversie en krijgt daarmee altijd dezelfde kosten.

Aggregatievolgorde in het artefact:
  1. per markt en ordergrootte: percentiel over de snapshots van max(koop, verkoop);
  2. per omzetklasse: mediaan over de markten in die klasse.

Kosten per kant = tarief + impact. Round-trip = 2 x (tarief + impact).

Sizing hoort hier NIET: order_notional is invoer. De colony heeft eigen
sizing-regels (paper_ant _TRADE_CAPITAL_FRACTION, edge_audit risk_per_trade);
dit model spreekt zich daar niet over uit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ESTIMATORS = ("median", "p75", "p90", "max")
DEFAULT_ARTIFACT = Path(__file__).resolve().parents[2] / "docs" / "COST_MODEL_V1_20260924.json"


def load_model(path: Path | str = DEFAULT_ARTIFACT) -> dict[str, Any]:
    model = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("version", "fee_per_side", "estimator_for_gate", "sizes_eur", "turnover_buckets"):
        if key not in model:
            raise ValueError(f"kostenmodel mist verplicht veld: {key}")
    if model["estimator_for_gate"] not in ESTIMATORS:
        raise ValueError(f"onbekende estimator_for_gate: {model['estimator_for_gate']}")
    return model


def bucket_for(model: dict, turnover_30d_eur: float) -> dict:
    """Omzetklasse waarin deze markt valt. Onder de laagste grens -> duurste klasse."""
    for b in model["turnover_buckets"]:
        upper = b.get("max_turnover")
        if upper is None or turnover_30d_eur <= upper:
            return b
    return model["turnover_buckets"][-1]


def impact_per_side(model: dict, turnover_30d_eur: float, order_notional_eur: float,
                    estimator: str | None = None) -> float:
    """Spread + prijsimpact per kant, exclusief tarief.

    Tussen gemeten ordergroottes wordt lineair geïnterpoleerd in log(omvang).
    Buiten het gemeten bereik: ValueError (fail-closed, geen extrapolatie).
    """
    est = estimator or model["estimator_for_gate"]
    if est not in ESTIMATORS:
        raise ValueError(f"onbekende estimator: {est}")
    sizes = sorted(model["sizes_eur"])
    if not sizes[0] <= order_notional_eur <= sizes[-1]:
        raise ValueError(
            f"order_notional {order_notional_eur} valt buiten het gemeten bereik "
            f"[{sizes[0]}, {sizes[-1]}]; het model extrapoleert niet")
    costs = bucket_for(model, turnover_30d_eur)["costs"][est]
    for lo, hi in zip(sizes, sizes[1:]):
        if lo <= order_notional_eur <= hi:
            c_lo, c_hi = costs[str(lo)], costs[str(hi)]
            if hi == lo:
                return float(c_lo)
            import math
            w = (math.log(order_notional_eur) - math.log(lo)) / (math.log(hi) - math.log(lo))
            return float(c_lo + w * (c_hi - c_lo))
    return float(costs[str(sizes[-1])])


def cost_per_side(model: dict, turnover_30d_eur: float, order_notional_eur: float,
                  estimator: str | None = None) -> float:
    return float(model["fee_per_side"]) + impact_per_side(model, turnover_30d_eur, order_notional_eur, estimator)


def round_trip(model: dict, turnover_30d_eur: float, order_notional_eur: float,
               estimator: str | None = None) -> float:
    return 2.0 * cost_per_side(model, turnover_30d_eur, order_notional_eur, estimator)


def weekly_cost(model: dict, turnover_30d_eur: float, order_notional_eur: float,
                turnover_fraction: float, estimator: str | None = None) -> float:
    """Kosten per week bij een portefeuille-omloop van `turnover_fraction` (0-1)."""
    if not 0.0 <= turnover_fraction <= 1.0:
        raise ValueError("turnover_fraction moet tussen 0 en 1 liggen")
    return turnover_fraction * round_trip(model, turnover_30d_eur, order_notional_eur, estimator)


def describe(model: dict) -> str:
    b = model["turnover_buckets"]
    return (f"{model['version']} | gate-estimator {model['estimator_for_gate']} | "
            f"tarief {model['fee_per_side']:.4f}/kant | referentie "
            f"{model.get('reference_notional_eur')} EUR | {len(b)} omzetklassen | "
            f"{model.get('created_from', {}).get('snapshots')} snapshots")
