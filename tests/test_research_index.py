"""Bewaakt dat de onderzoeksindex niet veroudert.

De index is gegenereerd. Wordt er een toets, gate of kostenmodel toegevoegd
zonder de index bij te werken, dan faalt deze test — en daarmee CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_research_index as B  # noqa: E402


def test_index_is_up_to_date():
    assert B.INDEX.exists(), "docs/RESEARCH_INDEX.md ontbreekt"
    actueel = B.build()
    op_schijf = B.INDEX.read_text(encoding="utf-8")
    assert op_schijf == actueel, (
        "RESEARCH_INDEX.md loopt achter op de bronbestanden; "
        "draai scripts/build_research_index.py en commit het resultaat"
    )


def test_index_lists_every_registered_test():
    inhoud = B.INDEX.read_text(encoding="utf-8")
    for test_id, *_ in B.register_rows():
        assert f"| {test_id} |" in inhoud


def test_index_lists_every_cost_model_version():
    inhoud = B.INDEX.read_text(encoding="utf-8")
    for p in B.DOCS.glob("COST_MODEL_*.json"):
        assert p.name in inhoud


def test_open_items_are_included_verbatim():
    inhoud = B.INDEX.read_text(encoding="utf-8")
    bron = B.OPEN_ITEMS.read_text(encoding="utf-8")
    for regel in bron.splitlines():
        if regel.startswith("- ") or regel.startswith("| "):
            assert regel in inhoud, f"ontbreekt in index: {regel[:60]}"


def test_generation_is_deterministic():
    assert B.build() == B.build()
