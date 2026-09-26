# Pre-test screening: turnover en minimale bruto edge

Vastgelegd 24-09-2026. Dit is een eligibility/screening-eigenschap, GEEN
edge-bewijs en geen reden om rendementen te bekijken.

Regel: voor iedere kandidaat-sensor wordt VOOR preregistratie, zonder enig
rendement te bekijken, gemeten:
  - de verwachte omloop van de bovenste groep per herbalancering
    (rank stability van de sensor binnen U(t));
  - de daaruit volgende kosten per week met het GELDENDE cost model
    (nu COST_MODEL_V1_20260924, estimator p75, referentieschaal EUR 5.000);
  - de minimale bruto edge die economisch nodig zou zijn.

Waarom dit mag zonder de holdout of rendementen te raken: omloop volgt
uitsluitend uit de rangschikking, niet uit uitkomsten — net als sigma bij de
powercontrole.

Stand bij COST_MODEL_V1 en omloop 44,25%/week (gemeten bij T001), order EUR 5.000:

| kwartiel | kosten/week | bruto nodig (kostenhorde x3) |
|---|---|---|
| Q1 (50k-77k) | 0,444% | 1,33%/week |
| Q2 (78k-148k) | 0,414% | 1,24%/week |
| Q3 (149k-379k) | 0,394% | 1,18%/week |
| Q4 (383k+) | 0,306% | 0,92%/week |

Ter vergelijking: delta_min op de ontwikkelperiode is circa 0,62%/week. De
kostenhorde ligt dus in elk kwartiel BOVEN de detectiegrens. Een effect dat net
detecteerbaar is, is bij deze omloop per definitie niet verhandelbaar.

NOG GEEN HARDE GRENS. Er wordt bewust geen turnoverdrempel vastgelegd voordat
bepaald is hoe deze screen formeel in de Research Constitution past: als
weigeringsgrond vooraf, als verplichte rapportage bij preregistratie, of als
onderdeel van de Evidence Gate. Die keuze is POLICY en volgt later.
