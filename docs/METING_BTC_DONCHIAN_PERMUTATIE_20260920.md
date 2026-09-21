# Meting — BTC-EUR Donchian trendvolgen, dagbasis, permutatietoets

**Datum:** 20 september 2026  |  **Instrument:** ant_colony/lab/permutation_test.py (b5c3907)
**Status:** AFGEROND — geen uitspraak mogelijk (onvoldoende power).

Opzet: Donchian 20/10, 5% stop, max 72 bars. 1440 Bitvavo-dagcandles.
10.000 permutaties, seed 42, block 1. Volledig + 3 deelperioden, elk eigen nullijn.
Gecorrigeerde drempel: 0,002083 (4 segmenten x 3 grootheden x 2 nulmodellen).

| segment | trades | gem./trade | mediaan | trefkans | p shuffle (gem.) | p random (gem.) | buy-and-hold |
|---|---|---|---|---|---|---|---|
| VOLLEDIG | 26 | +3,50% | -3,52% | 34,6% | 0,238 | 0,351 | +264,8% |
| DEEL1 | 10 | +2,66% | -4,31% | 40,0% | 0,578 | 0,675 | +92,4% |
| DEEL2 | 8 | +5,09% | -2,89% | 25,0% | 0,290 | 0,302 | +94,3% |
| DEEL3 | 9 | -2,08% | -4,58% | 22,2% | 0,651 | 0,707 | -17,6% |

power_ok = false in alle segmenten (drempel 80 trades).

Conclusie: geen bewijs voor een edge, en met deze methode op dagbasis ook niet
te leveren — vier jaar data geeft 26 trades. Willekeurige long-instap met
dezelfde houdduur haalt +2,7% per trade tegen +3,5% (p = 0,35).

Relatie tot eerdere claim: de beschreven BTC-trendresultaten (p = 0,0008;
+8,28% -> +5,85% -> +3,34%) reproduceren niet. Deelperiode 3 is hier negatief.
Met 8-10 trades per deelperiode is p = 0,0008 rekenkundig niet haalbaar.

Implicatie: timing van een enkel asset op dagbasis is met trade-level
permutatie niet toetsbaar in de beschikbare historie. Cross-sectionele opzet
(mand herrangschikken) levert per periode meerdere observaties en is het
enige crypto-spoor dat wel power kan halen.
