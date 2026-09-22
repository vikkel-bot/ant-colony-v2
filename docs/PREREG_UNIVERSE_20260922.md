# Pre-registratie — universum-drempel cross-sectioneel crypto

Vastgelegd vóór het lezen van de uitvoer van scripts/universe_census.py.

- Minimale breedte N = 40 munten in U(t)
- Bovenste fractie q = 1/5, dus k = 8 munten in de geselecteerde groep
- Keuzeregel omzetdrempel: hoogste kandidaat (0, 10k, 50k, 100k, 250k, 1M EUR
  mediane 30-daagse dagomzet) waarbij U(t) >= 40 in >= 90% van de maandmomenten
- Testperiode: laatste 48 maanden tot de referentiedatum van de census
- Powergrens: kleinst detecteerbaar effect <= 0,75% per week
  (sigma geschat uit census; formule 3,2 x sigma x sqrt(1/k - 1/N) / sqrt(T))
- Bij falen: haalt drempel 0 de 40 niet in 90% van de maanden, dan is de gate
  FAIL bij N = 40. N wordt niet verlaagd om alsnog te slagen.
- De drempelkeuze gebruikt geen rendementen.
