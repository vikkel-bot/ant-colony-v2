"""
Small additive regime vocabulary for dashboard/API observability.

The existing strategy logic still consumes its current regime strings.  This
module only normalizes display fields so RISK_ON, SIDEWAYS and unknown values
do not get mixed into one ambiguous dashboard label.
"""

from __future__ import annotations

from dataclasses import dataclass


MACRO_REGIMES = {"RISK_ON", "RISK_OFF", "NEUTRAL", "UNKNOWN"}
STRUCTURE_REGIMES = {"SIDEWAYS", "TRENDING", "VOLATILE", "UNKNOWN"}
WATCHTOWER_REGIMES = MACRO_REGIMES | STRUCTURE_REGIMES
EXECUTION_PERMISSIONS = {"BLOCKED", "PAPER_ONLY", "MANUAL_APPROVAL", "LIVE_ALLOWED"}


_MACRO_ALIASES = {
    "BULL": "RISK_ON",
    "BULLISH": "RISK_ON",
    "GROWTH": "RISK_ON",
    "RISK_ON": "RISK_ON",
    "BEAR": "RISK_OFF",
    "BEARISH": "RISK_OFF",
    "CRISIS": "RISK_OFF",
    "HIGH_RISK": "RISK_OFF",
    "RISK_OFF": "RISK_OFF",
    "NORMAL": "NEUTRAL",
    "NEUTRAL": "NEUTRAL",
}

_STRUCTURE_ALIASES = {
    "RANGE": "SIDEWAYS",
    "SIDEWAY": "SIDEWAYS",
    "SIDEWAYS": "SIDEWAYS",
    "TREND": "TRENDING",
    "TRENDING": "TRENDING",
    "TRENDING_UP": "TRENDING",
    "TRENDING_DOWN": "TRENDING",
    "VOLATILE": "VOLATILE",
    "HIGH_VOL": "VOLATILE",
    "HIGH_VOLATILITY": "VOLATILE",
    "VOLATILE_BULL": "VOLATILE",
}


@dataclass(frozen=True)
class RegimeSnapshot:
    macro_regime: str = "UNKNOWN"
    structure_regime: str = "UNKNOWN"
    watchtower_regime: str = "UNKNOWN"
    execution_permission: str = "BLOCKED"

    def as_dict(self) -> dict[str, str]:
        return {
            "macro_regime": self.macro_regime,
            "structure_regime": self.structure_regime,
            "watchtower_regime": self.watchtower_regime,
            "execution_permission": self.execution_permission,
        }


def _clean(value: object) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def normalize_macro_regime(value: object) -> str:
    text = _clean(value)
    if not text:
        return "UNKNOWN"
    if text in _MACRO_ALIASES:
        return _MACRO_ALIASES[text]
    if text in {"SIDEWAYS", "TRENDING", "VOLATILE"}:
        return "NEUTRAL"
    return "UNKNOWN"


def normalize_structure_regime(value: object) -> str:
    text = _clean(value)
    if not text:
        return "UNKNOWN"
    if text in _STRUCTURE_ALIASES:
        return _STRUCTURE_ALIASES[text]
    if text in {"RISK_ON", "RISK_OFF", "NEUTRAL", "CRISIS"}:
        return "UNKNOWN"
    return "UNKNOWN"


def normalize_watchtower_regime(value: object) -> str:
    text = _clean(value)
    if not text:
        return "UNKNOWN"
    structure = normalize_structure_regime(text)
    if structure != "UNKNOWN":
        return structure
    macro = normalize_macro_regime(text)
    if macro != "UNKNOWN":
        return macro
    return "UNKNOWN"


def normalize_execution_permission(value: object) -> str:
    text = _clean(value)
    if text in EXECUTION_PERMISSIONS:
        return text
    return "BLOCKED"


def build_regime_snapshot(
    *,
    macro_source: object = None,
    structure_source: object = None,
    watchtower_source: object = None,
    execution_permission: object = None,
) -> RegimeSnapshot:
    return RegimeSnapshot(
        macro_regime=normalize_macro_regime(macro_source),
        structure_regime=normalize_structure_regime(structure_source),
        watchtower_regime=normalize_watchtower_regime(watchtower_source),
        execution_permission=normalize_execution_permission(execution_permission),
    )
