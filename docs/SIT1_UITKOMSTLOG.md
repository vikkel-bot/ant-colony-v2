# SIT-1 uitkomstlog (append-only)

Dit bestand vervangt de invulplek van PREREG_SIT1_20260926.md §9 en §10.
Reden: scripts/verify_research_log.py (regel 4) laat geen enkele wijziging van een
PREREG-bestand na registratie toe; invullen in de PREREG zelf zou CI breken.
Besloten door Vik op 15-10-2026. Geen wijziging van specificatie, venster, drempel of
hypothese. Regels worden alleen toegevoegd, nooit gewijzigd of verwijderd.

## Vooraf vastgelegd, vóór de development-run (15-10-2026)

Voorspellingen eindoordeel SIT-1A development:

| wie | PASS | FAIL | INCONCLUSIVE |
|---|---|---|---|
| Vik | 20 % | 30 % | 50 % |
| Claude | 25 % | 20 % | 55 % |

Diagnostiek CW-size onder H0 (synthetisch, 5.000 runs per proces, ffcf642,
results/sit1/cw_size_sim.json): bij alpha 0,0083 verwerpt de toets 0,48 % (AR1) en
0,36 % (HAR); bij 0,05: 2,7 % en 2,3 %. De toets is conservatief. Reproductie op twee
machines gaf identieke tellingen. Diagnostisch; de beslisregel §3.6 verandert niet.

Open punten, vastgelegd vóór de run:
1. LHE (§2) eist targets t/m 2026-09; de Ken French-vintage CRSP 202608 loopt t/m 2026-08.
   scripts/sit1_run.py weigert de LHE (fail-closed). Besluit volgt vóór stap 5.
2. FRED/ALFRED: BAA en AAA voor 2016-10 t/m 2017-01 verschenen pas op 2017-03-22
   (onderbreking bij FRED). Structurele publicatievertraging: mediaan 2 dagen na maandeinde,
   dus geen extra verschuiving van dCS (§1). Geen correctie; vermeld bij elke LHE-uitslag.
3. §10 prospectief: forecasts moeten vóór aanvang van de maand gecommit zijn. 2026-10 is
   gemist. Eerste mogelijke prospectieve maand: 2026-11 (commit vóór 01-11-2026).

## §9 Uitkomstlog

| stap | datum | commit | vintage-datums | uitkomst | labels |
|---|---|---|---|---|---|
| Data opgehaald | 2026-10-15 | 1e13ae4 | FF daily/maand CRSP 202608; FF ex-US Bloomberg 202608; FRED BAA/AAA Last-Modified 2026-10-01; ALFRED 361 vintages 1996-12-03 t/m 2026-10-01; SPY yfinance 2026-10-15 | 8 bestanden, SHA256 in data/sit1/MANIFEST.json | - |
| Code + tests groen | 2026-10-15 | 891f673 | - | 2691 passed (22 nieuw) | - |
| Vintage-controle credit (§1) | 2026-10-15 | ffcf642 | idem | max afwijking dCS 2,0 bp; geen maand > 5 bp; geen herberekening | - |

## §10 Prospectieve pristine sectie

| maand | vastgelegd op (commit) | y0 | y1 | w_B1 | w_R1 | RV gerealiseerd | dCS gebruikt | delta_t | f_t |
|---|---|---|---|---|---|---|---|---|---|
