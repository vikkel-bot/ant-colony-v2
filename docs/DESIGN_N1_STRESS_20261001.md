# Ontwerp N1-stresstest onder niet-uniforme selectie (01-10-2026)

Preregistratie van een instrumenttoets, geen sensortoets. Gecommit voordat er code bestaat. Uitvoering van
beleid 119195a punt 7: geen sensor die op volatiliteit tilt wordt geregistreerd voordat deze toets is gedaan.
Ontwerp tot stand gekomen in twee adviesrondes (01-10-2026); toevoegingen van Claude zijn gemarkeerd met [C].

## Vraag

Maakt alleen het kiezen van conditioneel volatielere (of minder volatiele) munten N1 al vals-significant?
N1 = per week k(t) munten uniform willekeurig uit U(t), 10.000 trekkingen, p = (1 + #)/(1 + B), eenzijdig.

## Data

`docs/NOISE_STRUCTURE_20260926.json`, residuen r_rel(i,t), ontwikkelblok, 156 weken, 274 munten.
U(t) = munten met een residu in week t. Holdout niet aangeraakt.

## Nul: geconstrueerde stress-nul

De week-Rademacher is een geconstrueerde mean-zero stress-nul, geen claim dat de echte return-DGP
teken-symmetrisch is. Per week wordt de hele cross-sectionele vector met hetzelfde gewicht w_t vermenigvuldigd.
Selectie hangt alleen af van historische |r| en is dus onafhankelijk van w_t; conditioneel op de waargenomen
placebospread s_t is E[w_t * s_t] = 0. Co-beweging tussen munten binnen een week blijft intact.
Ondersteunend, niet bewijzend: het teken van r_rel heeft gemeten geen geheugen (lag 1 +0,010).

- Primair: Rademacher per week, w_t = +1 of -1 met kans 1/2.
- Gevoeligheid A: Rademacher in blokken van 3 weken. [C] Blokuitlijning per run willekeurig, offset uniform uit
  {0, 1, 2}; laatste blok mag korter zijn.
- Gevoeligheid B: Mammen per week, w_t = (1 - wortel5)/2 met kans (wortel5 + 1)/(2 wortel5) (~0,724) en
  (1 + wortel5)/2 met kans ~0,276. E(W)=0, E(W^2)=1, E(W^3)=1, E(W^4)=2. Een afwijking van primair wijst op
  hogere-moment/asymmetrie-effecten en kan niet uitsluitend aan scheefheid worden toegeschreven.

## Placebosensoren

Rangschikking op het gemiddelde van |r_rel| over de venster-weken voor t (t zelf niet). Selectie altijd op de
OORSPRONKELIJKE residuen, in alle varianten; het selectiepad per placebo ligt dus vast over primair, A en B.

| placebo | venster | kiest |
|---|---|---|
| 1w-hoog / 1w-laag | week t-1 | k(t) hoogste / laagste |
| 4w-hoog / 4w-laag | t-4 .. t-1 | k(t) hoogste / laagste |
| 12w-hoog / 12w-laag | t-12 .. t-1 | k(t) hoogste / laagste |
| uniform (controle) | - | per run een nieuw uniform pad, onafhankelijk van w en van N1 |

[C] Munten in U(t) zonder enig residu in het venster komen niet in de rangschikking. Zijn er dan minder dan
k(t) kandidaten, dan wordt aangevuld met een uniforme trekking uit de overige munten van U(t); het aantal weken
waarin dit gebeurt wordt per placebo gerapporteerd. Gelijke waarden: alfabetisch op munt, zoals in build_panel.
Weken waarin een placebo geen enkele kandidaat heeft (begin van het blok) vallen voor die placebo weg; het
aantal resterende weken wordt gerapporteerd. k(t) = max(8, n // 5) zoals in het instrument.

## Uitvoering

- Exact de productiecode: `spreads`, `null_random_selection`, `p_values` uit `ant_colony.lab.xs_rank_test`,
  B = N_DRAWS (10.000). Geen hergebruik van N1-trekkingen: elke run trekt een nieuwe N1-bank.
- 1.000 runs per (variant, placebo). Vaste master seed 20261001; per (variant, placebo, run) een eigen
  deterministische substroom (numpy SeedSequence met spawn_key).
- Rekentijd: 7 placebo's x 1.000 x ~3,6 s = ~7 uur per variant, ~21 uur totaal. Volgorde: primair, A, B.
- Vectoriseren of trekkingen hergebruiken mag later alleen na een parity-toets tegen deze procedure.

## Uitkomsten per (variant, placebo)

- Verwerpingsfractie bij alpha 0,05 en 0,0083, met 95%-Clopper-Pearson-interval.
- Diagnostiek Q = (gemiddelde s)^2 / gemiddelde(s^2) op de oorspronkelijke placebospread: het deel van het
  tweede moment dat van een eventueel echt gemiddeld effect komt. Q verandert geen oordeel.
- Monte-Carlo-onzekerheid (runs) en geschiedenisonzekerheid (een vast selectiepad uit 156 weken) worden apart
  benoemd; meer runs verkleinen alleen de eerste.

## Beslisregel [C]

Per placebo, primaire variant, alpha 0,05:
- ondergrens 95%-interval > 0,05: N1 anti-conservatief voor dit selectietype;
- bovengrens < 0,05: N1 conservatief;
- anders: geen aanwijzing voor een size-fout.
Ligt bij alpha 0,0083 de puntschatting boven 0,0083 terwijl het interval 0,0083 omsluit, dan worden voor die
placebo 3.000 extra runs gedaan voordat er een oordeel bij 0,0083 wordt gegeven.
De uniforme controle moet "geen aanwijzing" opleveren; doet hij dat niet, dan is de testopzet zelf verdacht en
worden de placebo-uitkomsten niet geinterpreteerd.
Gevolg: is een hoog- of laag-placebo in de primaire variant anti-conservatief of conservatief, dan worden een
gestudentiseerde toets en een gestratificeerd N1 naast elkaar gebouwd en blind gekalibreerd voordat een van beide
wordt gekozen (beleid 119195a punt 7). Gevoeligheden A en B veranderen het oordeel niet; ze bepalen alleen of de
conclusie robuust wordt genoemd.

## Voorspellingen (gokken, geen acceptatieband)

Monte-Carlo-standaardfout bij 1.000 runs: ~0,007 bij size 0,05; ~0,003 bij 0,0083.
- uniform: 0,04-0,06 bij alpha 0,05;
- 1w-hoog: 0,10-0,20; 4w-hoog en 12w-hoog: 0,07-0,15, met 1w-hoog het hoogst;
- laag-placebo's: onder 0,04;
- Q: onder 0,02 voor alle placebo's;
- A en B: hooguit 0,02 verschil met primair.

## Wat deze toets niet doet

Geen rendementen beoordelen: er wordt geen edge van een placebo gemeten of gerapporteerd behalve Q.
Liquiditeit en leeftijd zitten niet in de residuenmatrix en vallen buiten deze toets.
Een uitkomst "geen aanwijzing" bewijst niet dat N1 exact is; alleen dat deze stress-nul geen size-fout toont.
