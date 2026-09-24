"""Bewaakt de CI-configuratie zelf.

Twee eigenschappen mogen nooit stil verdwijnen:
  - fetch-depth: 0, anders kan verify_research_log commit-volgorde niet zien
    en faalt of slaagt hij om de verkeerde reden;
  - de registerverificatie draait als eigen stap, niet alleen de testsuite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> str:
    assert WORKFLOW.exists(), "CI-workflow ontbreekt"
    return WORKFLOW.read_text(encoding="utf-8")


def test_full_history_is_fetched(workflow):
    assert "fetch-depth: 0" in workflow


def test_suite_runs(workflow):
    assert "pytest tests" in workflow


def test_register_verification_runs(workflow):
    assert "verify_research_log.py" in workflow


def test_runs_on_push_and_pull_request(workflow):
    assert "push:" in workflow and "pull_request:" in workflow


def test_dependencies_are_installed_from_requirements(workflow):
    assert "requirements.txt" in workflow and "requirements-dev.txt" in workflow
