"""Indexdatum komt uit de bestandsnaam (beleid 119195a punt 9). Fail-closed zonder geldige datum."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_research_index as B  # noqa: E402


def test_name_date_from_filename():
    assert B.name_date("docs/POWER_CALIBRATION_20260928.md") == "2026-09-28"
    assert B.name_date("docs/GATE_UNIVERSE_v2_FROZEN_20260922.md") == "2026-09-22"
    assert B.name_date("docs/METING_BTC_DONCHIAN_PERMUTATIE_20260920_ruw.txt") == "2026-09-20"


def test_name_date_missing_fails_closed():
    with pytest.raises(ValueError):
        B.name_date("docs/TOETSREGISTER.md")


def test_name_date_impossible_date_fails_closed():
    with pytest.raises(ValueError):
        B.name_date("docs/X_20261340.md")