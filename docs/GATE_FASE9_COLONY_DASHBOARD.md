# Gate Fase 9 — Colony Dashboard

**Gate-conditie:** `DASHBOARD_OPERATIONEEL`
**Status:** BEWEZEN
**Datum:** 2026-04-16
**Testresultaat:** 591/591 passed

---

## Wat bewijst dit gate

Fase 9 levert een volledig operationeel read-only dashboard voor de ANT COLONY.
Het dashboard draait als FastAPI-server op PC2 (`0.0.0.0:8000`) en is bereikbaar
via browser op PC1 via het lokale netwerk. Alle colony-state wordt gelezen uit
de levende Queen, ColonyScheduler en append-only JSONL logs — het dashboard
schrijft nooit. Een crash van het dashboard raakt de colony niet.

---

## Geleverde deliverables

### D1 — `ant_colony/dashboard/api.py`
- `ColonyContext` — injecteerbare verwijzing naar Queen, Scheduler, logs_root, broker_names
- Pydantic response modellen voor alle 8 endpoints
- `create_router(ctx)` factory met volledige `APIRouter`:

| Endpoint | Methode | Beschrijving |
|---|---|---|
| `/api/status` | GET | Colony status + laatste tick timestamp uit `scheduler.jsonl` |
| `/api/metrics` | GET | Kapitaal totalen + utilization% + actieve agent-count |
| `/api/performance` | GET | PnL dag/week/maand/jaar/alltime uit paper trade-logs |
| `/api/biomes` | GET | Allocatie per biome via `queen.allocation_snapshot()` |
| `/api/ants` | GET | Actieve missions → TTL countdown op basis van `issued_at` |
| `/api/brokers` | GET | Broker naam + connected/standby + ingezet kapitaal |
| `/api/ticker` | GET | Laatste 20 audit events, gesorteerd op timestamp |
| `/api/killswitch` | POST | Queen kill-switch — vereist `operator_confirm=true` |

Fail-safe: elke endpoint retourneert lege/nul data als `ctx.queen` of
`ctx.scheduler` `None` is — nooit een crash.

### D2 — `ant_colony/dashboard/server.py`
- `create_app(ctx)` factory — FastAPI app met router + static mount + health check
- `GET /` serveert `index.html`; `GET /health` voor process monitors
- `run(ctx, host, port)` — blocking uvicorn start op `0.0.0.0:8000`
- `__main__` — standalone start zonder colony context (UI-test op PC2)

### D3 — `ant_colony/dashboard/static/index.html`
Één HTML bestand (32 KB), geen externe dependencies.

**Topbar** (volle breedte, sticky):
- Paarse Queen orb (♛) + "ANT COLONY v2" + "Queen dashboard · PC2 live"
- ECG-lijn SVG met CSS pulse-animatie + "laatste tick" `HH:MM:SS` + live `Xs geleden` counter
- Status-pill (`RUNNING` / `HALTED` / `PAUSED`) met pulserend bolletje + live klok
- Rode `NOODSTOP` knop → `confirm()` → Level 3 colony kill-switch

**Live tikker** (donkere balk):
- Pulserend groen bolletje + monospace tekst, wisselt elke 4 seconden via audit events

**Metrics rij** — 4 kaarten: Totaal kapitaal · Ingezet · Beschikbaar · Actieve mieren

**Linker kolom:**
- Performance — 5 periodes (dag/week/maand/jaar/all time), groen/rood op PnL-teken
- Biome allocatie — gekleurde progress bars (#1D9E75 crypto · #378ADD equities · #EF9F27 commodities)

**Rechter kolom:**
- Actieve mieren — tabel met gekleurde dot + agent type + node + live TTL countdown + live/actief badge
- Brokers — verbonden/standby badge + ingezet bedrag per biome
- Queen kill-switch — 3 knoppen (Level 1 agent · Level 2 node · Level 3 colony), allen rood

**JavaScript gedrag:**
- `fetchAll()` elke 5 seconden — alle 8 endpoints parallel via `Promise.all`
- `perSecond()` elke seconde — live klok, heartbeat countdown, TTL counters
- `rotateTicker()` elke 4 seconden — fade in/out door audit events
- Kill-switch: Level 1/2 vraagt `prompt()` voor scope, alle niveaus vragen `confirm()`

### D4 — `ant_colony/dashboard/__init__.py`
Exporteert: `ColonyContext`, `create_app`, `create_router`, `run`

### D5 — `tests/test_dashboard_api.py`
48 tests verdeeld over elf klassen:

| Klasse | Tests |
|---|---|
| `TestEmptyContext` | 8 — alle endpoints crashen niet op lege context |
| `TestStatusEndpoint` | 5 — RUNNING/HALTED status, last_tick uit JSONL |
| `TestMetricsEndpoint` | 4 — kapitaal, utilization, actieve agents |
| `TestPerformanceEndpoint` | 3 — PnL uit trade-logs, periodes correct |
| `TestBiomesEndpoint` | 3 — velden, fraction, leeg bij geen limieten |
| `TestAntsEndpoint` | 4 — velden, is_live, TTL aanwezig, meerdere agents |
| `TestBrokersEndpoint` | 4 — connected/standby, custom naam, default naam |
| `TestTickerEndpoint` | 4 — events lezen, sortering, leeg, kapotte regel geskipt |
| `TestKillSwitchEndpoint` | 5 — confirm vereist, niveau-validatie, delegatie Queen |
| `TestServerApp` | 3 — root HTML, health, None-context |
| `TestParseTs` | 4 — UTC, naive→UTC, None, garbage |

**requirements.txt** uitgebreid met `fastapi>=0.110.0`, `uvicorn[standard]>=0.29.0`,
`httpx>=0.27.0`.

---

## Gate-bewijs: kerngedrag aangetoond

### 1. Alle endpoints draaien zonder crash op lege context
```
TestEmptyContext::test_status_empty        PASSED
TestEmptyContext::test_metrics_empty       PASSED
TestEmptyContext::test_ticker_empty        PASSED
TestEmptyContext::test_killswitch_no_colony PASSED
```
`ColonyContext()` zonder queen/scheduler retourneert overal lege/nul data.
Kill-switch zonder colony retourneert 503 — geen crash.

### 2. Live state gelezen uit Queen en Scheduler
```
TestMetricsEndpoint::test_capital_total          PASSED
TestMetricsEndpoint::test_active_ants_count      PASSED
TestMetricsEndpoint::test_capital_allocated      PASSED
TestMetricsEndpoint::test_utilization_pct        PASSED
TestStatusEndpoint::test_running_status          PASSED
TestStatusEndpoint::test_halted_status           PASSED
```
Kapitaaltotalen en actieve missions worden vers gelezen uit de Queen.
Status volgt `ColonyScheduler.status` exact.

### 3. Laatste tick gelezen uit append-only log
```
TestStatusEndpoint::test_last_tick_from_log      PASSED
TestStatusEndpoint::test_no_log_file_last_tick_none PASSED
```
`scheduler.jsonl` wordt gelezen voor de heartbeat timestamp.
Ontbrekend logbestand → `last_tick=None`, geen exception.

### 4. Performance berekend uit trade-logs
```
TestPerformanceEndpoint::test_alltime_from_trades        PASSED
TestPerformanceEndpoint::test_old_trade_excluded_from_day PASSED
```
PnL-berekening leest `ANT_LOGS/paper/*_trades.jsonl`. Periodes worden
correct afgebakend op timestamp — een 2-dag-oude trade telt niet mee in "dag".

### 5. Ticker sorteert events en limiteert op 20
```
TestTickerEndpoint::test_returns_last_n_sorted   PASSED
TestTickerEndpoint::test_malformed_line_skipped  PASSED
```
25 events aangeboden → 20 teruggegeven, laatste event is de meest recente.
Corrupte JSONL-regels worden overgeslagen — geen crash.

### 6. Kill-switch fail-closed
```
TestKillSwitchEndpoint::test_requires_operator_confirm  PASSED
TestKillSwitchEndpoint::test_invalid_level_rejected     PASSED
TestKillSwitchEndpoint::test_level_3_activates_colony_kill PASSED
```
`operator_confirm=false` → 400, geen actie.
Ongeldig niveau → 400, geen actie.
Level 3 met `operator_confirm=true` → delegeert correct aan `queen.kill_switch()`.

### 7. Root serveert de dashboard HTML
```
TestServerApp::test_root_serves_html   PASSED
TestServerApp::test_health_endpoint    PASSED
```
`GET /` retourneert `text/html` met "ANT COLONY" in de body.
`GET /health` retourneert `{"ok": true}` voor process monitors.

---

## Totale teststand na Fase 9

| Fase | Tests |
|------|-------|
| Fase 1-2 (kolonie-kern) | 157 |
| Fase 3 (Queen governance) | 31 |
| Fase 4 (Strategy Lab) | 98 |
| Fase 5 (Multi-Biome) | 68 |
| Fase 6 (Queen Allocator) | 49 |
| Fase 7 (Multi-Node Sim) | 84 |
| Fase 8 (Guarded Live Adapters) | 56 |
| Fase 9 (Colony Dashboard) | 48 |
| **Totaal** | **591** |

```
591 passed in 1.59s
```

## Gebruik op PC2

```python
# Colony startup — dashboard als achtergrondthread
import threading
from ant_colony.dashboard.server import run
from ant_colony.dashboard.api import ColonyContext
from pathlib import Path

ctx = ColonyContext(
    queen=queen,
    scheduler=scheduler,
    logs_root=Path(r"C:\Trading\ANT_LOGS"),
)
t = threading.Thread(target=run, args=(ctx,), daemon=True)
t.start()
# Dashboard bereikbaar op http://<PC2-IP>:8000
```

---

**Gate gesloten.** `DASHBOARD_OPERATIONEEL`
