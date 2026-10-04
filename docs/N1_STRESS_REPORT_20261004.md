# N1-stresstest onder niet-uniforme selectie — rapport (04-10-2026)

Instrumenttoets, geen sensortoets. Ontwerp: `docs/DESIGN_N1_STRESS_20261001.md`, geregistreerd in 97c3b69 voor
er code bestond. Code: `scripts/n1_stress.py` (84067b9). Resultaten: 0623ad3 (primair), block3 (gevoeligheid A),
13e1334 (Mammen, gevoeligheid B), uniforme controle 4.000 runs. Artefacten: de vier
`docs/N1_STRESS_*.json`-bestanden; per cel 95%-Clopper-Pearson.

## Oordeel (beslisregel uit het ontwerp, primaire variant, 1.000 runs)

N1 is ANTI-CONSERVATIEF voor sensoren die naar hoge trailing volatiliteit tilten, op beide drempels, en het
oordeel is robuust onder blokomkering en Mammen-gewichten:

| placebo | primair a=0,05 | primair a=0,0083 | oordeel |
|---|---|---|---|
| 1w-hoog | 0,119 [0,100, 0,141] | 0,044 | ANTI_CONSERVATIEF |
| 4w-hoog | 0,095 [0,078, 0,115] | 0,042 | ANTI_CONSERVATIEF |
| 12w-hoog | 0,151 [0,129, 0,175] | 0,044 | ANTI_CONSERVATIEF |
| 1w-laag | 0,036 [0,025, 0,049] | 0,007 | CONSERVATIEF |
| 4w-laag | 0,041 [0,030, 0,055] | 0,004 | GEEN_AANWIJZING |
| 12w-laag | 0,024 [0,015, 0,036] | 0,001 | CONSERVATIEF |
| uniform (controle) | 0,045 [0,033, 0,060] | 0,009 (vlag) | GEEN_AANWIJZING |

Een informatieloze sensor die consequent de volatielste munten kiest, haalt p < 0,0083 dus ~5x te vaak.
De uniforme controle kreeg bij 0,0083 de meer-runs-vlag; met 4.000 runs: 0,052 [0,045, 0,059] bij a=0,05 en
0,0075 bij a=0,0083, GEEN_AANWIJZING. De toetsketen zelf is nominaal; de fout zit in de selectie.

## Gevoeligheden (veranderen het oordeel per ontwerp niet)

| placebo | primair | blok-3 | Mammen | (a=0,05) |
|---|---|---|---|---|
| 1w-hoog | 0,119 | 0,097 | 0,082 | |
| 4w-hoog | 0,095 | 0,097 | 0,121 | |
| 12w-hoog | 0,151 | 0,169 | 0,134 | |
| uniform | 0,045 | 0,054 | 0,051 | |

Alle hoog-placebo's blijven anti-conservatief in beide gevoeligheden (ook bij a=0,0083: blok-3 0,022-0,069,
Mammen 0,025-0,052). De afwijkingen van primair zijn groter dan de voorspelde 0,02 en gaan twee kanten op;
volgens de ontwerpformulering: hogere-moment- en asymmetrie-effecten zijn materieel en het verschil is niet
uitsluitend aan scheefheid toe te schrijven. Q <= 0,0096 overal: een echt gemiddeld placebo-effect speelt
vrijwel geen rol in het tweede moment.

## Voorspellingen vooraf (97c3b69) en uitkomst

| voorspelling | uitkomst | |
|---|---|---|
| uniform 0,04-0,06 bij a=0,05 | 0,045 | goed |
| 1w-hoog 0,10-0,20 | 0,119 | goed |
| 4w-hoog en 12w-hoog 0,07-0,15 | 0,095 en 0,151 | 4w goed; 12w net erboven |
| 1w-hoog het hoogst | volgorde 12w > 1w > 4w | fout |
| laag-placebo's onder 0,04 | 0,036 / 0,041 / 0,024 | 4w-laag erboven |
| Q onder 0,02 | max 0,0096 | goed |
| A en B hooguit 0,02 van primair | tot 0,037 (Mammen 1w-hoog) | fout |

De fout die telt: het voorspelde mechanisme was kortetermijnpersistentie van |r| (lag 1 +0,23), dat 1w als
sterkste placebo voorspelt. Dat 12w het sterkst is, past bij blijvende volatiliteitsverschillen tussen munten:
een 12-weeks gemiddelde kiest consequent de structureel wilde munten, een week vooral ruis. Niet getoetst;
relevant voor de stratificatiekeuze in de reparatie.

## Gevolgen

1. Beleid 119195a punt 7 is nu met meting onderbouwd: geen sensor die op volatiliteit (of verwante kenmerken)
   tilt wordt geregistreerd zolang N1 de enige nul is. Vrijwel elke prijsgebaseerde sensor tilt zo.
2. Reparatie zoals het beleid vastlegt: een gestudentiseerde toets EN een gestratificeerd N1 naast elkaar
   bouwen, beide blind kalibreren (zelfde stress-opzet als hier), dan pas kiezen. De Mammen-afwijkingen
   suggereren dat variantienormalisatie alleen mogelijk niet volstaat; de 12w-bevinding suggereert stratificatie
   op een lang volatiliteitsvenster. Beide zijn ontwerpinput, geen besluit.
3. De powerkalibratie van 28-09 geldt alleen voor uniforme selectie en moet voor een tiltende sensor onder de
   gerepareerde nul opnieuw.
4. T001 blijft FAIL; een anti-conservatieve nul maakt p alleen te klein, dus die uitkomst wordt er niet zwakker op.

## Beperkingen

Geconstrueerde mean-zero stress-nul, geen bewezen exacte nul van de echte return-DGP. Een vast selectiepad per
placebo uit deze 156 weken: generalisatie naar andere marktperiodes is een aparte, grotere onzekerheid dan de
Monte-Carlo-fout. Liquiditeit en leeftijd vallen buiten deze toets (niet in de residuenmatrix). no_trade overal
False.
