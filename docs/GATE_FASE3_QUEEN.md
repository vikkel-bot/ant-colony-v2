# Gate-bewijs Fase 3 — Queen Governance

**Gate-ID:** `KILL_SWITCH_BEWEZEN_IN_SIMULATIE`
**Datum:** 2026-04-16
**Status:** GOEDGEKEURD
**Testresultaat:** 241/241 passed (0.52s)

---

## Gate-criteria

Fase 3 is afgesloten als bewezen is dat:

1. Queen missions uitgeeft met correcte kapitaalvalidatie (P1: Queen is soeverein)
2. Queen de kolonie-niveau kapitaalhiërarchie handhaaft
3. Kill-switch Level 1 (agent), Level 2 (node) en Level 3 (colony) correct werken
4. Level 3 kill de scheduler permanent halteert — geen automatische herstart
5. Audit trail compleet is na elke kill-actie
6. **Kill-switch bewezen in simulatie** — multi-agent scenario's end-to-end

---

## Testklassen en gedekte gevallen

### TestQueenCapital — 8 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_capital_total_set_at_init` | capital_total correct bij instantiatie |
| `test_capital_allocated_zero_before_missions` | 0 gealloceerd voor eerste mission |
| `test_capital_available_equals_total_before_missions` | available = total zonder missions |
| `test_capital_allocated_increases_after_issue` | capital_limit toegevoegd na issue |
| `test_capital_available_decreases_after_issue` | available = total − allocated |
| `test_capital_available_restored_after_revoke` | kapitaal vrijgegeven na revoke |
| `test_two_missions_allocate_combined_capital` | gecombineerde allocatie correct |
| `test_negative_capital_total_raises` | negatief kapitaal verboden bij instantiatie |

### TestQueenMissionIssuance — 11 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_valid_mission_accepted` | geldige mission → accepted=True |
| `test_accepted_mission_appears_in_active_missions` | mission zichtbaar in active_missions |
| `test_revoked_mission_removed_from_active` | mission verwijderd na revoke |
| `test_duplicate_mission_rejected` | zelfde mission_id → DUPLICATE_MISSION_ID |
| `test_capital_exceeded_rejected` | capital_limit > available → CAPITAL_EXCEEDED |
| `test_capital_exactly_available_is_accepted` | capital_limit == available → geaccepteerd |
| `test_mission_enqueued_in_scheduler` | scheduler._pending_missions bevat de mission |
| `test_revoke_unknown_mission_is_safe` | onbekende mission_id → geen exception |
| `test_mission_issued_log_written` | JSONL geschreven met event_type=mission_issued |
| `test_mission_rejected_log_written` | afwijzing gelogd met event_type=mission_rejected |
| `test_no_log_when_logs_root_none` | logs_root=None → geen exception, geen log |

### TestQueenKillSwitch — 8 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_kill_level1_aborts_target_agent` | Level 1: target agent → ABORTED |
| `test_kill_level1_leaves_other_agents_running` | Level 1: overige agents onberoerd |
| `test_kill_level2_aborts_all_agents_on_node` | Level 2: alle agents op node → ABORTED |
| `test_kill_level2_leaves_other_nodes_running` | Level 2: andere nodes onberoerd |
| `test_kill_level3_halts_colony` | Level 3: scheduler.status == HALTED |
| `test_kill_level3_aborts_all_agents` | Level 3: alle agents → ABORTED |
| `test_kill_level3_tick_skipped_after_halt` | Level 3: scheduler verwerkt geen ticks meer |
| `test_kill_event_logged` | kill_switch.jsonl bevat correct event |

### TestKillSwitchSimulation — 4 tests (gate-kern)

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_simulation_level3_halts_all` | 3 agents, 2 nodes → allemaal ABORTED, kolonie HALTED |
| `test_simulation_level1_selective` | 3 agents → alleen target ABORTED, rest RUNNING, kolonie RUNNING |
| `test_simulation_level2_node_targeted` | 4 agents, 2 nodes → node-a ABORTED, node-b RUNNING |
| `test_simulation_audit_trail_complete` | kill_switch.jsonl aanwezig, level correct, sequence ≥ 1 |

---

## Kill-switch gedrag bewezen

### Level 1 — Agent kill
```
queen.kill_switch(KillLevel.AGENT, scope="ant-2")
→ ant-2.status == ABORTED
→ ant-1.status == RUNNING   (onberoerd)
→ ant-3.status == RUNNING   (onberoerd)
→ sched.status == RUNNING   (kolonie draait door)
```
Bewezen: `test_kill_level1_aborts_target_agent`, `test_simulation_level1_selective`

### Level 2 — Node kill
```
queen.kill_switch(KillLevel.NODE, scope="node-a")
→ ant-1.status == ABORTED   (op node-a)
→ ant-2.status == ABORTED   (op node-a)
→ ant-3.status == RUNNING   (op node-b)
→ sched.status == RUNNING   (kolonie draait door)
```
Bewezen: `test_kill_level2_aborts_all_agents_on_node`, `test_simulation_level2_node_targeted`

### Level 3 — Colony kill
```
queen.kill_switch(KillLevel.COLONY)
→ alle agents.status == ABORTED
→ sched.status == HALTED
→ sched.tick() → geen actie (tick_sequence ongewijzigd)
→ kill_switch.jsonl geschreven
```
Bewezen: `test_kill_level3_halts_colony`, `test_kill_level3_aborts_all_agents`,
`test_kill_level3_tick_skipped_after_halt`, `test_simulation_level3_halts_all`

---

## Kapitaalhiërarchie bewezen (P1)

```
capital_total      = 50.000 EUR  (Operator, vast)
capital_allocated  = som(mission.capital_limit) over actieve missions
capital_available  = max(0, capital_total − capital_allocated)
```

| Situatie | Bewezen door |
|----------|--------------|
| Allocatie na issue | `test_capital_allocated_increases_after_issue` |
| Beschikbaarheid daalt | `test_capital_available_decreases_after_issue` |
| Herstel na revoke | `test_capital_available_restored_after_revoke` |
| Gecombineerde allocatie | `test_two_missions_allocate_combined_capital` |
| Overschrijding geblokkeerd | `test_capital_exceeded_rejected` |
| Exact beschikbaar geaccepteerd | `test_capital_exactly_available_is_accepted` |

---

## Audit trail bewezen (P4)

| Event | Log-pad | Test |
|-------|---------|------|
| `mission_issued` | `ANT_LOGS/missions/{mission_id}.jsonl` | `test_mission_issued_log_written` |
| `mission_rejected` | `ANT_LOGS/missions/{mission_id}.jsonl` | `test_mission_rejected_log_written` |
| `mission_aborted` (revoke) | `ANT_LOGS/missions/{mission_id}.jsonl` | (via revoke flow) |
| `kill_switch_activated` | `ANT_LOGS/colony/kill_switch.jsonl` | `test_kill_event_logged` |

Audit event structuur conform `AuditEvent` schema: `event_id`, `event_type`, `timestamp`,
`source="queen"`, `mission_id`, `payload`, `sequence` (monotoon oplopend).

---

## P1 — Queen soevereiniteit afgedwongen

De `Mission` schema valideert `issued_by="queen"` architectureel:
```python
@model_validator(mode="after")
def issued_by_must_be_queen(self) -> Mission:
    if self.issued_by != "queen":
        raise ValueError(...)
```

Een mission van een andere bron kan het systeem structureel niet bereiken.
Bewezen in Fase 0 via `test_issued_by_other_is_rejected` (74 schema tests).

---

## Totale testresultaten

```
tests/test_exit_chain.py         74 passed  (Fase 1)
tests/test_mission_validation.py 21 passed  (Fase 0)
tests/test_paper_loop.py         63 passed  (Fase 2)
tests/test_queen.py              31 passed  (Fase 3)
tests/test_scheduler_tick.py     27 passed  (Fase 0)
tests/test_schemas.py            25 passed  (Fase 0)
                               ──────────
Totaal:                         241/241 passed in 0.52s (0 failures, 0 errors)
```

---

## Gate-declaratie

```
GATE: KILL_SWITCH_BEWEZEN_IN_SIMULATIE
STATUS: GOEDGEKEURD
DATUM: 2026-04-16
TESTS: 241/241 passed
FASE 3 AFGESLOTEN — KLAAR VOOR FASE 4
```

Fase 4: Strategy Lab (research only).
