"""Misbruiktests voor scripts/verify_research_log.py.

Elke test bouwt een echte git-repo met een specifieke schending en controleert
dat de verificatie hem vangt. Een groene test zonder overtreding bewijst niets;
daarom staat hier per regel een repo die hem breekt.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import verify_research_log as V  # noqa: E402

HEADER = (
    "# Toetsregister (append-only)\n\n"
    "| id | familie | pre-registratie | status | resultaat-commit |\n"
    "|---|---|---|---|---|\n"
)
ROW = "| {id} | FAM | {prereg} | {status} | {result} |\n"


def _git(repo: Path, *args: str, when: int | None = None) -> None:
    env = None
    if when is not None:
        stamp = f"@{when} +0000"
        env = {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**subprocess.os.environ, **(env or {})})


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / "docs").mkdir(parents=True)
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: Path, rel: str, text: str, when: int, msg: str = "c") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg, when=when)


def _clean(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    _commit(repo, "docs/PREREG_T1.md", "prereg", 1_000_000)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="PENDING", result="-"),
            1_000_100)
    _commit(repo, "docs/T1_RESULT.md", "resultaat", 1_000_200)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="PENDING", result="-")
            + ROW.format(id="T1", prereg="PREREG_T1.md", status="FAIL", result="T1_RESULT.md"),
            1_000_300)
    return repo


def test_clean_history_passes(tmp_path):
    f = V.verify(_clean(tmp_path), "docs/TOETSREGISTER.md")
    assert f.ok, f.violations


def test_result_before_prereg_is_caught(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/T1_RESULT.md", "resultaat", 1_000_000)      # eerst het resultaat
    _commit(repo, "docs/PREREG_T1.md", "prereg", 1_000_100)          # daarna pas de prereg
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="FAIL", result="T1_RESULT.md"),
            1_000_200)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("voor de pre-registratie" in v for v in f.violations)


def test_prereg_modified_after_registration_is_caught(tmp_path):
    repo = _clean(tmp_path)
    _commit(repo, "docs/PREREG_T1.md", "prereg MET GUNSTIGER DREMPEL", 1_000_400)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("na registratie gewijzigd" in v for v in f.violations)


def test_register_line_removed_is_caught(tmp_path):
    repo = _clean(tmp_path)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="PASS", result="T1_RESULT.md"),
            1_000_400)  # FAIL-regel weggepoetst
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("append-only" in v for v in f.violations)


def test_missing_prereg_file_is_caught(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_BESTAAT_NIET.md", status="PENDING", result="-"),
            1_000_000)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("ontbreekt of is niet getrackt" in v for v in f.violations)


def test_untracked_prereg_is_caught(tmp_path):
    repo = _repo(tmp_path)
    (repo / "docs" / "PREREG_T1.md").write_text("prereg", encoding="utf-8")  # wel op schijf
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="PENDING", result="-"),
            1_000_000)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("niet getrackt" in v for v in f.violations)


def test_missing_result_file_is_caught(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/PREREG_T1.md", "prereg", 1_000_000)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="FAIL", result="T1_RESULT.md"),
            1_000_100)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("resultaatbestand ontbreekt" in v for v in f.violations)


def test_unknown_status_is_caught(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/PREREG_T1.md", "prereg", 1_000_000)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status="BIJNA_PASS", result="-"),
            1_000_100)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok and any("niet toegestaan" in v for v in f.violations)


def test_empty_register_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/TOETSREGISTER.md", HEADER, 1_000_000)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not f.ok


def test_cli_exit_code_nonzero_on_violation(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "docs/TOETSREGISTER.md", HEADER, 1_000_000)
    r = subprocess.run([sys.executable,
                        str(Path(__file__).resolve().parents[1] / "scripts" / "verify_research_log.py"),
                        "--repo", str(repo)], capture_output=True, text=True)
    assert r.returncode == 1


@pytest.mark.parametrize("status", sorted(V.ALLOWED_STATUS))
def test_allowed_statuses_accepted(tmp_path, status):
    repo = _repo(tmp_path)
    _commit(repo, "docs/PREREG_T1.md", "prereg", 1_000_000)
    _commit(repo, "docs/TOETSREGISTER.md",
            HEADER + ROW.format(id="T1", prereg="PREREG_T1.md", status=status, result="-"),
            1_000_100)
    f = V.verify(repo, "docs/TOETSREGISTER.md")
    assert not any("niet toegestaan" in v for v in f.violations)
