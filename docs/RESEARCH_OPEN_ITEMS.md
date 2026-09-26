# Open keuzes en parkeerlijst

Met de hand bijgehouden. De onderzoeksindex neemt dit bestand ongewijzigd over.

### Open policy-keuzes (niet door Claude in te vullen)

| keuze | stand |
|---|---|
| plaats van de turnover/kosten-screen | weigeringsgrond vooraf, verplichte rapportage bij preregistratie, of onderdeel van de Evidence Gate — nog te bepalen |
| allocatiebenchmark (maatstaf 3) | nog nooit gebruikt; SPY, huidige portefeuille of een absolute ondergrens zoals 7%/jaar — nog te bepalen |
| alpha_round bij meerdere hypotheses op hetzelfde datablok | Holm/FWER is voorkeursrichting; waarde nog niet vastgelegd |
| target power en minimum-N per toetstype | nog niet vastgelegd |
| delta_econ (economische minimumedge in H0) | nog niet vastgelegd |
| P75 of P90 als conservatieve kostenschatting | nu P75, voorlopig vanwege 8 snapshots |

### De drie bindende getallen (gemeten, geen keuze)

- Power: delta_min ongeveer 0,62%/week op de ontwikkelperiode (156 weken).
- Kosten: round-trip 0,69% (hoogste omzetklasse) tot 1,00% (laagste) bij EUR 5.000 per order.
- Klem: bij 44%/week omloop vraagt de kostenhorde 0,92% tot 1,33%/week bruto — boven de
  detectiegrens. Turnover is daarmee het eerste selectiecriterium voor elke sensor.

### Geparkeerd

- Uitvoeringsconflict: research selecteert tot 34 posities; de paper-keten staat 10 posities
  a 10% kapitaal toe. Cross-sectioneel is nu niet uitvoerbaar. Aparte architectuurvraag.
- Meer orderboeksnapshots verzamelen voor een verdedigbaar percentiel.
- POSITIVE_EDGE in edge_audit is te sterk als bewijsclaim en stuurt ook gedrag aan.
- Resterende niet-atomaire write_text-plekken (lean_validator, pc2_validation).
- Onverklaarde instabiliteit: drie tests faalden tweemaal in een koude run, namen onbekend,
  niet reproduceerbaar in twaalf runs. CI is het vangnet.
- Watchtower: sensor en onderzoeksonderwerp, geen bewezen allocator.

### Niet overdoen

- T001 (3-weeks cross-sectioneel momentum): FAIL, definitief. Geen horizonvarianten.
- Eerdere crypto-permutatieclaims (XRP p=0,0001, BTC p=0,0008): nooit gemeten, behandelen
  als hypothese.
- Watchtower-nieuwssentiment: bron haalt de richtingsdrempel niet. Spoor gesloten.
