"""
ant_colony/biome/biome_registry.py

BiomeRegistry — centraal register van actieve biome-adapters.

De registry is de enige plek in het systeem waar adapters worden opgezocht.
Queen en agents vragen de registry om een adapter — ze kennen de concrete
implementatie niet.

Regels:
  - register() overschrijft een bestaande adapter voor hetzelfde biome_id
    en logt een waarschuwing (hot-swap is toegestaan — bijv. adapter-upgrade)
  - get() retourneert None voor onbekende biomes — nooit een exception (fail-closed P2)
  - unregister() is idempotent — onbekend biome_id wordt genegeerd
  - Registry is bewust niet singleton — caller injecteert de instantie
    (testbaar, geen verborgen global state, P7)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import logging

from ant_colony.biome.biome_adapter import BiomeAdapter

logger = logging.getLogger(__name__)


class BiomeRegistry:
    """
    Centraal register van actieve biome-adapters.

    Usage::

        registry = BiomeRegistry()
        registry.register(CryptoAdapter(...))
        registry.register(EquitiesAdapter(...))

        adapter = registry.get("crypto")
        if adapter and adapter.is_available():
            data = adapter.get_market_data("BTC-EUR", "1h")
    """

    def __init__(self) -> None:
        self._adapters: dict[str, BiomeAdapter] = {}

    # ------------------------------------------------------------------
    # Registratie
    # ------------------------------------------------------------------

    def register(self, adapter: BiomeAdapter) -> None:
        """
        Registreer een biome-adapter.

        Overschrijft een bestaande adapter voor hetzelfde biome_id met een
        waarschuwing. Dit maakt hot-swap mogelijk zonder herstart.

        Args:
            adapter:  BiomeAdapter-implementatie. biome_id moet niet leeg zijn.
        """
        biome_id = adapter.biome_id
        if not biome_id:
            raise ValueError("adapter.biome_id must not be empty")

        if biome_id in self._adapters:
            logger.warning(
                "BiomeRegistry: overwriting existing adapter for biome_id='%s'", biome_id
            )

        self._adapters[biome_id] = adapter
        logger.debug("BiomeRegistry: registered adapter for biome_id='%s'", biome_id)

    def unregister(self, biome_id: str) -> None:
        """
        Verwijder een adapter uit het register.

        Idempotent: onbekend biome_id wordt genegeerd zonder fout.
        """
        if biome_id not in self._adapters:
            logger.debug(
                "BiomeRegistry: unregister called for unknown biome_id='%s' — ignored", biome_id
            )
            return
        del self._adapters[biome_id]
        logger.debug("BiomeRegistry: unregistered adapter for biome_id='%s'", biome_id)

    # ------------------------------------------------------------------
    # Opzoeken
    # ------------------------------------------------------------------

    def get(self, biome_id: str) -> BiomeAdapter | None:
        """
        Zoek een adapter op biome_id.

        Returns:
            BiomeAdapter als geregistreerd, anders None (fail-closed — nooit raises).
        """
        return self._adapters.get(biome_id)

    def is_registered(self, biome_id: str) -> bool:
        """True als er een adapter geregistreerd is voor biome_id."""
        return biome_id in self._adapters

    # ------------------------------------------------------------------
    # Overzicht
    # ------------------------------------------------------------------

    def list_biomes(self) -> list[str]:
        """Gesorteerde lijst van geregistreerde biome_ids."""
        return sorted(self._adapters.keys())

    @property
    def count(self) -> int:
        """Aantal geregistreerde adapters."""
        return len(self._adapters)
