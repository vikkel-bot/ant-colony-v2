# Meting — Watchtower nieuwssentiment, BTC-EUR

**Datum:** 20 september 2026
**Status:** AFGEROND — negatief. Bron haalt de richtingsdrempel niet.

## Vraag

Produceert de Watchtower-signaallaag op historisch nieuws bruikbare
richtingssignalen voor BTC-EUR? Deze vraag komt vóór "voorspelt het signaal
iets": zonder niet-neutrale signalen is er niets om aan een nullijn te toetsen.

## Vooraf vastgelegd

- Asset: BTC-EUR, enkelvoudig.
- Periode: 2026-03-01 t/m 2026-04-28 (58 dagen), gelijk aan de replay van
  29-04-2026, zodat het een directe vergelijking is.
- Bronnen: EODHD Financial News (artikelen), Alpha Vantage NEWS_SENTIMENT.
- Richtingsdrempel: |sentiment| >= 0.12 — bestaande waarde in EntryScorer,
  NIET voor deze meting aangepast.
- Ondergrens bruikbare signalen: 80 (zie Powergrens).

## Voorgeschiedenis

De replay van 29-04-2026 (logs/backtest_runs/seed_clean_20260429_092512/)
leverde 315 signalen, waarvan 315x neutral en 0x long/short. Er is dus nooit
een Watchtower-meting geweest; er was niets om te meten. De keten meldde
"klaar" zonder te signaleren dat er geen bruikbaar signaal doorheen kwam.

## Fout 1 — freshness tegen wall-clock (GEREPAREERD)

_freshness_score in watchtower/services/scoring.py rekende de leeftijd van een
nieuwsevent af tegen utc_now(). Bij historisch seeden is dat altijd > 240
minuten, dus kreeg elk seed-signaal freshness 0.15 plus de stale_news-
strafpunten (+0.08). Live-signalen krijgen 0.75-1.0 en geen penalty. Het
seed-pad scoorde daardoor systematisch anders dan het live-pad;
backtestresultaten waren niet overdraagbaar naar live.

Reparatie: optionele referentietijd as_of door de keten seed -> _score_signal
-> RegionalEntryScorer.score -> EntryScorer.score -> _freshness_score. In het
seed-pad gelijk aan event.published_at. Live-pad ongewijzigd (as_of is overal
optioneel). Geverifieerd: in de nieuwe export staat "freshness": 1.0 en
"risk_penalty": 0.0.

## Fout 2 — sentiment-backfill keek vooruit (GEREPAREERD)

_sentiment_for_event in scripts/seed_backtest_signals.py had drie paden, alle
drie met look-ahead:
1. daggemiddelde van dezelfde kalenderdag — tot 24 uur vooruit;
2. het dichtstbijzijnde event binnen 3 dagen — mocht in de toekomst liggen;
3. anders het gemiddelde over ALLE sentimentevents — maximale look-ahead.

Omdat de richting van een seed-signaal volledig door het sentimentteken wordt
bepaald (gemeten: 0 afwijkingen in 840 parametercombinaties), zat de
look-ahead in de meest bepalende variabele van het signaal.

Reparatie: alleen events met published_at < event.published_at; venster van 24
uur terug, anders het meest recente event binnen 3 dagen terug, anders None.

## Fout 3 — candle-timestampsemantiek (OPEN)

_snapshot_from_single_candle in watchtower/services/crypto_market.py kiest de
candle die het dichtst bij het nieuwsmoment ligt, binnen [moment-1u,
moment+1u], en leidt daaruit change_1h_pct en trend_1h af. Als
Bitvavo-candles op hun STARTtijd gestempeld zijn, ligt de close tot 60 minuten
NA het nieuwsmoment en kijkt het signaal vooruit.

Niet gerepareerd omdat de stempelsemantiek niet geverifieerd is. Een
verkeerde aanname introduceert hier een nieuwe fout in plaats van er een op te
lossen. OPEN PUNT.

## Bijvangst — geen echte cross-field context

IntermarketEngine._adjustment haalt nergens QQQ- of DXY-data op. Het leest
uitsluitend volume_zscore, trend_1d en volatility_zscore van het asset zelf.
"BTC vs QQQ" is een these-string plus een label in linked_assets. In het
seed-pad staan die drie velden op 0, dus intermarket_adjustment is daar altijd
exact 0.0 — bevestigd in de export.

## Resultaat

Seed-run 20-09-2026, na reparatie van fout 1 en 2:

| grootheid | waarde |
|---|---|
| artikelen (EODHD) | 277 |
| sentimentevents (Alpha Vantage) | 100 |
| gegenereerde signalen | 277 |
| unieke dagen | 56 |
| degraded (live-data fallback) | 0 |
| gemiddelde entry_score | 0,3670 |
| **direction = neutral** | **277** |
| direction = long | 0 |
| direction = short | 0 |
| entry_score >= 0,50 | 0 |
| entry_score >= 0,72 (watchlist-default) | 0 |
| sentiment_strength = 0,0 | 222 van 277 |
| **max sentiment_strength** | **0,1105** |
| richtingsdrempel | 0,12 |

## Conclusie

Geen enkel signaal haalt de richtingsdrempel. Niet marginaal: het maximum
over 277 signalen ligt 8% onder de drempel, en 222 signalen hebben sentiment
exact 0 (label eodhd_default — er was geen Alpha Vantage-event beschikbaar dat
VOOR het artikel lag).

Dit is geen bug maar een eigenschap van de bron in combinatie met de methode:

- Alpha Vantage labelt op zijn eigen schaal alles tussen -0,15 en +0,15 als
  "Neutral". De Watchtower-drempel van 0,12 ligt vlak BINNEN die band.
- Het seed-pad middelt meerdere artikelen tot een daggemiddelde. Middelen
  trekt het signaal naar nul.
- Alpha Vantage leverde 100 sentimentevents voor 58 dagen. Na het wegnemen van
  look-ahead dekt dat 55 van de 277 artikelen.

**Watchtower kan op deze bron, met deze methode, geen richtingssignaal
produceren.**

## Powergrens — waarom 80

Onafhankelijk gemeten met een permutatietoets op synthetische data met
ingebouwde trendstructuur (Donchian 20/10, 5% stop, max 72 bars):

| bars | trades | p (return-herschikking) | gemiddelde per trade |
|---|---|---|---|
| 1.200 | 22 | 0,060 | -0,15% |
| 4.000 | 81 | 0,0033 | +4,55% |
| 12.000 | 265 | 0,0033 | +3,99% |

Bij ~22 trades komt een aantoonbaar aanwezig effect van ruim 4% per trade niet
tot significantie. Pas rond 80 trades wordt het zichtbaar. Dat is de
ondergrens waaronder een uitspraak niet mogelijk is.

Bijvangst van diezelfde validatie: blokherschikking met blokken die even lang
of langer zijn dan de structuur die je toetst, stopt het signaal in het
nulmodel en verbergt het effect. Blokken moeten KORTER zijn dan de te toetsen
structuur.

## Wat dit NIET zegt

- Niet dat nieuwssentiment geen voorspellende waarde heeft. Dat is niet
  getoetst — er was geen signaal om te toetsen.
- Niet dat Watchtower op aandelen hetzelfde zal doen. Niet gemeten.
  fetch_historical_snapshot bestaat alleen op CryptoMarketAdapter; voor
  aandelen is er geen historische marktdata-adapter.

## Verleiding die hier expliciet wordt afgewezen

De drempel verlagen naar 0,10 zou 55 signalen opleveren. Dat is een regel
aanpassen NA het zien van de data, en maakt het geval een hypothese, geen
bevinding. Bovendien ligt 55 onder de powergrens van 80.

Wil men die kant op, dan is het een NIEUWE HYPOTHESE met vooraf vastgelegd
criterium, een langere periode, en de expliciete erkenning dat de drempel
gekozen is omdat hij data oplevert — niet omdat hij ergens op berust.

## Openstaand

1. Candle-timestampsemantiek van Bitvavo verifiëren (fout 3).
2. tests/test_colony_feedback.py::test_backtest_signals_endpoint_exports_colony_ready_payload
   faalt, en faalde al VOOR deze reparaties — geverifieerd op een verse kloon
   van main. Losstaand spoor.
3. Er bestaat nergens in deze repo een permutatietoets, willekeurige-instap-
   nullijn of p-waardeberekening. edge_audit.py beoordeelt op drempelwaarden
   (expectancy_r, profit factor, aantal trades), niet op significantie.
   edge_audit.py is bovendien ongetrackt: niet gecommit, niet op GitHub.

## INGETROKKEN

De crypto-permutatieresultaten die elders zijn beschreven (XRP p = 0,0001;
BTC trend following p = 0,0008 tegen gecorrigeerde drempel 0,00069; 24 toetsen
à 10.000 herschikkingen; deelperioden +8,28% -> +5,85% -> +3,34%) zijn nergens
op schijf of in enige repo terug te vinden, en er bestaat geen code die ze kan
produceren. Doorzocht op 20-09-2026: beide repos volledig, plus de hele
OneDrive-boom op de specifieke getallen.

Behandelen als HYPOTHESE, niet als bevinding. Niet gebruiken als grond om een
strategie af te schrijven of te selecteren.
