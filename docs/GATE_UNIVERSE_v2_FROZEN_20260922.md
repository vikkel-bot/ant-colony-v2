# Stage-gate UNIVERSE DEFINITION — v2 FROZEN (22-09-2026)

Pre-registratie: docs/PREREG_UNIVERSE_v2_20260922.md (c7f2fe0, vóór de code).
Vervangt v1 (FAIL op power bij k = 8, f81eb23).

Implementatie: ant_colony/lab/universe.py — universe(candles, t) en top_k(n).
Parameters: Bitvavo EUR; historie >= 180 d; mediane dagomzet EUR over [t-30d, t)
>= 50.000; vaste uitsluitlijst; k(t) = max(8, N_t // 5); alleen data < t.

FROZEN-voorwaarden:
1. tests/test_universe.py: 8/8 groen (toekomst-onafhankelijkheid, candle op t
   genegeerd, 180-dagengrens, omzet = volume x close, uitsluitlijst,
   integer-afronding k, geen wall-clock, gelijk aan census).
2. Reproductie census op echte data: 48/48 maanden, 0 afwijkingen
   (min 42, mediaan 112; k van 8 tot 34).
3. Volledige suite: 2542 passed.

Power onder v2: delta_min 0,534%/week <= grens 0,75%. Uitsluitend ruisinformatie.

Beperkingen, overgenomen uit v1: survivorship (gedelistte markten onzichtbaar);
capaciteit (~EUR 500/order bij de drempel); kosten dunne munten > 0,25%/kant.

Open voor de rangschikkingstoets (nog niet vastgelegd): verrekening van een
geselecteerde munt die na t stopt met handelen.

Status: UNIVERSE DEFINITION = FROZEN. Nog geen enkele rangschikking gemeten.
