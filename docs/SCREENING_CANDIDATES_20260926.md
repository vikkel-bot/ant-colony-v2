# Screening kandidaat-sensoren — 26-09-2026

Outcome-blind. Script: scripts/screen_sensors.py. Artefact:
SCREENING_CANDIDATES_20260926.json. Kostenmodel COST_MODEL_V1_20260924 (p75,
EUR 5.000/order). 156 weken, ontwikkelperiode; holdout fysiek afgeknipt.
Geen rendement na enig selectiemoment gelezen (getoetst: vergiftigde
toekomstige koersen veranderen geen enkel getal).

Vooraf vastgelegde aannames: buffer symmetrisch met marge 0,25; beide
richtingen gemeten; informatiedichtheid als TELLING, geen afgeleide delta_min.

| sensor | turnover | buffer | horde/week | buffer | persistence | tenure | episodes | manden |
|---|---|---|---|---|---|---|---|---|
| realized_vol_60d@hoog | 0,158 | 0,127 | 0,39% | 0,31% | 0,954 | 6,07 | 505 | 152 |
| realized_vol_60d@laag | 0,152 | 0,114 | 0,38% | 0,29% | 0,954 | 6,21 | 494 | 155 |
| beta_btc_90d@hoog | 0,151 | 0,113 | 0,38% | 0,28% | 0,958 | 6,48 | 473 | 153 |
| beta_btc_90d@laag | 0,174 | 0,130 | 0,45% | 0,34% | 0,958 | 5,68 | 540 | 154 |
| corr_btc_90d@hoog | 0,177 | 0,125 | 0,44% | 0,31% | 0,956 | 5,52 | 556 | 152 |
| corr_btc_90d@laag | 0,171 | 0,130 | 0,44% | 0,33% | 0,956 | 5,86 | 523 | 154 |
| dist_from_52w_high@hoog | 0,173 | 0,121 | 0,41% | 0,29% | 0,953 | 5,28 | 581 | 151 |
| dist_from_52w_high@laag | 0,149 | 0,106 | 0,40% | 0,28% | 0,953 | 6,38 | 481 | 150 |
| liquidity_30d@hoog | 0,090 | 0,060 | 0,19% | 0,12% | 0,965 | 10,65 | 288 | 149 |
| liquidity_30d@laag | 0,389 | 0,344 | 1,16% | 1,02% | 0,965 | 2,60 | 1180 | 156 |
| listing_age@hoog | 0,060 | 0,059 | 0,15% | 0,15% | 1,000 | 15,41 | 199 | 121 |
| listing_age@laag | 0,095 | 0,086 | 0,25% | 0,22% | 1,000 | 10,33 | 297 | 135 |
| size_marketcap | DATA INELIGIBLE | | | | | | | |

Ter referentie, NIET als kandidaat: T001-momentum had turnover 0,4425 en een
horde van 0,92%-1,33%/week. T001 blijft FAIL en wordt niet opnieuw getest.

## Waarnemingen

1. Alle prijsgebaseerde sensoren hebben een horde van 0,38%-0,45%/week; met
   buffer 0,28%-0,34%. Bij T001 was dat 0,92%-1,33%. De kostenklem die daar
   gold, geldt hier niet.
2. INDICATIEF, NIET VASTGESTELD: delta_min van 0,62%/week is gemeten voor het
   T001-ontwerp (turnover 44%/week). Deze sensoren houden posities 5-6 weken
   vast met weekpersistentie 0,95; het aantal onafhankelijke waarnemingen is
   dus kleiner dan 156 en de werkelijke detectiegrens hoger. Hoeveel hoger is
   NIET berekend: daarvoor bestaat geen vastgelegde procedure. Dit moet gemeten
   worden voordat een van deze kandidaten wordt gepreregistreerd.
3. Persistence is richting-onafhankelijk (omgekeerde rangorde geeft dezelfde
   rangcorrelatie). Turnover is dat niet: zie liquidity 0,090 vs 0,389.
4. listing_age heeft persistentie 1,000 omdat noteringsduur voor elke munt even
   hard groeit; 121 unieke manden op 156 weken. Weinig beslissingen, en de
   zwaarste survivorship-gevoeligheid.
5. liquidity_30d@laag is het duurst (1,16%/week): munten rond de toelatingsgrens
   wisselen voortdurend in en uit het universum en zijn zelf het duurst.
   Bovendien is dit de toelatingsvariabele van U(t) zelf.

## Bewust NIET gedaan

Geen samengestelde score, geen ranglijst, geen winnaar. Dat zou gewichten
introduceren die niemand heeft vastgelegd. De keuze van sensor 2 is een
afzonderlijke, menselijke beslissing.
