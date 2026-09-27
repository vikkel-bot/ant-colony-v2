# Registerincident — 26-09-2026

## Wat er gebeurde

De pre-registratie van SIT-1A/1B (familie SIZING_CREDIT_INFO) is als volledige
tekst van 274 regels in docs/TOETSREGISTER.md geplaatst, commit
fa7eb64ed1bd0f64c893c76f0d758523338b3a19. Daarna is in commit
4915fb9933775f66120e66b7a939ea7956690f2b een hashveld in die tekst gewijzigd.

Twee vormfouten:
1. het register is een INDEX (een regel per registratie, met verwijzing naar een
   apart PREREG-document), geen opslagplaats voor registratietekst;
2. het register is append-only; ook het invullen van een hash achteraf is een
   wijziging.

De fail-closed verificatie sloeg aan met 79 schendingen. Dat is het instrument
dat werkt, niet een instrument dat te streng is.

## Wat er NIET is gebeurd

Er is geen SIT-1-data opgehaald, geen code geschreven en geen run uitgevoerd.
De stapregels in de registratie ("Data opgehaald", "SIT-1A development", enz.)
waren een vooraf leeg uitkomstlog. Er is dus geen INVALID/SPENT-situatie: de
registratie ging aantoonbaar vooraf aan elke dataverwerking.

## Herstel

Append-only, niets uit de geschiedenis verwijderd:
- de tekst is letterlijk verplaatst naar docs/PREREG_SIT1_20260926.md;
- het register kreeg twee NIEUWE indexregels: T002 (SIT-1A) en T003 (SIT-1B);
- beide commits staan in docs/REGISTER_EXCEPTIONS.json als gedocumenteerde,
  toegestane schending. Ze worden bij ELKE verificatie opnieuw gemeld.

fa7eb64 blijft het bewijs van het registratietijdstip. De herstelcommit zelf
verwijdert regels uit het register en staat daarom ook in de uitzonderingen.

## Wijziging aan het instrument

1. Uitzonderingenmechanisme toegevoegd: een verwijdering is alleen toegestaan als
   de commit met reden in REGISTER_EXCEPTIONS.json staat. Een niet-vermelde
   verwijdering laat de verificatie falen zoals voorheen (getoetst).
2. commit_times gebruikt niet langer --follow. De regel "een pre-registratie
   wijzigt niet na registratie" gaat over dat bestand, niet over de herkomst van
   de tekst. Met --follow kreeg het verplaatste document de hele geschiedenis van
   het register toegekend en zou een correcte verhuizing als wijziging gelden.

## Openstaand (beleid, niet hier ingevuld)

- SIT-1 hoort in familie SIZING_CREDIT_INFO met een eigen multiplicity-regel.
  Of alle families daarnaast een globale alpha_round delen, is nog open.
- SIT-1 gebruikt andere kostenaannames (0,10%/zijde hypothetical turnover
  penalty op Mkt-RF; 0,04%/zijde voor de SPY-check) dan cost model v1 (Bitvavo,
  0,25%/zijde). Dat vraagt een APART versioned kostenmodel voor aandelen voor
  SIT-1B draait, geen uitzondering binnen v1.
