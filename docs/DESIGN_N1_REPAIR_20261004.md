# Ontwerp reparatie N1 — twee kandidaten, blind gekalibreerd (04-10-2026)

Preregistratie van een instrumentontwerp, geen sensortoets. Gecommit voor er code bestaat. Uitvoering van het
reparatiebesluit in beleid 119195a punt 7, na de gemeten size-fout (N1_STRESS_REPORT_20261004, 632e075).
Twee adviesrondes verwerkt; toevoegingen van Claude gemarkeerd met [C].

## Kandidaat A — HAC-gestudentiseerde S1-toets

- Teststatistiek t = gemiddelde(s) / SE_HAC(gemiddelde(s)), Newey-West met Bartlett-gewichten en lag 3,
  vooraf vast. Lag 3 volgt uit de gemeten omvangpersistentie (uitdovend tot lag 3), niet uit optimalisatie.
- Exact dezelfde HAC-berekening op de waargenomen spreadreeks EN op elke N1-nultrekking (elke trekking heeft
  een eigen weekreeks). p = (1 + #{t_nul >= t_obs}) / (1 + B), B = 10.000.
- Faalt A, dan is A FAIL: geen andere lags of bandbreedtes proberen. Self-normalized statistieken zijn een
  kandidaat voor een LATERE ontwerpronde, niet iets om nu bij te schuiven.
- Estimand blijft de oorspronkelijke vraag: heeft deze selectie een gemiddelde spread boven nul, gegeven haar
  eigen onzekerheid.

## Kandidaat B — gestratificeerd N1

- Per week vier strata (Q=4) binnen U(t) op gemiddelde |r_rel| over t-12..t-1; munten met minder dan 4 geldige
  waarnemingen in het venster vormen een eigen onbekend-stratum. De nultrekking trekt per stratum exact evenveel
  munten als het sensormandje daar heeft (de oorspronkelijk geselecteerde munten mogen terug in de trekking).
  Geen samenvoegregel: een stratum bevat per definitie minstens de munten die de sensor eruit koos.
- Q=4 en niet Q=5: met k = N/5 valt het hoogste kwintiel vrijwel samen met het mandje van een 12w-hoog-sensor,
  waardoor de nul bijna hetzelfde mandje zou worden gedwongen. Bij Q=4 blijft ~80% van het hoogste stratum
  vrije randomisatie. Geen per-munt-matching: dat voegt vrijheidsgraden toe (afstandsmaat, caliper, replacement).
- LET OP, andere nulhypothese: B toetst "hogere spread dan een willekeurig mandje met dezelfde
  volatiliteitsblootstelling" — incrementele alpha gegeven volatiliteit. Een echte volatiliteitspremie wordt
  weggeconditioneerd. Dat is een inhoudelijke verschuiving van de onderzoeksvraag en de reden dat A bij gelijke
  uitkomst voorgaat.

## Blinde kalibratie — beide kandidaten door dezelfde meetlat

Zelfde stress-opzet als N1_STRESS (DESIGN_N1_STRESS_20261001): 7 placebo's (1/4/12w hoog/laag, uniform per run),
week-Rademacher primair, productiecode-keten, selectie en strata altijd op de oorspronkelijke residuen,
Clopper-Pearson per cel, master seed 20261004, deterministische substromen per (kandidaat, placebo, run).

- Fase 1: 1.000 runs per cel, beoordeling bij alpha 0,05. Een kandidaat met een ANTI_CONSERVATIEF-oordeel op
  enige placebo stopt daar (FAIL); geen extra runs verbranden.
- Fase 2: overlevers worden per cel uitgebreid tot 4.000 runs (zelfde seeds, runs 0-999 identiek) en beoordeeld
  bij alpha 0,05 EN alpha 0,0083. Bij 4.000 runs is een echte size van ~0,015 met ~99% zekerheid zichtbaar,
  ~0,02 vrijwel zeker; kleinere afwijkingen niet — dat staat zo in het rapport.
- SLAAGCRITERIUM per kandidaat: geen placebo ANTI_CONSERVATIEF bij 0,05 (fase 1 en 2) noch bij 0,0083 (fase 2);
  uniforme controle GEEN_AANWIJZING op beide. CONSERVATIEF is toegestaan en wordt als powerverlies gerapporteerd.
- "Geen bewijs voor anti-conservatisme" is niet "bewezen correct": elke cel rapporteert fractie plus
  95%-interval, en de Holm-aanname (superuniforme p's) blijft een aanname die eindig Monte-Carlo-werk niet kan
  bewijzen.
- [C] Bekende asymmetrie: B slaagt op de volatiliteitsplacebo's bijna per constructie (hij matcht precies wat
  zij doen). B's size-kalibratie draagt dus minder informatie dan die van A; voor B zijn de uniforme controle en
  de power de onderscheidende metingen. Dit staat in het rapport bij de B-resultaten.

## Powermeting (alleen bij size-PASS van beide; anders wint de enige overlever)

- Sensor-conditionele injectie op de vaste paden 1w-hoog, 4w-hoog en 12w-hoog. Niet alleen 12w: B conditioneert
  zelf op 12w-volatiliteit en zou daar een thuiswedstrijd krijgen.
- [C] Per run eerst een week-Rademacher-realisatie op de residuen (maakt de verwachte spread van het vaste pad
  exact nul zonder die spread te meten; |r| en dus selectie en strata veranderen niet), daarna +delta op de
  padmunten en -delta*k/(n-k) op de rest. E[spread] = delta, outcome-vrij.
- 300 runs per (kandidaat, pad, delta), delta 0,4 / 0,6 / 0,8 %/week, detectie bij alpha 0,0083.
- KEUZEREGEL: size gaat voor power. Slagen beide, dan wint de hoogste GEMIDDELDE detectiekans over de drie paden
  bij delta = 0,4% (de economisch relevante zone, kostenhorde 0,38-0,45%); delta 0,6 en 0,8 zijn diagnostiek en
  alle padresultaten worden getoond. Ligt het verschil bij 0,4% binnen de Monte-Carlo-ruis (overlappende
  95%-intervallen van de gemiddelden), dan wint A, omdat A de oorspronkelijke estimand bewaart.
- Slaagt geen van beide: terug naar ontwerp; geen derde kandidaat improviseren.

## Na de keuze

- De verliezer blijft als gekalibreerd artefact in de repo staan; anders is de keuze achteraf onverifieerbaar.
- Begrensde validiteitsclaim: het gekozen instrument is gekalibreerd tegen uniforme selectie en tegen de
  1/4/12-weekse volatiliteitstilts. Liquiditeits-, leeftijds- of andere structurele tilts vallen buiten deze
  validatie totdat ze afzonderlijk zijn getoetst.
- De powerkalibratie van 28-09 (uniforme selectie) wordt voor tiltende sensoren vervangen door de
  sensor-conditionele powercijfers van het gekozen instrument; de delta_min-verwijzingen in beleid punt 4 worden
  dan bijgewerkt, als expliciete beleidswijziging na de keuze.
- Kandidaat A verandert de teststatistiek, niet de estimand; beleid punt 1 krijgt bij keuze voor A een
  expliciete aanvulling, geen stille herlezing.

## Rekenbudget

Fase 1: 2 x 7 x 1.000 x ~3,6 s ~ 14 uur per kandidaat (A iets trager door HAC per trekking). Fase 2: per
overlever 7 x 3.000 x ~3,6 s ~ 21 uur. Power: ~3 uur per kandidaat. Totaal 3 tot 6 nachten, afhankelijk van
hoeveel kandidaten fase 1 overleven.

## Voorspellingen (gokken, geen acceptatieband; MC-SE ~0,007 bij 1.000 runs)

- A, fase 1, alpha 0,05: uniforme controle 0,04-0,06; hoog-placebo's 0,04-0,08 — HAC repareert het grootste
  deel, maar mogelijk niet alles omdat de nul uniform geselecteerde mandjes blijft trekken. Dat A uberhaupt
  fase 1 haalt: eerder wel dan niet, zonder overtuiging.
- B, fase 1, alpha 0,05: alle placebo's 0,04-0,06 (grotendeels per constructie); uniforme controle 0,04-0,06.
- Fase 2, alpha 0,0083, overlevers: 0,005-0,012 per cel.
- Power bij delta 0,4%, gemiddeld over de drie paden: beide 0,10-0,35, en lager dan de 0,32 van de uniforme
  kalibratie, omdat volatiele mandjes ruisiger zijn; A en B binnen 0,10 van elkaar.

## Wat deze toets niet doet

Geen rendementen beoordelen (de Rademacher-stap in de powermeting bestaat juist om de echte padspread niet te
hoeven kennen). Holdout dicht. Geen parametrisch model. Liquiditeit en leeftijd buiten scope.
