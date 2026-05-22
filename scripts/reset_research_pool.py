"""
Reset de actieve ResearchAnt-pool door historische research-records te archiveren.

Sessie A - Pool Reset (mei 2026)

Doel:
  - ANT_LOGS/research/*.jsonl bevat alleen actuele candidate_accepted records.
  - Records van voor 2026-05-21T00:00:00Z gaan naar ANT_LOGS/archive/.
  - Het archief bevat een manifest en is herstelbaar met --restore.

Gebruik op PC2:
    python scripts/reset_research_pool.py --yes
    python scripts/reset_research_pool.py --dry-run
    python scripts/reset_research_pool.py --restore C:\\Trading\\ANT_LOGS\\archive\\research_pool_reset_...

Veiligheidsregels:
  - Alleen ANT_LOGS/research/*.jsonl wordt opgeschoond.
  - Records zonder parsebare timestamp blijven actief (fail-closed tegen dataverlies).
  - Het script is idempotent: een tweede run verplaatst niets nieuws.
  - Restore voegt alleen ontbrekende archiefregels terug toe, geen duplicaten.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LOGS_ROOT = Path(r"C:\Trading\ANT_LOGS")
DEFAULT_CUTOFF = datetime(2026, 5, 21, tzinfo=timezone.utc)
_TIMESTAMP_KEYS = (
    "timestamp",
    "created_at",
    "generated_at",
    "detected_at",
    "_accepted_at",
    "accepted_at",
)


@dataclass(frozen=True)
class ResetResearchPoolResult:
    scanned_files: int
    archived_files: int
    scanned_records: int
    archived_records: int
    current_records: int
    skipped_invalid_records: int
    archive_dir: Path | None


@dataclass(frozen=True)
class RestoreResearchPoolResult:
    archive_files: int
    restored_records: int
    skipped_duplicates: int


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    payload = record.get("payload") or {}
    for key in _TIMESTAMP_KEYS:
        ts = _parse_ts(record.get(key))
        if ts is not None:
            return ts
    if isinstance(payload, dict):
        for key in _TIMESTAMP_KEYS:
            ts = _parse_ts(payload.get(key))
            if ts is not None:
                return ts
    return None


def _is_research_candidate(record: dict[str, Any]) -> bool:
    payload = record.get("payload") or {}
    if isinstance(payload, dict) and payload.get("action") == "candidate_accepted":
        return True
    return record.get("action") == "candidate_accepted"


def _collect_research_files(logs_root: Path) -> list[Path]:
    research_dir = logs_root / "research"
    if not research_dir.exists():
        return []
    return sorted(path for path in research_dir.glob("*.jsonl") if path.is_file())


def _unique_archive_dir(logs_root: Path) -> Path:
    base = logs_root / "archive"
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    candidate = base / f"research_pool_reset_{ts}"
    suffix = 1
    while candidate.exists():
        candidate = base / f"research_pool_reset_{ts}_{suffix}"
        suffix += 1
    return candidate


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n[AFGEBROKEN] Geen wijzigingen aangebracht.")
        return False
    return answer == "y"


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _split_research_file(path: Path, cutoff: datetime) -> tuple[list[str], list[str], int, int]:
    archived: list[str] = []
    current: list[str] = []
    scanned_records = 0
    invalid_records = 0

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            current.append(line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid_records += 1
            current.append(line)
            continue

        scanned_records += 1
        if not _is_research_candidate(record):
            current.append(line)
            continue

        ts = _record_timestamp(record)
        if ts is not None and ts < cutoff:
            archived.append(line)
        else:
            current.append(line)

    return archived, current, scanned_records, invalid_records


def reset_research_pool(
    logs_root: Path,
    *,
    cutoff: datetime = DEFAULT_CUTOFF,
    dry_run: bool = False,
    yes: bool = False,
) -> ResetResearchPoolResult:
    """
    Archiveer candidate_accepted records ouder dan cutoff uit ANT_LOGS/research.

    Records zonder timestamp blijven actief. Dit is bewust fail-closed: liever een
    verdachte kandidaat laten staan dan actuele data per ongeluk archiveren.
    """
    logs_root = Path(logs_root)
    paths = _collect_research_files(logs_root)
    if not paths:
        print(f"[INFO] Geen research JSONL-bestanden gevonden in {logs_root / 'research'}.")
        return ResetResearchPoolResult(0, 0, 0, 0, 0, 0, None)

    split_by_path: dict[Path, tuple[list[str], list[str], int, int]] = {}
    scanned_records = 0
    archived_records = 0
    current_records = 0
    invalid_records = 0

    for path in paths:
        archived, current, scanned, invalid = _split_research_file(path, cutoff)
        split_by_path[path] = (archived, current, scanned, invalid)
        scanned_records += scanned
        archived_records += len(archived)
        current_records += len(current)
        invalid_records += invalid

    print("Research pool reset")
    print(f"  logs_root:          {logs_root}")
    print(f"  cutoff:             {cutoff.isoformat()}")
    print(f"  bestanden gescand:  {len(paths)}")
    print(f"  records gescand:    {scanned_records}")
    print(f"  te archiveren:      {archived_records}")
    print(f"  actief behouden:    {current_records}")
    if invalid_records:
        print(f"  invalid behouden:   {invalid_records}")

    if archived_records == 0:
        print("[INFO] Geen historische research-records gevonden. Niets te doen.")
        return ResetResearchPoolResult(
            len(paths),
            0,
            scanned_records,
            0,
            current_records,
            invalid_records,
            None,
        )

    archive_dir = _unique_archive_dir(logs_root)
    print(f"  archief:            {archive_dir}")

    if dry_run:
        print("[DRY-RUN] Geen wijzigingen aangebracht.")
        return ResetResearchPoolResult(
            len(paths),
            sum(1 for archived, *_ in split_by_path.values() if archived),
            scanned_records,
            archived_records,
            current_records,
            invalid_records,
            archive_dir,
        )

    if not _confirm("Historische research-records archiveren?", yes=yes):
        return ResetResearchPoolResult(
            len(paths),
            0,
            scanned_records,
            0,
            current_records,
            invalid_records,
            None,
        )

    manifest_entries: list[dict[str, Any]] = []
    archived_files = 0
    research_dir = logs_root / "research"

    for src, (archived, current, _scanned, _invalid) in split_by_path.items():
        if not archived:
            continue
        rel = src.relative_to(research_dir)
        archive_path = archive_dir / "research" / rel
        _write_lines(archive_path, archived)
        _write_lines(src, current)
        archived_files += 1
        manifest_entries.append(
            {
                "source_rel": str(Path("research") / rel),
                "archive_rel": str(Path("research") / rel),
                "archived_records": len(archived),
                "current_records": len(current),
            }
        )
        print(f"[ARCHIVED] research/{rel} -> {archive_path}")

    manifest = {
        "version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "cutoff": cutoff.isoformat(),
        "logs_root": str(logs_root),
        "scanned_files": len(paths),
        "archived_files": archived_files,
        "archived_records": archived_records,
        "current_records": current_records,
        "entries": manifest_entries,
        "restore_command": f"python scripts/reset_research_pool.py --restore {archive_dir}",
    }
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"[KLAAR] {archived_records} record(s) gearchiveerd in {archive_dir}")
    print("[INFO] Restore kan met --restore en hetzelfde archiefpad.")
    return ResetResearchPoolResult(
        len(paths),
        archived_files,
        scanned_records,
        archived_records,
        current_records,
        invalid_records,
        archive_dir,
    )


def restore_research_pool(
    logs_root: Path,
    archive_dir: Path,
    *,
    yes: bool = False,
    dry_run: bool = False,
) -> RestoreResearchPoolResult:
    """
    Zet een research_pool_reset archief terug in ANT_LOGS/research.

    Restore is exact-line deduped: regels die al in het actieve bestand staan
    worden niet opnieuw toegevoegd.
    """
    logs_root = Path(logs_root)
    archive_dir = Path(archive_dir)
    manifest_path = archive_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Geen manifest gevonden: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError(f"Ongeldig manifest: entries ontbreekt in {manifest_path}")

    if not entries:
        print("[INFO] Manifest bevat geen te herstellen bestanden.")
        return RestoreResearchPoolResult(0, 0, 0)

    print("Research pool restore")
    print(f"  logs_root: {logs_root}")
    print(f"  archief:   {archive_dir}")
    print(f"  bestanden: {len(entries)}")

    if dry_run:
        restored = 0
        duplicates = 0
        for entry in entries:
            archive_path = archive_dir / str(entry.get("archive_rel") or "")
            if not archive_path.exists():
                continue
            target_path = logs_root / str(entry.get("source_rel") or "")
            current_lines: set[str] = set()
            if target_path.exists():
                current_lines = set(target_path.read_text(encoding="utf-8").splitlines())
            for line in archive_path.read_text(encoding="utf-8").splitlines():
                if line in current_lines:
                    duplicates += 1
                else:
                    restored += 1
        print(f"[DRY-RUN] Zou {restored} record(s) herstellen, {duplicates} duplicaat/duplicaten overslaan.")
        return RestoreResearchPoolResult(len(entries), restored, duplicates)

    if not _confirm("Research-archief terugzetten?", yes=yes):
        return RestoreResearchPoolResult(len(entries), 0, 0)

    restored_records = 0
    skipped_duplicates = 0

    for entry in entries:
        archive_rel = str(entry.get("archive_rel") or "")
        source_rel = str(entry.get("source_rel") or "")
        if not archive_rel or not source_rel:
            continue
        archive_path = archive_dir / archive_rel
        target_path = logs_root / source_rel
        if not archive_path.exists():
            print(f"[WARN] Archiefbestand ontbreekt: {archive_path}")
            continue

        archive_lines = [
            line
            for line in archive_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        current_lines = []
        if target_path.exists():
            current_lines = target_path.read_text(encoding="utf-8").splitlines()
        seen = set(current_lines)
        new_lines = []
        for line in archive_lines:
            if line in seen:
                skipped_duplicates += 1
                continue
            new_lines.append(line)
            seen.add(line)

        if new_lines:
            with target_path.open("a", encoding="utf-8") as fh:
                for line in new_lines:
                    fh.write(line + "\n")
            restored_records += len(new_lines)
            print(f"[RESTORED] {len(new_lines)} record(s) -> {target_path}")

    print(
        f"[KLAAR] {restored_records} record(s) hersteld; "
        f"{skipped_duplicates} duplicaat/duplicaten overgeslagen."
    )
    return RestoreResearchPoolResult(len(entries), restored_records, skipped_duplicates)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archiveer historische ResearchAnt candidate_accepted records."
    )
    parser.add_argument(
        "--logs-root",
        type=Path,
        default=DEFAULT_LOGS_ROOT,
        help=f"Pad naar ANT_LOGS (default: {DEFAULT_LOGS_ROOT})",
    )
    parser.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF.isoformat(),
        help="Archiveer records met timestamp voor deze ISO-tijd.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Toon zonder te schrijven.")
    parser.add_argument("--yes", action="store_true", help="Sla bevestigingsvraag over.")
    parser.add_argument(
        "--restore",
        type=Path,
        default=None,
        help="Herstel een eerder research_pool_reset archief.",
    )
    args = parser.parse_args()

    cutoff = _parse_ts(args.cutoff)
    if cutoff is None:
        print(f"[FOUT] Ongeldige cutoff: {args.cutoff}")
        sys.exit(2)

    logs_root = Path(args.logs_root)
    if args.restore is not None:
        restore_research_pool(
            logs_root,
            args.restore,
            yes=args.yes,
            dry_run=args.dry_run,
        )
        return

    reset_research_pool(
        logs_root,
        cutoff=cutoff,
        dry_run=args.dry_run,
        yes=args.yes,
    )


if __name__ == "__main__":
    main()
