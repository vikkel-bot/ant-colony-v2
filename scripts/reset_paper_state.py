"""
scripts/reset_paper_state.py

Verplaatst ALLE state in ANT_LOGS/paper/ naar een timestamped archief-map
zodat PaperAnt schoon herstart zonder zombie-posities uit oude logs.

Gebruik (op PC2):
    python scripts/reset_paper_state.py
    python scripts/reset_paper_state.py --logs-root C:\\Trading\\ANT_LOGS
    python scripts/reset_paper_state.py --dry-run    # toon wat er verplaatst
                                                      # wordt zonder te doen
    python scripts/reset_paper_state.py --reset-queen-log  # archiveer ook
                                                            # ANT_LOGS/queen/

Veiligheidsregels:
  - Toont wat er verplaatst wordt vóór uitvoering
  - Vereist expliciete bevestiging (y/N) — tenzij --yes opgegeven
  - BEWEEGT bestanden — originals zijn altijd terug te vinden in het archief
  - Gooit nooit stille fouten — elke actie wordt gelogd naar stdout
  - Recursive glob (rglob) zodat subdirectories niet gemist worden

State-inventaris (wat dit script reset en wat niet):
  ✅ ANT_LOGS/paper/           — gearchiveerd (scout + research positie logs)
  ⚙️ ANT_LOGS/queen/          — optioneel met --reset-queen-log (zie opmerking)
  ❌ ANT_LOGS/research/        — NIET gereset (ResearchAnt output, read-only)
  ❌ ANT_LOGS/approved/        — NIET gereset (Queen-approved kandidaten)
  ❌ ANT_LIVE/                 — NIET gereset (broker artifacts, geen paper state)
  ❌ PaperLedger               — in-memory, geen disk state

Opmerking Queen stats:
  Queen-beslissingen (deprioriteer, kapitaal_verlagen) zijn PUUR IN-MEMORY.
  Ze worden elke 5 minuten opnieuw berekend vanuit paper-trade statistieken.
  Na een paper reset + herstart zijn alle Queen-beslissingen automatisch schoon.
  --reset-queen-log archiveert alleen het audit-log (decisions.jsonl) maar
  heeft GEEN effect op in-memory beslissingen — herstart is altijd genoeg.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


_DEFAULT_LOGS_ROOT = Path(r"C:\Trading\ANT_LOGS")


def _collect_paper_files(paper_dir: Path) -> list[Path]:
    """Geef alle bestanden recursief onder paper_dir terug (gesorteerd)."""
    return sorted(f for f in paper_dir.rglob("*") if f.is_file())


def _archive_paper_logs(
    logs_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """
    Verplaats alle bestanden uit logs_root/paper/ (recursief) naar
    logs_root/paper_archive/paper_YYYYMMDD_HHMMSS/.

    Retourneert het aantal verplaatste bestanden.
    """
    paper_dir = logs_root / "paper"

    if not paper_dir.exists():
        print(f"[INFO] {paper_dir} bestaat niet — niets te archiveren.")
        _print_state_inventory(logs_root)
        return 0

    all_files = _collect_paper_files(paper_dir)
    if not all_files:
        print(f"[INFO] Geen bestanden gevonden in {paper_dir} (inclusief subdirectories).")
        _print_state_inventory(logs_root)
        return 0

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = logs_root / "paper_archive" / f"paper_{ts}"

    print(f"\nDe volgende {len(all_files)} bestand(en) worden verplaatst naar:")
    print(f"  {archive_dir}\n")
    for f in all_files:
        rel = f.relative_to(paper_dir)
        print(f"  {rel}")

    _print_state_inventory(logs_root)

    if dry_run:
        print("\n[DRY-RUN] Geen wijzigingen aangebracht.")
        return 0

    print()
    if not yes:
        try:
            antwoord = input("Doorgaan? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[AFGEBROKEN] Geen wijzigingen aangebracht.")
            return 0
        if antwoord != "y":
            print("[AFGEBROKEN] Geen wijzigingen aangebracht.")
            return 0

    archive_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    for src in all_files:
        rel = src.relative_to(paper_dir)
        dst = archive_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"[MOVED] {rel} → {dst}")
        moved += 1

    print(f"\n[KLAAR] {moved} bestand(en) gearchiveerd in {archive_dir}")
    print("[INFO] PaperAnt herstart nu zonder open posities.")
    print("[INFO] Verwachte startlog: '0 scout positie(s), 0 research positie(s) hersteld'")
    return moved


def _archive_queen_log(
    logs_root: Path,
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """
    Archiveer ANT_LOGS/queen/ naar een timestamped archief-map.

    Opmerking: Queen-beslissingen zijn in-memory en worden elke 5 minuten
    herberekend. Dit archiveert alleen het audit-log — herstart is altijd
    voldoende om deprioriteer-beslissingen te wissen.
    """
    queen_dir = logs_root / "queen"
    if not queen_dir.exists():
        print(f"[INFO] {queen_dir} bestaat niet — niets te archiveren.")
        return 0

    all_files = sorted(f for f in queen_dir.rglob("*") if f.is_file())
    if not all_files:
        print(f"[INFO] Geen bestanden gevonden in {queen_dir}.")
        return 0

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = logs_root / "queen_archive" / f"queen_{ts}"

    print(f"\nDe volgende {len(all_files)} Queen-log bestand(en) worden verplaatst naar:")
    print(f"  {archive_dir}\n")
    for f in all_files:
        print(f"  {f.relative_to(queen_dir)}")
    print(
        "\n  [OPMERKING] Queen-beslissingen zijn in-memory en worden automatisch"
        " herberekend na herstart. Dit archiveert alleen het audit-log."
    )

    if dry_run:
        print("\n[DRY-RUN] Geen wijzigingen aangebracht.")
        return 0

    print()
    if not yes:
        try:
            antwoord = input("Queen-log archiveren? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[AFGEBROKEN] Geen wijzigingen aangebracht.")
            return 0
        if antwoord != "y":
            print("[AFGEBROKEN] Geen wijzigingen aangebracht.")
            return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in all_files:
        rel = src.relative_to(queen_dir)
        dst = archive_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"[MOVED] queen/{rel} → {dst}")
        moved += 1

    print(f"\n[KLAAR] {moved} Queen-log bestand(en) gearchiveerd in {archive_dir}")
    return moved


def _print_state_inventory(logs_root: Path) -> None:
    """Print een overzicht van wat gereset wordt en wat niet."""
    print()
    print("State-inventaris:")
    paper_dir    = logs_root / "paper"
    queen_dir    = logs_root / "queen"
    research_dir = logs_root / "research"
    approved_dir = logs_root / "approved"
    live_dir     = logs_root.parent / "ANT_LIVE"

    def _status(path: Path) -> str:
        if not path.exists():
            return "(bestaat niet)"
        n = sum(1 for _ in path.rglob("*") if _.is_file())
        return f"({n} bestand(en))"

    print(f"  ✅ ANT_LOGS/paper/     {_status(paper_dir)} — wordt gearchiveerd")
    print(f"  ⚙️  ANT_LOGS/queen/     {_status(queen_dir)} — optioneel met --reset-queen-log (audit-log, in-memory state)")
    print(f"  ❌ ANT_LOGS/research/  {_status(research_dir)} — NIET gereset (read-only voor PaperAnt)")
    print(f"  ❌ ANT_LOGS/approved/  {_status(approved_dir)} — NIET gereset (Queen-approved)")
    print(f"  ❌ ANT_LIVE/           {_status(live_dir)} — NIET gereset (broker artifacts, geen paper state)")
    print(f"  ❌ PaperLedger         — in-memory, geen disk state")
    print(f"  ❌ Queen beslissingen  — in-memory, herberekend na herstart (herstart = schoon)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset PaperAnt state door paper logs te archiveren."
    )
    parser.add_argument(
        "--logs-root",
        type=Path,
        default=_DEFAULT_LOGS_ROOT,
        help=f"Pad naar ANT_LOGS (default: {_DEFAULT_LOGS_ROOT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Toon wat er verplaatst zou worden zonder te doen.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Sla de bevestigingsvraag over (voor scripts).",
    )
    parser.add_argument(
        "--reset-queen-log",
        action="store_true",
        help=(
            "Archiveer ook ANT_LOGS/queen/ (alleen audit-log). "
            "Queen-beslissingen zijn in-memory en worden automatisch "
            "herberekend na herstart — dit is optioneel."
        ),
    )
    args = parser.parse_args()

    logs_root: Path = args.logs_root
    if not logs_root.exists():
        print(f"[FOUT] logs_root bestaat niet: {logs_root}")
        sys.exit(1)

    _archive_paper_logs(logs_root, dry_run=args.dry_run, yes=args.yes)

    if args.reset_queen_log:
        _archive_queen_log(logs_root, dry_run=args.dry_run, yes=args.yes)


if __name__ == "__main__":
    main()
