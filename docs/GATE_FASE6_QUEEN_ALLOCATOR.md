# Gate Fase 6 — Queen Allocator Upgrade

**Gate-conditie:** `QUEEN_ALLOCATOR_UPGRADE_BEWEZEN`
**Status:** BEWEZEN
**Datum:** 2026-04-16
**Testresultaat:** 403/403 passed

---

## Wat bewijst dit gate

Fase 6 voegt een formeel kapitaalallocatiemodel toe aan de Queen. Biome-limieten
kunnen nu worden uitgedrukt als fracties van het colony-kapitaal via een
AllocationPlan, en de volledige allocatiestaat is op elk moment inzichtelijk
via een AllocationSnapshot.

---

## Geleverde deliverables

### D1 — `ant_colony/queen/allocator.py`
- `AllocationPlan` (Pydantic, immutable):
  - Mapping biome_id → fractie (0, 1]
  - Valideert elke fractie en som ≤ 1.0
  - `total_fraction`, `unallocated_fraction` properties
  - `capital_for(biome_id, colony_total)` — berekent absolute limiet
- `AllocationResult` — resultaat van `apply_allocation_plan()`:
  - `applied: bool`, `biome_limits: dict[str, float]`, `rejection_reason: str`
  - `AllocationResult.rejected(reason)` classmethod
- `BiomeAllocationState` — staat van één biome:
  - limit, allocated, available, utilization_pct (alle nullable voor onbeperkte biomes)
- `AllocationSnapshot` — point-in-time weergave:
  - colony_total, colony_allocated, colony_available
  - `colony_utilization_pct` property (None als colony_total == 0)
  - `biome(biome_id)` lookup — None als niet aanwezig

### D2 — `ant_colony/queen/queen.py` uitgebreid
- `apply_allocation_plan(plan: AllocationPlan) → AllocationResult`:
  - Berekent alle absolute limieten vooraf (fraction × capital_total)
  - Atomair: valideert alles vóór toepassen — alles of niets
  - Biomes buiten het plan worden niet geraakt (partiële updates toegestaan)
  - Interne `_biome_limits` direct bijgewerkt (omzeilt redundante validatie)
- `allocation_snapshot() → AllocationSnapshot`:
  - Altijd vers berekend — nooit gecached (P4: observability)
  - Biomes gesorteerd op biome_id
  - Utilization_pct = allocated / limit × 100; 0% als limit == 0

### D3 — `ant_colony/queen/__init__.py` uitgebreid
- Exporteert: `AllocationPlan`, `AllocationResult`, `AllocationSnapshot`,
  `BiomeAllocationState`

### D4 — `tests/test_allocator.py`
49 nieuwe tests verdeeld over vijf klassen:

| Klasse | Tests |
|--------|-------|
| TestAllocationPlan | 16 |
| TestAllocationResult | 3 |
| TestAllocationSnapshot | 8 |
| TestQueenApplyPlan | 10 |
| TestQueenSnapshot | 12 |

---

## Gate-bewijs: kerngedrag aangetoond

### 1. Fractie-validatie
```
TestAllocationPlan::test_fraction_zero_raises               PASSED
TestAllocationPlan::test_fraction_negative_raises           PASSED
TestAllocationPlan::test_fraction_above_one_raises          PASSED
TestAllocationPlan::test_sum_above_one_raises               PASSED
TestAllocationPlan::test_fraction_exactly_one_accepted      PASSED
TestAllocationPlan::test_empty_allocations_raises           PASSED
```
Elke grenswaarde is getest; zowel te laag als te hoog wordt afgewezen.

### 2. Schaling naar colony_total
```
TestAllocationPlan::test_capital_for_scales_with_colony_total    PASSED
TestQueenApplyPlan::test_apply_plan_scales_with_capital_total    PASSED
TestQueenApplyPlan::test_apply_plan_zero_capital_total_sets_zero_limits  PASSED
```
`capital_for()` en `apply_allocation_plan()` schalen correct mee bij elke
colony_total, inclusief 0.

### 3. Partiële update — unlisted biomes onaangetast
```
TestQueenApplyPlan::test_apply_plan_does_not_touch_unlisted_biomes  PASSED
```
Een plan met alleen "crypto" laat een bestaande "commodities"-limiet intact.

### 4. Hertoepassing — tweede plan wint
```
TestQueenApplyPlan::test_apply_plan_twice_second_wins        PASSED
TestQueenApplyPlan::test_apply_plan_overwrites_existing_limit  PASSED
```
Hot-swap van allocatieplan is mogelijk zonder herstart.

### 5. Missions respecteren nieuwe limieten
```
TestQueenApplyPlan::test_apply_plan_issue_mission_respects_new_limit  PASSED
```
Na apply_allocation_plan blokkeert issue_mission() missions die de nieuwe
biome-limiet overschrijden.

### 6. Snapshot is altijd vers
```
TestQueenSnapshot::test_snapshot_is_freshly_computed         PASSED
TestQueenSnapshot::test_snapshot_fresh_after_revoke          PASSED
```
Twee opeenvolgende snapshots geven verschillende waarden na een mutatie.
Revoke herstelt allocated naar 0.

### 7. Utilization correctheid
```
TestQueenSnapshot::test_snapshot_biome_utilization_pct_after_mission  PASSED
TestQueenSnapshot::test_snapshot_colony_utilization_pct               PASSED
TestAllocationSnapshot::test_colony_utilization_pct_none_when_total_zero  PASSED
```
25k allocated op 50k limiet = 50% utilisatie. Geen deling door nul bij
colony_total = 0.

### 8. Gesorteerde biomes
```
TestQueenSnapshot::test_snapshot_biomes_sorted_by_biome_id   PASSED
```
Snapshot geeft biomes altijd alfabetisch gesorteerd terug.

---

## Totale teststand na Fase 6

| Fase | Tests |
|------|-------|
| Fase 1-2 (kolonie-kern) | 157 |
| Fase 3 (Queen governance) | 31 |
| Fase 4 (Strategy Lab) | 98 |
| Fase 5 (Multi-Biome) | 68 |
| Fase 6 (Queen Allocator) | 49 |
| **Totaal** | **403** |

```
403 passed in 0.67s
```

---

**Gate gesloten.** `QUEEN_ALLOCATOR_UPGRADE_BEWEZEN`
