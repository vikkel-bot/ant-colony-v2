# Strategie Test Plan — ANT COLONY v2

**Doel:** Na het afslanken van de colony systematisch 5 quant-strategieën testen om een daadwerkelijke edge te vinden.

**Volgorde:** Eerst afslanken (Scout weg, hybride strategieën weg, 3-4 heldere strategieën over), daarna deze 5 strategieën één voor één testen via de harness-aanpak.

**Succescriterium per strategie:** Minimaal 30 dagen paper trading met:

- Win rate > 55% over ≥ 30 trades
- Sharpe > 1.0
- Max drawdown < 15%
- Duidelijke entry- en exit-regels die geen ambiguïteit toelaten

---

## 1. Mean Reversion (Statistische Arbitrage)

**Principe:** De prijs van een asset (of de spread tussen twee gecorreleerde assets) keert altijd terug naar het historische gemiddelde.

**Entry criterium:**

- Bereken de Z-score (aantal standaarddeviaties afwijking van het voortschrijdend gemiddelde)
- Trigger entry bij Z-score van ±2.0
- Statistisch valt 95% van prijsactie binnen deze bandbreedte; daarbuiten is omkeer-kans groot

**Exit criterium:**

- Take Profit: Prijs keert terug naar mean (Z-score = 0), of asymmetrisch target (Z-score = ±0.5) voor sneller winst pakken
- Stop Loss: Z-score = ±3.5 (duidt op structurele regime change)

**Geschikt voor:** Crypto en aandelen in sideways markt. Werkt slecht in trending markt.

---

## 2. Trend Following / Momentum (Breakouts)

**Principe:** Niet de bodem of top voorspellen, maar meeliften op een sterke bestaande trend.

**Entry criterium:**

- Breakout buiten een dynamisch kanaal: Donchian Channels of x-dagen High
- Voorbeeld: prijs sluit boven hoogste prijs van afgelopen 20 of 50 dagen

**Exit criterium:**

- Trailing Stop: Geen vast winstdoel. Trailing stop op basis van ATR (Average True Range). Exit wanneer prijs 2 × ATR onder hoogste koers sinds entry zakt
- Stop Loss: Direct bij instap geplaatst op 1.5 × ATR of 2 × ATR onder instapprijs (bescherming tegen valse breakouts)

**Geschikt voor:** Commodities, crypto in trending markten, indices na breakouts. Werkt slecht in sideways markt.

---

## 3. Volatility Expansion / Compression (Squeeze)

**Principe:** Periodes van lage volatiliteit (compressie) worden altijd opgevolgd door periodes van hoge volatiliteit (expansie).

**Entry criterium:**

- Scan naar activa met historisch lage volatiliteit
- Bollinger Bands die binnen Keltner Channels kruisen (BB Squeeze)
- Entry triggert zodra prijs met volume-toename buiten dit nauwe percentage-bereik breekt

**Exit criterium:**

- Take Profit: Wanneer volatiliteit zijn piek bereikt en begint af te vlakken (Bollinger Bands breedte krimpt met x% vanaf piek)
- Stop Loss: Prijs valt terug in compressiekanaal (doorbreken van mediaan van Bollinger Bands)

**Geschikt voor:** Crypto na consolidatie, aandelen voor earnings, commodities voor seizoenspatronen.

**Backtest-status 2026-05-21 (harness):**

- Script: `scripts/harness/harness_volatility_squeeze.py --days 90 --params bb=2.0,kc=1.5,sl=0.02,tp=0.06`
- Data: Bitvavo 1h candles, 90 dagen, fixed parameters (geen sweep/optimalisatie)
- BTC-EUR: 32 trades, win rate 59.4%, Sharpe 4.75, max DD 4.7% → GO op harness-criteria
- ETH-EUR: 29 trades, win rate 62.1%, Sharpe 5.39, max DD 5.8% → technisch sterk, maar één trade onder 30-trade criterium
- SOL-EUR: 29 trades, win rate 48.3%, Sharpe 2.24, max DD 17.2% → uitgesloten voor ResearchAnt activatie
- XRP-EUR: 27 trades, win rate 48.1%, Sharpe 0.96, max DD 12.8% → uitgesloten voor ResearchAnt activatie
- Besluit: `volatility_squeeze` alleen actief maken voor BTC-EUR en ETH-EUR in ResearchAnt paper-fase. Geen live-promotie zonder minimaal 30 dagen paper-validatie.

---

## 4. Cross-Sectional Momentum (Factor-Based)

**Principe:** Portfolio-quant-strategie. Rangschik een hele mand assets op prestaties over een periode.

**Entry criterium:**

- Rangschik mand (bijv. S&P 500 of top 50 crypto) op prestaties afgelopen 6 of 12 maanden
- Long op top 10% best presterende assets
- Optioneel short op slechtste 10%
- Periodieke herbeoordeling (maandelijks)

**Exit criterium:**

- Rebalancing: Tijdens herbeoordeling — valt asset buiten top 10% of 20% van momentum-ranglijst? Sluit positie direct
- Roteer kapitaal naar nieuwe koplopers

**Geschikt voor:** Equities biome (S&P 500 sectoren), grote crypto-baskets. Niet geschikt voor commodities.

---

## 5. Opening Range Breakout (ORB) / Intraday Momentum

**Principe:** Profiteren van volatiliteit en volume direct na marktopening.

**Entry criterium:**

- Observeer prijsrange van eerste 5, 15 of 30 minuten van handelsdag
- Kooporder als prijs x% (of fractie van ATR) boven high van openingsrange breekt
- Verkooporder als prijs eronder zakt

**Exit criterium:**

- Take Profit: Vaste risk-to-reward ratio (1:2 of 1:3 t.o.v. grootte openingsrange)
- Time-based Exit: Harde regel — alle posities automatisch sluiten 15 minuten voor marktsluiting (eliminatie overnight-risico)

**Geschikt voor:** US equities (NYSE/Nasdaq open), AEX open. Niet voor 24/7 crypto.

---

## Test-volgorde & rationale

| Volgorde | Strategie | Biome | Reden |
|---|---|---|---|
| 1 | Mean Reversion | Crypto (BTC-EUR) | Eenvoudigste te implementeren, duidelijke statistische regels |
| 2 | Trend Following | Commodities (NATGAS, BRENT, COPPER) | Past bij Watchtower commodity signalen die we al hebben |
| 3 | Volatility Squeeze | Crypto (BTC, ETH) | Bouwt voort op #1, voegt volatiliteit-dimensie toe |
| 4 | Cross-Sectional Momentum | Equities (XLK, XLE, XLF etc.) | Past bij bestaande sector-rotatie infrastructuur |
| 5 | Opening Range Breakout | Equities (AAPL, NVDA, ASML) | Vereist IBKR live data, complexste implementatie |

Per strategie: harness bouwen → 30 dagen paper → meten → beslissen go/no-go → pas dan volgende strategie.

**Geen parallel testen. Eén strategie tegelijk, vol focus, totdat we weten of hij werkt.**

---

## Wat we NIET doen

- Geen hybride strategieën die meerdere indicatoren combineren zonder duidelijke regels
- Geen "unknown" strategy_type — elke trade heeft één expliciete strategie als reden
- Geen optimalisatie van parameters over historische data zonder out-of-sample validatie (curve fitting!)
- Geen live trading voordat paper-resultaat 30 dagen positief is
- Geen 11.233 hybride varianten zoals nu in ResearchAnt

---

## Watchtower-rol in dit plan

Watchtower wordt een veto-systeem, geen score-systeem:

- Default antwoord: NEE, niet handelen
- Geeft alleen GROEN licht als nieuws-tone, intermarket-confirmation én regime-fit samenkomen
- Binaire GO/NO-GO output, niet 0.35-0.57 scores
- 80% van de tijd zegt Watchtower "wacht" — dat is intelligentie
