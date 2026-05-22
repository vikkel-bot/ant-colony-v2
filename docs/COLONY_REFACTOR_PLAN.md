# Colony Refactor Plan — Mei 2026

Prompt-context:

TAAK: Maak `docs/COLONY_REFACTOR_PLAN.md` met de structurele aanpak.
Maak een nieuw document `docs/COLONY_REFACTOR_PLAN.md` met onderstaande inhoud.
Geen bestaande documenten aanpassen — dit is een nieuw, separaat plan.

## Context

De colony werkt technisch (`RUNNING_GREEN`, paper trades worden geopend en gesloten),
maar Vik kan niet leren van het systeem omdat het een black box is. Vier structurele
problemen zijn geïdentificeerd op 22 mei 2026 die niet met quick fixes opgelost
moeten worden.

## Niet-onderhandelbare principes

1. **Eén probleem tegelijk afmaken** — geen parallelle wijzigingen
2. **Elke fix krijgt een regressie-test** voordat we de volgende oppakken
3. **Geen "snelle workaround"-cultuur meer** — als iets fundamenteel kapot is,
   pakken we het fundamenteel aan
4. **Dashboard krijgt eigen testdekking** — minimaal één snapshot-test per view
5. **Geen v3** — de codebase is solide, de problemen zijn architectureel

## Drie sessies, in deze volgorde

### Sessie A — Pool Reset (eerst)

**Probleem:** Queen's top-3 toont al 2 maanden dezelfde strategieën
(`rsi_based`, `sma_crossover`, `bollinger_bands`) ondanks dat deze zijn uitgeschakeld.
Oorzaak: 25.000+ historische kandidaten in research-pool die nooit zijn opgeschoond.

**Acceptatiecriteria:**

- Alle research-logs van vóór 21 mei 2026 verplaatst naar `ANT_LOGS/archive/`
- Queen leest alleen actuele research-pool
- Top-3 op dashboard toont nu `volatility_squeeze` of `mean_reversion`
- `scripts/reset_research_pool.py` bestaat en is idempotent
- Test bevestigt dat reset script geen actuele data verwijdert
- Backup-mechanisme: archive is reversible

### Sessie B — Dashboard Redesign

**Probleem:** Dashboard is statusscherm in plaats van leerinstrument.

- Drie ongebruikte L1/L2/L3 knoppen
- Paper diagnostiek toont alleen aggregaten, geen "waarom"
- Positie-balkjes met PnL-indicatoren zijn verdwenen (regressie)
- Watchtower signaal-status onduidelijk (laatste veto vs heartbeat)

**Aanpak:**

1. Eerst ontwerp op papier — wat moet erin, wat moet eruit
2. Pas dan bouwen
3. Visuele regressie-test toevoegen

**Acceptatiecriteria:**

- L1/L2/L3 knoppen verwijderd of functioneel gemaakt
- Positie-balkjes met PnL terug zichtbaar
- Watchtower toont aparte heartbeat- en signaal-tijdstempel
- Minimaal één snapshot-test in `tests/test_dashboard_*`

### Sessie C — Trade Journal

**Probleem:** Vik kan niet leren omdat trades als getallen worden getoond,
niet als verhalen. Geen "hypothesis vs werkelijkheid" inzicht.

**Aanpak:**

Aparte view die elke gesloten trade toont als één leesbare regel:

- Welke ant + strategie
- Entry-hypothesis
- Wat Queen's regime was
- Exit-reden + interpretatie: klopte de hypothesis?

**Acceptatiecriteria:**

- Nieuwe route `/journal` in dashboard
- Per trade één regel met volledige narrative
- Filtering per `strategy_type`, `biome`, `exit_reason`
- Export naar markdown/CSV mogelijk

## Werkwijze per sessie

1. Start sessie met expliciete verwijzing naar dit document
2. Acceptatiecriteria bovenaan elke Codex/Claude Code prompt
3. **Test eerst, code daarna**
4. Sessie eindigt pas als alle acceptatiecriteria groen zijn
5. Volgende sessie start nooit voordat vorige afgerond is

## Wat we niet meer doen

- Reactief debuggen zonder root cause analyse
- Features stapelen op kapotte fundamenten
- Quick fixes voor symptomen
- Parallel werken aan meerdere problemen
- Dashboard-wijzigingen zonder visuele check

## Toetssteen bij elke nieuwe sessie

Voordat we beginnen vraagt Claude:

1. Hebben we de vorige sessie's acceptatiecriteria afgevinkt?
2. Zijn er regressie-tests voor wat we daarvoor hebben gebouwd?
3. Is dit een fundamenteel probleem of een symptoom?

Als één van deze drie "nee" is, stoppen we.
