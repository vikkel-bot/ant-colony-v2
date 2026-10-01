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

## Beleid na powerkalibratie (29-09-2026, docs/POWER_CALIBRATION_20260928.md)

Geldt voor toetsen die NA deze datum worden geregistreerd. T001 (FAIL, definitief) en SIT-1 (T002/T003,
familiedrempel 0,05/(6 x m)) blijven onder de regels die bij hun registratie golden. Niets hieronder wijzigt
een bestaande registratie.

### 1. Statistiek
Primaire statistiek is S1: gemiddelde wekelijkse spread van de geselecteerde k(t) munten minus het universum.
Elke sensor heeft precies een primaire p-waarde, onder nulmodel N1 (willekeurige selectie, 10.000 trekkingen).
S2 (mediaan) en S3 (fractie positieve weken) zijn vooraf gespecificeerde diagnostiek; ze kunnen geen PASS
veroorzaken en geen FAIL redden.
Reden: de economische claim is een verwachtingswaarde. De mediaan beantwoordt een andere vraag.

### 2. Multiple testing: Holm 5% over een bevroren ronde van 6
- Een ronde bestaat uit precies 6 sensoren binnen het crypto-spoor, elk met een eigen preregistratie.
- De ronde is bevroren op het moment dat de zesde preregistratie is gecommit. Voor dat moment wordt voor geen
  enkele sensor uit de ronde een p-waarde berekend. Het bevriezingscommit wordt in het register vermeld.
- Beoordeling: sorteer de 6 ruwe primaire p-waarden oplopend; p(1) <= 0,05/6, p(2) <= 0,05/5, ...,
  p(j) <= 0,05/(7-j). Stop bij de eerste die faalt; die en alle latere zijn FAIL. Er wordt pas beoordeeld als
  alle 6 p-waarden bekend zijn; geen tussentijdse verdicts.
- Terugtrekken na bevriezing (data-ineligible, ontwerpfout, wat dan ook): de sensor telt als p = 1. M blijft 6.
  Vervangen is niet toegestaan; een vervanger hoort in de volgende ronde.
- Meerdere PASS in een ronde: uitsluitend de PASS-sensor met de kleinste ruwe primaire p-waarde gaat door naar
  confirmatie (punt 8). Bij exact gelijke ruwe p beslist de preregistratievolgorde. De overige PASS-sensoren
  krijgen het verdict DEVELOPMENT_PASS en mogen niet alsnog op dezelfde holdout worden getoetst.
- De eerste Holm-drempel (0,05/6 = 0,0083) is gelijk aan de oude familiedrempel.
Reden: de oude regel gaf voor een open reeks toetsen geen eindige FWER. Deze wijziging is vastgelegd VOOR enig
nieuw resultaat, niet vanwege een resultaat.

### 3. Economische grens los van power
delta_econ = kostenhorde van het sensortype, 3 x gemeten kosten volgens kostenmodel v1 op P75.
P90 wordt in elk rapport als gevoeligheid meegegeven, niet als grens, zolang er 8 orderboeksnapshots zijn.
Power wordt bij delta_econ GERAPPORTEERD; er is geen eis dat delta_econ >= de detectiegrens. Volgorde: eerst
economisch, dan statistisch.

### 4. Wat elke preregistratie uit de powerkalibratie vermeldt
- power van S1 bij delta_econ, bij alpha 0,0083;
- effectgrootte met 80% en met 90% power (plafond, perfecte sensor);
- de blinde vlek: de zone tussen delta_econ en het 80%-punt. Verhandelbaar maar in 156 weken niet betrouwbaar
  detecteerbaar. Wordt VOOR de toets benoemd, nooit erna als verklaring.
Alpha 0,0083 is hierbij de conservatieve worst-case ontwerpdrempel van Holm (de eerste stap); de feitelijke
Holm-drempel voor later gerangschikte hypothesen ligt hoger en de power daar dus ook.
Huidige waarden (POWER_CALIBRATION_20260928): S1 80% ~0,69%/week, 90% ~0,76%; prijsgebaseerde delta_econ
0,38-0,45%; power bij 0,45% ruwweg 0,4.

### 5. Geen INCONCLUSIVE zonder instrument
Zolang er geen gekalibreerd en bevroren betrouwbaarheidsinterval voor S1 in de repo staat, is p boven de
Holm-drempel FAIL, ook in de blinde vlek. Een INCONCLUSIVE-verdict (bovengrens interval >= delta_econ) bestaat
pas als dat instrument er is, en geldt dan alleen voor rondes die daarna zijn bevroren.

### 6. Verwacht effect
Het verwachte effect in een preregistratie komt uit theorie of externe literatuur, nooit uit rendementen van het
ontwikkelblok. Sensorscreening blijft beperkt tot turnover, kosten en datageschiktheid.

### 7. N1 alleen geldig voor uniforme selectie (aanvaard 29-09)
N1 is gekalibreerd voor uniform willekeurige selectie. De omvang van relatieve returns is persistent (lag 1
+0,23), dus een sensor die structureel op volatiliteit, liquiditeit of leeftijd tilt, kan een bredere
spreadverdeling hebben dan N1 veronderstelt. Zo'n sensor mag pas worden geregistreerd nadat N1 onder
niet-uniforme selectie is getoetst (placebo-selecties op ex-ante kenmerken, zonder rendementen te beoordelen).
Dat is de eerstvolgende instrumentstap. Tot die tijd: alleen sensoren die daar aantoonbaar niet op tilten.

### 8. Holdout: een sensor per holdout
- De holdout (2025-10 t/m 2026-09, 52 weken) wordt hooguit een keer geopend, voor de ene sensor die volgens
  punt 2 naar confirmatie gaat.
- Holdout-confirmatie: dezelfde primaire statistiek S1 en hetzelfde nulmodel N1 worden toegepast op de 52
  holdoutweken; eenzijdig, dezelfde richting als in de ontwikkeltoets, alpha = 0,05. Er wordt exact een, vooraf
  volgens punt 2 aangewezen sensor getoetst. Geen aanvullende multipliciteitscorrectie op de holdout: de zes
  kansen zaten in de ontwikkelronde en zijn daar met Holm afgerekend; hier wordt een vooraf geselecteerde
  hypothese een keer getoetst. Een sensor krijgt pas een definitieve confirmatoire PASS als zowel de
  ontwikkelgate (punt 2) als deze onafhankelijke holdouttoets is gepasseerd.
- Voordat een holdout wordt geopend, is N1 gekalibreerd voor n = 52: op 52-weekse vensters uit het
  ontwikkelblok, met willekeurige selectie en injectie zoals in POWER_CALIBRATION, worden de Type-I-fout bij
  alpha 0,05 en de MDE bij 80%/90% power gemeten. Geen enkele holdout-return wordt daarbij bekeken. Voorspelling
  vooraf: MDE ~0,9%/week (factor wortel 3 vanuit het ontwikkelblok). Het gemeten getal komt als
  ontwerpbeperking in de preregistratie van de confirmatiekandidaat.
  Overlappende vensters zijn onvermijdelijk (slechts drie niet-overlappende 52-weekse vensters in 156 weken).
  Daardoor is vooral de onzekerheid over generalisatie naar andere marktperiodes groter; deze
  tijdvenster-/kalibratieonzekerheid wordt in de rapportage apart onderscheiden van de Monte-Carlo-onzekerheid
  van de simulatie, die met meer runs wel klein te maken is.
- Na gebruik is de holdout opgebrand. De volgende confirmatoire sensor wacht op nieuwe, ongeziene data.
  Consequentie: bij ~52 nieuwe weken per jaar is dat in de regel een confirmatoire toets per jaar.
- Nadat een holdout eenmaal correct confirmatoir is gebruikt en het verdict definitief is gecommit, mag die
  periode voor volgende rondes aan het ontwikkelblok worden toegevoegd. Vanaf dat moment is die data expliciet
  development en nooit meer holdout. De powerkalibratie wordt dan opnieuw gedaan op het vergrote ontwikkelblok.
  Voor de volgende confirmatoire toets wordt uitsluitend nieuwe, nog ongeziene data gebruikt.

### 9. Onderzoeksindex
De datumkolom komt uit de bestandsnaam, niet uit git. Een document is daarmee in een commit compleet in de
index. Chronologische controle van registraties blijft bij verify_research_log.py. (Generator en test nog aan te
passen; tot dan twee commits per document.)
