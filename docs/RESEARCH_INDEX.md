<!-- GEGENEREERD door scripts/build_research_index.py — niet met de hand bewerken.
     Open keuzes en parkeerlijst staan in docs/RESEARCH_OPEN_ITEMS.md. -->

# Onderzoeksindex — Colony V2

Eén pagina met de actuele stand. Gegenereerd uit het toetsregister, de
gate-documenten, de kostenmodellen en de git-historie.

## De drie maatstaven (niet door elkaar halen)

| # | maatstaf | beantwoordt | waartegen |
|---|---|---|---|
| 1 | nulmodel | bevat de sensor informatie? | verdeling van toeval op dezelfde data |
| 2 | kostenhorde | is het verhandelbaar? | gemeten kosten x factor 3 |
| 3 | allocatiebenchmark | verdient dit kapitaal? | nog niet vastgelegd; pas relevant na 1 en 2 |

Maatstaf 3 is nog nooit gebruikt: er is nog niets door 1 en 2 gekomen.

## Toetsen

Bron: docs/TOETSREGISTER.md (append-only). Elke regel is een aparte registratie;
de laatste regel per id is de huidige status.

| id | familie | status | uitkomst | pre-registratie | resultaat |
|---|---|---|---|---|---|
| T001 | CRYPTO_XS_PRICE_MOMENTUM | **PENDING** | - | [PREREG_T001_MOMENTUM21_20260922.md](PREREG_T001_MOMENTUM21_20260922.md) (2026-09-22) | - |
| T001 | CRYPTO_XS_PRICE_MOMENTUM | **FAIL** | FAIL (S1 niet significant onder N1). | [PREREG_T001_MOMENTUM21_20260922.md](PREREG_T001_MOMENTUM21_20260922.md) (2026-09-22) | [T001_RESULT_20260922.md](T001_RESULT_20260922.md) |

## Stage-gates

| document | titel | eerste commit |
|---|---|---|
| [GATE_FASE1_EXIT_KETEN.md](GATE_FASE1_EXIT_KETEN.md) | Gate-bewijs — Fase 1: EXIT_KETEN_VOLLEDIG_CORRECT | 2026-04-16 |
| [GATE_FASE2_PAPER_LOOP.md](GATE_FASE2_PAPER_LOOP.md) | Gate-bewijs Fase 2 — Paper Loop | 2026-04-16 |
| [GATE_FASE3_QUEEN.md](GATE_FASE3_QUEEN.md) | Gate-bewijs Fase 3 — Queen Governance | 2026-04-16 |
| [GATE_FASE4_STRATEGY_LAB.md](GATE_FASE4_STRATEGY_LAB.md) | Gate-bewijs Fase 4 — Strategy Lab | 2026-04-16 |
| [GATE_FASE5_MULTI_BIOME.md](GATE_FASE5_MULTI_BIOME.md) | Gate Fase 5 — Multi-Biome Scaffolding | 2026-04-16 |
| [GATE_FASE6_QUEEN_ALLOCATOR.md](GATE_FASE6_QUEEN_ALLOCATOR.md) | Gate Fase 6 — Queen Allocator Upgrade | 2026-04-16 |
| [GATE_FASE7_MULTI_NODE_SIM.md](GATE_FASE7_MULTI_NODE_SIM.md) | Gate Fase 7 — Paper-mode multi-node kolonie simulatie | 2026-04-16 |
| [GATE_FASE8_GUARDED_LIVE_ADAPTERS.md](GATE_FASE8_GUARDED_LIVE_ADAPTERS.md) | Gate Fase 8 — Guarded live adapters | 2026-04-16 |
| [GATE_FASE9_COLONY_DASHBOARD.md](GATE_FASE9_COLONY_DASHBOARD.md) | Gate Fase 9 — Colony Dashboard | 2026-04-16 |
| [GATE_UNIVERSE_20260922.md](GATE_UNIVERSE_20260922.md) | Stage-gate UNIVERSE DEFINITION — resultaat 22-09-2026 | 2026-09-22 |
| [GATE_UNIVERSE_v2_FROZEN_20260922.md](GATE_UNIVERSE_v2_FROZEN_20260922.md) | Stage-gate UNIVERSE DEFINITION — v2 FROZEN (22-09-2026) | 2026-09-22 |

## Kostenmodellen

Een versie wordt nooit overschreven. Een nieuwe meting levert een nieuwe versie op;
reeds geregistreerde toetsen blijven verwijzen naar de versie die toen gold.

| versie | gate-estimator | tarief/kant | referentieschaal | snapshots | markten |
|---|---|---|---|---|---|
| [COST_MODEL_V1_20260924](COST_MODEL_V1_20260924.json) | p75 | 0.0025 | EUR 5000 | 8 | 172 |

## Metingen en rapporten

| document | titel | eerste commit |
|---|---|---|
| [METING_BTC_DONCHIAN_PERMUTATIE_20260920.md](METING_BTC_DONCHIAN_PERMUTATIE_20260920.md) | Meting — BTC-EUR Donchian trendvolgen, dagbasis, permutatietoets | 2026-09-21 |
| [METING_WATCHTOWER_SENTIMENT_20260920.md](METING_WATCHTOWER_SENTIMENT_20260920.md) | Meting — Watchtower nieuwssentiment, BTC-EUR | 2026-09-20 |
| [COST_CENSUS_20260924.md](COST_CENSUS_20260924.md) | Kostenmeting Bitvavo — 22 t/m 24 september 2026 | 2026-09-24 |
| [SCREENING_TURNOVER_20260924.md](SCREENING_TURNOVER_20260924.md) | Pre-test screening: turnover en minimale bruto edge | niet gecommit |
| [OPEN_EXECUTION_CONFLICT_20260924.md](OPEN_EXECUTION_CONFLICT_20260924.md) | Openstaand architectuurconflict — cross-sectionele research vs paper-keten | 2026-09-24 |
| [PREREG_T001_MOMENTUM21_20260922.md](PREREG_T001_MOMENTUM21_20260922.md) | Pre-registratie T001 — crypto cross-sectioneel momentum, 3 weken | 2026-09-22 |
| [PREREG_UNIVERSE_20260922.md](PREREG_UNIVERSE_20260922.md) | Pre-registratie — universum-drempel cross-sectioneel crypto | 2026-09-22 |
| [PREREG_UNIVERSE_v2_20260922.md](PREREG_UNIVERSE_v2_20260922.md) | Pre-registratie v2 — universum cross-sectioneel crypto | 2026-09-22 |

## Open keuzes en parkeerlijst

Bron: docs/RESEARCH_OPEN_ITEMS.md

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
