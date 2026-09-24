"""Atomair schrijven en robuust lezen van kleine statusbestanden.

Aanleiding (24-09-2026): `Path.write_text()` kapt het doelbestand eerst af.
Een gelijktijdige lezer kan daardoor een leeg bestand zien. Dat brak CI met een
JSONDecodeError op een lege string, terwijl het lokaal jarenlang goed leek te
gaan — een timingafhankelijkheid, geen toeval.

De oplossing is schrijven naar een tijdelijk bestand en dat hernoemen. Op
Windows kost dat een tweede probleem: zolang een lezer het doelbestand open
heeft, weigert `os.replace` (WinError 5), en omgekeerd kan een lezer tijdens het
hernoemen een PermissionError krijgen. Beide kanten doen daarom een korte
herhaalpoging.

Waarom dit beter is dan de oude situatie: een lezer krijgt nooit meer halve of
lege inhoud die als geldig wordt behandeld. Wat overblijft is een kortstondige,
zichtbare fout die met een herhaalpoging verdwijnt.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_ATTEMPTS = 50
DEFAULT_DELAY = 0.005


def atomic_write_text(path: Path, text: str, attempts: int = DEFAULT_ATTEMPTS,
                      delay: float = DEFAULT_DELAY) -> None:
    """Schrijf `text` naar `path` zonder dat een lezer ooit halve inhoud ziet.

    Lukt het hernoemen na alle pogingen niet, dan wordt het tijdelijke bestand
    opgeruimd en de fout doorgegeven: stil verlies van een update is erger dan
    een zichtbare fout.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def read_text_with_retry(path: Path, attempts: int = DEFAULT_ATTEMPTS,
                         delay: float = DEFAULT_DELAY) -> str:
    """Lees `path`, met herhaalpoging bij een tijdelijke PermissionError.

    Op Windows kan het openen mislukken terwijl een schrijver het bestand
    vervangt. Zonder herhaling zou een lezer dan ten onrechte terugvallen op
    lege of standaardwaarden.
    """
    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    raise AssertionError("onbereikbaar")
