# Gate Fase 8 — Guarded live adapters

**Gate-conditie:** `GUARDED_LIVE_ADAPTERS_BEWEZEN`
**Status:** BEWEZEN
**Datum:** 2026-04-16
**Testresultaat:** 543/543 passed

---

## Wat bewijst dit gate

Fase 8 introduceert het volledige order-uitvoeringslaag van de ANT COLONY.
Orders bereiken de exchange (of paper simulator) uitsluitend via een gate
die vijf opeenvolgende veiligheidscontroles uitvoert. Fail-closed op elk punt:
twijfel blokkeert het order (P2). Exit-logica (stop_loss, take_profit) is
verplicht aanwezig vóór elke order wordt geaccepteerd (P3).

---

## Geleverde deliverables

### D1 — `ant_colony/schemas/order.py`
- `OrderSide`  — `BUY` / `SELL`
- `OrderType`  — `MARKET` / `LIMIT`
- `OrderRejectionReason` — 7 zakelijke weigeringsredenen
- `LiveOrder`  — gevalideerd order-voorstel (immutable Pydantic)
  - `stop_loss_price` en `take_profit_price` altijd verplicht (P3)
  - LIMIT order vereist `limit_price > 0`
  - BUY: `stop_loss < take_profit`; SELL: `stop_loss > take_profit`
- `OrderResult` — exchange-respons of gate-weigering
  - `rejected()` factory: accepted=False + reden
  - `accepted_result()` factory: accepted=True + exchange_order_id + fill

### D2 — `ant_colony/biome/biome_adapter.py` uitgebreid
- `LivePosition` dataclass toegevoegd:
  - `unrealized_pnl` — LONG en SHORT beide correct
  - `market_value` — current_price × quantity
- `BiomeAdapter` Protocol uitgebreid met:
  - `place_order(order: LiveOrder) -> OrderResult | None`
  - `get_positions() -> list[LivePosition] | None`

### D3 — `ant_colony/execution/live_gate.py`
- `LiveExecutionGate` met vijf pre-order checks (in volgorde):
  1. Kolonie niet HALTED (ColonyScheduler.status)
  2. Mission actief (Queen.active_missions)
  3. Kapitaal niet overschreden (mission.capital_limit > 0)
  4. Marktdata niet stale (MarketData.is_stale())
  5. Adapter beschikbaar (BiomeAdapter.is_available())
- Bij alle checks groen: delegeert naar `adapter.place_order(order)`
- `execute()` gooit nooit — retourneert `OrderResult` of `None` (alleen bij adapter-fout)
- Audit log per order naar `ANT_LOGS/execution/{order_id}.jsonl`

### D4 — `ant_colony/execution/paper_gate.py`
- `PaperExecutionGate` — zelfde contract als `LiveExecutionGate`
- Vier checks (check 5 adapter-beschikbaar is altijd groen in paper mode)
- Simuleert fill: MARKET → `market_data.close`, LIMIT → `order.limit_price`
- Synthetisch `exchange_order_id` met prefix `paper-`
- Retourneert altijd `OrderResult` (nooit `None`)
- Audit log per order naar `ANT_LOGS/paper_execution/{order_id}.jsonl`

### D5 — `tests/test_order_schema.py`
25 tests verdeeld over vijf klassen:

| Klasse | Tests |
|--------|-------|
| TestLiveOrderDefaults | 5 |
| TestLiveOrderValidation | 8 |
| TestLiveOrderBuyDirectionality | 3 |
| TestLiveOrderSellDirectionality | 3 |
| TestOrderResult | 4 |
| TestOrderRejectionReason | 2 |

### D6 — `tests/test_execution_gate.py`
31 tests verdeeld over acht klassen:

| Klasse | Tests |
|--------|-------|
| TestLiveGateColonyHalted | 2 |
| TestLiveGateMissionNotActive | 2 |
| TestLiveGateCapitalCheck | 1 |
| TestLiveGateMarketDataStale | 3 |
| TestLiveGateAdapterAvailability | 4 |
| TestLiveGateCheckOrder | 3 |
| TestLiveGateAuditLog | 3 |
| TestPaperGateChecks | 5 |
| TestPaperGateFill | 6 |
| TestPaperGateAuditLog | 2 |

---

## Gate-bewijs: kerngedrag aangetoond

### 1. P3 — exit vóór entry (stop_loss + take_profit altijd verplicht)
```
TestLiveOrderValidation::test_stop_loss_must_be_positive        PASSED
TestLiveOrderBuyDirectionality::test_buy_stop_loss_above_take_profit_rejected   PASSED
TestLiveOrderSellDirectionality::test_sell_stop_loss_below_take_profit_rejected PASSED
```
`LiveOrder` wordt niet aangemaakt zonder geldige exit-logica. BUY en SELL
hebben tegengestelde directionaliteitsvereisten — beide afgedwongen door Pydantic.

### 2. P2 — fail-closed: elke onzekere check blokkeert het order
```
TestLiveGateColonyHalted::test_halted_colony_rejects_order      PASSED
TestLiveGateMissionNotActive::test_unknown_mission_rejected     PASSED
TestLiveGateMarketDataStale::test_none_market_data_rejected     PASSED
TestLiveGateMarketDataStale::test_stale_market_data_rejected    PASSED
TestLiveGateAdapterAvailability::test_unavailable_adapter_rejected PASSED
```
Vijf afzonderlijke checks, elk met een eigen `OrderRejectionReason`. Geen
enkele check wordt overgeslagen als een eerdere al faalt.

### 3. Volgorde van checks bewezen
```
TestLiveGateCheckOrder::test_halted_before_mission_check        PASSED
TestLiveGateCheckOrder::test_mission_check_before_market_data_check PASSED
TestLiveGateCheckOrder::test_market_data_before_adapter_check   PASSED
```
Check 1 (HALTED) gaat vóór check 2 (mission). Check 2 gaat vóór check 4
(market data). Check 4 gaat vóór check 5 (adapter).

### 4. Adapter-resultaat wordt transparant doorgegeven
```
TestLiveGateAdapterAvailability::test_adapter_returns_none_propagated     PASSED
TestLiveGateAdapterAvailability::test_adapter_returns_rejected_propagated PASSED
TestLiveGateAdapterAvailability::test_available_adapter_delegates_order   PASSED
```
`None` van de adapter = `None` van de gate (adapter-fout, niet zakelijke weigering).
Exchange-weigering = `OrderResult.accepted=False` met `EXCHANGE_REJECTED`.

### 5. PaperExecutionGate — MARKET en LIMIT fill correct
```
TestPaperGateFill::test_market_order_fills_at_close             PASSED
TestPaperGateFill::test_limit_order_fills_at_limit_price        PASSED
TestPaperGateFill::test_paper_gate_never_returns_none           PASSED
TestPaperGateFill::test_two_fills_have_different_exchange_ids   PASSED
```
MARKET fill = `market_data.close`. LIMIT fill = `order.limit_price`.
Paper gate retourneert nooit `None` — altijd een `OrderResult`.

### 6. Audit logs bewezen voor beide gates
```
TestLiveGateAuditLog::test_rejection_written_to_log             PASSED
TestLiveGateAuditLog::test_accepted_written_to_log              PASSED
TestPaperGateAuditLog::test_accepted_written_to_log             PASSED
TestPaperGateAuditLog::test_rejection_written_to_log            PASSED
```
Live gate schrijft naar `ANT_LOGS/execution/{order_id}.jsonl`.
Paper gate schrijft naar `ANT_LOGS/paper_execution/{order_id}.jsonl`.
Geen log wanneer `logs_root=None` (geen exception).

---

## Totale teststand na Fase 8

| Fase | Tests |
|------|-------|
| Fase 1-2 (kolonie-kern) | 157 |
| Fase 3 (Queen governance) | 31 |
| Fase 4 (Strategy Lab) | 98 |
| Fase 5 (Multi-Biome) | 68 |
| Fase 6 (Queen Allocator) | 49 |
| Fase 7 (Multi-Node Sim) | 84 |
| Fase 8 (Guarded Live Adapters) | 56 |
| **Totaal** | **543** |

```
543 passed in 0.86s
```

---

**Gate gesloten.** `GUARDED_LIVE_ADAPTERS_BEWEZEN`
