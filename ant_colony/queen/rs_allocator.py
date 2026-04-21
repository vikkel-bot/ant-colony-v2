"""
ant_colony/queen/rs_allocator.py

RS Regime Allocator — past equities-kapitaal aan op basis van het marktregime
dat RSRegimeAnt classificeert.

Toewijzingslogica (fractie van base_equities_capital):
  RISK_ON:   100% — normale groei-allocatie
  NEUTRAL:    80% — lichte voorzichtigheid, 20% cash behouden
  RISK_OFF:   50% — defensive rotatie, helft naar cash/defensief
  CRISIS:     20% — minimale blootstelling, zwaar defensief

Gebruik door Queen of advisor:
    from ant_colony.queen.rs_allocator import apply_rs_regime_allocation
    apply_rs_regime_allocation(queen, logs_root, base_equities_capital=10_000.0)

Regels:
  - Alle functies zijn pure of fail-safe (gooit nooit)
  - Onbekend regime of geen signaal → geen wijziging (fail-open)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Allocatiefactoren per regime
# ---------------------------------------------------------------------------

_ALLOCATION_FACTORS: dict[str, float] = {
    "RISK_ON":  1.0,
    "NEUTRAL":  0.8,
    "RISK_OFF": 0.5,
    "CRISIS":   0.2,
}


def compute_allocation_factor(regime: str) -> float:
    """
    Geeft de fractie van base-kapitaal die actief ingezet mag worden.

    Args:
        regime: "RISK_ON" | "NEUTRAL" | "RISK_OFF" | "CRISIS"

    Returns:
        Float tussen 0.2 en 1.0. Onbekend regime → 1.0 (fail-open).
    """
    return _ALLOCATION_FACTORS.get(regime, 1.0)


def apply_rs_regime_allocation(
    queen,
    logs_root: Path,
    base_equities_capital: float,
) -> str | None:
    """
    Lees het laatste RS-regime-signaal en pas de equities biome-limiet aan.

    Args:
        queen:                  Queen-instantie met set_biome_capital().
        logs_root:              Root van ANT_LOGS (bevat rs_regime/).
        base_equities_capital:  Maximaal equities-kapitaal bij RISK_ON.

    Returns:
        Het gedetecteerde regime als string, of None als geen signaal aanwezig.
        Bij None: geen wijziging aangebracht (fail-open).
    """
    from ant_colony.ants.equities.rs_regime_ant import read_latest_rs_regime

    try:
        signal = read_latest_rs_regime(logs_root)
    except Exception:
        return None

    if signal is None:
        return None

    regime = signal.get("regime")
    if not isinstance(regime, str) or regime not in _ALLOCATION_FACTORS:
        return None

    factor = compute_allocation_factor(regime)
    adjusted_capital = base_equities_capital * factor

    try:
        queen.set_biome_capital("equities", adjusted_capital)
    except Exception:
        return None

    return regime
