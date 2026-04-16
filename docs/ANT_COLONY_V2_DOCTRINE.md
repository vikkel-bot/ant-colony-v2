# ANT COLONY v2 — Doctrine

*Dit document is de grondwet van het systeem. Elke agent, elke module, elke beslissing
is hieraan ondergeschikt. Het wordt nooit automatisch gewijzigd.*

---

## 1. Wat is ANT COLONY v2?

ANT COLONY v2 is een soeverein, multi-agent trading systeem dat zelfstandig
strategieën ontdekt, valideert en gecontroleerd inzet over meerdere markten.

Het systeem is gebouwd op de lessen van v1:
- Exit-logica vóór entry-logica
- Observability vóór intelligentie
- Fail-closed boven alles
- Eén markt, één strategie, volledige loop — bewijs eerst, schaal daarna

---

## 2. Kernprincipes (niet-onderhandelbaar)

### P1 — Queen is altijd soeverein
De Queen is de enige entiteit die mag deployen, kapitaal toewijzen, nodes
vertrouwen en agents promoveren naar live. Geen enkele agent kan zichzelf
autoriseren. Geen enkele module kan de Queen omzeilen.

### P2 — Fail-closed boven alles
Bij twijfel: stop, log, wacht op operator. Het systeem faalt nooit open.
Een gestopt systeem is beter dan een ongecontroleerd draaiend systeem.

Harde fail conditions die onmiddellijk stoppen:
- Stale heartbeat → stop agent
- Stale market data → blokkeer execution
- Risk breach → onmiddellijk stop
- Ontbrekend audit trail → weiger actie

### P3 — Exit-logica vóór entry-logica
Bewezen in v1. De exit-keten (stop-loss, take-profit, TTL, risk breach) wordt
altijd als eerste gebouwd en gevalideerd. Geen entry mogelijk zonder bewezen exit.

### P4 — Observability vóór intelligentie
Logging en audit trail zijn altijd eerste prioriteit. Een agent die niet
observeerbaar is, bestaat niet voor de operator. Alle state is append-only.

### P5 — Research → Paper → Live
Deze scheiding is strikt en nooit over te slaan. Een strategie doorloopt altijd
alle drie de fasen. Er is geen shortcut naar live.

### P6 — Scheduler is infrastructuur
De scheduler draait altijd. Hij is nooit een afterthought. Agents komen en gaan;
de scheduler is permanent.

### P7 — Geen module die code uitvoert bij import
v1 architectuurfout. Alle modules zijn passief bij import. Alleen expliciete
aanroepen triggeren gedrag.

### P8 — Één markt, één strategie, volledige loop
Bewijs eerst volledig op één markt met één strategie. Schaal daarna. Nooit
parallelle expansie zonder bewezen basis.

---

## 3. Autonomiegrens

### Agents mogen zelfstandig:
- Kansen detecteren op markten
- Strategieën zoeken op publieke bronnen
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

## 4. Veiligheidsregels (absoluut)

Het systeem bevat nooit:
- Self-spreading software
- Autonome installatie op externe of onbekende systemen
- Self-replicating code
- Hidden persistence
- Privilege escalation
- Unsanctioned network actions

Deployment alleen naar door operator goedgekeurde nodes.
Logs zijn altijd append-only. Nooit overschrijven.

---

## 5. Fasemodel

Het systeem groeit in bewezen stappen. Geen fase wordt overgeslagen.

| Fase | Doel | Gate |
|------|------|------|
| 0 | Doctrine + schemas + scheduler skeleton | Doctrine goedgekeurd |
| 1 | Exit-keten validatie harness | EXIT_KETEN_VOLLEDIG_CORRECT |
| 2 | Entry + volledige loop, paper only | 50+ paper trades bewezen |
| 3 | Queen governance + mission + kill-switch | Kill-switch bewezen in simulatie |
| 4 | Strategy Lab (research only) | Na bewezen live loop |
| 5 | Multi-biome scaffolding | Na single-market paper bewijs |
| 6 | Queen allocator upgrade | Na multi-biome paper bewijs |
| 7 | Paper-mode multi-node simulatie | Volledig bewezen paper kolonie |
| 8 | Guarded live adapters | Alleen na volledige paper proof |

---

## 6. v1 Stabiele modules — SACRED

Deze modules zijn production-proven en verwerken echte live orders.
Ze worden niet herschreven. Ze worden alleen gewrapt, geëxtend of geadapteerd.

| Module | Status | Gebruik in v2 |
|--------|--------|---------------|
| `live_execution_gate.py` | Stabiel | Behouden als execution gate |
| `bitvavo_adapter.py` | Stabiel | Basis voor Biome Crypto adapter |
| `broker_request_builder.py` | Stabiel | Behouden |
| `queen_feedback_intake.py` | Stabiel | Causaal feedback schema hergebruiken |
| `live_artifact_writer.py` | Stabiel | Artifact-based state behouden |

---

## 7. Infrastructuur

### PC1 — laptop (brein)
Rol: ontwikkeling, planning, Claude Code sessies, code review.
Draait hier NIET: colony, live execution, trading software.

### PC2 — desktop (spieren)
Rol: productie runtime, colony execution.
Runtime paden:
- `C:\Trading\ANT_OUT` — algemene outputs
- `C:\Trading\ANT_LIVE` — live execution artifacts
- `C:\Trading\ANT_LOGS` — append-only logs

Deployment: automatische `git pull` + herstart bij nieuwe commit op `main`.

---

*Laatste update: Fase 0 initialisatie*
*Dit document wordt bijgehouden bij elke fase-overgang door de operator.*
