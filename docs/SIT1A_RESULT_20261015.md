# SIT-1A: resultaat development (15-10-2026)

Pre-registratie: docs/PREREG_SIT1_20260926.md (T002). Code: 891f673. Run op 8f6c4ab
(2026-10-15T21:28:20Z), resultaat ongewijzigd gecommit in 2e5f61b.
Ruwe uitvoer: results/sit1/sit1a_development.json. Data: data/sit1/MANIFEST.json (CRSP 202608).

VERDICT: FAIL (relevant effect uitgesloten: CI_upper(Delta) 0,49 % < 2 %)

## Uitslag volgens §3.6 (development, forecasts 1937-07 t/m 1990-12, n = 642, inloop 120)

| grootheid | waarde | regel |
|---|---|---|
| Delta QLIKE (M1 t.o.v. M0) | +0,04 % | PASS vereist >= 2 % |
| 95 %-CI Delta (stationary bootstrap, blok 12, 10.000) | [-0,38 %, +0,49 %] | FAIL als bovengrens < 2 % |
| Clark-West | 0,33, p = 0,370 | PASS vereist p <= 0,0083 |
| d-hat (OLS t/m 1990-12, NW12) | +0,00107, t = 1,99 | PASS vereist d > 0 |
| OOS-R² log-RV (M1 t.o.v. M0) | -0,03 % | beschrijvend |

Power (§3.7): MDE_CW = 0,0030; MDE_Delta = 0,62 %. Het instrument kon een effect ruim onder
de 2 %-drempel zien. Dit is geen FAIL door gebrek aan data.

## Eindoordeel SIT-1A
PASS vereist PASS op development EN LHE (§3.6). Development is FAIL, dus het eindoordeel is
FAIL, ongeacht de LHE. FAIL is definitief (§3.6, operationeel). SIT-1B (T003) wordt niet
geopend en is VOID.

LHE niet gedraaid (besluit Vik, 16-10-2026): geen beslissingswaarde na development-FAIL;
bovendien loopt de Ken French-vintage CRSP 202608 maar t/m 2026-08, terwijl §2 targets
t/m 2026-09 eist. Het venster 1991-01 t/m 2026-09 is voor SIT-1 dus niet aangeraakt.

## Beschrijvend (geen poort)
- In-sample significant (t = 1,99), out-of-sample nul: de informatie in dCS overleeft
  recursieve voorspelling niet.
- Episodes (§5): het kleine positieve Delta komt uit de periode 1937-07 t/m 1944-04
  (episode 1929-10 t/m 1944-04). Zonder grootste episode: Delta -0,19 %; zonder twee
  grootste: -0,27 %; zonder alle: -0,18 %.
- 37,9 % van de som van delta_t komt uit de 5 % maanden met grootste |delta_t|.
- Perturbaties dCS-horizon (alleen rapportage): 1 maand CW -2,16 (Delta -0,03 %);
  6 maanden CW 0,61 (Delta +0,07 %).
- Reverse causality (§1): volatiliteitstermen voorspellen dCS_{t+1} (NW12-t ybar3 2,77;
  ybar12 -3,24). Merton-richting zichtbaar; descriptief.
- CW-size-diagnostiek vooraf (ffcf642): toets conservatief (0,36-0,48 % bij alpha 0,0083).

## Voorspellingen (vastgelegd in 8f6c4ab, vóór de run)
Vik: PASS 20 / FAIL 30 / INCONCLUSIVE 50. Claude: PASS 25 / FAIL 20 / INCONCLUSIVE 55.
Uitkomst FAIL. Brier-score: Vik 0,78; Claude 1,01.

## Interpretatieplafond (§7)
Deze FAIL betekent uitsluitend: Moody's Baa-Aaa (3-maandsverandering, één maand vertraagd)
bevat geen incrementele pseudo-OOS informatie over de gerealiseerde variantie van de
Amerikaanse marktfactor in de volgende maand boven mHAR-1/3/12, in 1937-1990.
Het zegt niets over credit als rendementsvoorspeller (dat zou een nieuwe SENSOR-registratie zijn).
