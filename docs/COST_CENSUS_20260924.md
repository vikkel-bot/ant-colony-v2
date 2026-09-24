# Kostenmeting Bitvavo — 22 t/m 24 september 2026

Script: scripts/cost_census.py (4c59463). Ruwe samenvatting: COST_CENSUS_20260924.json.
8 snapshots (05:51/11:51/17:51/23:51 UTC+2 over 3 dagen), 172 markten = U(ref) bij
drempel EUR 50.000. Meting, geen toets: geen hypothese, geen rendementen.

Kosten per kant, EXCLUSIEF tarief (mediaan over 8 snapshots):

| kwartiel | dagomzet | 1k | 5k | 10k | 25k |
|---|---|---|---|---|---|
| Q1 laagste | 50k-77k | 0,138% | 0,237% | 0,349% | 0,802% |
| Q2 | 78k-148k | 0,132% | 0,197% | 0,312% | 0,742% |
| Q3 | 149k-379k | 0,101% | 0,171% | 0,241% | 0,584% |
| Q4 hoogste | 383k-33M | 0,054% | 0,079% | 0,096% | 0,134% |

Boek te dun: 0 gevallen, in elk kwartiel en bij elke ordergrootte.
Bitvavo-tarief taker: 0,25% per kant (<EUR 100k/maand), 0,20% daarboven;
maker 0,15% resp. 0,10%.

Gevolgen:
- Round-trip bij 1k in Q1 = 2 x (0,25% + 0,138%) = 0,78%, dus HOGER dan de
  0,5% die T001 aannam. T001 wordt hier niet op herzien (FAIL stond op
  significantie; kostenaanname achteraf wijzigen is regelwijziging na uitkomst).
- Bij ordergroottes van 1k-5k domineert het TARIEF, niet de impact.
- Bij 44% omloop per week (T001-meting) en 0,8% round-trip: 0,35% kosten per
  week; kostenhorde (x3) vraagt dan ruim 1%/week bruto.

Beperkingen:
- Rustende liquiditeit op een meetmoment; quotes kunnen wegtrekken. Voor orders
  die een aanzienlijk deel van de dagomzet beslaan is de werkelijke impact
  waarschijnlijk hoger dan deze boekwandeling suggereert. [Likely]
- Huidige boeken; geen historische orderboeken beschikbaar voor 2022-2025.
- Eén gemist meetmoment (23-09 23:51) door een herstart van de pc.
