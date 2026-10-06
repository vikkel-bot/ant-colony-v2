# DESIGN — N1-reparatie: addendum powermeting HAC (06-10-2026)

Status: ONTWERP, bevroren bij commit van dit bestand. Hoort bij DESIGN_N1_REPAIR_20261004 (e3a67bf).
Uitgangspunt: fase 2 (3f7cd9c) — HAC (Newey-West-gestudentiseerde S1, Bartlett, lag 3, identiek op observed
en nul) GESLAAGD; strat FAIL en blijft als gekalibreerd artefact staan.
Totstandkoming: drie adviesrondes met externe adviseur (ChatGPT) plus keuzes van Vik, 06-10-2026.

## Aanleiding

1. De powermeting in e3a67bf was een keuzeregel tussen twee kandidaten. Met één overlever is dat doel
   vervallen. Het nieuwe doel is anders: de powerkalibratie van 28-09 vervangen door powercijfers voor de
   HAC-t-statistiek, als input voor de beleidsaanvulling (punt 1).
2. Code-inspectie (scripts/n1_repair.py: alleen multipliers("rademacher", ...), één teken per week) en het
   ontwerp ("week-Rademacher primair") tonen: de size-validatie van HAC in fase 1 en 2 draaide uitsluitend
   onder een iid-weekflip. Die verwijdert de lineaire autocovariantie in verwachting. Seriële afhankelijkheid,
   waarvoor de lags bestaan, is dus nooit getoetst.

## Claimcorrectie (gaat ook in N1_REPAIR_REPORT)

HAC lag 3 herstelde nominale size onder volatiliteitstilt binnen een iid-week-Rademacher stress-nul.
Validiteit onder seriële afhankelijkheid is daarbij niet getest. Het succes van fase 2 kan niet aan de
lagcorrectie worden toegeschreven; studentisering is de voornaamste plausibele verklaring (niet bewezen).

## Bevroren tijdens dit addendum

- Estimand S1; statistiek HAC-t, Newey-West, Bartlett, lag 3. Lag 3 wordt binnen deze ronde niet aangepast.
- Productiecode-keten (ant_colony.lab.xs_rank_test: spreads, null_random_selection, p_values), N1 ongewijzigd.
- Paden: 1w-hoog, 4w-hoog, 12w-hoog. Selectie en strata uitsluitend op de oorspronkelijke, ongeflipte en
  ongeïnjecteerde residuen, vastgelegd vóór flip en vóór injectie.
- Claim blijft begrensd: power conditioneel op drie vooraf bekende selectiegeometrieën; geen end-to-end-power
  van een adaptieve sensor; liquiditeit/leeftijd apart.

## Definities

- Block-L-Rademacher (L = 4 of 8): de hele cross-sectionele weekvector van residuen krijgt per blok van L
  weken één gemeenschappelijk teken w = +1 of -1, kans 1/2, onafhankelijk tussen blokken. Blokoffset per run
  uniform uit {0, ..., L-1}, zoals de blok-3-variant van DESIGN_N1_STRESS_20261001. Waar mogelijk de bestaande,
  geteste multipliers-helper hergebruiken.
- Injectie, na de flip: week t, met N_t en k_t exact de munten die die week in de S1-berekening zitten ná alle
  eligibility- en missing-data-regels. Padmunten +delta; overige munten -delta*k_t/(N_t-k_t). Dan blijft het
  universumgemiddelde gelijk en is de verschuiving van S1 per week exact delta. Delta is gedefinieerd als
  S1-verschuiving in %/week, dezelfde eenheid als de kostenhorde.
- Detectie: eenzijdig, p <= 0,0083 bij de nominale productiegrens.

## Deel A — gepreregistreerde iid-power (exact volgens e3a67bf)

- Iid-week-Rademacher op de residuen, daarna injectie; 3 paden; delta 0,4 / 0,6 / 0,8 %/week; 300 runs per
  (pad, delta); detectie bij 0,0083.
- Geen delta 1,0, geen uitbreiding naar 1.000 runs, geen andere wijziging.
- Status: gepreregistreerde diagnostiek. Geen rol in MDE80-eligibility of instrumentkeuze.
- Doel als diagnostiek: het verschil iid -> block-4 toont hoeveel detectiekracht seriële afhankelijkheid kost.

## Deel B — dependence-size-gate (eerst, vóór elke delta>0-run onder block-L)

- DGP block-4 en block-8; delta = 0; 4.000 runs per (DGP, pad); 3 paden. Totaal 24.000 runs.
- Per cel de rejectiefractie bij nominale 0,05 en bij nominale 0,0083, elk met 95%-Clopper-Pearson-interval.
- Per (DGP, pad, alpha) een eenzijdige exacte binomiale toets H0: q <= alpha tegen H1: q > alpha.
  Dat geeft 2 x 3 x 2 = 12 p-waarden.
- Gatefamilie: Holm, FWER 5%, over alle 12. Elke Holm-verwerping = GATE FAIL voor het instrument.
  Drempels worden door de code berekend uit n, alpha en Holm-stap, nooit hardcoded.
- Rapportage van het werkingskarakteristiek van de gate (berekend, niet beslissend): per alpha de kans dat
  één cel op de strengste Holm-stap verwerpt bij een ware size van 1,25x, 1,5x en 2x alpha. Zo staat in het
  rapport hoeveel inflatie de gate in de praktijk doorlaat.
- Bij GATE FAIL: geen beleidsmatige powercurve. Deel C mag alleen diagnostisch draaien voor een DGP zonder
  verworpen cel, en die cijfers tellen niet voor MDE80/eligibility.

## Deel C — power onder block-L (alleen bij GATE PASS op alle 12)

- DGP block-4 (primair) en block-8 (dependence-sensitivity); 3 paden; delta 0,4 / 0,6 / 0,8 / 1,0 %/week.
- 300 runs per (DGP, pad, delta). Uitbreiding naar 1.000 runs (zelfde seeds, runs 0-299 identiek) als het
  95%-CP-interval na 300 runs 0,80 of 0,90 bevat. Deze regel wordt één keer toegepast, na 300 runs.
- Raw power bij 0,0083 is bindend.
- MDE80 en MDE90 per (DGP, pad): lineaire interpolatie tussen aangrenzende gridpunten van de puntschattingen;
  niet bereikt bij 1,0 -> gerapporteerd als "> 1,0 %/week". Geen extra gridpunten achteraf.
- MDE voor eligibility = de hoogste over 3 paden x 2 DGP's.

## Diagnostiek (geen beslisfunctie)

- Size-gecorrigeerde powercurve: per (DGP, pad) met het empirische alpha-kwantiel van de p-waarden uit de
  delta=0-runs als kritieke waarde. Verandert verdict en eligibility niet.
- R_native = Var_HAC / Var_iid van het gemiddelde, per pad op de originele spreadreeks.
- R_block: mediaan en interkwartielbereik over de delta=0-runs van block-4 en block-8.

## Beslisregels

- GATE PASS: raw MDE80/MDE90 uit Deel C vervangen in de beleidsaanvulling de powercijfers van 28-09.
- GATE FAIL (block-4 of block-8, welke cel ook): HAC lag 3 faalt deze instrumentvalidatiestap. Geen
  confirmatoire toepassing op volatiliteitstiltende sensoren. Een nieuwe, apart gepreregistreerde
  ontwerpronde vergelijkt alternatieven (bijv. andere vooraf gemotiveerde bandbreedteregels, fixed-b/HAR,
  self-normalized statistiek). Lag 3 wordt niet binnen deze ronde aangepast.
- Formulering bij een block-8 FAIL: "HAC lag 3 is niet size-valid gebleken binnen de vooraf gekozen block-8
  dependence-stress". Niet: "de echte cryptoreeks heeft te veel afhankelijkheid voor HAC lag 3".
- R_native heeft nu geen beslissende functie. In een eventuele volgende ontwerpronde mag het dienen om te
  beoordelen hoe realistisch block-8 als stressomgeving was.

## Volgorde

1. Commit dit addendum (docs + onderzoeksindex), suite groen.
2. Code: uitbreiding van scripts/n1_repair.py (Deel A, B, C, diagnostiek) met tests; commit via gate.
3. Run Deel A en Deel B; resultaten committen.
4. Alleen bij GATE PASS: Deel C; resultaten committen.
5. N1_REPAIR_REPORT, inclusief claimcorrectie; daarna beleidsaanvulling punt 1; dan pas sensor 2.

Master seed 20261006; deterministische substromen per (deel, DGP, pad, delta, run).

## Voorspellingen vooraf (Claude, expliciet gokken)

- Deel A, iid, power bij 0,0083: delta 0,4 -> 15-30%; 0,6 -> 45-65%; 0,8 -> 70-90%. De onderbouwing (lineair
  terugrekenen vanaf de kalibratie van 28-09) is zwak; geen benchmark waaraan HAC moet voldoen.
- Deel B: block-4 GATE PASS waarschijnlijker dan niet (Bartlett-gematcht); block-8 onzeker, rond fifty-fifty.
- Deel C, als het draait: block-4-power lager dan iid-power bij elke delta; block-8 nog lager.

## Niet doen

Lag of kernel wijzigen. DGP's, paden of delta's toevoegen. Deel C draaien vóór GATE PASS. Rendementen
beoordelen buiten deze meting. Een GATE FAIL repareren via een kritieke-waardecorrectie.
