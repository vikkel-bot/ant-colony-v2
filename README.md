# Ant Colony v2

Ant Colony v2 is een experimentele trading-colony in Python. De core bestaat
uit een Queen, scheduler, gespecialiseerde ants, broker/market-data adapters en
een FastAPI-dashboard. De standaardmodus is defensief: paper trading en
fail-closed gedrag hebben voorrang boven live execution.

## Belangrijkste onderdelen

- `ant_colony/queen/`: missie-uitgifte, kapitaalallocatie en advisor-logica.
- `ant_colony/colony/scheduler/`: agentregistratie, heartbeats, TTL en kill-switches.
- `ant_colony/ants/`: scout, research, strategy, paper, execution, audit en equities ants.
- `ant_colony/biome/adapters/`: Bitvavo, Yahoo Finance en IBKR adapters.
- `ant_colony/dashboard/`: FastAPI API en statische dashboard UI.
- `tests/`: regressietests voor schemas, ants, dashboard, adapters en pipelines.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Voor alleen runtime-dependencies kun je `requirements.txt` gebruiken.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

De huidige suite bevat ruim tweeduizend tests en is de beste eerste check na
wijzigingen aan ants, scheduler, adapters of dashboard endpoints.

## Colony starten

```powershell
.\.venv\Scripts\python.exe scripts\start_colony.py --capital 50000 --port 8000
```

Zonder `--capital` probeert de colony startkapitaal af te leiden uit de
geregistreerde adapters. Gebruik voor lokale ontwikkeling liever een expliciete
capital override.

## Veiligheidsinstellingen

- `BITVAVO_PAPER_MODE=true` is de veilige standaard voor crypto-orders.
- `IBKR_PAPER_MODE=true` gebruikt de IBKR paper trading poort.
- `EQUITIES_ENABLED=true` zet equities ants expliciet aan.
- `NEWS_ANT_ENABLED=true` vereist daarnaast `NEWS_API_KEY`.
- `WATCHTOWER_ENABLED=true` activeert de Watchtower poller.

Zet live modes alleen aan wanneer credentials, brokeromgeving en risico-limieten
bewust zijn gecontroleerd.
