from ant_colony.queen.regime_schema import (
    build_regime_snapshot,
    normalize_execution_permission,
    normalize_macro_regime,
    normalize_structure_regime,
    normalize_watchtower_regime,
)


def test_macro_regime_normalizes_known_values() -> None:
    assert normalize_macro_regime("RISK_ON") == "RISK_ON"
    assert normalize_macro_regime("crisis") == "RISK_OFF"
    assert normalize_macro_regime("sideways") == "NEUTRAL"


def test_structure_regime_normalizes_known_values() -> None:
    assert normalize_structure_regime("SIDEWAYS") == "SIDEWAYS"
    assert normalize_structure_regime("trending_up") == "TRENDING"
    assert normalize_structure_regime("high_volatility") == "VOLATILE"


def test_unknown_values_are_unknown_or_blocked() -> None:
    assert normalize_macro_regime("moon") == "UNKNOWN"
    assert normalize_structure_regime("risk_on") == "UNKNOWN"
    assert normalize_watchtower_regime("") == "UNKNOWN"
    assert normalize_execution_permission("maybe_live") == "BLOCKED"


def test_regime_snapshot_has_all_dashboard_fields() -> None:
    snapshot = build_regime_snapshot(
        macro_source="RISK_ON",
        structure_source="SIDEWAYS",
        watchtower_source="VOLATILE",
        execution_permission="PAPER_ONLY",
    )
    assert snapshot.as_dict() == {
        "macro_regime": "RISK_ON",
        "structure_regime": "SIDEWAYS",
        "watchtower_regime": "VOLATILE",
        "execution_permission": "PAPER_ONLY",
    }
