# Gate-bewijs — Fase 1: EXIT_KETEN_VOLLEDIG_CORRECT

*Dit document bewijst dat de exit-keten van ANT COLONY v2 volledig correct werkt
voordat enige entry-logica wordt gebouwd. Het is de formele gate voor Fase 1.*

---

## Gate-conditie

```
EXIT_KETEN_VOLLEDIG_CORRECT
```

**Status: BEWEZEN**
**Datum: 2026-04-16**
**Test run: 147 passed, 0 failed, 0 errors — 0.24s**

---

## Wat bewezen is

### 1. PaperPosition — schema-integriteit

| Bewijs | Testmethode |
|--------|-------------|
| LONG stop-loss moet onder entry liggen | `test_long_stop_loss_at_or_above_entry_is_rejected` |
| LONG take-profit moet boven entry liggen | `test_long_take_profit_at_or_below_entry_is_rejected` |
| SHORT stop-loss moet boven entry liggen | `test_short_stop_loss_at_or_below_entry_is_rejected` |
| SHORT take-profit moet onder entry liggen | `test_short_take_profit_at_or_above_entry_is_rejected` |
| Gesloten positie vereist exit_price + closed_at + exit_reason | `test_closed_position_without_exit_fields_is_rejected` |
| PnL berekening LONG (winst en verlies) | `test_realized_pnl_long_profit`, `test_realized_pnl_long_loss` |
| PnL berekening SHORT (winst) | `test_realized_pnl_short_profit` |
| Drawdown-berekening LONG en SHORT | `test_drawdown_from_peak_long`, `test_drawdown_from_peak_short` |

### 2. Stop-loss — alle paden bewezen

| Bewijs | Testmethode |
|--------|-------------|
| LONG triggert op exact stop-loss prijs | `test_long_triggers_at_stop_loss_price` |
| LONG triggert onder stop-loss prijs | `test_long_triggers_below_stop_loss_price` |
| LONG triggert NIET boven stop-loss prijs | `test_long_does_not_trigger_above_stop_loss_price` |
| SHORT triggert op exact stop-loss prijs | `test_short_triggers_at_stop_loss_price` |
| SHORT triggert boven stop-loss prijs | `test_short_triggers_above_stop_loss_price` |
| SHORT triggert NIET onder stop-loss prijs | `test_short_does_not_trigger_below_stop_loss_price` |

### 3. Take-profit — alle paden bewezen

| Bewijs | Testmethode |
|--------|-------------|
| LONG triggert op exact take-profit prijs | `test_long_triggers_at_take_profit_price` |
| LONG triggert boven take-profit prijs | `test_long_triggers_above_take_profit_price` |
| LONG triggert NIET onder take-profit prijs | `test_long_does_not_trigger_below_take_profit_price` |
| SHORT triggert op exact take-profit prijs | `test_short_triggers_at_take_profit_price` |
| SHORT triggert onder take-profit prijs | `test_short_triggers_below_take_profit_price` |
| SHORT triggert NIET boven take-profit prijs | `test_short_does_not_trigger_above_take_profit_price` |

### 4. TTL — alle paden bewezen

| Bewijs | Testmethode |
|--------|-------------|
| Triggert op exact TTL | `test_triggers_when_age_equals_ttl` |
| Triggert voorbij TTL | `test_triggers_when_age_exceeds_ttl` |
| Triggert NIET vóór TTL | `test_does_not_trigger_before_ttl` |
| Triggert NIET voor verse positie | `test_does_not_trigger_for_fresh_position` |

### 5. Drawdown — alle paden bewezen

| Bewijs | Testmethode |
|--------|-------------|
| LONG triggert bij drawdown > drempel | `test_long_triggers_when_drawdown_exceeds_threshold` |
| LONG triggert NIET bij drawdown = exact drempel | `test_long_does_not_trigger_at_exact_threshold` |
| LONG triggert NIET bij drawdown < drempel | `test_long_does_not_trigger_below_threshold` |
| SHORT triggert bij drawdown > drempel | `test_short_triggers_when_drawdown_exceeds_threshold` |
| SHORT triggert NIET bij drawdown < drempel | `test_short_does_not_trigger_below_threshold` |
| Geen drawdown bij prijs op peak | `test_no_drawdown_when_at_peak` |
| Invalide drempel geweigerd | `test_invalid_threshold_raises` |

### 6. Daily-loss-breach — alle paden bewezen

| Bewijs | Testmethode |
|--------|-------------|
| Triggert op exact limiet | `test_triggers_at_limit` |
| Triggert boven limiet | `test_triggers_above_limit` |
| Triggert NIET onder limiet | `test_does_not_trigger_below_limit` |
| Triggert NIET zonder context (fail-safe) | `test_does_not_trigger_without_context` |
| Invalide limiet geweigerd | `test_invalid_limit_raises` |

### 7. ExitEvaluator — elk exit-pad individueel bewezen

| Bewijs | Testmethode |
|--------|-------------|
| Stop-loss sluit LONG correct | `test_stop_loss_long` |
| Stop-loss sluit SHORT correct | `test_stop_loss_short` |
| Take-profit sluit LONG correct | `test_take_profit_long` |
| Take-profit sluit SHORT correct | `test_take_profit_short` |
| TTL sluit positie correct | `test_ttl_exit` |
| Drawdown sluit LONG correct | `test_drawdown_exit_long` |
| Drawdown sluit SHORT correct | `test_drawdown_exit_short` |
| Daily-loss sluit positie correct | `test_daily_loss_exit` |
| exit_price = prijs op moment van sluiten | `test_stop_loss_long` (assert `exit_price == 28_999.0`) |
| closed_at aanwezig na sluiting | `test_stop_loss_long` (assert `closed_at is not None`) |

### 8. Prioriteitsordening bewezen

| Bewijs | Testmethode |
|--------|-------------|
| Daily-loss verslaat stop-loss | `test_daily_loss_beats_stop_loss` |
| Daily-loss verslaat take-profit | `test_daily_loss_beats_take_profit` |
| Drawdown verslaat stop-loss | `test_drawdown_beats_stop_loss` |
| Stop-loss verslaat TTL | `test_stop_loss_beats_ttl` |
| Stop-loss verslaat take-profit | `test_stop_loss_beats_take_profit` |

Bewezen volgorde: `daily_loss > drawdown > stop_loss > ttl > take_profit`

### 9. Peak-trailing bewezen

| Bewijs | Testmethode |
|--------|-------------|
| LONG peak stijgt mee met prijs | `test_long_peak_moves_up_with_price` |
| LONG peak daalt NIET mee | `test_long_peak_does_not_move_down` |
| SHORT peak daalt mee met prijs | `test_short_peak_moves_down_with_price` |
| SHORT peak stijgt NIET mee | `test_short_peak_does_not_move_up` |
| Drawdown triggert correct na peak + daling | `test_drawdown_triggers_only_after_peak_then_reversal` |

### 10. Fail-closed gedrag bewezen

| Bewijs | Testmethode |
|--------|-------------|
| Stale prijs (0) blokkeert evaluatie | `test_stale_price_zero_is_blocked` |
| Stale prijs (negatief) blokkeert evaluatie | `test_stale_price_negative_is_blocked` |
| Gesloten positie passeert ongewijzigd | `test_closed_position_passes_through_unchanged` |
| Lege conditielijst geweigerd | `test_empty_conditions_list_raises` |
| Positie ongewijzigd bij geen exit | `test_position_unchanged_when_no_exit` |

### 11. Multi-tick scenario's bewezen

| Bewijs | Testmethode |
|--------|-------------|
| LONG take-profit na gestage stijging | `test_long_profitable_journey_then_take_profit` |
| LONG stop-loss na drawdown binnen drempel | `test_long_stop_loss_after_drawdown_within_threshold` |
| Daily-loss stopt positie mid-journey | `test_daily_loss_halts_position_mid_journey` |
| SHORT volledige levenscyclus tot take-profit | `test_short_full_lifecycle` |

---

## Test-uitvoering

```
platform win32 — Python 3.14.3, pytest 9.0.3
rootdir: C:\Users\vikke\ant-colony-v2

tests/test_exit_chain.py         74 passed
tests/test_mission_validation.py 24 passed
tests/test_scheduler_tick.py     22 passed
tests/test_schemas.py            27 passed

147 passed in 0.24s
```

---

## Wat dit betekent voor Fase 2

De exit-keten is bewezen. De volgende fase (entry + volledige loop, paper only)
mag worden gestart met de volgende garanties:

1. Elke positie heeft altijd een stop-loss, take-profit én TTL — schema dwingt dit af
2. Elke exit-conditie triggert correct voor zowel LONG als SHORT
3. Risk-condities (drawdown, daily-loss) hebben hogere prioriteit dan profit-condities
4. Stale marktdata blokkeert evaluatie — systeem faalt niet open
5. Gesloten posities worden nooit dubbel verwerkt
6. Peak-trailing werkt correct in beide richtingen

Entry-logica bouwt op deze bewezen exit-keten. Niet andersom.

---

## Gate-verklaring

```
GATE: EXIT_KETEN_VOLLEDIG_CORRECT
BEWEZEN OP: 2026-04-16
DOOR: ANT COLONY v2 test suite — 147/147 passed
OPERATOR BEVESTIGING VEREIST voor activatie Fase 2
```

---

*Dit document wordt niet automatisch gewijzigd.*
*Operator bevestigt gate door commit met bericht: `gate: EXIT_KETEN_VOLLEDIG_CORRECT`*
