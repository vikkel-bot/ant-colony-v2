"""
tests/test_reset_paper_state.py

Tests voor scripts/reset_paper_state.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Voeg scripts/ toe aan het pad zodat we de module kunnen importeren
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from reset_paper_state import _archive_paper_logs, _collect_paper_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _collect_paper_files
# ---------------------------------------------------------------------------

class TestCollectPaperFiles:
    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        paper_dir.mkdir()
        assert _collect_paper_files(paper_dir) == []

    def test_finds_top_level_jsonl(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        f1 = _write(paper_dir / "ant1.jsonl")
        f2 = _write(paper_dir / "ant2.jsonl")
        result = _collect_paper_files(paper_dir)
        assert set(result) == {f1, f2}

    def test_finds_nested_files(self, tmp_path: Path) -> None:
        """rglob moet bestanden in subdirectories ook vinden."""
        paper_dir = tmp_path / "paper"
        top = _write(paper_dir / "top.jsonl")
        nested = _write(paper_dir / "subdir" / "nested.jsonl")
        result = _collect_paper_files(paper_dir)
        assert set(result) == {top, nested}

    def test_finds_non_jsonl_files(self, tmp_path: Path) -> None:
        """Alle bestanden, niet alleen .jsonl."""
        paper_dir = tmp_path / "paper"
        f = _write(paper_dir / "state.json")
        result = _collect_paper_files(paper_dir)
        assert f in result

    def test_returns_only_files_not_dirs(self, tmp_path: Path) -> None:
        paper_dir = tmp_path / "paper"
        subdir = paper_dir / "subdir"
        subdir.mkdir(parents=True)
        f = _write(subdir / "file.jsonl")
        result = _collect_paper_files(paper_dir)
        assert result == [f]


# ---------------------------------------------------------------------------
# _archive_paper_logs
# ---------------------------------------------------------------------------

class TestArchivePaperLogs:
    def test_no_paper_dir_returns_zero(self, tmp_path: Path) -> None:
        count = _archive_paper_logs(tmp_path, yes=True)
        assert count == 0

    def test_empty_paper_dir_returns_zero(self, tmp_path: Path) -> None:
        (tmp_path / "paper").mkdir()
        count = _archive_paper_logs(tmp_path, yes=True)
        assert count == 0

    def test_archives_top_level_files(self, tmp_path: Path) -> None:
        _write(tmp_path / "paper" / "ant.jsonl", '{"event": "trade_opened"}')
        count = _archive_paper_logs(tmp_path, yes=True)
        assert count == 1
        # Origineel verdwenen
        assert not (tmp_path / "paper" / "ant.jsonl").exists()
        # Archief aangemaakt
        archives = list((tmp_path / "paper_archive").rglob("ant.jsonl"))
        assert len(archives) == 1

    def test_archives_nested_files(self, tmp_path: Path) -> None:
        """Bestanden in subdirectories worden ook gearchiveerd."""
        _write(tmp_path / "paper" / "sub" / "nested.jsonl")
        count = _archive_paper_logs(tmp_path, yes=True)
        assert count == 1
        assert not (tmp_path / "paper" / "sub" / "nested.jsonl").exists()
        archives = list((tmp_path / "paper_archive").rglob("nested.jsonl"))
        assert len(archives) == 1

    def test_archives_multiple_files(self, tmp_path: Path) -> None:
        for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
            _write(tmp_path / "paper" / name)
        count = _archive_paper_logs(tmp_path, yes=True)
        assert count == 3

    def test_dry_run_does_not_move_files(self, tmp_path: Path) -> None:
        src = _write(tmp_path / "paper" / "ant.jsonl")
        count = _archive_paper_logs(tmp_path, dry_run=True)
        assert count == 0
        assert src.exists()

    def test_paper_dir_empty_after_archive(self, tmp_path: Path) -> None:
        """Na archivering: paper dir bestaat nog maar is leeg."""
        _write(tmp_path / "paper" / "ant.jsonl")
        _archive_paper_logs(tmp_path, yes=True)
        paper_dir = tmp_path / "paper"
        remaining = list(paper_dir.rglob("*"))
        remaining_files = [f for f in remaining if f.is_file()]
        assert remaining_files == []

    def test_preserves_relative_structure_in_archive(self, tmp_path: Path) -> None:
        _write(tmp_path / "paper" / "subdir" / "file.jsonl")
        _archive_paper_logs(tmp_path, yes=True)
        archives = list((tmp_path / "paper_archive").rglob("file.jsonl"))
        assert len(archives) == 1
        # Subdir-structuur behouden in archief
        assert archives[0].parent.name == "subdir"

    def test_research_dir_untouched(self, tmp_path: Path) -> None:
        """ANT_LOGS/research/ wordt niet aangepast."""
        _write(tmp_path / "paper" / "ant.jsonl")
        research_file = _write(tmp_path / "research" / "research_ant.jsonl")
        _archive_paper_logs(tmp_path, yes=True)
        assert research_file.exists()

    def test_approved_dir_untouched(self, tmp_path: Path) -> None:
        _write(tmp_path / "paper" / "ant.jsonl")
        approved_file = _write(tmp_path / "approved" / "candidate.jsonl")
        _archive_paper_logs(tmp_path, yes=True)
        assert approved_file.exists()
