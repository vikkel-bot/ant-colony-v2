"""
tests/test_start_colony_node_registration.py

Controleert dat alle ant-types die in start_colony.py gebruikt worden
in de AntType enum staan, zodat node-registratie nooit faalt door een
ontbrekend type.
"""

from __future__ import annotations

from ant_colony.schemas.ant import AntType


_ALL_ENUM_VALUES = {t.value for t in AntType}


class TestAntTypeEnumVolledigheid:
    def test_rs_regime_ant_in_enum(self):
        assert "rs_regime_ant" in _ALL_ENUM_VALUES

    def test_time_filter_ant_in_enum(self):
        assert "time_filter_ant" in _ALL_ENUM_VALUES

    def test_momentum_rank_ant_in_enum(self):
        assert "momentum_rank_ant" in _ALL_ENUM_VALUES

    def test_rebalance_ant_in_enum(self):
        assert "rebalance_ant" in _ALL_ENUM_VALUES

    def test_rotation_ant_in_enum(self):
        assert "rotation_ant" in _ALL_ENUM_VALUES

    def test_volatility_ant_in_enum(self):
        assert "volatility_ant" in _ALL_ENUM_VALUES

    def test_alle_crypto_ants_in_enum(self):
        for ant_type in (
            "scout_ant", "research_ant", "paper_ant", "execution_ant",
            "audit_ant", "ingestion_ant", "strategy_ant", "operator_ant",
            "claude_ant", "time_filter_ant",
        ):
            assert ant_type in _ALL_ENUM_VALUES, f"{ant_type} ontbreekt in AntType"

    def test_alle_equities_ants_in_enum(self):
        for ant_type in (
            "sector_scout_ant", "fundamental_ant", "dividend_scout_ant",
            "piotroski_ant", "breakout_ant", "momentum_rank_ant",
            "rebalance_ant", "rotation_ant", "volatility_ant", "rs_regime_ant",
        ):
            assert ant_type in _ALL_ENUM_VALUES, f"{ant_type} ontbreekt in AntType"

    def test_node_allowed_types_bevat_rs_regime(self):
        """Simuleert wat start_colony.py doet: bouw allowed_ant_types uit enum."""
        all_ant_types = [t.value for t in AntType]
        assert "rs_regime_ant" in all_ant_types

    def test_node_allowed_types_bevat_time_filter(self):
        all_ant_types = [t.value for t in AntType]
        assert "time_filter_ant" in all_ant_types
