# N1-reparatie — eindrapport HAC-instrument (11-10-2026)

Status: RAPPORT. Dit document beslist niets. Het legt vast wat gemeten is, wat daaruit volgt en wat niet.
Beleidsbeslissingen (punt 1 van de beleidsaanvulling, de eligibility-regel, brokerkeuze) volgen apart.

## 1. Conclusie

1. Kandidaat strat (gestratificeerd N1) is FAIL en blijft als gekalibreerd artefact staan. Stratificatie repareert alleen de eigen as (1w/4w-hoog anti-conservatief, 0,070).
2. Kandidaat HAC (S1 Newey-West-gestudentiseerd, Bartlett, lag 3, identiek op observed en nul) is nominaal onder een iid-week-Rademacher stress-nul met volatiliteitstilt (fase 1 en 2).
3. HAC passeert de vooraf vastgelegde dependence-gate onder block-4 en block-8 (12 size-tests, Holm 5%). Eén cel is krap: block-8, 12w-hoog, alpha 0,0083 gaf 49 verwerpingen in 4.000 runs (0,0123).
4. Eligibility-MDE80 en MDE90 volgens de vooraf vastgelegde regel: niet bereikt binnen het grid, dus groter dan 1,0 %/week. Die uitkomst hangt aan één cel (block-4, 12w-hoog: 799 van 1.000 bij 1,0 %).
5. Praktisch: met 156 weken zijn alleen edges rond 1 % per week betrouwbaar bevestigbaar. De blinde vlek (verhandelbaar maar niet detecteerbaar) is groot en hoort in elke preregistratie.

## 2. Wat is gedraaid, op welke code

| Stap | Commit |
|---|---|
| N1-stresstest ontwerp / rapport | 97c3b69 / 632e075 |
| Reparatie-ontwerp | e3a67bf |
| Reparatiecode | 68d062a |
| Fase 1 / fase 2 (HAC, 4.000 runs) | c8c0369 / 3f7cd9c |
| Powermeting-addendum (na drie adviesrondes) | 1945c1c |
| Powermeting-code (n1_repair_power.py) | 196d784 |
| Resultaten Deel A en B | 0035618 |
| Resultaat Deel C | 4edc596 |
| Printfix (alleen uitvoertekst) | f1475ee |

Master seed fase 1/2: 20261004. Master seed powermeting: 20261006. Seeds zijn deterministisch per cel en run.
Verwerping is p < alpha, gelijk aan fase 2. Met 10.000 nultrekkingen is (1 + draws) x 0,0083 = 83,008, geen geheel getal, dus < en <= zijn hier identiek.

## 3. Claim en begrenzing

Wat aangetoond is:
- Fase 1 (1.000 runs per cel, alpha 0,05): alle cellen 0,048 tot 0,060.
- Fase 2 (4.000 runs): alle cellen GEEN_AANWIJZING op alpha 0,05 en 0,0083 (0,047 tot 0,053, respectievelijk 0,0057 tot 0,0107).
- Kanttekening: bij 4.000 runs is een werkelijke size van 0,015 zichtbaar, 0,010 niet. Puntschattingen bij 0,0083 liggen deels boven de drempel (tot 0,0107), binnen Monte-Carlo-onzekerheid.

Wat NIET getoetst is:
- Fase 1 en 2 draaiden uitsluitend onder een iid-week-Rademacher. Dat verwijdert de lineaire autocovariantie in verwachting. Het succes kan niet aan de lagcorrectie worden toegeschreven. Studentisering is de voornaamste plausibele verklaring, niet bewezen.
- Validiteit onder seriële afhankelijkheid is pas getoetst in Deel B (block-4 en block-8), op drie vaste hoog-volatiliteitspaden.
- Geen uitspraak over andere selectiegeometrieën, liquiditeit, muntleeftijd of een andere lag dan 3. Lag 3 is niet aangepast.
- De block-DGP's zijn stress-constructies, geen schatting van de echte afhankelijkheid.

Formulering bij een block-8 uitkomst: een FAIL had betekend dat HAC lag 3 niet size-valid is gebleken binnen de vooraf gekozen block-8 stress. Niet dat de echte cryptoreeks te veel afhankelijkheid heeft.

## 4. Deel B: dependence-size-gate (delta = 0, 4.000 runs per cel)

| DGP | pad | fractie a=0,05 | fractie a=0,0083 | binomiale p (0,05) | binomiale p (0,0083) |
|---|---|---|---|---|---|
| block-4 | 1w-hoog | 0,0473 | 0,0070 | 0,7970 | 0,8399 |
| block-4 | 4w-hoog | 0,0442 | 0,0085 | 0,9579 | 0,4678 |
| block-4 | 12w-hoog | 0,0498 | 0,0092 | 0,5391 | 0,2762 |
| block-8 | 1w-hoog | 0,0350 | 0,0037 | 1,0000 | 0,9999 |
| block-8 | 4w-hoog | 0,0320 | 0,0030 | 1,0000 | 1,0000 |
| block-8 | 12w-hoog | 0,0558 | 0,0123 | 0,0531 | 0,0058 |

Familie: 12 eenzijdige exacte binomiale toetsen, Holm 5%. Kleinste p = 0,0058; eerste Holm-drempel = 0,05/12 = 0,0042. Geen verwerping, oordeel GATE PASS.

Krappe cel: block-8, 12w-hoog, alpha 0,0083: 49 verwerpingen (0,0123, ongeveer 1,5x nominaal). De drempel bij de strengste Holm-stap is 50 verwerpingen. Eén verwerping meer was een FAIL geweest met een nieuwe ontwerpronde.

Werkingskarakteristiek van de gate (kans dat een cel op de strengste Holm-stap verwerpt, 4.000 runs):

| ware size | alpha 0,05 | alpha 0,0083 |
|---|---|---|
| 1,25x | 0,79 | 0,11 |
| 1,5x | 1,00 | 0,51 |
| 2,0x | 1,00 | 0,98 |

De gate vangt bij alpha 0,0083 een inflatie van 25 % slechts in ongeveer 1 op 9. De PASS sluit matige inflatie niet uit.

Bevindingen block-8: op 1w en 4w toetst HAC conservatief (0,0037 en 0,0030), op 12w iets te ruim. De stap-voor-stap hypothese "lag 3 is te kort voor langere afhankelijkheid" verklaart het 12w-resultaat niet, want R_block is daar 1,05 (zie sectie 7).

## 5. Power

Detectie bij p < 0,0083, eenzijdig, delta = exacte S1-verschuiving in %/week. Selectiepad bevroren op de oorspronkelijke data vóór flip en injectie: power conditioneel op drie vooraf bekende geometrieën, geen end-to-end-power van een adaptieve sensor.

Deel A (iid, gepreregistreerde diagnostiek volgens e3a67bf, 300 runs per cel, geen rol in eligibility):

| pad | 0,4 % | 0,6 % | 0,8 % |
|---|---|---|---|
| 1w-hoog | 0,143 | 0,403 | 0,657 |
| 4w-hoog | 0,220 | 0,433 | 0,730 |
| 12w-hoog | 0,170 | 0,343 | 0,600 |

Deel C (block-4 en block-8, raw power; * = uitgebreid naar 1.000 runs volgens de CP-regel):

| DGP | pad | 0,4 % | 0,6 % | 0,8 % | 1,0 % |
|---|---|---|---|---|---|
| block-4 | 1w-hoog | 0,213 | 0,470 | 0,742* | 0,947 |
| block-4 | 4w-hoog | 0,163 | 0,407 | 0,687 | 0,883* |
| block-4 | 12w-hoog | 0,163 | 0,363 | 0,550 | 0,799* |
| block-8 | 1w-hoog | 0,213 | 0,497 | 0,816* | 0,970 |
| block-8 | 4w-hoog | 0,137 | 0,410 | 0,750* | 0,940 |
| block-8 | 12w-hoog | 0,110 | 0,343 | 0,570 | 0,801* |

MDE (lineaire interpolatie tussen gridpunten; leeg = niet bereikt):

| DGP | pad | MDE80 | MDE90 |
|---|---|---|---|
| block-4 | 1w-hoog | 0,857 % | 0,954 % |
| block-4 | 4w-hoog | 0,915 % | > 1,0 % |
| block-4 | 12w-hoog | > 1,0 % | > 1,0 % |
| block-8 | 1w-hoog | 0,790 % | 0,909 % |
| block-8 | 4w-hoog | 0,853 % | 0,958 % |
| block-8 | 12w-hoog | 0,999 % | > 1,0 % |

Eligibility (slechtste over 3 paden en 2 DGP's): MDE80 > 1,0 % en MDE90 > 1,0 % per week.

Marge: block-4 12w-hoog haalde 799 van 1.000 bij 1,0 %. Eén verwerping meer was 0,800 geweest. Het 95%-interval loopt van ongeveer 0,77 tot 0,82. De meting kan "net onder" en "net boven" 80 % niet onderscheiden. De regel is vooraf vastgelegd en wordt niet afgerond; de werkelijke MDE80 van deze cel ligt vermoedelijk rond 1,0 %.

Size-gecorrigeerde curve (diagnostiek, verandert niets aan het oordeel): bij block-8/12w-hoog is de gecorrigeerde power bij 1,0 % 0,720 tegenover raw 0,801. De raw MDE80 van die cel (0,999 %) is dus iets te gunstig. Bij block-8 op 1w en 4w ligt de gecorrigeerde power hoger dan raw, omdat HAC daar conservatief toetst.

## 6. Voorspellingen vooraf (Claude) tegenover uitkomst

Vastgelegd in het addendum (Deel A, alpha 0,0083): 0,4 % -> 15 tot 30 %; 0,6 % -> 45 tot 65 %; 0,8 % -> 70 tot 90 %.
Uitkomst: 3 van 9 cellen binnen het bereik (0,4 %: 4w en 12w binnen, 1w 0,143 net eronder; 0,6 %: alle drie eronder; 0,8 %: alleen 4w binnen). Systematisch te optimistisch. De onderbouwing (lineair terugrekenen vanaf de kalibratie van 28-09) was zwak.

Voor Deel C in de chat gegeven, niet in het addendum: block-4 5 tot 10 procentpunt onder iid, block-8 nog lager, block-8/12w hoger door de te ruime size, MDE80 niet bereikt. Uitkomst: 1 van 4 juist (MDE80 niet bereikt, met een marge van één run). Het blijkt dat seriële afhankelijkheid hier nauwelijks power kost; block-8 gaf op 1w en 4w zelfs hogere power dan block-4; block-8/12w gaf juist de laagste power.

## 7. Diagnostiek R = Var_HAC / Var_iid van het gemiddelde

| pad | R_native (originele spreadreeks) | R_block-4 mediaan [q25-q75] | R_block-8 mediaan [q25-q75] |
|---|---|---|---|
| 1w-hoog | 0,79 | 0,85 [0,78-0,92] | 0,81 [0,77-0,86] |
| 4w-hoog | 0,88 | 0,96 [0,89-1,04] | 0,93 [0,89-0,98] |
| 12w-hoog | 1,06 | 1,06 [0,98-1,15] | 1,05 [1,01-1,10] |

Lezing (voorzichtig): op 1w en 4w ligt R_native onder 1, dus de HAC-variantie is kleiner dan de iid-variantie, wat bij licht negatieve seriële afhankelijkheid past. Op 12w is R ongeveer 1. De block-DGP's laten R dicht bij R_native uitkomen. Dat past bij de uitkomst dat afhankelijkheid hier weinig power kost. R heeft geen beslisfunctie; in een eventuele volgende ontwerpronde mag R_native dienen om te beoordelen hoe realistisch block-8 was.

## 8. Wat dit betekent voor EUR 10.000

Aanname: kostenhorde midden van 0,38 tot 0,45 %/week = 0,42 %/week. Meerrendement ten opzichte van het muntenuniversum, geen absolute winst. "Test ziet het" = bandbreedte over de zes block-cellen.

| Echte edge per week | Bruto per week | Netto per week | Netto per jaar | Test ziet het |
|---|---|---|---|---|
| 0,4 % | 40 | ca. 0 | ca. 0 | 11 tot 21 % |
| 0,6 % | 60 | 18 | ca. 940 | 34 tot 50 % |
| 0,8 % | 80 | 38 | ca. 1.980 | 55 tot 82 % |
| 1,0 % | 100 | 58 | ca. 3.020 | 80 tot 97 % |

Een sensor zonder edge keurt de test in de slechtste cel goed in ongeveer 1 op 80 gevallen (0,0123), nominaal 1 op 120. Zo'n sensor kost aan handelskosten ongeveer 2.200 per jaar tot de holdout hem ontmaskert.
Een sensor die netto 1.000 tot 2.000 per jaar zou opleveren, wordt in ongeveer de helft van de gevallen niet gezien.

## 9. Open punten (niet in dit rapport beslist)

1. Beleidsaanvulling punt 1: estimand blijft S1, teststatistiek wordt HAC-t, de powerkalibratie van 28-09 wordt vervangen door de cijfers van sectie 5.
2. Wat de eligibility-regel doet bij MDE80 > 1,0 %/week: praktisch komt geen prijssensor erdoor. Dit is een programmabeslissing (opties met gevolgen horen in de beleidsaanvulling).
3. Literatuurprotocol en routes L / T / X: beleidsaanvulling.
4. Brokerkeuze (kosten, X-Perps-voorwaarde van OKX Europe) staat geparkeerd tot er een sensor door de holdout komt.
5. Meer weken data of een ander ontwerp is de enige route naar lagere MDE; dat is geen onderdeel van dit instrument.

## 10. Reproduceerbaarheid

- Bestanden: docs/N1_REPAIR_POWER_A_20261006.json, docs/N1_REPAIR_POWER_B_20261006.json, docs/N1_REPAIR_POWER_C_20261007.json.
- Commando's: scripts/n1_repair_power.py --part A / B / C (C met --gate op het B-bestand). Deel C weigert zonder compleet B-bestand met oordeel PASS.
- Printfix f1475ee wijzigt alleen de uitvoertekst bij een onvolledige Deel-B-run.
