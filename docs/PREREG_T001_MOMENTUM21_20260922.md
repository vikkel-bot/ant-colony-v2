# Pre-registratie T001 — crypto cross-sectioneel momentum, 3 weken

Familie: CRYPTO_XS_PRICE_MOMENTUM. Toets 1 in deze familie.
Universum: FROZEN v2 (ant_colony/lab/universe.py, 366e820).
Bron voor de sensorkeuze: Liu, Tsyvinski & Wu, "Common Risk Factors in
Cryptocurrency" (J. Finance 2022; data 2014-2018). Drie weken daar gekozen op
hun eigen data; voor onze periode is dit een replicatie op niet-overlappende data.

## Periode
- Ontwikkeling: t_i = 2022-10-01 00:00 UTC + 7*i dagen, zolang t_i + 7d <= 2025-10-01.
- Holdout 2025-10-01 t/m 2026-09-30: GESLOTEN. Alleen te openen na PASS.
  Confirmatory, niet pristine: universum- en spreidingsstatistieken over 48
  maanden zijn al gebruikt; geen sensor- of rangschikkingsresultaat bekeken.

## Sensor
mom21(m, t) = close(laatste candle < t) / close(laatste candle < t - 21d) - 1.
Geen overgeslagen periode. Als geïsoleerde functie, alleen data < t.

## Selectie en uitkomst
- Rangschik U(t) op mom21 aflopend; gelijke waarden: marktnaam oplopend.
- Bovenste groep: k(t) = max(8, N_t // 5). Gelijke weging.
- Rendement munt: close(laatste candle < t+7d) / close(laatste candle < t) - 1.
- Wekelijks verschil: gemiddelde(bovenste groep) - gemiddelde(U(t)).
- Geen candle in [t, t+7d): rendement 0 (primair); aantal rapporteren.

## Grootheden (3)
S1 gemiddeld wekelijks verschil; S2 mediaan wekelijks verschil;
S3 fractie weken met verschil > 0.

## Nulmodellen (2), elk 10.000 trekkingen, seed 42
N1 willekeurige selectie: per week k(t) munten willekeurig uit U(t).
N2 blok-bootstrap van de verschilreeks, gecentreerd op 0, blokken van 4 weken.
p = (1 + aantal null >= waargenomen) / (1 + trekkingen). Eenzijdig.
Drempel: 0,05 / (3 grootheden x 2 nulmodellen) = 0,0083.

## Beslisregel
PASS alleen als ALLE:
1. S1 p < 0,0083 onder N1 en onder N2;
2. S2 of S3 p < 0,0083 onder N1;
3. S1 > 0 in minstens 2 van 3 deelperioden (3 aaneengesloten blokken van
   gelijke lengte);
4. controle met -100% voor weken zonder handel wijzigt 1-3 niet;
5. kostenhorde: S1 / (gemiddelde wekelijkse omloop bovenste groep x 0,50%
   round-trip) >= 3.
FAIL: S1 niet significant onder N1.
INCONCLUSIVE: S1 significant onder N1, maar een van de overige voorwaarden faalt.
Uitkomst 1-4 PASS maar 5 faalt: "informatief, niet verhandelbaar".

## Verwachting vooraf
Effect fors kleiner dan in het bronpaper (publicatie-afname, andere markt,
gelijke weging; het paper vond momentum zwak bij gelijke weging en sterk bij
grotere munten). delta_min ontwikkelperiode ~0,62%/week.

## Multiple testing in de familie
Elke volgende toets in CRYPTO_XS_PRICE_MOMENTUM (andere horizon, weging,
variant) gebruikt 0,05 / (6 x aantal toetsen in de familie tot en met die toets).
