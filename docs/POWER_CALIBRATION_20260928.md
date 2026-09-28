# Powerkalibratie xs_rank_test op de residuenmatrix (28-09-2026)

Meting, geen keuze. Gemeten is hoe vaak het bestaande cross-sectionele instrument
(`ant_colony.lab.xs_rank_test`: `spreads`, nulmodel N1 `null_random_selection`,
`p_values`) een effect van bekende grootte terugvindt in de echte residuen.

- Instrument: `scripts/power_calibration.py`, commit 062273d; tests `tests/test_power_calibration.py` (8).
- Data: `docs/NOISE_STRUCTURE_20260926.json`, ontwikkelblok, 156 weken, 274 munten. Holdout niet aangeraakt.
- Injectie: per week k(t) = max(8, n // 5) willekeurige munten +delta, de overige -delta * k / (n - k);
  weekgemiddelde blijft exact gelijk, dus delta = bruto spread boven universum per week.
- Sensor: perfect, selecteert de geinjecteerde deelverzameling. Dit is het plafond; een echte sensor haalt minder.
- Reps 300 per delta, draws 10.000 (= N_DRAWS van het instrument), seed 20260928. Standaardfout ~0,03 per cel bij power 0,8.
- `no_trade` staat overal op False: delisting-afhandeling valt buiten deze meting.
- Alleen N1 gekalibreerd; N2 (blok-bootstrap) niet.
- Artefact: `docs/POWER_CALIBRATION_20260928.json`.

## Detectiekans (fractie van 300 injecties met p < alpha)

| delta/week | S1 a=0.05 | S2 a=0.05 | S3 a=0.05 | S1 a=0.0083 | S2 a=0.0083 | S3 a=0.0083 |
|---|---|---|---|---|---|---|
| 0,00% | 0,040 | 0,037 | 0,027 | 0,000 | 0,003 | 0,007 |
| 0,20% | 0,240 | 0,270 | 0,233 | 0,057 | 0,083 | 0,070 |
| 0,40% | 0,637 | 0,713 | 0,700 | 0,323 | 0,447 | 0,440 |
| 0,60% | 0,920 | 0,943 | 0,960 | 0,680 | 0,823 | 0,810 |
| 0,80% | 1,000 | 1,000 | 1,000 | 0,957 | 0,987 | 0,990 |
| 1,00% | 1,000 | 1,000 | 1,000 | 1,000 | 1,000 | 1,000 |

S1 = gemiddelde, S2 = mediaan, S3 = fractie positieve weken. alpha 0,0083 = familiedrempel eerste toets (0,05 / 6).

Lineair geinterpoleerd 80%-punt voor S1: ~0,52%/week bij alpha 0,05; ~0,69%/week bij alpha 0,0083.
Ter vergelijking, analytisch (alpha onbekend): delta_min 0,534% (Universe v2), ~0,62% (T001-ontwerp).

Voor S2: ~0,58%/week (80%) en ~0,69% (90%) bij alpha 0,0083. Voor S1 bij alpha 0,0083, 90%: ~0,76%.

Bij delta = 0 ligt de puntschatting onder of op de nominale alpha. Met 300 herhalingen (95%-bovengrens ~1,2% bij 0/300)
is dat geen aanwijzing voor anti-conservatisme; het bewijst ook geen conservatisme.
S2 haalt bij elke delta >= 0,40% ten minste de power van S1, bij beide alpha's.

## Niveauverschil per munt (meeneemvraag)

Spreiding van het gemiddelde r_rel per munt (274 munten met >= 30 weken): std 0,01430.
Nulverdeling door muntlabels binnen elke week te herschikken (1.000 trekkingen): mediaan 0,01460, p95 0,01688.
p = 0,62. Er is in dit blok geen meetbaar blijvend niveauverschil tussen munten.

## Voorspellingen vooraf en uitkomst

| voorspelling | uitkomst | |
|---|---|---|
| delta 0, S1, a=0.05 in 0,03-0,07 | 0,040 | goed |
| delta 0, S1, a=0.0083: 1-5 van 300 | 0 | fout; minder detecties dan verwacht, binnen de Monte-Carlo-onzekerheid |
| 80% S1 bij a=0.0083 in 0,60-0,75% | ~0,69% | goed |
| 80% S1 bij a=0.05 in 0,35-0,45% | ~0,52% | fout, te optimistisch na rookproef R=20 |
| S2 >= S1 bij a=0.0083 voor delta >= 0,4% | ja | goed |
| niveauverschil p in 0,3-0,9 | 0,62 | goed |

Les: een rookproef met R = 20 is een werkingstest, geen basis voor een schatting.

## Wat hier niet uit volgt

Geen keuze van target power, alpha of delta_econ; geen keuze om S2 boven S1 te stellen. Dat zijn
beleidskeuzes voor de preregistratie van sensor 2 (`docs/RESEARCH_OPEN_ITEMS.md`).

Correctie 28-09-2026 na externe review: conservatisme-claim teruggebracht tot wat 300 herhalingen dragen;
S2- en 90%-punten toegevoegd (eerder in chat als 0,68 genoemd, juist is 0,69). Metingen ongewijzigd.
