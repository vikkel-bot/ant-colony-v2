"""Tests voor atomair schrijven en robuust lezen.

Inclusief nagebootst Windows-gedrag: os.replace en open() kunnen tijdelijk
weigeren zolang de andere kant het bestand vasthoudt.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from ant_colony.tools import atomic_io


def test_reader_never_sees_partial_content(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_io.atomic_write_text(path, json.dumps({"n": 0}))
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            atomic_io.atomic_write_text(path, json.dumps({"n": n}))

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        for _ in range(300):
            text = atomic_io.read_text_with_retry(path)
            assert text != ""
            json.loads(text)
    finally:
        stop.set()
        t.join(timeout=5)
    assert json.loads(atomic_io.read_text_with_retry(path))["n"] > 0


def test_write_retries_when_replace_is_refused(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "a.json"
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] % 4 != 0:
            raise PermissionError(5, "Toegang geweigerd")
        return real(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    atomic_io.atomic_write_text(path, "hallo", delay=0.0)
    assert path.read_text(encoding="utf-8") == "hallo"
    assert calls["n"] >= 4


def test_write_gives_up_loudly_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "b.json"

    def always_fail(src, dst):
        raise PermissionError(5, "Toegang geweigerd")

    monkeypatch.setattr(atomic_io.os, "replace", always_fail)
    with pytest.raises(PermissionError):
        atomic_io.atomic_write_text(path, "x", attempts=3, delay=0.0)
    assert not list(tmp_path.glob("*.tmp")), "tijdelijk bestand blijft staan"
    assert not path.exists()


def test_read_retries_on_permission_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "c.json"
    path.write_text("inhoud", encoding="utf-8")
    calls = {"n": 0}
    real_read = Path.read_text

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "Permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert atomic_io.read_text_with_retry(path, delay=0.0) == "inhoud"
    assert calls["n"] == 3


def test_read_gives_up_loudly(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "d.json"
    path.write_text("x", encoding="utf-8")

    def always_fail(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", always_fail)
    with pytest.raises(PermissionError):
        atomic_io.read_text_with_retry(path, attempts=3, delay=0.0)
