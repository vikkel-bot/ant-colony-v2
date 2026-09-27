# Ruisstructuur relatieve weekreturns — 26-09-2026

Outcome-vrij: r_rel(i,t) = r(i,t) - gelijkgewogen gemiddelde van U(t). Geen
rangschikking, geen selectie, geen sensor. Ontwikkelblok; holdout afgeknipt.
Script scripts/noise_structure.py, artefact NOISE_STRUCTURE_20260926.json
(bevat de volledige residuenmatrix voor latere empirische resampling).

156 weken, 274 munten, 14.797 paren op lag 1 tot 11.782 op lag 8.

## Teken van de relatieve return: geen geheugen

| lag | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| pooled | +0,010 | +0,003 | -0,017 | -0,005 | -0,023 | -0,016 | -0,001 | +0,007 |

Alle 95%-intervallen omsluiten nul, behalve lag 5 (-0,038 tot -0,004); bij acht
lags is een dergelijk geval te verwachten.

## Omvang van de beweging: wel geheugen (volatiliteitsclustering)

|r_rel|: lag1 +0,230, lag2 +0,120, lag3 +0,081, lag4 +0,044, daarna 0,02-0,04.
r_rel^2: lag1 +0,078, daarna vrijwel nul — het verschil met |r_rel| wijst op
zware staarten (enkele extreme weken domineren de kwadraten).

## Spread van een WILLEKEURIGE mand die n weken wordt vastgehouden

| tenure | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| 1 | +0,002 | -0,010 | -0,009 | -0,006 |
| 6 | +0,007 | -0,016 | -0,017 | -0,004 |
| 12 | +0,024 | -0,012 | -0,007 | -0,009 |

## Conclusies

1. N1 (onafhankelijke willekeurige selectie per week) is GELDIG voor deze
   kandidatenfamilie: vasthouden creeert vrijwel geen extra afhankelijkheid.
2. N2 met blokken van 4 weken is ruim voldoende VOOR HET GEMIDDELDE. Voor
   spreidingsgrootheden (variantie, Sharpe) zou 4 weken krap zijn vanwege
   volatiliteitsclustering tot lag 3.
3. Selectiepersistentie en returnafhankelijkheid zijn aantoonbaar losse zaken.
   Zonder de tweede kost de eerste geen informatie.

## NIET door deze meting beschreven

- Gelijktijdige samenhang binnen subgroepen munten (sector/thema).
- Blijvende niveauverschillen in gemiddeld rendement per munt. Dit is de
  belangrijkste openstaande; te meten uit dezelfde residuenmatrix. [Likely]
- Staartgedrag buiten de tweede momenten.

## Gevolg voor de powerkalibratie

Een EMPIRISCHE kalibratie op de opgeslagen residuen is verdedigbaar: echte
residuen hergebruiken, effect van bekende grootte injecteren, detectiekans
meten. Geen parametrisch AR(1)-model nodig of gerechtvaardigd.
