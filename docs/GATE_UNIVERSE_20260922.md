# Stage-gate UNIVERSE DEFINITION — resultaat 22-09-2026

Pre-registratie: docs/PREREG_UNIVERSE_20260922.md (b091e55).
Kanttekening: die commit volgde op een census-run in hetzelfde venster en bewijst
dus niet dat de regels vóór de uitkomst lagen. N = 40 was eerder in het gesprek
gekozen; de overige regels waren voorgesteld voordat de uitvoer bekend was.
Het powerscript (2db735d, 16:52) is wel aantoonbaar vóór de run gecommit.

## Census (scripts/universe_census.py)
- 427 EUR-markten (426 trading, 1 halted). Uitgesloten: EURC, EURCV, EUROP, FRAX,
  USDC, USDCV. Drie daarvan werden door de ingebouwde peg-controle gevonden.
- Dagcandles gestempeld op 00:00 UTC (begin van de periode). Volume in basismunt;
  omzet EUR = volume x close (BTC-EUR ~EUR 143 mln/dag, plausibel).
- Mediane munt ~EUR 38.000 omzet/dag; P90 ~EUR 385.000.
- Geen gestopte markten zichtbaar: gedelistte markten staan niet in /markets.
  SURVIVORSHIP BIAS is een gedocumenteerde beperking, niet afwezig.

## Drempelregel (N = 40 in >= 90% van 48 maanden, 2022-10 t/m 2026-09)
| drempel | maanden >= 40 | min | mediaan |
|---|---|---|---|
| 0 | 48/48 | 98 | 180 |
| 10k | 48/48 | 90 | 157 |
| **50k** | **48/48** | **42** | **112** |
| 100k | 42/48 | 26 | 87 |
| 250k | 30/48 | 12 | 49 |
| 1M | 1/48 | 4 | 15 |
Gekozen volgens regel: EUR 50.000. BREEDTE: PASS.

## Powercontrole (scripts/universe_power.py)
T = 207 weken; universum mediaan 112, min 43.
sigma (std) 12,04% per week; sigma (robuust, MAD) 5,88%.
delta_min pre-registratie (N=40, k=8): 0,847%/week > grens 0,75%. POWER: FAIL.
delta_min werkelijk universum (k = 1/5 van N_t): 0,534%/week.

## Status
- U(t)-definitie bij drempel EUR 50.000: bruikbaar, nog niet FROZEN (module + tests ontbreken).
- Ontwerp vervolgtoets met k = 8: FAIL op power. Vervangen alleen via een nieuwe,
  gecommitte versie van de pre-registratie, vóór enige rangschikking is gemeten.
- Staarten: std is 2x de robuuste sigma. Gemiddelde wordt door enkele munten
  gedomineerd; mediaan en trefkans wegen zwaarder.
- Capaciteit: bij EUR 50.000 dagomzet is ~EUR 500 per order realistisch;
  kosten boven 0,25%/kant zijn waarschijnlijk. Onderzoeksresultaat != verhandelbaar.
