from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock


def _load_watchdog_module():
    path = Path(__file__).parent.parent / "scripts" / "watchdog.py"
    spec = importlib.util.spec_from_file_location("ant_watchdog_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluate_status_accepts_fresh_tick() -> None:
    watchdog = _load_watchdog_module()

    healthy, reason = watchdog.evaluate_status({
        "status": "RUNNING",
        "colony_status": "RUNNING_GREEN",
        "seconds_ago": 42,
    })

    assert healthy is True
    assert "tick_age=42.0s" in reason


def test_evaluate_status_rejects_stale_tick() -> None:
    watchdog = _load_watchdog_module()

    healthy, reason = watchdog.evaluate_status({"seconds_ago": 301})

    assert healthy is False
    assert "tick_age=301.0s>300s" in reason


def test_check_colony_reports_failed_status_request(monkeypatch) -> None:
    watchdog = _load_watchdog_module()

    def boom(*_, **__):
        raise TimeoutError("slow dashboard")

    monkeypatch.setattr(watchdog, "fetch_status", boom)

    healthy, reason = watchdog.check_colony()

    assert healthy is False
    assert reason.startswith("status_request_failed:TimeoutError")


def test_restart_colony_uses_nssm_restart(monkeypatch) -> None:
    watchdog = _load_watchdog_module()
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(watchdog.subprocess, "run", run)
    monkeypatch.setattr(watchdog.logger, "warning", MagicMock())
    monkeypatch.setattr(watchdog.logger, "info", MagicMock())

    assert watchdog.restart_colony("tick_age=999.0s>300s") is True

    run.assert_called_once()
    assert run.call_args.args[0] == [
        r"C:\Trading\nssm.exe",
        "restart",
        "AntColony",
    ]
