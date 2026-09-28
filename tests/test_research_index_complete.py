"""Elk gedateerd document in docs/ moet in RESEARCH_INDEX.md voorkomen.

Fail-closed: een nieuw rapport dat de generator niet herkent, laat deze test
falen in plaats van stil buiten de index te blijven.
"""
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"
DATED = re.compile(r"_20\d{6}\.md$")


def test_every_dated_doc_is_in_index():
    index = (DOCS / "RESEARCH_INDEX.md").read_text(encoding="utf-8")
    dated = sorted(p.name for p in DOCS.glob("*.md") if DATED.search(p.name))
    assert dated, "geen gedateerde documenten gevonden"
    missing = [n for n in dated if n not in index]
    assert not missing, f"niet in RESEARCH_INDEX.md: {missing}"