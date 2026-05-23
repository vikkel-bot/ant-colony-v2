# Colony Refactor Plan — Mei 2026

## Context

De colony werkt technisch (`RUNNING_GREEN`, paper trades worden geopend en gesloten),
maar Vik moet ook kunnen leren van het systeem. Dit plan houdt de structurele
verbeteringen bij en voorkomt dat we terugvallen in losse quick fixes.

## Niet-onderhandelbare principes

1. **Eén probleem tegelijk afmaken** — geen parallelle wijzigingen
2. **Elke fix krijgt een regressie-test** voordat we de volgende oppakken
3. **Geen "snelle workaround"-cultuur meer** — als iets fundamenteel kapot is,
   pakken we het fundamenteel aan
4. **Dashboard krijgt eigen testdekking** — minimaal één snapshot-test per view
5. **Geen v3** — de codebase is solide, de problemen zijn architectureel

## Voltooide Sessies

### Sessie A — Pool Reset ✅

Commit: `fc05c4b`

Status:

- 25.043 historische kandidaten gearchiveerd
- Queen toont nu `volatility_squeeze` als #1
- `scripts/reset_research_pool.py` gebouwd
- Reset-script is idempotent en reversible
- Backup-mechanisme via archive aanwezig

### Sessie B — Dashboard Redesign ✅

Commits: `c08e65c`, `3c73399`

Status:

- L1/L2/L3 knoppen verwijderd
- `/journal` route toegevoegd met top-10 gesloten trades
- Operator override paneel toegevoegd
- Operator override kleuren:
  - groen = voorkeur
  - rood = vermijd
  - grijs = neutraal
- Operator override gewichten vervallen automatisch na 5 dagen
- Positie-balkjes met PnL-indicatoren hersteld
- Watchtower toont nu aparte heartbeat- en signaal-tijdstempel
- Dashboard-regressietests toegevoegd

## Extra Fixes Buiten Sessies

Voltooide technische schuld:

- StrategyPromoter warning verwijderd (`66a243b`)
- Watchtower heartbeat toegevoegd (`66a243b`)
- Colony draait als Windows Service via NSSM: `AntColony`
- Watchtower draait als Windows Service via NSSM: `AntWatchtower`
- Beide services starten automatisch bij reboot
- Beide services herstarten automatisch na crash
- Beheer via:

```powershell
C:\Trading\nssm.exe restart "AntColony"
```

Belangrijk na elke `git pull` op PC2:

```powershell
C:\Trading\nssm.exe restart "AntColony"
```

Voer dit uit in een administrator PowerShell.

## Nog Te Doen

### Sessie C — Trade Journal

Probleem:

De `/journal` route bestaat, maar de trade-verhalen zijn nog niet volledig
leerbaar. De huidige "waarom" zin is nog generiek, vooral omdat historische
trades vaak geen volledig `regime` en `strategy_type` veld bevatten.

Nog te bouwen:

- Betere "waarom" zin op basis van complete trade-context
- Filtering per `strategy_type`
- Filtering per `biome`
- Filtering per `exit_reason`
- Export naar markdown
- Export naar CSV

Acceptatiecriteria:

- Per trade één leesbare narrative
- Entry-hypothesis zichtbaar
- Queen-regime bij entry zichtbaar als data beschikbaar is
- Exit-reden vertaald naar interpretatie
- Filters werken zonder dashboard-crash bij lege data
- Markdown/CSV export werkt
- Regressietests toegevoegd

## Werkwijze Per Sessie

1. Start sessie met expliciete verwijzing naar dit document
2. Acceptatiecriteria bovenaan elke Codex/Claude Code prompt
3. **Test eerst, code daarna**
4. Sessie eindigt pas als alle acceptatiecriteria groen zijn
5. Volgende sessie start nooit voordat vorige afgerond is

## Wat We Niet Meer Doen

- Reactief debuggen zonder root cause analyse
- Features stapelen op kapotte fundamenten
- Quick fixes voor symptomen
- Parallel werken aan meerdere problemen
- Dashboard-wijzigingen zonder visuele check

## Toetssteen Bij Elke Nieuwe Sessie

Voordat we beginnen vraagt Claude:

1. Hebben we de vorige sessie's acceptatiecriteria afgevinkt?
2. Zijn er regressie-tests voor wat we daarvoor hebben gebouwd?
3. Is dit een fundamenteel probleem of een symptoom?

Als één van deze drie "nee" is, stoppen we.

## Operationele Herinnering

- Eén probleem tegelijk
- Elke fix krijgt een regressie-test
- Sessie eindigt pas als acceptatiecriteria groen zijn
- Na `git pull` altijd herstarten:

```powershell
C:\Trading\nssm.exe restart "AntColony"
```

Doe dit in administrator PowerShell.
