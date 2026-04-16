# Gate-bewijs Fase 4 — Strategy Lab

**Gate-ID:** `STRATEGIE_KANDIDAAT_PIPELINE_BEWEZEN`
**Datum:** 2026-04-16
**Status:** GOEDGEKEURD
**Testresultaat:** 286/286 passed (0.61s)

---

## Gate-criteria

Fase 4 is afgesloten als bewezen is dat:

1. De `Backtester` deterministisch en correct backtests uitvoert op OHLCV-data
2. `PromotionCriteria` correct beoordeelt op sharpe, drawdown en trade count
3. Queen de volledige promotieketen uitvoert: RESEARCH → PAPER → APPROVED → LIVE
4. Queen een candidate op elk moment kan afwijzen met audit trail
5. Promoties zijn immutabel — origineel blijft ongewijzigd, provenance groeit append-only
6. **End-to-end pipeline bewezen**: bars → BacktestResults → StrategyCandidate → Queen beslist

---

## Testklassen en gedekte gevallen

### TestBacktesterConfig — 4 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_invalid_direction_raises` | Ongeldige direction → ValueError |
| `test_zero_tp_raises` | take_profit_pct=0 → ValueError |
| `test_zero_sl_raises` | stop_loss_pct=0 → ValueError |
| `test_zero_max_bars_raises` | max_bars_held=0 → ValueError |

### TestBacktester — 12 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_empty_bars_raises` | Lege bars → ValueError |
| `test_long_take_profit_hit` | LONG prijs raakt TP → win, win_rate=1.0 |
| `test_long_stop_loss_hit` | LONG prijs raakt SL → verlies, win_rate=0.0 |
| `test_long_ttl_exit` | Geen TP/SL binnen max_bars → TTL exit |
| `test_short_take_profit_hit` | SHORT prijs raakt TP → win |
| `test_short_stop_loss_hit` | SHORT prijs raakt SL → verlies |
| `test_win_rate_all_wins` | Alle trades winstgevend → win_rate=1.0 |
| `test_win_rate_all_losses` | Alle trades verliesgevend → win_rate=0.0 |
| `test_max_drawdown_zero_when_all_wins` | Equity stijgt altijd → drawdown=0.0 |
| `test_max_drawdown_positive_after_loss` | Win gevolgd door verlies → drawdown > 0 |
| `test_sharpe_none_with_single_trade` | Minder dan 2 trades → sharpe=None |
| `test_total_trades_matches_price_series` | Trade count klopt met prijsserie |

### TestPromotionCriteria — 10 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_passes_when_all_criteria_met` | Alle drempels gehaald → passed=True |
| `test_fails_on_low_sharpe` | Sharpe onder drempel → failed met reden |
| `test_fails_on_high_drawdown` | Drawdown boven max → failed |
| `test_fails_on_insufficient_trades` | Te weinig trades → failed |
| `test_fails_multiple_criteria_combined_in_reason` | Alle failures gecombineerd in één reden |
| `test_fails_when_sharpe_is_none` | sharpe=None → failed (ontbrekend veld) |
| `test_no_results_for_non_research_paper_status` | APPROVED-status → geen resultaten → failed |
| `test_paper_results_used_for_paper_status` | PAPER-status → paper_results gebruikt |
| `test_invalid_max_drawdown_raises` | max_drawdown_pct=0 → ValueError |
| `test_invalid_min_trades_raises` | min_trades=0 → ValueError |

### TestQueenPromotion — 15 tests

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_promote_research_to_paper` | RESEARCH → PAPER geaccepteerd |
| `test_promote_paper_to_approved_sets_approved_by` | PAPER → APPROVED: approved_by="queen" |
| `test_promote_approved_to_live` | APPROVED → LIVE: status en approved_by correct |
| `test_promotion_original_candidate_unchanged` | Origineel ongewijzigd na promotie (immutabiliteit) |
| `test_promotion_appends_provenance` | Provenance +1 entry, actor="queen" |
| `test_promotion_with_passing_criteria` | Criteria geslaagd → promotie geaccepteerd |
| `test_promotion_blocked_by_failing_criteria` | Criteria gefaald → promotie geweigerd |
| `test_invalid_transition_rejected` | RESEARCH→APPROVED (overslaat PAPER) → geblokkeerd |
| `test_live_is_terminal_cannot_be_promoted` | LIVE is terminaal → promotie geblokkeerd |
| `test_rejected_is_terminal_cannot_be_promoted` | REJECTED is terminaal → promotie geblokkeerd |
| `test_reject_candidate_sets_rejected_status` | reject_candidate() → status=REJECTED |
| `test_reject_appends_provenance_with_reason` | Reden van afwijzing in provenance opgeslagen |
| `test_reject_already_rejected_is_idempotent` | Al-afgewezen candidate ongewijzigd teruggegeven |
| `test_promotion_log_written` | strategy/{candidate_id}.jsonl bevat promoted event |
| `test_rejection_log_written` | strategy/{candidate_id}.jsonl bevat rejected event |

### TestStrategyPipeline — 4 tests (gate-kern)

| Test | Wat wordt bewezen |
|------|-------------------|
| `test_pipeline_good_candidate_promoted` | Bars → backtest → criteria slagen → Queen promoveert |
| `test_pipeline_bad_candidate_rejected` | Bars → backtest → criteria falen → promotie geweigerd |
| `test_pipeline_full_research_to_approved` | RESEARCH → PAPER → APPROVED volledig doorlopen |
| `test_pipeline_provenance_tracks_full_history` | Na 2 promoties: 2 provenance-entries, beide van Queen |

---

## Promotieketen bewezen (P1 — Queen is soeverein)

```
RESEARCH → PAPER    ← backtest_results + criteria (optioneel)
PAPER    → APPROVED ← paper_results + criteria (optioneel), approved_by="queen" gezet
APPROVED → LIVE     ← approved_by="queen" vereist
elk stadium → REJECTED ← Queen kan altijd afwijzen
```

Geblokkeerde transities bewezen:
- RESEARCH → APPROVED (skip PAPER): `test_invalid_transition_rejected`
- LIVE → alles: `test_live_is_terminal_cannot_be_promoted`
- REJECTED → alles: `test_rejected_is_terminal_cannot_be_promoted`

---

## Immutabiliteit en provenance bewezen (P4 — Observability)

- `test_promotion_original_candidate_unchanged`: `model_copy(update={...})` — origineel intact
- `test_promotion_appends_provenance`: provenance groeit met precies +1 per actie
- `test_pipeline_provenance_tracks_full_history`: na 2 promoties = 2 entries, in volgorde
- `test_reject_already_rejected_is_idempotent`: dubbele reject voegt geen entry toe

---

## Backtester — statistische correctheid

| Statistiek | Bewezen door |
|------------|--------------|
| win_rate=1.0 bij alle wins | `test_win_rate_all_wins` |
| win_rate=0.0 bij alle verliezen | `test_win_rate_all_losses` |
| drawdown=0.0 bij stijgende equity | `test_max_drawdown_zero_when_all_wins` |
| drawdown>0 na een verlies | `test_max_drawdown_positive_after_loss` |
| sharpe=None bij < 2 trades | `test_sharpe_none_with_single_trade` |
| TP/SL/TTL exits correct | `test_long_*`, `test_short_*`, `test_long_ttl_exit` |

---

## Audit trail bewezen (P4)

| Event | Log-pad | Test |
|-------|---------|------|
| `strategy_candidate_promoted` | `ANT_LOGS/strategy/{candidate_id}.jsonl` | `test_promotion_log_written` |
| `strategy_candidate_rejected` | `ANT_LOGS/strategy/{candidate_id}.jsonl` | `test_rejection_log_written` |

---

## Totale testresultaten

```
tests/test_exit_chain.py         74 passed  (Fase 1)
tests/test_mission_validation.py 21 passed  (Fase 0)
tests/test_paper_loop.py         63 passed  (Fase 2)
tests/test_queen.py              31 passed  (Fase 3)
tests/test_scheduler_tick.py     27 passed  (Fase 0)
tests/test_schemas.py            25 passed  (Fase 0)
tests/test_strategy_lab.py       45 passed  (Fase 4)
                               ──────────
Totaal:                         286/286 passed in 0.61s (0 failures, 0 errors)
```

---

## Gate-declaratie

```
GATE: STRATEGIE_KANDIDAAT_PIPELINE_BEWEZEN
STATUS: GOEDGEKEURD
DATUM: 2026-04-16
TESTS: 286/286 passed
FASE 4 AFGESLOTEN — KLAAR VOOR FASE 5
```

Fase 5: Multi-biome scaffolding.
