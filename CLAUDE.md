# CLAUDE.md — ANT COLONY v2

Dit bestand wordt gelezen door Claude Code aan het begin van elke sessie.
Volg deze instructies altijd, zonder uitzondering.

---

## Project

**ANT COLONY v2** — een soeverein, multi-agent trading systeem dat zelfstandig
strategieën ontdekt, valideert en gecontroleerd inzet over meerdere markten.

- Repo: https://github.com/vikkel-bot/ant-colony-v2
- Taal: Python (tenzij anders aangegeven)
- Tests: `python -m pytest tests/ -q` — altijd groen houden

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
- **Toegang:** via AnyDesk (desktop) en Tailscale (dashboard op localhost:8000)
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
- Types: `scout_ant`, `research_ant`, `paper_ant`, `execution_ant`, `audit_ant`,
  `time_filter_ant`, `ingestion_ant`, `strategy_ant`, `operator_ant`, `claude_ant`
- Mag zelfstandig: kansen ontdekken, strategieën vinden, backtesten, paper traden
- Mag NOOIT zelfstandig: live gaan, kapitaallimieten verhogen, eigen policies aanpassen

### Mission
Bevat: `mission_id`, `ant_type`, `allowed_node`, `allowed_actions`,
`market_scope`, `capital_limit`, `risk_limit`, `ttl`, `heartbeat_interval`,
`success_conditions`, `abort_conditions`

### Biome
- crypto (operationeel via Bitvavo)
- equities (in bouw via IBKR / Yahoo Finance)
- commodities (gepland)
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

## TimeFilterAnt — ICT Kill Zone logica

Kill Zones (UTC):

| Window | Sessie | Trade toegestaan |
|--------|--------|-----------------|
| 02:00 – 05:00 | London Kill Zone | ✅ JA |
| 13:30 – 15:00 | NY AM Kill Zone | ✅ JA |
| 15:00 – 16:00 | NY PM | ❌ NEE |
| 19:00 – 24:00 | Asian Session | ❌ NEE |
| overig | Neutral | ❌ NEE |

**Gedragsregel voor alle ants:**
- `read_latest_time_signal()` retourneert `None` als TimeFilterAnt niet actief is
- Bij `None` → `trade_allowed = True` (fail-open: filter niet actief = geen blokkade)
- Bij `trade_allowed = False` → blokkeer alle nieuwe entries (scout + research + approved)
- Exits worden nooit geblokkeerd door de time filter (P3: exit altijd eerst)
- Alle drie entry-paden in PaperAnt moeten de time filter respecteren:
  `_process_new_signals()`, `_process_research_candidates()`, `_process_approved_candidates()`

---

## Exchanges en biomes

| Biome | Exchange | Status |
|-------|----------|--------|
| Crypto | Bitvavo | v1 bewezen — primary, live op PC2 |
| Crypto | Binance / Kraken | v2 backup |
| Equities | Interactive Brokers | in bouw |
| Equities | Yahoo Finance | data source (gratis) |
| Commodities | Saxo Bank | v2 target |

Architectuur: één adapter interface per biome.
Queen weet niet welke exchange — alleen welk biome en risicoprofiel.

---

## Huidige staat van de colony (PC2)

Colony draait live op PC2 — dashboard bereikbaar via Tailscale op localhost:8000.

**Actieve ants (9 missions):**
- ScoutAnt — detecteert price_move en volume_spike signalen
- ResearchAnt — genereert StrategyCandidate objecten (SMA crossover, RSI, Bollinger)
- PaperAnt — paper trades op basis van scout + research kandidaten
- AuditAnt — monitort systeemgezondheid
- IngestionAnt — ingesteert GitHub repos (45+ per dag)
- StrategyAnt — genereert strategy varianten
- ExecutionAnt — actief maar nog geen live orders
- OperatorAnt — verwerkt operator input via Claude Vision
- ClaudeAnt — AI-gestuurde analyse (€10/maand budget)
- TimeFilterAnt — ICT Kill Zone filter

**Bekende issues:**
- PaperAnt opent geen posities buiten kill zones (correct gedrag)
- PaperAnt time filter wordt inconsistent toegepast: `_process_new_signals()` respecteert
  de filter, maar `_process_research_candidates()` en `_process_approved_candidates()` niet
- Dit is de prioritaire fix voor de volgende sessie

**Dashboard staat (laatste snapshot):**
- Totaal kapitaal: €1.591 | Beschikbaar op Bitvavo: €80,64
- Regime: SIDEWAYS
- Top strategieën: momentum (ETH-EUR, Sharpe 0.29), hybrid (BTC-EUR, Sharpe 0.29)
- Colony v1: heartbeat rood (verwacht — v1 draait als apart process)

---

## Werkwijze voor Claude Code

1. **Lees dit bestand eerst** — altijd, elke sessie
2. **Controleer bestaande code** voordat je iets toevoegt
3. **Breek nooit een v1 stabiele module** zonder expliciete operator goedkeuring
4. **Werk stap voor stap** — één deliverable per keer, dan stoppen en wachten
5. **Kies altijd de simpelste veilige oplossing** bij twijfel
6. **Elke stap moet**: uitlegbaar zijn, testbaar zijn, veilig zijn, passen binnen bestaande structuur
7. **Tests draaien na elke wijziging** — `python -m pytest tests/ -q` moet groen blijven

**Na elke deliverable: stop, toon output, wacht op bevestiging.**

---

## Faseoverzicht

| Fase | Doel | Tests | Status |
|------|------|-------|--------|
| 0 | Doctrine + schemas + scheduler skeleton | 73 | ✅ bewezen |
| 1 | Exit-keten validatie harness | 147 | ✅ bewezen |
| 2 | Entry + volledige loop, paper only | 210 | ✅ bewezen |
| 3 | Queen governance + mission + kill-switch | 241 | ✅ bewezen |
| 4 | Strategy Lab (research only) | 286 | ✅ bewezen |
| 5 | Multi-biome scaffolding | 354 | ✅ bewezen |
| 6 | Queen allocator upgrade | 403 | ✅ bewezen |
| 7 | Paper-mode multi-node simulatie | 487 | ✅ bewezen |
| 8 | Guarded live adapters | 543 | ✅ bewezen |
| 9 | Colony Dashboard | 591 | ✅ bewezen |
| 10 | Live op PC2 — Bitvavo + equities biome | 1757 | 🔄 actief |

---

## FASE 10 — actief

Doel: Colony draait live op PC2. Bitvavo adapter operationeel.
Equities biome in bouw. Paper trading actief maar nog geen trades door kill zone filter.

**Huidige prioriteit: PaperAnt time filter fix**

Probleem: `_process_research_candidates()` en `_process_approved_candidates()` in
`paper_ant.py` respecteren de TimeFilterAnt niet. Alleen `_process_new_signals()` doet dat.

Fix: extraheer de time filter check naar een helper `_is_trading_allowed() -> bool` en
roep deze aan aan het begin van alle drie entry-methoden. Exits blijven ongemoeid.

```python
def _is_trading_allowed(self) -> bool:
    """True als de TimeFilterAnt trading toestaat (of niet actief is)."""
    if self.logs_root is None:
        return True
    sig = read_latest_time_signal(self.logs_root)
    if sig is None:
        return True  # filter niet actief → fail-open
    return sig.get("trade_allowed", True)
```

**Equities biome (in bouw):**
- Yahoo Finance adapter (data) + IBKR adapter (execution)
- Setup 1: Sector Rotatie (SectorScoutAnt, MomentumRankAnt, RotationAnt)
- Setup 2: Fundamenteel + Technisch (FundamentalAnt, PiotroskiAnt, BreakoutAnt)
- Setup 3: Defensief Dividend (DividendScoutAnt, VolatilityAnt, RebalanceAnt)
- Gate voor live: 30 handelsdagen paper, Sharpe > 0.5, max drawdown < 25%

---

## Environment variabelen

```
# Bitvavo
BITVAVO_API_KEY=...
BITVAVO_API_SECRET=...
BITVAVO_PAPER_MODE=true

# Claude Ant (opt-in, default uitgeschakeld)
ANTHROPIC_API_KEY=...
CLAUDE_ANT_ENABLED=false
CLAUDE_ANT_MONTHLY_BUDGET_EUR=10.00
```

---

*Dit bestand bijhouden bij elke fase-overgang.*
*Laatste update: 2026-04-21 — Time filter fix toegevoegd, colony staat bijgewerkt, startprompt sectie verwijderd (staat in chat AC2-ch2)*
