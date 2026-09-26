# Toetsregister (append-only)

Elke poging telt, inclusief FAIL, INCONCLUSIVE en varianten. Regels worden
nooit gewijzigd of verwijderd; alleen toegevoegd.

| id | familie | pre-registratie | status | resultaat-commit |
|---|---|---|---|---|
| T001 | CRYPTO_XS_PRICE_MOMENTUM | PREREG_T001_MOMENTUM21_20260922.md | PENDING | - |
| T001 | CRYPTO_XS_PRICE_MOMENTUM | PREREG_T001_MOMENTUM21_20260922.md | FAIL | T001_RESULT_20260922.md |

# REGISTERREGEL — Familie `SIZING_CREDIT_INFO`

**Registratiedatum:** 2026-09-26
**T-nummers:** SIT-1A = T002, SIT-1B = T003
**Commit-hash van deze regel:** fa7eb64ed1bd0f64c893c76f0d758523338b3a19
**Status:** PREREGISTERED — geen data geopend, geen code geschreven op registratiedatum
**Categorie:** capital allocation / position sizing. NIET alpha/edge discovery. NIET Sensor 2.
**Multiple-testingregel (familie):** p-drempel = 0,05 / (6 × aantal toetsen t/m deze toets). SIT-1A: n=1 → 0,0083. SIT-1B: n=2 → 0,0042. Perturbaties, replicaties, transfer- en implementatiechecks tellen niet als toetsen.
**Wijzigingsbeleid:** append-only. Na deze commit is geen wijziging van specificatie, lag, horizon, loss, drempel, venster of hypothese toegestaan. Elke afwijking = nieuwe registerregel met eigen nummer.

Confidence-labels in deze regel: [Certain] / [Likely] / [Guessing] gelden voor literatuur- en dataclaims, niet voor ontwerpbeslissingen (die zijn beslissingen).

---

## 0. Onderzoeksvraag en bewijsniveaus

**Primaire vraag (SIT-1A):** Bevat één vooraf gekozen credit-stress-proxy (verandering Moody's Baa–Aaa) incrementele informatie over de gerealiseerde variantie van de Amerikaanse marktfactor in de volgende maand, boven een sterke volatility-only benchmark (`mHAR-1/3/12`)?

**Conditionele vraag (SIT-1B, alleen na PASS SIT-1A):** Levert die informatie, via een identieke vooraf vastgelegde inverse-variantie-sizingfunctie, een betere netto Sharpe dan dezelfde functie zonder die informatie, onder cap 1, zonder leverage of shorts?

**Drie bewijsniveaus, strikt gescheiden:**

| Niveau | Inhoud | Data | Toegestane term |
|---|---|---|---|
| A. Historical mechanism evidence | SIT-1A, M0 vs M1 | Fama-French Mkt-RF (huidige vintage) | recursive historical pseudo-OOS evidence using current-vintage academic data |
| B. Historical sizing evidence | SIT-1B, B1 vs R1 | zelfde Mkt-RF-reeks | "wat de sizingregels op deze historische equity-premiumreeks zouden hebben gedaan" — geen uitvoerbaarheidsclaim |
| C. Point-in-time tradable implementation evidence | SPY-only signalen + SPY-uitvoering | SPY total return, vanaf inloop | het enige niveau waar "implementable" / "tradable historical simulation" mag |

Alle drie zijn historisch en bekend, dus niet pristine. Het woord **prospective/pristine** is gereserveerd voor data die ontstaan ná 2026-09-26.

**Terminologieregel vintage:** de Fama-French Data Library reconstrueert historische returns bij updates; observaties kunnen wijzigen als CRSP herziet [Certain]. Alle historische forecasts zijn daarom *recursive / point-in-sequence pseudo-OOS forecasts using the currently available data vintage*: op forecastmoment t worden uitsluitend observaties met datum ≤ t gebruikt, maar er wordt NIET geclaimd dat exact deze vintage op kalenderdatum t beschikbaar was. Het woord "realtime" wordt in geen enkele conclusie over niveau A of B gebruikt.

---

## 1. Data

| Reeks | Definitie | Bron | Vintage-/timingregel |
|---|---|---|---|
| `Mkt-RF_d` | dagelijkse excess return VS-marktfactor | Ken French Data Library, daily factors [Certain] | vintage-datum en download-URL vastleggen bij eerste download; huidige vintage |
| `RF_m` | maandelijkse risicovrije voet (1-maands T-bill) | Ken French Data Library [Certain] | idem |
| `RV_t` | Σ over alle handelsdagen van maand t van (`Mkt-RF_d`)² | afgeleid | bekend op slot laatste handelsdag maand t |
| `y_t` | log `RV_t` | afgeleid | idem |
| `CS_m` | maandgemiddelde Moody's Baa − Aaa seasoned corporate bond yield (bp) | FRED, BAA en AAA maandreeksen [Certain dat beide bestaan; startjaar 1919 Likely] | publicatiedag vóór eerste download documenteren; bij lag > 1 maand schuift `ΔCS` één extra maand op (vastgelegd, geen discretie) |
| `ΔCS_t` | `CS_{t−1} − CS_{t−4}` | afgeleid | gebruikt uitsluitend spreads t/m maand t−1: één volle maand publicatiespeling |
| `SPY_d` | dagelijkse total-return van SPY (dividenden herbelegd) | IBKR of Yahoo adjusted close [Likely] | inceptie januari 1993 [Certain]; eerste volledige maand 1993-02 |
| `ExUS_d` | dagelijkse developed-ex-US marktfactor | Ken French Data Library [Likely vanaf 1990-07] | huidige vintage; alleen transfer |

**Vintage-controle credit (verplicht, vooraf):** maximale afwijking tussen huidige vintage en alle ALFRED-vintages van BAA/AAA waar beide bestaan, gerapporteerd in bp. Bij afwijking > 5 bp in `ΔCS_t` voor enige maand: `ΔCS_t` voor die maanden herberekend met de vintage die op einde maand t bestond. Geen andere actie toegestaan.

**Reverse-causality-diagnostiek (descriptief, geen poort):** regressie `ΔCS_{t+1}` op de mHAR-termen van maand t en `ΔCS_t`, expanding, gerapporteerd om te tonen welke richting in de data domineert. Merton-type structurele relatie (equity-vol → creditspread) is bekend [Certain]; de lagstructuur hieronder handicapt het credit-signaal juist daarom met één maand t.o.v. de nieuwste vol-observatie.

---

## 2. Vensters

| Venster | Periode | Rol | Label |
|---|---|---|---|
| Inloop | eerste 120 regressieobservaties (targets 1927-07 t/m 1937-06; de mHAR-lags vereisen 12 maanden vóór de eerste target) | schatting | — |
| Development | forecasts 1937-07 t/m 1990-12 (642 maanden) | ontwikkelingsuitkomst | historical |
| Locked Historical Evaluation (LHE) | forecasts 1991-01 t/m 2026-09 (429 maanden) | confirmatoire uitkomst op historische data | locked historical, NIET pristine |
| Transfer | ex-US, eigen inloop (targets 1991-07 t/m 2001-06), forecasts 2001-07 t/m 2026-09 | external validity | historical transfer, alleen rapportage |
| SPY implementation | eigen inloop op SPY (targets 1994-02 t/m 2004-01), forecasts 2004-02 t/m 2026-09 | implementability | historical implementability, alleen rapportage |
| Prospective | vanaf 2026-10, append-only | enige pristine test | prospective pristine |

**Volgorde, bindend:** (1) commit deze regel → (2) data ophalen, vintage-datums loggen → (3) code + tests, incl. assert "geen observatie met datum > t in forecast t" → (4) development draaien, uitkomst en power-herberekening loggen → (5) pas daarna LHE, één run → (6) transfer en SPY-check, één run elk → (7) SIT-1B alleen bij PASS SIT-1A op development én LHE. Modellen blijven expanding doorschatten in de LHE; specificatie wordt niet aangeraakt. Geen pooled analyse van development en LHE.

---

## 3. SIT-1A — Mechanismegate

### 3.1 Modellen

**M0 — `mHAR-1/3/12` (monthly HAR-style adaptation).** Past het HAR-principe van Corsi (2009) — volatiliteitscomponenten over meerdere horizons — toe op een maandelijkse forecasttaak met 1/3/12-maandscomponenten [Certain dat het principe van Corsi is; de 1/3/12-maandsconstructie is onze adaptatie, geen letterlijke replicatie].

```
M0:  y_{t+1} = a + b1·y_t + b3·ȳ_{t−2:t} + b12·ȳ_{t−11:t} + ε_{t+1}
M1:  y_{t+1} = a + b1·y_t + b3·ȳ_{t−2:t} + b12·ȳ_{t−11:t} + d·ΔCS_t + ε_{t+1}
```
`ȳ_{t−k:t}` = gemiddelde van `y` over maanden t−k t/m t. Precies één verschil tussen M0 en M1: `d·ΔCS_t`. `d` ongerestricteerd.

**Schatting:** OLS; expanding (recursief); maandelijks herschat op alle regressieobservaties met target ≤ t; horizon 1 maand; forecast `ŷ_{t+1|t}`.

**Terugtransformatie (alleen voor QLIKE):** `σ̂²_{t+1|t} = exp(ŷ_{t+1|t} + ½·s²_t)`, met `s²_t` de expanding residuvariantie van het betreffende model op observaties ≤ t. Identieke procedure voor M0 en M1, elk met eigen residuen.

### 3.2 Formele inferentie — Clark–West adjusted-MSPE op log-RV

Nesting: onder H0 (`d = 0`) vallen forecasts asymptotisch samen en draagt M1 alleen parameterruis; de gewone loss-differential is niet-standaard en tegen M1 vertekend [Certain — Clark & McCracken 2001]. Correctie volgens Clark & West (2007) [Certain]:

```
e0_t = y_t − ŷ0_{t|t−1} ;  e1_t = y_t − ŷ1_{t|t−1}
f_t  = e0_t² − [ e1_t² − (ŷ0_{t|t−1} − ŷ1_{t|t−1})² ]
CW   = mean(f_t) / SE_NW12(mean f_t)          (Newey-West, 12 lags)
```
H0: gelijke populatie-MSPE (`d = 0`). H1 eenzijdig: M1 beter. Kritieke waarden standaardnormaal, eenzijdig. Geldigheid: genest, één extra predictor, één stap vooruit, recursief schema — het geval van CW [Certain]. Bekende beperking: normale benadering is onder recursief schema licht undersized [Likely]; bij P/R ≈ 642/120 ≈ 5,4 en α = 0,0083 is de kwaliteit van de benadering niet gesimuleerd in de bronpaper [Guessing]. Dit is de grootste resterende econometrische onzekerheid en wordt bij de uitslag herhaald. Clark–West is conservatief in de richting die de toets strenger maakt.

**Gerapporteerd, telt niet:** Clark–McCracken ENC-NEW zonder bootstrap-kritieke waarden (alleen de statistiek); gewone DM op `f`-ongecorrigeerde MSPE; OOS-R² van log-RV.

### 3.3 Operationele/economische loss — QLIKE

`QLIKE(σ̂², RV) = RV/σ̂² − log(RV/σ̂²) − 1`, op variantieniveau na terugtransformatie. Motivatie: robuustheid van forecastrangordes bij ruisige RV-proxy [Certain — Patton 2011]; asymmetrie (onderschatting variantie duurder) consistent met inverse-variantie-sizing.

`δ_t = QLIKE_M0,t − QLIKE_M1,t`; `Δ = mean(δ) / mean(QLIKE_M0)`.
Betrouwbaarheidsinterval: stationary block bootstrap op `δ_t`, gemiddelde bloklengte 12, 10.000 replicaties, seed = 20260926, 95% tweezijdig. Onder H0 is dit interval naar beneden vertekend (nesting); dat maakt het FAIL-criterium "relevant effect uitgesloten" eerder waar — conservatief in de juiste richting. Onder H1 (`d ≠ 0`) gelden gewone asymptotiek [Certain].

**2% = minimum observed practical effect / practical relevance threshold.** Geen onderdeel van H1. Beslissing zonder theoretische afleiding [Guessing dat 2% de juiste orde is voor incrementele predictors boven HAR].

### 3.4 Richting

Geregistreerde eigenschap, één getal per venster: `d̂_dev` (OLS op alle regressieobservaties t/m 1990-12) > 0; `d̂_LHE` (t/m 2026-09) > 0. Geen percentage, geen telling van afhankelijke expanding schattingen. Restrictie `d ≥ 0` in de schatting is NIET toegestaan (randparameter, niet-standaard inferentie [Certain]).

### 3.5 Hypotheses

- **H0:** `d = 0` — `ΔCS` bevat geen incrementele informatie over `y_{t+1}` boven M0.
- **H1 (eenzijdig):** `d ≠ 0` met M1 betere populatie-MSPE; operationeel `Δ_QLIKE > 0`; richting `d > 0`.

### 3.6 Uitkomstregels (identiek development en LHE; p-drempel development 0,0083, LHE 0,05)

- **PASS:** `CW p ≤ drempel` ∧ `d̂ > 0` ∧ `Δ̂ ≥ 2%`.
- **FAIL:** `CI_upper(Δ) < 2%` (relevant effect uitgesloten) ∨ (`CW p ≤ drempel` ∧ `d̂ < 0`; informatie in de verkeerde richting).
- **INCONCLUSIVE:** al het overige, inclusief CW niet significant met positieve punt, en CW significant met `Δ̂ < 2%`.
- **Eindoordeel SIT-1A = development × LHE; PASS vereist PASS in beide.**
- **Operationeel:** alleen PASS opent SIT-1B. FAIL is definitief. INCONCLUSIVE sluit de credit-sizinglijn en mag uitsluitend heropend worden met het prospectieve venster na ≥ 60 nieuwe maanden — nooit met een andere specificatie, lag, horizon, transformatie, loss of proxy. "Misschien voorspelt credit rendement" is, indien ooit gewenst, een nieuwe SENSOR-registratie.

### 3.7 Power

Vóór de LHE wordt aangeraakt: `MDE_CW = 2,8 · SE_NW12(mean f)` en `MDE_Δ = 2,8 · SE_boot(Δ)` op development, gelogd. Geen vooraf geraden getal; formule bepaalt de INCONCLUSIVE-band.

### 3.8 Verplichte descriptieve rapportage SIT-1A

Per venster: `Δ̂` met CI; CW-statistiek en p; `d̂` met HAC-SE; MSE op log-RV en OOS-R²; aandeel van `Σδ_t` uit de 5% maanden met grootste `|δ_t|`; episode-attributie (§5); reverse-causality-diagnostiek (§1); perturbaties (§6), alleen rapportage.

---

## 4. SIT-1B — Sizingtest (uitsluitend na PASS SIT-1A op development én LHE)

### 4.1 Sizingfunctie, identiek voor B1 en R1

```
w_t = min(1, c_t / σ̂²_{t+1|t})
c_t = expanding mediaan van RV_s over alle maanden s ≤ t (vanaf 1926-07)
```
- **B0:** `w_t = 1`. Uitsluitend descriptieve unmanaged referentie; B1 vs B0 gerapporteerd met CI, geen hypothese (replicatie van Moreira & Muir 2017 [Certain], betwist door Cederburg et al. 2020 [Likely]).
- **B1:** `σ̂²_{t+1|t}` uit M0. **R1:** uit M1. Dezelfde forecastbestanden als SIT-1A (hash in register); geen herschatting.
- Zelfde `c_t` (uit gerealiseerde RV, dus vrij van forecast-informatie en identiek), zelfde cap 1, geen leverage, geen shorts, zelfde kosten, zelfde herweging.
- Het enige verschil tussen B1 en R1: wel of geen credit-informatie in de voorafgaande risk forecast.

### 4.2 Uitvoering en rendementen

- `w_t` geldt van handelsdag 2 t/m laatste handelsdag van maand t+1; handelsdag 1 van maand t+1 krijgt `w_{t−1}` (signaal op slot dag 0 is niet op slot dag 0 uitvoerbaar).
- Niveau B (Mkt-RF): `r^port_d = w · Mkt-RF_d − friction_d`, excess-returnvorm. Frictie: **hypothetical turnover penalty** van 0,10% per zijde op iedere `|Δw|`, over de hele reeks (ook na 1993, voor vergelijkbaarheid binnen niveau B). Dit is geen transactiekostenclaim; Mkt-RF is vóór 1993 niet verhandelbaar [Certain]. Gerapporteerd: gross én friction-adjusted. **Uitslag wordt bepaald op friction-adjusted.**
- Niveau C (SPY): zie §4.6.

### 4.3 Primaire maat en toets

`ΔSR = SR(R1) − SR(B1)`, friction-adjusted, maandelijks, geannualiseerd. Ledoit & Wolf (2008) gestudentiseerde circulaire block-bootstrap voor het Sharpe-verschil, blok 12, 10.000 replicaties, seed 20260926, 95% tweezijdig CI en p-waarde [Certain dat de toets robuust is voor seriële correlatie, heteroskedasticiteit en niet-normaliteit].

- **Minimum observed practical effect:** `ΔSR ≥ +0,10`. Beslissing.
- **Label-marge:** −0,05 (zie 4.4). Geen non-inferioriteitsclaim.
- Power: `Var(ΔSR̂) ≈ 2(1−ρ)(1+SR²/2)/T` als eerste-orde indicatie [Guessing]; werkelijke ρ(R1,B1) op development gemeten, `MDE = 2,8·SE_boot` gelogd vóór LHE.

### 4.4 Hypotheses en uitkomstregels (identiek development en LHE; p-drempel 0,0042 resp. 0,05)

- **H0:** `ΔSR ≤ 0`. **H1:** `ΔSR > 0`.
- **PASS:** `CI_lower(ΔSR) > 0` ∧ `ΔSR̂ ≥ 0,10` ∧ `MDD(R1) ≤ MDD(B1)` ∧ `ES95(R1) ≤ ES95(B1)`.
- **FAIL:** `CI_upper(ΔSR) < 0,10` ∨ staartpoort geschonden ∨ tekenwissel van `ΔSR̂` in ≥ 2 van 3 perturbaties (§6).
- **INCONCLUSIVE:** al het overige. CI dat −0,05 uitsluit maar 0 bevat: label "niet slechter, niet aantoonbaar beter" — geen PASS, geen claim.
- **Eindoordeel SIT-1B = development × LHE; PASS vereist beide.**
- **Labels bij PASS (verplicht in elke conclusie):** "risk-transfer PASS" indien Δ meetkundig rendement < 0; "crisis-only instrument" indien `ΔSR` zonder alle episodes significant negatief (§5); "implementability not confirmed" indien §4.6 een tegengesteld teken geeft.

### 4.5 Staartmaten

Poorten: maximum drawdown (op cumulatief friction-adjusted rendement) en ES95 (gemiddelde van de 5% slechtste maandrendementen), R1 niet slechter dan B1. Verplicht descriptief: Δ meetkundig rendement; ΔCER bij γ=1 en γ=5; gemiddelde `w`; aantal maanden met `|w_R1 − w_B1| > 0,10`; turnover; spanning-alpha (`r_R1 = α + β·r_B1`, HAC) — diagnostisch, telt niet.

### 4.6 SPY point-in-time implementability check (niveau C; descriptief, geen poort)

Volledig aparte keten, uitsluitend op werkelijk observeerbare SPY-data:
- `RV^SPY_t` uit dagelijkse SPY total-return; `y^SPY = log RV^SPY`.
- Zelfde `mHAR-1/3/12`, zelfde `ΔCS_t`, zelfde credit-lag, expanding schatting met eigen inloop (targets 1994-02 t/m 2004-01), forecasts 2004-02 t/m 2026-09; zelfde M0/M1-structuur; zelfde sizingfunctie met `c_t` = expanding mediaan `RV^SPY` vanaf 1993-02; zelfde cap. Geen parameter aangepast.
- `r^port_d = r^cash_d + w·(r^SPY_d − r^cash_d) − kosten_d`, cash = FF RF; **transactiekosten** 0,04% per zijde (fee 0,02% + spread + slippage [Likely voor SPY]) op iedere `|Δw|`.
- Gerapporteerd: `ΔSR`, MDD, ES95, turnover, en het teken van `ΔSR` t.o.v. niveau B over hetzelfde venster. Bij tegengesteld teken: label "implementability not confirmed" op elke SIT-1B-conclusie.
- Toegestane termen voor dit niveau: "implementable", "tradable historical simulation". Niet pristine.

### 4.7 Transfer test (descriptief, geen poort)

Ex-US marktfactor, eigen mHAR-schatting op eigen RV, Amerikaanse `ΔCS` als wereldproxy (gewijzigde hypothese; alleen external validity). Gerapporteerd: SIT-1A-grootheden en, indien SIT-1B is geopend, `ΔSR` met CI. Geen onafhankelijke confirmatie.

---

## 5. Episode-attributie (geen crisis-FAIL)

Episodes bepaald door regel, niet met de hand: aaneengesloten maanden waarin de B0-drawdown vanaf piek > 20% is, samengevoegd bij een gat < 6 maanden. Verwachting [Likely]: 1929–33, 1937–38, 1973–74, 2000–02, 2008–09, 2020. Per venster gerapporteerd, voor SIT-1A (`Δ̂`, CW) en SIT-1B (`ΔSR`, ΔMDD, Δgroei): volledig / zonder grootste episode / zonder twee grootste / zonder alle episodes. Geen van deze is een poort. Significant negatief zonder alle episodes → label "crisis-only instrument".

---

## 6. Alle vooraf vastgelegde keuzes

| # | Keuze | Waarde | Rechtvaardiging | Perturbatie |
|---|---|---|---|---|
| 1 | M0-vorm | mHAR-1/3/12 | HAR-principe Corsi 2009 [Certain]; sterke benchmark | geen |
| 2 | Target | log RV | ABDE 2001 [Certain] | geen |
| 3 | Terugtransformatie | exp(ŷ + ½s²), expanding, per model | standaard; identieke procedure | geen |
| 4 | Schatting | OLS, expanding, maandelijks herschat | pseudo-OOS standaard; CW-geldig | geen |
| 5 | Inloop | 120 regressieobservaties (+12 mnd lags) | beslissing [Guessing] | geen |
| 6 | Credit-proxy | Baa−Aaa maandgemiddelde | lange historie; publiek; niet-herzien [Likely] | geen |
| 7 | ΔCS-horizon | 3 maanden | beslissing [Guessing] | 1 en 6 maanden (M0 en M1 beide; alleen rapportage in 1A; telt in 1B-FAIL-regel) |
| 8 | Credit-lag | `CS_{t−1}` | fail-closed publicatie | geen |
| 9 | Formele toets 1A | Clark–West, eenzijdig, NW12 | nested recursive [Certain] | geen |
| 10 | Operationele loss | QLIKE | Patton 2011 [Certain]; sizing-asymmetrie | geen |
| 11 | Bootstrap | stationary block, blok 12, 10.000, seed 20260926 | jaarlijkse vol-persistentie [Likely] | geen |
| 12 | Richting | `d̂_dev > 0`, `d̂_LHE > 0` | hypothese is directioneel; één getal | geen |
| 13 | Praktische drempel 1A | Δ̂ ≥ 2% | beslissing [Guessing] | geen |
| 14 | `c_t` | expanding mediaan RV | robuust tegen 1929-dominantie; Kelly bij constante μ | expanding gemiddelde (telt in 1B-FAIL-regel) |
| 15 | Cap / leverage / shorts | 1 / geen / geen | scope-beslissing | geen |
| 16 | Herweging | maandelijks, dag-1-regel | literatuur; turnover; fail-closed | geen |
| 17 | Frictie niveau B | 0,10%/zijde, hypothetical turnover penalty | conservatief; geen kostenclaim | geen |
| 18 | Kosten niveau C | 0,04%/zijde | IBKR-ervaring [Likely] | geen |
| 19 | Primaire maat 1B | ΔSR, Ledoit–Wolf | schaalinvariant; parametervrij; robuuste toets [Certain] | geen |
| 20 | Praktische drempel 1B / labelmarge | 0,10 / −0,05 | beslissing | geen |
| 21 | Staartpoorten | MDD, ES95 | minimale set | geen |
| 22 | Episoderegel | DD > 20%, gat < 6 mnd | bearmarket-conventie [Likely] | geen |
| 23 | Splitsdatum | 1990-12 | grote episodes aan beide zijden; ex-US-start | geen |
| 24 | Familiedrempel | 0,05/(6×n) | bestaande registerregel | geen |

24 keuzes, 3 perturbaties, 0 geoptimaliseerd. Acht keuzes (#5, #7, #13, #17, #18, #20, #22, #23) zijn beslissingen zonder theoretische afleiding.

---

## 7. Interpretatieplafond

- **SIT-1A PASS betekent uitsluitend:** `ΔCS` bevat incrementele pseudo-OOS informatie over de gerealiseerde variantie van de volgende maand boven `mHAR-1/3/12`, op de huidige vintage van de Amerikaanse marktfactor, in historische data.
- **SIT-1B PASS betekent uitsluitend:** die informatie kan via een vooraf vastgelegde inverse-variantieregel een betere friction-adjusted Sharpe geven dan dezelfde regel zonder die informatie, onder cap 1, op de historische equity-premiumreeks.
- **Het betekent NIET:** dat Colony alpha heeft; dat creditstress rendement voorspelt; dat funding-liquiditeit, margins of haircuts aandelen bewegen (Baa−Aaa is een credit-stress-proxy, geen funding-liquiditeitsmeting; Brunnermeier & Pedersen 2009 gaat over andere grootheden [Certain]); dat het op crypto werkt; dat het op een toekomstige Colony-edge werkt; dat het hogere groei geeft; dat het uitvoerbaar was vóór 1993; dat het pristine is bevestigd.
- Toepassing op een echte Colony-edge vereist een nieuwe confirmatoire test nadat die edge zelfstandig PASS heeft. Elke verdere claim = nieuwe registerregel.

---

## 8. Waarom zelfs een volledige PASS fout-positief kan zijn

1. De hypothese is gekozen ná kennis van 2008; geen correctie herstelt selectie vóór registratie. Beide vensters zijn historisch en bekend.
2. Baa−Aaa bewoog in de jaren '70–'80 mede op inflatie en rentevolatiliteit [Likely]; forecastwinst uit dat tijdvak kan een macroregime meten dat niet terugkeert.
3. Mkt-RF is achteraf uit CRSP opgebouwd [Certain] en kan bij vintage-updates wijzigen; Moody's indexsamenstelling en duration verschuiven over een eeuw [Likely].
4. Sharpe is blind voor groei: R1 kan slagen door exposure af te bouwen in lage-Sharpe-maanden zonder één euro meer rendement.
5. Kwaliteit van de CW-normaalbenadering bij P/R ≈ 5,4 en α = 0,0083 is niet gesimuleerd [Guessing]; randresultaten neigen naar INCONCLUSIVE, niet PASS.
6. Een QLIKE-winst van 2% kan uit een handvol maanden komen; de concentratie wordt gerapporteerd maar is per protocol geen poort.
7. Ex-US-transfer gebruikt een Amerikaanse proxy en dezelfde wereldcrises; geen onafhankelijk bewijs.
8. Baa−Aaa verbreedt meestal tijdens aandelendalingen; een PASS kan deels een drawdown-proxy meten, familie van Faber (al gelogd als risk-transfer zonder Sharpe-winst). Er is bewust geen ontwarringsvariabele.

---

## 9. Uitkomstlog (append-only; alleen invullen na de betreffende run)

| Stap | Datum | Commit | Vintage-datums | Uitkomst | Labels |
|---|---|---|---|---|---|
| Data opgehaald | | | | | |
| Code + tests groen | | | | | |
| SIT-1A development | | | | PASS/FAIL/INCONCLUSIVE | |
| Power-herberekening (MDE_CW, MDE_Δ) | | | | | |
| SIT-1A LHE | | | | | |
| SIT-1A eindoordeel | | | | | |
| Transfer (1A-grootheden) | | | | rapportage | |
| SPY implementation check (1A-grootheden) | | | | rapportage | |
| SIT-1B development (alleen bij 1A PASS) | | | | | |
| SIT-1B LHE | | | | | |
| SIT-1B eindoordeel | | | | | risk-transfer / crisis-only / implementability |

---

## 10. Prospective pristine sectie (append-only vanaf 2026-10)

Regels: één regel per kalendermaand, toegevoegd na afloop van de maand; forecasts `ŷ0`, `ŷ1`, `w_B1`, `w_R1` worden vóór aanvang van de maand vastgelegd (met commit-hash) en daarna niet gewijzigd. Herbeoordeling van een INCONCLUSIVE SIT-1A is pas toegestaan na ≥ 60 prospectieve maanden, met dezelfde specificatie en dezelfde uitkomstregels (p-drempel 0,05, eenzijdig CW). Dit is de enige sectie waar het woord "pristine" mag staan.

| Maand | Vastgelegd op (commit) | ŷ0 | ŷ1 | w_B1 | w_R1 | RV gerealiseerd | ΔCS gebruikt | δ_t | f_t |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10 | | | | | | | | | |
