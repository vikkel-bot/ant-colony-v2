# CLAUDE.md — ANT COLONY v2

Dit bestand wordt gelezen door Claude Code aan het begin van elke sessie.
Volg deze instructies altijd, zonder uitzondering.

---

## Project

**ANT COLONY v2** — een soeverein, multi-agent trading systeem dat zelfstandig
strategieën ontdekt, valideert en gecontroleerd inzet over meerdere markten.

- Repo: https://github.com/vikkel-bot/ant-colony-v2
- Taal: Python (tenzij anders aangegeven)

---

## Infrastructuur

### PC1 — laptop (brein)
- **Rol:** ontwikkeling, planning, Claude Code sessies, code review
- **Wat hier draait:** Claude Code, Git, editor
- **Wat hier NIET draait:** colony, live execution, trading software
- **Werkwijze:** schrijf code → push naar GitHub → klaar

### PC2 — desktop (spieren)
- **Rol:** productie runtime, colony execution
- **Wat hier draait:** ant-colony-v2, scheduler, alle agents, live execution
- **Runtime paden:**
  - `C:\Trading\ANT_OUT` — algemene outputs
  - `C:\Trading\ANT_LIVE` — live execution artifacts
  - `C:\Trading\ANT_LOGS` — append-only logs
- **Deployment:** automatische `git pull` + herstart bij nieuwe commit op `main`
- **Wat hier NIET draait:** development tools, Claude Code, editors

### Deployment flow
```
PC1 (laptop)           GitHub              PC2 (desktop)
──────────────    ─────────────────    ──────────────────────
Claude Code    →  ant-colony-v2/main →  git pull (auto)
schrijft code     (centrale repo)        scheduler herstart
review + push                            colony draait door
```

### Regels voor Claude Code
- Schrijf altijd code alsof PC2 de enige runtime is
- Gebruik altijd absolute paden gebaseerd op `C:\Trading\`
- Bouw geen aannames in over de development machine
- Scheduler op PC2 moet zelfstandig herstarten na `git pull`
- Alle file paths moeten Windows-compatibel zijn (`\` of `os.path.join`)

---

## Niet-onderhandelbare principes

1. **Queen is altijd soeverein** — geen agent promoveert zichzelf naar live
2. **Fail-closed boven alles** — bij twijfel: stop, log, wacht op operator
3. **Exit-logica vóór entry-logica** — bewezen in v1, bouw hierop verder
4. **Observability vóór intelligentie** — logging en audit trail zijn altijd eerste prioriteit
5. **Research → Paper → Live** — deze scheiding is strikt en nooit over te slaan
6. **Scheduler is infrastructuur** — draait altijd, is nooit een afterthought
7. **Geen module die code uitvoert bij import** — v1 architectuurfout, nooit herhalen
8. **Één markt, één strategie, volledige loop** — bewijs eerst, schaal daarna

---

## Veiligheidsregels (absoluut)

- GEEN self-spreading software
- GEEN autonome installatie op externe of onbekende systemen
- GEEN self-replicating code
- GEEN hidden persistence
- GEEN privilege escalation
- GEEN unsanctioned network actions
- ALLEEN deployment naar door operator goedgekeurde nodes
- ALTIJD append-only logs — nooit overschrijven

**Fail conditions (onmiddellijk stoppen):**
- Stale heartbeat → stop agent
- Stale market data → blokkeer execution
- Risk breach → onmiddellijk stop
- Ontbrekend audit trail → weiger actie

---

## v1 stabiele modules — SACRED

Deze modules zijn production-proven en verwerken echte live orders.
**Niet herschrijven. Alleen wrappen, extenden of adapteren.**

| Module | Status | Gebruik in v2 |
|--------|--------|---------------|
| `live_execution_gate.py` | Stabiel | Behouden als execution gate |
| `bitvavo_adapter.py` | Stabiel | Basis voor Biome Crypto adapter |
| `broker_request_builder.py` | Stabiel | Behouden |
| `queen_feedback_intake.py` | Stabiel | Causaal feedback schema hergebruiken |
| `live_artifact_writer.py` | Stabiel | Artifact-based state behouden |

---

## v2 Object model

### Queen
- Capital allocator, mission issuer, promotie gatekeeper
- Kill-switch authority op colony niveau
- Enige entiteit die mag: deployen, intrekken, kapitaal toewijzen, nodes trusten

### Node
- Gecontroleerde habitat op goedgekeurde infrastructuur
- Scoped permissions, heartbeat vereist, append-only logs
- Mag nooit zelf expanderen of zichzelf autoriseren

### Ant (agent)
- Task-bound, TTL, budget, rapporteert aan Queen
- Types: `scout_ant`, `research_ant`, `paper_ant`, `execution_ant`, `audit_ant`
- Mag zelfstandig: kansen ontdekken, strategieën vinden, backtesten, paper traden
- Mag NOOIT zelfstandig: live gaan, kapitaallimieten verhogen, eigen policies aanpassen

### Mission
Bevat: `mission_id`, `ant_type`, `allowed_node`, `allowed_actions`,
`market_scope`, `capital_limit`, `risk_limit`, `ttl`, `heartbeat_interval`,
`success_conditions`, `abort_conditions`

### Biome
- crypto, equities, commodities
- Per biome: market universe, data adapter interface, risk profiel, execution constraints

### StrategyCandidate
- Alleen research artifact — nooit directe execution
- Bevat provenance, validatieresultaten, fitness score
- Promotie alleen via Queen goedkeuring

---

## Agent autonomie — wat mag en wat niet

### Agents mogen zelfstandig:
- Kansen detecteren op markten
- Strategieën zoeken op publieke bronnen (GitHub, papers, internet)
- Gevonden ideeën normaliseren naar intern schema
- Backtesten en walk-forward validatie uitvoeren
- Paper traden om strategieën te valideren
- Strategievarianten voorstellen aan Queen
- Bestaande strategieën combineren en muteren
- PnL en state rapporteren aan Queen

### Agents mogen NOOIT zelfstandig:
- Live execution starten
- Kapitaal- of risicolimieten verhogen
- Code deployen naar nodes zonder Queen opdracht
- Externe code direct uitvoeren (altijd normaliseren naar intern schema eerst)
- Eigen trust boundaries bepalen
- Zichzelf installeren op onbekende systemen
- Policies aanpassen

---

## Exchanges en biomes

| Biome | Exchange | Status |
|-------|----------|--------|
| Crypto | Bitvavo | v1 bewezen — primary |
| Crypto | Binance / Kraken | v2 backup |
| Equities | Interactive Brokers | v2 target |
| Commodities | Saxo Bank | v2 target |

Architectuur: één adapter interface per biome.
Queen weet niet welke exchange — alleen welk biome en risicoprofiel.

---

## Werkwijze voor Claude Code

1. **Lees dit bestand eerst** — altijd, elke sessie
2. **Controleer bestaande code** voordat je iets toevoegt
3. **Breek nooit een v1 stabiele module** zonder expliciete operator goedkeuring
4. **Werk stap voor stap** — één deliverable per keer, dan stoppen en wachten
5. **Kies altijd de simpelste veilige oplossing** bij twijfel
6. **Elke stap moet**: uitlegbaar zijn, testbaar zijn, veilig zijn, passen binnen bestaande structuur

**Na elke deliverable: stop, toon output, wacht op bevestiging.**

---

## Faseoverzicht

| Fase | Doel | Tests | Status |
|------|------|-------|--------|
| 0 | Doctrine + schemas + scheduler skeleton | — | ✅ bewezen |
| 1 | Exit-keten validatie harness | 147/147 | ✅ bewezen |
| 2 | Entry + volledige loop, paper only | 210/210 | ✅ bewezen |
| 3 | Queen governance + mission + kill-switch | 241/241 | ✅ bewezen |
| 4 | Strategy Lab (research only) | 286/286 | ✅ bewezen |
| 5 | Multi-biome scaffolding | 354/354 | ✅ bewezen |
| 6 | Queen allocator upgrade | 403/403 | ✅ bewezen |
| 7 | Paper-mode multi-node simulatie | 487/487 | ✅ bewezen |
| 8 | Guarded live adapters | 543/543 | ✅ bewezen |
| 9 | Colony Dashboard | 591/591 | ✅ bewezen |
| 10 | Eerste echte Bitvavo adapter op PC2 | — | 🔄 actief |

---

## Huidige fase

**FASE 10 — actief**

Doel: eerste echte Bitvavo adapter draaien op PC2 tegen de live Bitvavo API.
De volledige paper/simulatie stack is bewezen (fases 0–9, 591 tests groen).
Dit is de eerste stap over de grens van simulatie naar echte marktdata en orders.

Gate: Bitvavo adapter op PC2 draait stabiel, heartbeat groen, market data binnenkomend,
execution gate blokkeert live orders tot Queen expliciete toestemming geeft.

**Constraints voor fase 10:**
- Adapter wraps de v1 `bitvavo_adapter.py` — niet herschrijven
- Execution gate staat standaard op PAPER — live alleen na expliciete Queen promotie
- Alle market data en orders loggen naar `C:\Trading\ANT_LOGS` (append-only)
- Heartbeat vereist — stale heartbeat = adapter stopt zichzelf

---

*Dit bestand bijhouden bij elke fase-overgang.*
*Laatste update: 2026-04-16 — fases 0–9 afgesloten, fase 10 gestart*
