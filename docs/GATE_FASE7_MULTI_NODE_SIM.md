# Gate Fase 7 — Paper-mode multi-node kolonie simulatie

**Gate-conditie:** `MULTI_NODE_KOLONIE_SIMULATIE_BEWEZEN`
**Status:** BEWEZEN
**Datum:** 2026-04-16
**Testresultaat:** 487/487 passed

---

## Wat bewijst dit gate

Fase 7 introduceert node-governance in de Queen en de eerste volledige
multi-node kolonie simulatie in paper mode. Meerdere PaperLoop-instanties
draaien gelijktijdig op eigen nodes, elk met eigen kapitaal en missie.
De Queen is de enige autoriteit die nodes registreert en missions valideert.

---

## Geleverde deliverables

### D1 — `ant_colony/colony/node_registry.py`
- `NodeRegistry` — centraal register van vertrouwde nodes (geen singleton, P7)
- `register(node)` — hot-swap met waarschuwing; lege node_id raises
- `unregister(node_id)` — idempotent
- `get(node_id)` — None voor onbekend (fail-closed, P2)
- `is_trusted(node_id)` — True alleen als bekend én `ACTIVE` (primaire poort)
- `can_run_ant(node_id, ant_type)` — `is_trusted` + `ant_type in allowed_ant_types`
- `can_run_biome(node_id, biome_id)` — `is_trusted` + `biome_id in allowed_biomes`
- `set_status(node_id, status)` — idempotent; onbekend node_id genegeerd
- `record_heartbeat(node_id)` — zet `last_heartbeat` + status terug op `ACTIVE`
- `list_nodes()` / `list_trusted()` — gesorteerde lijsten

### D2 — `ant_colony/queen/queen.py` uitgebreid
- `MissionRejectionReason` uitgebreid met:
  - `NODE_NOT_TRUSTED` — node onbekend of niet `ACTIVE`
  - `ANT_TYPE_NOT_ALLOWED` — ant_type niet in `allowed_ant_types`
  - `BIOME_NOT_ALLOWED_ON_NODE` — biome niet in `allowed_biomes`
- `_node_registry: NodeRegistry` toegevoegd in `__init__`
- `register_node(node)` / `unregister_node(node_id)` / `trusted_nodes()`
- `issue_mission()` heeft drie nieuwe checks vóór kapitaalvalidatie:
  1. Node bekend en `ACTIVE` → anders `NODE_NOT_TRUSTED`
  2. `ant_type` in `allowed_ant_types` → anders `ANT_TYPE_NOT_ALLOWED`
  3. `biome` in `allowed_biomes` → anders `BIOME_NOT_ALLOWED_ON_NODE`
- Bestaande test-fixtures bijgewerkt met standaard node-registratie

### D3 — `ant_colony/colony/multi_node_sim.py`
- `NodeSlot` — intern: node_id + mission + PaperLoop
- `NodeReport` — resultaten per node: ticks, trades, PnL, kapitaal, `return_pct`
- `SimulationReport` — `total_pnl`, `total_trades`, `active_nodes`, `node()` lookup
- `MultiNodeSimulator`:
  - `add_node(node_id, mission)` — Queen valideert mission; bouwt PaperLoop
    met `StopLossCondition`, `TakeProfitCondition`, `TTLCondition`,
    `DrawdownCondition` (max_drawdown_pct uit mission.risk_limits)
  - `run(prices, signals)` — één tick = alle nodes verwerken dezelfde prijs;
    stale prijs (≤ 0) slaat alle nodes over; gesorteerde NodeReports

### D4 — `tests/test_node_registry.py`
50 tests verdeeld over vier klassen:

| Klasse | Tests |
|--------|-------|
| TestNodeRegistryBasic | 13 |
| TestNodeRegistryTrust | 15 |
| TestNodeRegistryStatus | 7 |
| TestQueenNodeGovernance | 15 |

### D5 — `tests/test_multi_node_sim.py`
34 tests verdeeld over drie klassen:

| Klasse | Tests |
|--------|-------|
| TestSimulationReport | 9 |
| TestMultiNodeSimSetup | 8 |
| TestMultiNodeSimRun | 17 |

---

## Gate-bewijs: kerngedrag aangetoond

### 1. Fail-closed node trust
```
TestNodeRegistryTrust::test_unknown_node_is_not_trusted          PASSED
TestNodeRegistryTrust::test_stale_node_is_not_trusted            PASSED
TestNodeRegistryTrust::test_suspended_node_is_not_trusted        PASSED
TestQueenNodeGovernance::test_no_registered_node_rejects_mission PASSED
```
`is_trusted()` is False voor elk niet-`ACTIVE` geval. Queen weigert missions
zonder vertrouwde node.

### 2. Scoped permissions per node
```
TestNodeRegistryTrust::test_can_run_ant_disallowed_type          PASSED
TestNodeRegistryTrust::test_can_run_biome_disallowed             PASSED
TestQueenNodeGovernance::test_ant_type_not_allowed_rejects       PASSED
TestQueenNodeGovernance::test_biome_not_allowed_on_node_rejects  PASSED
TestQueenNodeGovernance::test_multiple_nodes_each_scoped         PASSED
```
Elke node heeft zijn eigen `allowed_ant_types` en `allowed_biomes`; overtreding
levert de correcte `MissionRejectionReason`.

### 3. Volgorde van checks in issue_mission
```
TestQueenNodeGovernance::test_node_check_before_capital_check    PASSED
```
`NODE_NOT_TRUSTED` gaat vóór `CAPITAL_EXCEEDED` — node-governance is de
eerste poort.

### 4. Hot-swap node update
```
TestNodeRegistryBasic::test_register_overwrites_existing         PASSED
TestQueenNodeGovernance::test_hot_swap_node_updates_permissions  PASSED
```
Na een tweede `register_node()` voor dezelfde node_id gelden de nieuwe
permissions onmiddellijk.

### 5. Heartbeat herstelt vertrouwen
```
TestNodeRegistryStatus::test_record_heartbeat_sets_active        PASSED
TestNodeRegistryStatus::test_record_heartbeat_sets_timestamp     PASSED
```
`record_heartbeat()` zet status terug op `ACTIVE` en registreert timestamp.

### 6. Multi-node simulatie — onafhankelijkheid
```
TestMultiNodeSimRun::test_two_nodes_independent_pnl              PASSED
TestMultiNodeSimRun::test_two_nodes_ticks_run_equal              PASSED
```
Twee nodes met identieke missions en signalen produceren dezelfde PnL
(eigen ledger per node).

### 7. Stale prijs — alle nodes overgeslagen
```
TestMultiNodeSimRun::test_stale_price_counted                    PASSED
TestMultiNodeSimRun::test_stale_price_not_counted_as_node_tick   PASSED
TestMultiNodeSimRun::test_all_stale_prices_no_trades             PASSED
```
Stale prijs (≤ 0) telt mee in `stale_ticks` maar niet in `ticks_run` per node.

### 8. Queen als enige autoriteit bij add_node
```
TestMultiNodeSimSetup::test_add_node_unregistered_raises         PASSED
TestMultiNodeSimSetup::test_add_node_stale_node_raises           PASSED
TestMultiNodeSimSetup::test_add_node_capital_exceeded_raises     PASSED
```
`add_node()` delegeert volledig aan Queen; elke Queen-weigering resulteert
in een `ValueError`.

### 9. Take-profit én stop-loss bewezen in simulatie
```
TestMultiNodeSimRun::test_trade_pnl_positive_on_take_profit      PASSED
TestMultiNodeSimRun::test_stop_loss_closes_position              PASSED
```
Beide exit-condities sluiten posities correct in een multi-node context.

---

## Totale teststand na Fase 7

| Fase | Tests |
|------|-------|
| Fase 1-2 (kolonie-kern) | 157 |
| Fase 3 (Queen governance) | 31 |
| Fase 4 (Strategy Lab) | 98 |
| Fase 5 (Multi-Biome) | 68 |
| Fase 6 (Queen Allocator) | 49 |
| Fase 7 (Multi-Node Sim) | 84 |
| **Totaal** | **487** |

```
487 passed in 0.78s
```

---

**Gate gesloten.** `MULTI_NODE_KOLONIE_SIMULATIE_BEWEZEN`
