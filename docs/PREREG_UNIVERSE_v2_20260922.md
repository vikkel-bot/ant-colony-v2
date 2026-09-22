# Pre-registratie v2 — universum cross-sectioneel crypto

Vervangt het ontwerp van v1 (docs/PREREG_UNIVERSE_20260922.md), dat FAIL gaf op
power bij k = 8 (docs/GATE_UNIVERSE_20260922.md, f81eb23). Herziening op basis
van ruis (sigma), vóór enige rangschikking of meting van rendementen van een
selectie.

Bevroren:
- Bron: Bitvavo EUR-markten. Historie >= 180 dagen.
- Liquiditeit: mediane dagomzet EUR (volume x close) over [t-30d, t) >= 50.000.
- U(t) gebruikt uitsluitend candles met timestamp < t. Geen wall-clock.
- Uitsluitlijst: vaste lijst in ant_colony/lab/universe.py. Peg-verdachten uit
  de datacontrole worden NIET automatisch uitgesloten; lijstwijziging alleen via
  nieuwe pre-registratie.
- q = 1/5. k(t) = max(8, N_t // 5), integer-deling. Niet wisselen van afronding.
- Powergrens 0,75% per week, ongewijzigd.
- Testperiode 2022-10 t/m 2026-09.

Bekend vóór v2: delta_min onder deze k-regel = 0,534%/week (universe_power.py).
Dit is uitsluitend powerinformatie uit ruis, geen aanwijzing dat enige strategie
werkt.

Genoemd, niet hier geregeld: hoe een geselecteerde munt die na t stopt met
handelen wordt verrekend. Dat wordt vastgelegd in de pre-registratie van de
rangschikkingstoets. (universe_power.py filtert zulke munten weg; dat gebruikt
informatie na t en is alleen acceptabel voor de ruisschatting.)

FROZEN alleen als:
1. ant_colony/lab/universe.py + tests/test_universe.py aanwezig, tests groen;
2. U(t) reproduceert de census-telling bij 50.000 in alle 48 maanden exact;
3. volledige testsuite groen.
