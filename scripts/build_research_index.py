"""Genereert docs/RESEARCH_INDEX.md uit de bronbestanden.

De index wordt nooit met de hand bijgewerkt. Hij wordt gegenereerd uit:
  - docs/TOETSREGISTER.md      (append-only toetsregister)
  - docs/GATE_*.md             (stage-gates)
  - docs/COST_MODEL_*.json     (kostenmodelversies)
  - docs/RESEARCH_OPEN_ITEMS.md (met de hand bijgehouden: open keuzes en parkeerlijst)
  - de git-historie            (datum van eerste commit per document)

Een test vergelijkt de gegenereerde inhoud met het gecommitte bestand. Loopt de
index achter, dan faalt CI. Zo kan het overzicht niet stilletjes verouderen.

Gebruik:
    py -3.14 scripts/build_research_index.py            # schrijft docs/RESEARCH_INDEX.md
    py -3.14 scripts/build_research_index.py --check    # faalt als de index verouderd is
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
INDEX = DOCS / "RESEARCH_INDEX.md"
OPEN_ITEMS = DOCS / "RESEARCH_OPEN_ITEMS.md"
REGISTER = DOCS / "TOETSREGISTER.md"

HEADER = """<!-- GEGENEREERD door scripts/build_research_index.py — niet met de hand bewerken.
     Open keuzes en parkeerlijst staan in docs/RESEARCH_OPEN_ITEMS.md. -->

# Onderzoeksindex — Colony V2

Eén pagina met de actuele stand. Gegenereerd uit het toetsregister, de
gate-documenten, de kostenmodellen en de git-historie.

## De drie maatstaven (niet door elkaar halen)

| # | maatstaf | beantwoordt | waartegen |
|---|---|---|---|
| 1 | nulmodel | bevat de sensor informatie? | verdeling van toeval op dezelfde data |
| 2 | kostenhorde | is het verhandelbaar? | gemeten kosten x factor 3 |
| 3 | allocatiebenchmark | verdient dit kapitaal? | nog niet vastgelegd; pas relevant na 1 en 2 |

Maatstaf 3 is nog nooit gebruikt: er is nog niets door 1 en 2 gekomen.
"""


def git_date(rel: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), "log", "--diff-filter=A", "--format=%cs", "--", rel],
                         capture_output=True, text=True)
    lines = [x for x in out.stdout.splitlines() if x.strip()]
    return lines[-1] if lines else "niet gecommit"


def first_heading(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def verdict_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.upper().startswith("VERDICT"):
            return line.split(":", 1)[-1].strip()
    return "-"


def register_rows() -> list[list[str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0].lower() == "id" or set(cells[0]) <= set("-: "):
            continue
        rows.append(cells[:5])
    return rows


def tests_section() -> str:
    rows = register_rows()
    out = ["## Toetsen", "",
           "Bron: docs/TOETSREGISTER.md (append-only). Elke regel is een aparte registratie;",
           "de laatste regel per id is de huidige status.", "",
           "| id | familie | status | uitkomst | pre-registratie | resultaat |",
           "|---|---|---|---|---|---|"]
    for test_id, family, prereg, status, result in rows:
        res_path = DOCS / result
        verdict = verdict_of(res_path) if result not in {"-", ""} and res_path.exists() else "-"
        res_cell = f"[{result}]({result})" if result not in {"-", ""} else "-"
        out.append(f"| {test_id} | {family} | **{status}** | {verdict} | "
                   f"[{prereg}]({prereg}) ({git_date('docs/' + prereg)}) | {res_cell} |")
    return "\n".join(out) + "\n"


def gates_section() -> str:
    out = ["## Stage-gates", "", "| document | titel | eerste commit |", "|---|---|---|"]
    for p in sorted(DOCS.glob("GATE_*.md")):
        out.append(f"| [{p.name}]({p.name}) | {first_heading(p)} | {git_date('docs/' + p.name)} |")
    return "\n".join(out) + "\n"


def cost_models_section() -> str:
    out = ["## Kostenmodellen", "",
           "Een versie wordt nooit overschreven. Een nieuwe meting levert een nieuwe versie op;",
           "reeds geregistreerde toetsen blijven verwijzen naar de versie die toen gold.", "",
           "| versie | gate-estimator | tarief/kant | referentieschaal | snapshots | markten |",
           "|---|---|---|---|---|---|"]
    for p in sorted(DOCS.glob("COST_MODEL_*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        cf = m.get("created_from", {})
        out.append(f"| [{m['version']}]({p.name}) | {m['estimator_for_gate']} | "
                   f"{m['fee_per_side']:.4f} | EUR {m.get('reference_notional_eur')} | "
                   f"{cf.get('snapshots')} | {cf.get('markets')} |")
    return "\n".join(out) + "\n"


def measurements_section() -> str:
    out = ["## Metingen en rapporten", "", "| document | titel | eerste commit |", "|---|---|---|"]
    patterns = ("METING_*.md", "COST_CENSUS_*.md", "SCREENING_*.md", "OPEN_*.md", "PREREG_*.md")
    seen: set[str] = set()
    for pat in patterns:
        for p in sorted(DOCS.glob(pat)):
            if p.name in seen:
                continue
            seen.add(p.name)
            out.append(f"| [{p.name}]({p.name}) | {first_heading(p)} | {git_date('docs/' + p.name)} |")
    return "\n".join(out) + "\n"


def open_items_section() -> str:
    if not OPEN_ITEMS.exists():
        return "## Open keuzes en parkeerlijst\n\n(docs/RESEARCH_OPEN_ITEMS.md ontbreekt)\n"
    body = OPEN_ITEMS.read_text(encoding="utf-8")
    body = re.sub(r"^#\s.*\n", "", body, count=1).strip()
    return "## Open keuzes en parkeerlijst\n\nBron: docs/RESEARCH_OPEN_ITEMS.md\n\n" + body + "\n"


def build() -> str:
    parts = [HEADER, tests_section(), gates_section(), cost_models_section(),
             measurements_section(), open_items_section()]
    return "\n".join(p.rstrip() + "\n" for p in parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="faal als de index verouderd is")
    args = ap.parse_args()
    content = build()
    if args.check:
        if not INDEX.exists():
            print("RESEARCH_INDEX.md ontbreekt")
            return 1
        if INDEX.read_text(encoding="utf-8") != content:
            print("RESEARCH_INDEX.md is verouderd — draai scripts/build_research_index.py")
            return 1
        print("RESEARCH_INDEX.md is actueel")
        return 0
    INDEX.write_text(content, encoding="utf-8")
    print(f"geschreven: {INDEX} ({len(content.splitlines())} regels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
