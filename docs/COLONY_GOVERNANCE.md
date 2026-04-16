# COLONY GOVERNANCE

*Dit document beschrijft hoe beslissingen worden genomen, wie wat mag autoriseren,
hoe de promotieketen werkt en wat er gebeurt als het systeem faalt.*

---

## 1. Gezagshiërarchie

```
Operator (mens)
    │  ← enige die doctrine en governance mag wijzigen
    ▼
  Queen
    │  ← enige die missions uitgeeft, kapitaal toewijst, promoveert
    ▼
  Node
    │  ← geautoriseerde runtime, scoped permissions
    ▼
  Ant
       ← task-bound, mag nooit zelf autoriseren
```

Elke laag kan alleen handelen binnen de grenzen die de laag erboven heeft
vastgelegd. Omzeilen van een laag is niet mogelijk — architectureel afgedwongen,
niet alleen afgesproken.

---

## 2. Autorisatieregels

### Wat alleen de Operator mag:
- Doctrine en governance documenten wijzigen
- Nieuwe fase activeren
- PC2 als productie-Node goedkeuren
- De Queen zelf configureren of resetten
- v1 Sacred modules aanpassen

### Wat alleen de Queen mag:
- Missions uitgeven aan agents
- Kapitaal toewijzen en limieten instellen
- Nodes toevoegen aan trusted set
- StrategyCandidate promoveren (Research → Paper → Live)
- Kill-switch activeren op colony niveau
- Agents intrekken of pauzeren

### Wat Nodes mogen (binnen Queen-scope):
- Agents hosten die door de Queen zijn geautoriseerd
- Heartbeat rapporteren
- Scoped acties uitvoeren conform Mission
- Append-only logs schrijven

### Wat Agents mogen (binnen Mission-scope):
- Acties uit `allowed_actions` uitvoeren
- State rapporteren via artifact files
- StrategyCandidate voorstellen (nooit zelf promoveren)
- Heartbeat sturen

### Wat niemand mag:
- Kapitaallimieten verhogen zonder Queen actie
- Live execution starten zonder goedgekeurde Mission met `execution_ant` type
- Externe code direct uitvoeren (altijd normaliseren naar intern schema)
- Logs overschrijven of verwijderen
- Zichzelf installeren op niet-geautoriseerde nodes

---

## 3. Mission lifecycle

```
Queen geeft Mission uit
        │
        ▼
Ant ontvangt Mission → valideert Mission schema
        │
        ▼
Ant rapporteert: mission_accepted + first heartbeat
        │
        ▼
Ant voert uit binnen sandbox
  ├── heartbeat elke N seconden
  ├── state via append-only artifact files
  └── abort bij elke abort_condition hit
        │
        ▼
Mission eindigt via:
  ├── success_conditions bereikt → completed
  ├── abort_conditions getriggerd → aborted
  ├── TTL verlopen → aborted
  └── Queen kill-switch → terminated
        │
        ▼
Ant schrijft final report → Queen verwerkt resultaat
```

### Mission validatieregels (bij ontvangst door Ant):
1. `mission_id` aanwezig en uniek
2. `ant_type` overeenkomend met eigen type
3. `allowed_node` overeenkomend met huidige node
4. `abort_conditions` aanwezig en niet leeg
5. `ttl` groter dan nul
6. `heartbeat_interval` groter dan nul en kleiner dan `ttl`
7. `capital_limit` aanwezig (mag nul zijn voor non-execution missions)
8. `issued_by` gelijk aan `queen`

Bij validatiefout: weiger mission, log reden, rapporteer aan Queen.

---

## 4. StrategyCandidate promotieketen

De promotieketen is strikt lineair en nooit over te slaan.

```
RESEARCH
  research_ant vindt/bouwt strategie
  normaliseert naar intern schema
  voert backtest uit
  schrijft StrategyCandidate(status=research)
        │
        ▼  ← Queen beoordeelt: geschikt voor paper?
PAPER
  paper_ant voert walk-forward validatie uit
  paper_ant draait paper trading (minimaal N trades)
  schrijft StrategyCandidate(status=paper, paper_results=...)
        │
        ▼  ← Queen beoordeelt: voldoet aan fitness criteria?
APPROVED
  Queen schrijft status=approved, approved_by=queen
  Kandidaat wacht op deployment beslissing
        │
        ▼  ← Queen besluit: live deployment
LIVE
  execution_ant krijgt Mission met strategy_id
  execution_ant handelt via guarded execution gate
  status=live
```

### Promotiecriteria (minimaal, door Queen te configureren):
- Research → Paper: backtest sharpe ≥ drempel, max drawdown ≤ drempel, N≥ min trades
- Paper → Approved: paper sharpe ≥ drempel, paper drawdown ≤ drempel, ≥ 50 paper trades
- Approved → Live: operator goedkeuring + Queen kill-switch bewezen in simulatie

### Demotie:
Een StrategyCandidate kan worden afgewezen (`status=rejected`) door de Queen op
elk moment. Afgewezen candidates worden nooit verwijderd — ze blijven in het
audit trail met reden van afwijzing.

---

## 5. Heartbeat protocol

Heartbeat is de primaire health-check voor alle actieve agents.

**Regels:**
- Elke Ant stuurt een heartbeat elke `heartbeat_interval` seconden
- De scheduler controleert heartbeats bij elke tick
- Een heartbeat wordt als stale beschouwd na `heartbeat_interval * 2` seconden
- Stale heartbeat → agent wordt als dood beschouwd → abort_condition getriggerd

**Heartbeat payload:**
```
{
  "ant_id": str,
  "mission_id": str,
  "node_id": str,
  "timestamp": ISO8601,
  "status": "running" | "paused",
  "budget_used": float,
  "last_action": str
}
```

**Heartbeat opslag:** append-only naar `C:\Trading\ANT_LOGS\heartbeats\{ant_id}.jsonl`

---

## 6. Kill-switch protocol

De kill-switch is de noodstop van het systeem. Er zijn drie niveaus.

### Level 1 — Agent kill
- Scope: één specifieke agent
- Trigger: Queen of operator
- Effect: agent ontvangt stop-signaal → schrijft final state → stopt
- Logs: blijven intact

### Level 2 — Node kill
- Scope: alle agents op één node
- Trigger: Queen of operator
- Effect: alle agents op de node ontvangen stop-signaal
- Open posities: worden gesloten via execution gate (indien live)
- Logs: blijven intact

### Level 3 — Colony kill
- Scope: het gehele systeem
- Trigger: alleen operator
- Effect: alle agents stoppen, scheduler pauzeert, geen nieuwe missions
- Open posities: worden gesloten via execution gate (indien live)
- Status: `COLONY_HALTED` — systeem herstart niet automatisch

**Colony kill vereist altijd handmatige herstart door operator.**

### Kill-switch fail-safe:
Als een agent niet reageert op een kill-signaal binnen `heartbeat_interval * 3`
seconden, wordt de agent als zombie beschouwd en de scheduler markeert hem als
`terminated_by_timeout`. De scheduler voert geen verdere acties uit namens de
zombie-agent.

---

## 7. Risk governance

### Kapitaalhiërarchie:
```
Colony total capital (door Operator vastgesteld)
    └── per Biome capital_limit (door Queen vastgesteld)
            └── per Mission capital_limit (door Queen vastgesteld)
                    └── per trade position_size (door Ant berekend binnen Mission)
```

Elke laag mag nooit de limiet van de laag erboven overschrijden.
Limieten kunnen alleen omhoog worden bijgesteld door de laag erboven.

### Risk breach protocol:
1. Ant detecteert risk breach (drawdown, positiegrootte, dagverlies)
2. Ant stopt onmiddellijk met nieuwe acties
3. Open posities: exit via normale exit-logica (niet geforceerd, tenzij level-2 breach)
4. Ant schrijft risk_breach event naar audit trail
5. Ant rapporteert aan Queen
6. Mission status → `aborted_risk_breach`
7. Queen beslist over herstart (nooit automatisch)

### Verplichte risk parameters in elke Mission:
- `max_drawdown_pct` — maximaal relatief verlies
- `max_position_size` — maximale positiegrootte in basismunt
- `daily_loss_limit` — maximaal dagverlies
- `stop_loss_required` — boolean, altijd `true` voor execution missions

---

## 8. Audit trail vereisten

Elk significant event wordt gelogd. Logs zijn append-only en nooit verwijderd.

### Verplichte audit events:
| Event | Wie logt | Locatie |
|-------|----------|---------|
| Mission issued | Queen | `ANT_LOGS\missions\{mission_id}.jsonl` |
| Mission accepted/rejected | Ant | `ANT_LOGS\missions\{mission_id}.jsonl` |
| Heartbeat | Ant | `ANT_LOGS\heartbeats\{ant_id}.jsonl` |
| Action executed | Ant | `ANT_LOGS\actions\{ant_id}.jsonl` |
| Risk breach | Ant | `ANT_LOGS\risk\{ant_id}.jsonl` |
| Mission completed/aborted | Ant | `ANT_LOGS\missions\{mission_id}.jsonl` |
| Kill-switch activated | Queen/Operator | `ANT_LOGS\colony\kill_switch.jsonl` |
| StrategyCandidate promoted | Queen | `ANT_LOGS\strategy\{candidate_id}.jsonl` |
| Node heartbeat stale | Scheduler | `ANT_LOGS\colony\scheduler.jsonl` |

### Audit event format:
```json
{
  "event_id": "uuid4",
  "event_type": "string",
  "timestamp": "ISO8601",
  "source": "ant_id | queen | scheduler | operator",
  "mission_id": "string | null",
  "node_id": "string | null",
  "payload": {},
  "sequence": 0
}
```

`sequence` is een monotoon oplopend getal per log file. Gaten in de sequence
worden als audit anomalie beschouwd.

---

## 9. Scheduler governance

De scheduler is infrastructuur — hij draait altijd en is verantwoordelijk voor:
- Heartbeat monitoring van alle actieve agents
- TTL handhaving
- Abort condition polling
- Mission dispatch
- Colony status bewaking

**Regels:**
- Scheduler start automatisch na `git pull` op PC2
- Scheduler pauzeert bij Colony kill (Level 3)
- Scheduler herstart nooit automatisch na Colony kill
- Scheduler logt elke tick naar `ANT_LOGS\colony\scheduler.jsonl`
- Scheduler heeft geen eigen kapitaal of mission authority

---

## 10. Fase-gate protocol

Elke fase heeft een expliciete gate die door de operator wordt bevestigd.
Geen automatische fase-overgang.

**Gate bevestiging vereist:**
1. Alle deliverables van de fase aanwezig en getest
2. Gate condition expliciet bewezen (niet alleen aangenomen)
3. Operator bevestigt schriftelijk in git commit message of issue

**Gate conditions:**
| Fase | Gate condition |
|------|----------------|
| 0 | `DOCTRINE_GOEDGEKEURD` |
| 1 | `EXIT_KETEN_VOLLEDIG_CORRECT` |
| 2 | `50_PAPER_TRADES_BEWEZEN` |
| 3 | `KILL_SWITCH_BEWEZEN_IN_SIMULATIE` |
| 4–8 | Zie faseoverzicht in doctrine |

---

*Laatste update: Fase 0 initialisatie*
*Dit document wordt bijgehouden bij elke fase-overgang door de operator.*
