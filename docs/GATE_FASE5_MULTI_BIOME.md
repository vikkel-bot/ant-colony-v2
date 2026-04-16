# Gate Fase 5 — Multi-Biome Scaffolding

**Gate-conditie:** `MULTI_BIOME_SCAFFOLDING_BEWEZEN`
**Status:** BEWEZEN
**Datum:** 2026-04-16
**Testresultaat:** 354/354 passed

---

## Wat bewijst dit gate

Fase 5 introduceert de multi-biome laag: het formele contract voor biome-adapters,
een centraal adapter-register, een Pydantic-schema voor biome-configuratie, en
per-biome kapitaalallocatie in de Queen.

De gate is bewezen als alle onderstaande eigenschappen aantoonbaar werken.

---

## Geleverde deliverables

### D1 — `ant_colony/biome/biome_adapter.py`
- `MarketData` dataclass: symbol, timeframe, OHLCV, biome_id, timestamp (UTC-aware)
  - `is_stale(max_age_seconds)` — fail-closed: stale data blokkeert executie (P2)
  - `is_valid_price` — True alleen als alle prijsvelden > 0
- `AccountState` dataclass: balance, positions_value, currency, timestamp
  - `equity` property = balance + positions_value
  - `is_stale(max_age_seconds=60)` — kortere TTL dan marktdata
- `BiomeAdapter` Protocol (`@runtime_checkable`):
  - `biome_id` property, `is_available()`, `get_market_data()`, `get_account_state()`
  - Duck typing — geen inheritance vereist; `isinstance()` werkt via Protocol

### D2 — `ant_colony/biome/biome_registry.py`
- `BiomeRegistry` — centraal register, bewust geen singleton (injecteerbaar, P7)
- `register()` — hot-swap: overschrijft bestaande adapter met waarschuwing
- `unregister()` — idempotent; onbekend biome_id genegeerd
- `get()` — retourneert None voor onbekend biome (fail-closed, P2)
- `list_biomes()` — gesorteerde lijst van geregistreerde biome_ids

### D3 — `ant_colony/schemas/biome.py`
- `BiomeRiskProfile` — max_position_pct (0,1], min_trade_size, max_daily_trades
- `ExecutionConstraints` — trading_hours_utc, min_lot_size, price_precision
- `BiomeConfig` — biome_id (lowercase, no spaces), symbols (min 1), is_active
  - `BiomeConfig.crypto()` — Bitvavo, 24/7, fractele lots
  - `BiomeConfig.equities()` — Interactive Brokers, NYSE-uren (14:30-21:00 UTC)
  - `BiomeConfig.commodities()` — Saxo Bank, beperkte vensters

### D4 — `ant_colony/queen/queen.py` uitgebreid
- `MissionRejectionReason.BIOME_CAPITAL_EXCEEDED` toegevoegd
- `_biome_limits: dict[str, float]` in Queen.__init__
- `set_biome_capital(biome_id, capital_limit)`:
  - Valideert `≥ 0` en `≤ colony capital_total`
  - Stelregel: biome is een subset van de kolonie, niet de som van alle biomes
- `biome_capital_limit(biome_id) → float | None` — None = geen limiet ingesteld
- `biome_capital_allocated(biome_id) → float` — som van actieve missions in biome
- `biome_capital_available(biome_id) → float | None` — None = onbeperkt; float = budget resterend
- `issue_mission()` controleert biome-kapitaal na kolonie-check, vóór acceptatie

### D5 — `tests/test_biome.py`
68 nieuwe tests verdeeld over zes klassen:

| Klasse | Tests |
|--------|-------|
| TestMarketData | 9 |
| TestAccountState | 7 |
| TestBiomeAdapterProtocol | 7 |
| TestBiomeRegistry | 14 |
| TestBiomeConfig | 12 |
| TestQueenBiomeCapital | 19 |

---

## Gate-bewijs: kerngedrag aangetoond

### 1. Protocol duck typing
```
TestBiomeAdapterProtocol::test_stub_satisfies_protocol           PASSED
TestBiomeAdapterProtocol::test_object_without_biome_id_fails_protocol  PASSED
```
Een klasse die niet alle Protocol-methoden implementeert, slaagt niet voor `isinstance`.

### 2. Registry fail-closed
```
TestBiomeRegistry::test_get_unknown_returns_none                 PASSED
TestBiomeRegistry::test_unregister_unknown_is_idempotent         PASSED
```
`get()` retourneert None; `unregister()` op onbekend id gooit geen exception.

### 3. Hot-swap
```
TestBiomeRegistry::test_register_overwrites_existing             PASSED
```
Tweede `register()` voor zelfde biome_id overschrijft de adapter; count blijft 1.

### 4. Biome kapitaalisolatie
```
TestQueenBiomeCapital::test_biome_allocated_only_counts_matching_biome  PASSED
TestQueenBiomeCapital::test_multiple_biome_limits_independent           PASSED
```
Missions in biome "crypto" tellen niet mee voor biome "equities" en vice versa.

### 5. Unconstrained biome passeert biome-check
```
TestQueenBiomeCapital::test_biome_available_none_when_no_limit   PASSED
TestQueenBiomeCapital::test_unconstrained_biome_passes_biome_check  PASSED
```
Geen `set_biome_capital()` call → `biome_capital_available()` = None → biome-check overgeslagen.

### 6. BIOME_CAPITAL_EXCEEDED afwijzing
```
TestQueenBiomeCapital::test_issue_mission_rejected_when_biome_limit_exceeded  PASSED
TestQueenBiomeCapital::test_biome_rejection_reason_in_result                  PASSED
```
Mission overschrijdt biome-budget → `MissionRejectionReason.BIOME_CAPITAL_EXCEEDED`.

### 7. Kapitaalherstel na revoke
```
TestQueenBiomeCapital::test_biome_available_restored_after_revoke  PASSED
```
`revoke_mission()` geeft biome-budget terug (via allocated recalculation).

### 8. Validators BiomeConfig
```
TestBiomeConfig::test_biome_id_must_be_lowercase                 PASSED
TestBiomeConfig::test_biome_id_must_not_contain_spaces           PASSED
TestBiomeConfig::test_risk_profile_max_position_pct_bounds       PASSED
```

---

## Totale teststand na Fase 5

| Fase | Tests |
|------|-------|
| Fase 1-2 (kolonie-kern) | 157 |
| Fase 3 (Queen governance) | 31 |
| Fase 4 (Strategy Lab) | 98 |
| Fase 5 (Multi-Biome) | 68 |
| **Totaal** | **354** |

```
354 passed in 0.61s
```

---

**Gate gesloten.** `MULTI_BIOME_SCAFFOLDING_BEWEZEN`
