# COLONY OBJECT MODEL

*Dit document beschrijft de kernobjecten van ANT COLONY v2: hun verantwoordelijkheden,
relaties en grenzen. Het is de blauwdruk voor alle implementatie.*

---

## Objecthiërarchie

```
Queen
 └── Node (goedgekeurde infrastructuur)
      └── Ant (agent, task-bound)
           └── Mission (opdracht + constraints)

Biome (marktdomein, orthogonaal aan bovenstaande)
StrategyCandidate (research artifact, nooit directe execution)
```

---

## Queen

**Rol:** Capital allocator, mission issuer, promotie gatekeeper, kill-switch authority.

De Queen is de enige soevereine entiteit in het systeem. Ze delegeert taken via
Missions aan Ants, maar behoudt altijd de eindbeslissing over kapitaal, deployment
en promotie naar live.

**Verantwoordelijkheden:**
- Kapitaal toewijzen aan missions
- Missions uitgeven aan agents
- Nodes goedkeuren en vertrouwen
- StrategyCandidate promotie beoordelen (Research → Paper → Live)
- Kill-switch uitvoeren op colony niveau
- Audit trail bewaken

**Mag als enige:**
- Deployen naar nodes
- Kapitaallimieten instellen en aanpassen
- Agents promoveren naar live execution
- Nodes toevoegen aan trusted set
- Policies aanpassen

**Weet niet:**
- Welke exchange achter een biome zit (alleen biome + risicoprofiel)
- Implementatiedetails van adapters

**State:**
- Lijst van actieve nodes (met heartbeat status)
- Lijst van actieve missions
- Kapitaalverdeling per biome
- StrategyCandidate pipeline
- Kill-switch status

---

## Node

**Rol:** Gecontroleerde habitat op goedgekeurde infrastructuur.

Een Node is een geautoriseerde runtime omgeving. Momenteel is PC2 de enige
productie-Node. Nodes worden door de Queen vertrouwd, niet door agents zelf.

**Eigenschappen:**
- `node_id` — unieke identifier
- `hostname` — machine identifier
- `allowed_biomes` — welke biomes hier mogen draaien
- `allowed_ant_types` — welke agent types hier mogen instantiëren
- `heartbeat_interval` — verwacht heartbeat interval in seconden
- `last_heartbeat` — timestamp van laatste heartbeat
- `status` — `active` | `stale` | `suspended` | `untrusted`
- `runtime_paths` — absolute paden voor output, live artifacts, logs

**Regels:**
- Scoped permissions — een Node mag alleen wat expliciet toegestaan is
- Heartbeat vereist — stale heartbeat → agent stop
- Append-only logs — nooit overschrijven
- Mag zichzelf nooit expanderen of autoriseren

**Huidige productie-Node (PC2):**
```
node_id:          pc2-desktop
hostname:         DESKTOP (PC2)
runtime_paths:
  output:         C:\Trading\ANT_OUT
  live:           C:\Trading\ANT_LIVE
  logs:           C:\Trading\ANT_LOGS
```

---

## Ant (Agent)

**Rol:** Task-bound agent met TTL, budget en rapportageplicht aan Queen.

Ants zijn kortstondige uitvoerders. Ze krijgen een Mission, werken binnen de
grenzen van die Mission, en rapporteren terug. Ze houden geen permanente state bij —
state is altijd extern (artifact files, logs).

**Agent types:**

| Type | Verantwoordelijkheid |
|------|----------------------|
| `scout_ant` | Marktkansen detecteren, signalen genereren |
| `research_ant` | Strategieën zoeken, normaliseren naar intern schema |
| `paper_ant` | Backtesten, walk-forward validatie, paper trading |
| `execution_ant` | Live execution via guarded gate (Fase 8+) |
| `audit_ant` | PnL verificatie, audit trail bewaking, anomalie detectie |

**Eigenschappen:**
- `ant_id` — unieke identifier per instantie
- `ant_type` — één van de bovenstaande types
- `mission_id` — verwijzing naar actieve Mission
- `node_id` — node waarop de ant draait
- `status` — `idle` | `running` | `paused` | `completed` | `aborted`
- `started_at` — timestamp
- `ttl` — maximale levensduur in seconden
- `budget_used` — verbruikt kapitaal (paper of live)

**Regels:**
- Altijd task-bound — geen agent zonder Mission
- TTL is hard — overschrijden → abort
- Rapporteert state via append-only artifact files
- Mag NOOIT zelfstandig live execution starten
- Mag NOOIT eigen TTL of budget verhogen

---

## Mission

**Rol:** Opdracht + constraints die een Ant autoriseert om te handelen.

Een Mission is de enige manier waarop een Ant geautoriseerd wordt. Zonder geldige
Mission mag een Ant niets doen. De Mission definieert de volledige sandbox.

**Schema:**

| Veld | Type | Beschrijving |
|------|------|--------------|
| `mission_id` | str | Unieke identifier |
| `ant_type` | str | Welk agent type wordt uitgegeven |
| `allowed_node` | str | Op welke node de ant mag draaien |
| `allowed_actions` | list[str] | Expliciete whitelist van toegestane acties |
| `market_scope` | dict | Welke markten/symbolen in scope zijn |
| `capital_limit` | float | Maximaal in te zetten kapitaal |
| `risk_limit` | dict | Drawdown, positiegrootte, stop-loss limieten |
| `ttl` | int | Maximale levensduur in seconden |
| `heartbeat_interval` | int | Verwacht heartbeat interval in seconden |
| `success_conditions` | dict | Wanneer de mission als geslaagd wordt beschouwd |
| `abort_conditions` | dict | Wanneer de mission onmiddellijk wordt gestopt |
| `issued_at` | datetime | Tijdstip van uitgifte door Queen |
| `issued_by` | str | Altijd `queen` |

**Abort conditions (altijd aanwezig):**
- Stale heartbeat
- Capital limit overschreden
- Risk limit bereikt
- TTL verlopen
- Market data stale

---

## Biome

**Rol:** Marktdomein met eigen adapter interface, risicoprofiel en execution constraints.

De Queen weet welk biome — niet welke exchange. Adapters zijn verwisselbaar
binnen een biome zonder dat de Queen of agents dat merken.

**Biome types:**

| Biome | Primaire exchange | Status |
|-------|-------------------|--------|
| `crypto` | Bitvavo | v1 bewezen — primary |
| `equities` | Interactive Brokers | v2 target |
| `commodities` | Saxo Bank | v2 target |

**Eigenschappen per biome:**
- `biome_id` — identifier
- `market_universe` — beschikbare symbolen/markten
- `adapter_interface` — welk adapter contract gebruikt wordt
- `risk_profile` — biome-specifieke risicoparameters
- `execution_constraints` — handelsuren, minimale lot sizes, etc.
- `data_adapter` — interface naar marktdata

**Adapter interface (contract):**
Elke biome-adapter implementeert:
- `get_market_data(symbol, timeframe)` → MarketData
- `place_order(order)` → OrderResult (alleen via execution gate)
- `get_positions()` → list[Position]
- `get_account_state()` → AccountState

---

## StrategyCandidate

**Rol:** Research artifact — nooit directe execution.

Een StrategyCandidate is het resultaat van research. Het is een voorstel aan de
Queen, geen executeerbaar object. Promotie naar paper of live vereist expliciete
Queen goedkeuring.

**Schema:**

| Veld | Type | Beschrijving |
|------|------|--------------|
| `candidate_id` | str | Unieke identifier |
| `name` | str | Leesbare naam |
| `source` | str | Herkomst (GitHub, paper, intern, mutatie) |
| `source_url` | str \| None | Verwijzing naar originele bron |
| `biome` | str | Doelbiome |
| `market_scope` | dict | Beoogde markten/symbolen |
| `logic_summary` | str | Korte beschrijving van de strategie |
| `parameters` | dict | Configureerbare parameters |
| `entry_conditions` | dict | Entry logica beschrijving |
| `exit_conditions` | dict | Exit logica beschrijving (verplicht) |
| `backtest_results` | dict \| None | Resultaten van backtests |
| `paper_results` | dict \| None | Resultaten van paper trading |
| `fitness_score` | float \| None | Geaggregeerde fitnessscore |
| `status` | str | `research` \| `paper` \| `approved` \| `rejected` \| `live` |
| `provenance` | list[dict] | Volledige historiek van mutaties en validaties |
| `proposed_at` | datetime | Tijdstip van voorstel aan Queen |
| `approved_by` | str \| None | Altijd `queen` indien goedgekeurd |

**Promotiepad:**
```
research_ant → StrategyCandidate(status=research)
paper_ant    → StrategyCandidate(status=paper, paper_results=...)
Queen        → StrategyCandidate(status=approved)  ← enige die dit mag
execution_ant → StrategyCandidate(status=live)     ← alleen na Queen approval
```

**Regels:**
- `exit_conditions` is altijd verplicht — geen candidate zonder exit logica
- `provenance` is append-only — nooit muteren
- Status kan alleen vooruit — nooit terugzetten zonder Queen actie
- `approved_by` mag alleen `queen` zijn

---

## Relatiediagram

```
Queen
 ├── vertrouwt: Node[]
 ├── geeft uit: Mission[]
 ├── beoordeelt: StrategyCandidate[]
 └── heeft kill-switch over: alles

Node
 ├── host van: Ant[]
 └── heeft: heartbeat, scoped permissions, append-only logs

Ant
 ├── heeft: Mission (1:1)
 ├── draait op: Node
 └── produceert: artifacts, StrategyCandidate (alleen research/paper ants)

Mission
 ├── uitgegeven door: Queen
 ├── geeft sandbox aan: Ant
 └── bevat: abort_conditions (altijd)

Biome
 ├── heeft: adapter_interface
 └── scoped door: Mission.market_scope

StrategyCandidate
 ├── geproduceerd door: research_ant / paper_ant
 ├── gepromoveerd door: Queen (enige)
 └── bevat: provenance (append-only)
```

---

*Laatste update: Fase 0 initialisatie*
*Dit document wordt bijgehouden bij elke fase-overgang door de operator.*
