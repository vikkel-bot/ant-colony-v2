"""
scripts/reset_paper_state.py

Eenmalig cleanup script: verplaatst alle ANT_LOGS/paper/*.jsonl bestanden
naar een timestamped archief-map zodat PaperAnt schoon herstart zonder
zombie-posities uit oude logs.

Gebruik (op PC2):
    python scripts/reset_paper_state.py
    python scripts/reset_paper_state.py --logs-root C:\\Trading\\ANT_LOGS

Veiligheidsregels:
  - Toont wat er verplaatst wordt vóór uitvoering
  - Vereist expliciete bevestiging (y/N)
  - BEWEEGT bestanden — originals zijn altijd terug te vinden in het archief
  - Gooit nooit stille fouten — elke actie wordt gelogd naar stdout
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


_DEFAULT_LOGS_ROOT = Path(r"C:\Trading\ANT_LOGS")


def _archive_paper_logs(logs_root: Path) -> int:
    """
    Verplaats alle *.jsonl bestanden uit logs_root/paper/ naar
    logs_root/paper_archive/paper_YYYYMMDD_HHMMSS/.

    Retourneert het aantal verplaatste bestanden.
    """
    paper_dir = logs_root / "paper"

    if not paper_dir.exists():
        print(f"[INFO] {paper_dir} bestaat niet — niets te archiveren.")
        return 0

    jsonl_files = sorted(paper_dir.glob("*.jsonl"))
    if not jsonl_files:
        print(f"[INFO] Geen *.jsonl bestanden gevonden in {paper_dir}.")
        return 0

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = logs_root / "paper_archive" / f"paper_{ts}"

    print(f"\nDe volgende {len(jsonl_files)} bestanden worden verplaatst naar:")
    print(f"  {archive_dir}\n")
    for f in jsonl_files:
        print(f"  {f.name}")

    print()
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
    for src in jsonl_files:
        dst = archive_dir / src.name
        shutil.move(str(src), str(dst))
        print(f"[MOVED] {src.name} → {dst}")
        moved += 1

    print(f"\n[KLAAR] {moved} bestand(en) gearchiveerd in {archive_dir}")
    print("[INFO] PaperAnt herstart nu zonder open posities.")
    return moved


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
    args = parser.parse_args()

    logs_root: Path = args.logs_root
    if not logs_root.exists():
        print(f"[FOUT] logs_root bestaat niet: {logs_root}")
        sys.exit(1)

    _archive_paper_logs(logs_root)


if __name__ == "__main__":
    main()
