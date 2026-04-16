"""
ant_colony/queen/allocator.py

AllocationPlan en AllocationSnapshot — formeel kapitaalallocatiemodel voor de Queen.

Probleem dat dit oplost:
  Biome-limieten werden tot Fase 5 als losse absolute bedragen ingesteld.
  Bij meerdere biomes is er geen garantie dat de som van alle limieten het
  colony-kapitaal niet meervoudig overschrijdt, en er is geen gestructureerd
  overzicht van de huidige allocatiestaat.

Oplossing:
  AllocationPlan — gewenste verdeling als fracties van colony_total.
    Voordelen:
      - Som van fracties ≤ 1.0 is afdwingbaar bij constructie.
      - Schaling is automatisch: bij wijziging van colony_total hoeft alleen
        het plan opnieuw te worden toegepast.
      - Eén enkel object beschrijft de volledige biome-verdeling.

  AllocationSnapshot — point-in-time weergave van de werkelijke allocatie.
    Geeft per biome: limiet, gealloceerd, beschikbaar, utilisatie-%.
    Plus colony-totalen. Dient als observability primitief (P4).

Regels:
  - Fracties zijn (0, 1] per biome; som over alle biomes ≤ 1.0
  - Unallocated fractie = 1.0 − som(fracties) — reservekapitaal, niet gebonden
  - AllocationPlan is immutable na constructie (Pydantic)
  - AllocationSnapshot wordt altijd vers berekend — nooit gecached
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# AllocationPlan
# ---------------------------------------------------------------------------

class AllocationPlan(BaseModel):
    """
    Gewenste kapitaalverdeling over biomes als fracties van colony_total.

    allocations:  Mapping van biome_id → fractie (0, 1].
                  Som van alle fracties moet ≤ 1.0 zijn.
                  De resterende fractie (1.0 − som) blijft ongebonden
                  reservekapitaal op colony-niveau.

    Validaties:
      - allocations is niet leeg — een leeg plan is zinloos
      - Elke fractie is > 0 en ≤ 1.0
      - Som van fracties is ≤ 1.0

    Usage::

        plan = AllocationPlan(allocations={
            "crypto":      0.50,
            "equities":    0.30,
            "commodities": 0.10,
        })
        # 10% blijft ongebonden reservekapitaal
    """

    allocations: dict[str, float] = Field(
        min_length=1,
        description="biome_id → fractie van colony_total (0, 1]",
    )

    @model_validator(mode="after")
    def validate_allocations(self) -> AllocationPlan:
        for biome_id, fraction in self.allocations.items():
            if fraction <= 0:
                raise ValueError(
                    f"fraction for biome '{biome_id}' must be > 0, got {fraction}"
                )
            if fraction > 1.0:
                raise ValueError(
                    f"fraction for biome '{biome_id}' must be ≤ 1.0, got {fraction}"
                )

        total = sum(self.allocations.values())
        if total > 1.0 + 1e-9:  # kleine float-tolerantie
            raise ValueError(
                f"sum of fractions must be ≤ 1.0, got {total:.6f}"
            )
        return self

    @property
    def total_fraction(self) -> float:
        """Som van alle toegewezen fracties."""
        return sum(self.allocations.values())

    @property
    def unallocated_fraction(self) -> float:
        """Fractie die niet gebonden is aan een biome (reservekapitaal)."""
        return max(0.0, 1.0 - self.total_fraction)

    def capital_for(self, biome_id: str, colony_total: float) -> float | None:
        """
        Berekende absolute limiet voor biome_id gegeven colony_total.

        Returns:
            float als biome_id in plan staat, anders None.
        """
        fraction = self.allocations.get(biome_id)
        if fraction is None:
            return None
        return fraction * colony_total


# ---------------------------------------------------------------------------
# AllocationResult
# ---------------------------------------------------------------------------

class AllocationResult(BaseModel):
    """
    Resultaat van Queen.apply_allocation_plan().

    applied:         True als het plan volledig is doorgevoerd.
    biome_limits:    Berekende absolute limieten per biome_id.
    rejection_reason: Reden bij afwijzing (leeg bij acceptatie).
    """

    applied: bool
    biome_limits: dict[str, float] = Field(default_factory=dict)
    rejection_reason: str = ""

    @classmethod
    def rejected(cls, reason: str) -> AllocationResult:
        return cls(applied=False, rejection_reason=reason)


# ---------------------------------------------------------------------------
# BiomeAllocationState
# ---------------------------------------------------------------------------

class BiomeAllocationState(BaseModel):
    """
    Allocatiestaat van één biome op één moment.

    biome_id:        Biome-identifier.
    limit:           Ingestelde limiet, of None als onbeperkt.
    allocated:       Gealloceerd kapitaal (som actieve missions).
    available:       Beschikbaar kapitaal, of None als onbeperkt.
    utilization_pct: Benutting als percentage van de limiet (None als onbeperkt).
    """

    biome_id: str
    limit: float | None
    allocated: float
    available: float | None
    utilization_pct: float | None


# ---------------------------------------------------------------------------
# AllocationSnapshot
# ---------------------------------------------------------------------------

class AllocationSnapshot(BaseModel):
    """
    Point-in-time weergave van de volledige kapitaalallocatie.

    Geeft inzicht in colony-totalen en per-biome staat.
    Wordt altijd vers berekend — nooit gecached (P4: observability).

    colony_total:     Totaal beschikbaar colony-kapitaal.
    colony_allocated: Gealloceerd over alle actieve missions.
    colony_available: Vrij colony-kapitaal.
    biomes:           Per-biome allocatiestaat (alleen biomes met een limiet).
    """

    colony_total: float
    colony_allocated: float
    colony_available: float
    biomes: list[BiomeAllocationState] = Field(default_factory=list)

    @property
    def colony_utilization_pct(self) -> float | None:
        """Colony-utilisatie als percentage. None als colony_total == 0."""
        if self.colony_total == 0:
            return None
        return (self.colony_allocated / self.colony_total) * 100.0

    def biome(self, biome_id: str) -> BiomeAllocationState | None:
        """Zoek de staat van één biome op naam. None als niet aanwezig."""
        for b in self.biomes:
            if b.biome_id == biome_id:
                return b
        return None
