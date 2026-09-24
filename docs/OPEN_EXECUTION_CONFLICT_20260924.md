# Openstaand architectuurconflict — cross-sectionele research vs paper-keten

Vastgesteld 24-09-2026 bij de controle op bestaande sizing-regels. NU NIET oplossen.

Research (ant_colony/lab/xs_rank_test.py + universe.py): selecteert per week
k(t) = max(8, N_t // 5) munten, in de testperiode 8 tot 34, gelijk gewogen.

Uitvoering (ant_colony/ants/paper_ant.py): _MAX_OPEN_POSITIONS = 10 en
_TRADE_CAPITAL_FRACTION = 0,10 (10% van beschikbaar kapitaal per trade).
Bij 22 posities zou dat 220% van het kapitaal zijn.

Gevolg: een cross-sectionele strategie is op dit moment niet uitvoerbaar in de
bestaande paper-keten, ongeacht welke sensor slaagt.

Daarnaast bestaan er twee verschillende sizing-conventies naast elkaar:
- paper-keten: vaste fractie van kapitaal per trade;
- edge_audit (AuditAssumptions): risicogebaseerd, 1% van equity met 1,5x ATR-stop.

Besluit: eerst research governance en kostenmodel afronden. Dit wordt daarna een
aparte stage-gated architectuurvraag. Het kostenmodel introduceert GEEN derde
sizing-regel: order_notional is daar invoer, en EUR 5.000 is uitsluitend een
research-referentieschaal.
