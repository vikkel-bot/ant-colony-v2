# Watchtower ↔ Colony Backtest Plan

Doel: Watchtower en Colony blijven twee autonome systemen, maar delen een
strak signaal- en outcome-contract. Watchtower levert entry-intelligence;
Colony valideert, paper-tradet, backtest en beslist via Queen.

## Scheiding van verantwoordelijkheden

- Watchtower: data-ingestie, cross-field analyse, signal scoring, signal export.
- Colony: replay/backtest, paper/live risk gates, Queen-promotie, execution.
- Geen live orderlogica in Watchtower.
- Geen feature-engineering of modeltraining verplicht in Colony.

## Watchtower signal contract

Minimaal veldenset voor elk signaal:

```json
{
  "signal_id": "wt-...",
  "timestamp": "2026-04-27T10:00:00+00:00",
  "data_asof": "2026-04-27T09:59:00+00:00",
  "asset": "BTC-EUR",
  "asset_class": "crypto",
  "direction": "LONG",
  "entry_score": 0.82,
  "confidence": 0.74,
  "risk_flags": [],
  "source_field": "crypto",
  "linked_assets": ["QQQ", "DXY"],
  "take_profit_pct": 0.03,
  "stop_loss_pct": 0.015,
  "feature_version": "wt-features-v1",
  "model_version": "wt-rules-v1"
}
```

Regels:

- `timestamp` is het beslismoment; Colony mag pas vanaf de volgende bar instappen.
- `data_asof` moet kleiner dan of gelijk aan `timestamp` zijn.
- `asset_class` is expliciet: `crypto`, `equities`, `rates`, `fx`, `commodities`, `macro`.
- `linked_assets` markeert cross-field context. Bijvoorbeeld BTC-signaal met QQQ/DXY/VIX context.
- Versies zijn verplicht zodra regels/features kunnen wijzigen.

## Watchtower crypto-uitbreiding

Toevoegen aan Watchtower:

- Read-only crypto market-data adapter(s): candles, volume, spread/orderbook indien beschikbaar.
- Crypto feature set: momentum, volatility, volume spike, liquidity, BTC/ETH breadth, funding/open interest indien later beschikbaar.
- Cross-field features: BTC vs QQQ, BTC vs DXY, ETH/BTC ratio, crypto beta bij risk-on/risk-off equities.
- Export van alle historische signalen via een backtest endpoint.

Gewenste endpoints:

- `GET /colony/signals?limit=50` voor live polling.
- `GET /backtest/signals?from=...&to=...&asset_class=...` voor zuivere replay.
- `POST /outcomes/evaluate` voor Colony feedback na paper/live exits.

## Colony backtest discipline

De Colony backtester moet conservatief blijven:

- entry op de eerstvolgende bar open na het signaal;
- fees op entry en exit;
- slippage tegen de trade in;
- als TP en SL in dezelfde bar geraakt worden, wint SL;
- max gelijktijdige posities en vaste notional per trade;
- rapportage per symbol, asset_class en cross-field subset.

## Queen promotiepad

Aanbevolen stappen:

1. Watchtower signal replay: bewijs positieve netto expectancy na kosten.
2. Paper trading met dezelfde rules en outcome feedback naar Watchtower.
3. Queen accepteert alleen signaaltypes met voldoende sample size en drawdown-limiet.
4. Canary live: klein kapitaal, geen leverage, harde daily-loss stop.
5. Opschalen pas na stabiele paper + canary resultaten per regime.

