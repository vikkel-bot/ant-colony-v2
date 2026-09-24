"""Verifieert het toetsregister tegen de git-geschiedenis. Fail-closed.

Controleert per geregistreerde toets:
  1. het PREREG-bestand bestaat en is getrackt;
  2. het resultaatbestand bestaat en is getrackt (als de regel er een noemt);
  3. de pre-registratie is gecommit VOOR het resultaat;
  4. de pre-registratie is na registratie niet meer gewijzigd;
  5. het register zelf is append-only (geen commit verwijdert of wijzigt regels);
  6. elke status komt uit de toegestane verzameling.

Geen oordeel over statistiek of bewijskracht: alleen of de volgorde en de
onveranderlijkheid kloppen. Exitcode 1 bij elke schending.

Gebruik:
    py -3.14 scripts/verify_research_log.py [--register docs/TOETSREGISTER.md] [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_STATUS = {
    "PENDING", "PASS", "FAIL", "INCONCLUSIVE",
    "INFORMATIEF_NIET_VERHANDELBAAR", "VOID", "INVALID",
}
NO_VALUE = {"-", "—", "", "n.v.t."}


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


def tracked(repo: Path, rel: str) -> bool:
    return bool(git(repo, "ls-files", "--", rel))


def commit_times(repo: Path, rel: str) -> list[int]:
    """Commit-tijden (unix, oud -> nieuw) van elke commit die dit pad raakt."""
    out = git(repo, "log", "--follow", "--format=%ct", "--", rel)
    return sorted(int(x) for x in out.splitlines() if x.strip())


def register_deletions(repo: Path, rel: str) -> list[str]:
    """Commits die regels uit het register verwijderen of wijzigen."""
    bad = []
    for sha in git(repo, "log", "--format=%H", "--", rel).splitlines():
        stat = git(repo, "show", "--numstat", "--format=", sha, "--", rel)
        for line in stat.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) > 0:
                bad.append(f"{sha[:7]} verwijdert {parts[1]} regel(s)")
    return bad


@dataclass
class Row:
    test_id: str
    family: str
    prereg: str
    status: str
    result: str


@dataclass
class Finding:
    ok: bool = True
    violations: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.violations.append(msg)


def parse_register(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0].lower() in {"id", ""} or set(cells[0]) <= set("-: "):
            continue
        rows.append(Row(*cells[:5]))
    return rows


def verify(repo: Path, register_rel: str) -> Finding:
    f = Finding()
    reg_path = repo / register_rel
    if not reg_path.exists():
        f.fail(f"register ontbreekt: {register_rel}")
        return f
    if not tracked(repo, register_rel):
        f.fail(f"register niet getrackt in git: {register_rel}")
        return f

    for msg in register_deletions(repo, register_rel):
        f.fail(f"register niet append-only: {msg}")

    rows = parse_register(reg_path.read_text(encoding="utf-8"))
    if not rows:
        f.fail("register bevat geen toetsregels")
        return f

    docs_dir = str(Path(register_rel).parent)
    by_id: dict[str, list[Row]] = {}
    for r in rows:
        by_id.setdefault(r.test_id, []).append(r)

    for test_id, entries in by_id.items():
        for r in entries:
            if r.status not in ALLOWED_STATUS:
                f.fail(f"{test_id}: status '{r.status}' niet toegestaan")

        prereg_rel = f"{docs_dir}/{entries[0].prereg}"
        if entries[0].prereg in NO_VALUE:
            f.fail(f"{test_id}: geen pre-registratie opgegeven")
            continue
        if not (repo / prereg_rel).exists() or not tracked(repo, prereg_rel):
            f.fail(f"{test_id}: pre-registratie ontbreekt of is niet getrackt: {prereg_rel}")
            continue

        prereg_times = commit_times(repo, prereg_rel)
        if len(prereg_times) > 1:
            f.fail(f"{test_id}: pre-registratie is na registratie gewijzigd "
                   f"({len(prereg_times)} commits op {prereg_rel})")

        results = [r.result for r in entries if r.result not in NO_VALUE]
        if not results:
            f.checked.append(f"{test_id}: geregistreerd, nog geen resultaat")
            continue

        result_rel = f"{docs_dir}/{results[-1]}"
        if not (repo / result_rel).exists() or not tracked(repo, result_rel):
            f.fail(f"{test_id}: resultaatbestand ontbreekt of is niet getrackt: {result_rel}")
            continue

        result_times = commit_times(repo, result_rel)
        if result_times[0] <= prereg_times[0]:
            f.fail(f"{test_id}: resultaat gecommit op/voor de pre-registratie "
                   f"(prereg {prereg_times[0]}, resultaat {result_times[0]})")
        else:
            f.checked.append(f"{test_id}: prereg -> resultaat volgorde correct "
                             f"({result_times[0] - prereg_times[0]}s ertussen)")
    return f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--register", default="docs/TOETSREGISTER.md")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = verify(Path(args.repo), args.register)
    if args.json:
        print(json.dumps({"ok": result.ok, "violations": result.violations, "checked": result.checked}, indent=2))
    else:
        for line in result.checked:
            print(f"  ok   : {line}")
        for line in result.violations:
            print(f"  FOUT : {line}")
        print("REGISTER OK" if result.ok else f"REGISTER NIET IN ORDE ({len(result.violations)} schending(en))")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
