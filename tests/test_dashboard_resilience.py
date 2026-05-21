"""
tests/test_dashboard_resilience.py

Tests voor dashboard OSError-retry (server.py) en watchdog (start_colony.py).

Dekt:
  - OSError tijdens uvicorn.run() wordt afgevangen en server herstart
  - Clean exit (geen exception) stopt de retry-lus
  - Niet-OSError exceptions propageren ongewijzigd
  - Watchdog herstart dashboard na crash en logt WARNING
  - Watchdog stopt bij clean exit
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _dashboard_port_free(monkeypatch):
    monkeypatch.setattr(
        "ant_colony.dashboard.server._port_has_listener",
        lambda host, port: False,
    )


# ---------------------------------------------------------------------------
# server.run() — OSError retry
# ---------------------------------------------------------------------------

class TestServerRunRetry:
    """server.run() vangt OSError op en herstart uvicorn."""

    def test_retries_on_oserror(self):
        call_count = 0

        def fake_uvicorn_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("WinError 64 — netwerknaam niet beschikbaar")
            # Tweede aanroep: clean exit

        with (
            patch("ant_colony.dashboard.server.time") as mock_time,
            patch("uvicorn.run", side_effect=fake_uvicorn_run),
            patch("ant_colony.dashboard.server.create_app", return_value=MagicMock()),
        ):
            from ant_colony.dashboard.server import run
            run()

        assert call_count == 2
        mock_time.sleep.assert_called_once()

    def test_exits_on_clean_return(self):
        call_count = 0

        def fake_uvicorn_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1

        with (
            patch("uvicorn.run", side_effect=fake_uvicorn_run),
            patch("ant_colony.dashboard.server.create_app", return_value=MagicMock()),
        ):
            from ant_colony.dashboard.server import run
            run()

        assert call_count == 1

    def test_multiple_oserrors_all_retried(self):
        call_count = 0

        def fake_uvicorn_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise OSError(f"transient error {call_count}")

        with (
            patch("ant_colony.dashboard.server.time"),
            patch("uvicorn.run", side_effect=fake_uvicorn_run),
            patch("ant_colony.dashboard.server.create_app", return_value=MagicMock()),
        ):
            from ant_colony.dashboard.server import run
            run()

        assert call_count == 4

    def test_non_oserror_propagates(self):
        with (
            patch("uvicorn.run", side_effect=RuntimeError("unexpected")),
            patch("ant_colony.dashboard.server.create_app", return_value=MagicMock()),
        ):
            from ant_colony.dashboard.server import run
            with pytest.raises(RuntimeError, match="unexpected"):
                run()

    def test_warning_logged_per_retry(self):
        call_count = 0

        def fake_uvicorn_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("WinError 64")

        with (
            patch("ant_colony.dashboard.server.time"),
            patch("uvicorn.run", side_effect=fake_uvicorn_run),
            patch("ant_colony.dashboard.server.create_app", return_value=MagicMock()),
            patch("ant_colony.dashboard.server.logger") as mock_logger,
        ):
            from ant_colony.dashboard.server import run
            run()

        # warning moet minimaal één keer aangeroepen zijn voor de OSError
        mock_logger.warning.assert_called()
        warning_messages = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("socket fout" in m or "Dashboard" in m for m in warning_messages)

    def test_bind_conflict_raises_dashboard_bind_error(self, monkeypatch):
        monkeypatch.setattr(
            "ant_colony.dashboard.server._port_has_listener",
            lambda host, port: True,
        )
        from ant_colony.dashboard.server import DashboardBindError, run

        with pytest.raises(DashboardBindError):
            run()


# ---------------------------------------------------------------------------
# start_colony._run_dashboard_watchdog() — watchdog herstart
# ---------------------------------------------------------------------------

class TestDashboardWatchdog:
    """_run_dashboard_watchdog herstart dashboard bij crash en stopt bij clean exit."""

    def _get_watchdog(self):
        import importlib, sys
        # Force reload zodat we altijd de nieuwste versie pakken
        if "scripts.start_colony" in sys.modules:
            del sys.modules["scripts.start_colony"]
        import scripts.start_colony as sc
        return sc._run_dashboard_watchdog

    def test_restarts_after_crash(self):
        watchdog = self._get_watchdog()
        call_count = 0

        def fake_run(ctx, host, port):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")

        log = MagicMock(spec=logging.Logger)

        with patch("scripts.start_colony.time") as mock_time:
            watchdog(fake_run, None, "0.0.0.0", 8000, log)

        assert call_count == 2
        mock_time.sleep.assert_called_once()

    def test_logs_warning_on_restart(self):
        watchdog = self._get_watchdog()
        call_count = 0

        def fake_run(ctx, host, port):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")

        log = MagicMock(spec=logging.Logger)

        with patch("scripts.start_colony.time"):
            watchdog(fake_run, None, "0.0.0.0", 8000, log)

        log.warning.assert_called()

    def test_stops_on_clean_exit(self):
        watchdog = self._get_watchdog()
        call_count = 0

        def fake_run(ctx, host, port):
            nonlocal call_count
            call_count += 1

        log = MagicMock(spec=logging.Logger)

        watchdog(fake_run, None, "0.0.0.0", 8000, log)

        assert call_count == 1
        log.info.assert_called()

    def test_multiple_crashes_all_restarted(self):
        watchdog = self._get_watchdog()
        call_count = 0

        def fake_run(ctx, host, port):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("transient")

        log = MagicMock(spec=logging.Logger)

        with patch("scripts.start_colony.time"):
            watchdog(fake_run, None, "0.0.0.0", 8000, log)

        assert call_count == 3


# ---------------------------------------------------------------------------
# start_colony._start_supervised_ant() — ant crash restart
# ---------------------------------------------------------------------------

class TestAntSupervisor:
    """_start_supervised_ant geeft gecrashte ants een fresh TTL restart."""

    def _get_module(self):
        import importlib, sys
        # Force reload zodat we altijd de nieuwste versie pakken
        if "scripts.start_colony" in sys.modules:
            del sys.modules["scripts.start_colony"]
        import scripts.start_colony as sc
        return sc

    def test_restarts_after_ant_crash(self, monkeypatch):
        sc = self._get_module()

        class DummyMission:
            mission_id = "mission-test"
            ttl = 60
            heartbeat_interval = 10

        class DummyScheduler:
            def __init__(self):
                self.registered = []

            def register_agent(self, record):
                self.registered.append(record)

        run_calls = []

        class CrashOnceAnt:
            def __init__(self, ant_id):
                self.ant_id = ant_id

            def run(self):
                run_calls.append(self.ant_id)
                if len(run_calls) == 1:
                    raise RuntimeError("simulated ant crash")
                return "completed"

        sleep_calls = []
        monkeypatch.setattr(sc, "_MAX_RESTARTS_PER_HOUR", 1)
        monkeypatch.setattr(sc.time, "sleep", lambda seconds: sleep_calls.append(seconds))
        monkeypatch.setattr(sc.time, "monotonic", lambda: float(len(sleep_calls) * 10))

        scheduler = DummyScheduler()
        thread = sc._start_supervised_ant(
            label="DummyAnt",
            ant_type="dummy_ant",
            id_prefix="dummy",
            mission=DummyMission(),
            scheduler=scheduler,
            node_id="node-test",
            make_ant=lambda ant_id: CrashOnceAnt(ant_id),
            log=MagicMock(spec=logging.Logger),
        )
        thread.join(timeout=2.0)

        assert not thread.is_alive()
        assert len(run_calls) == 2
        assert run_calls[0] != run_calls[1]
        assert len(scheduler.registered) == 2
        assert sleep_calls
        assert sleep_calls[0] == sc._ANT_RESTART_DELAY_S


class TestScoutAntRuntimeFlag:
    """ScoutAnt kan uit staan zonder de colony bootstrap te blokkeren."""

    def _get_module(self):
        import importlib, sys
        if "scripts.start_colony" in sys.modules:
            del sys.modules["scripts.start_colony"]
        import scripts.start_colony as sc
        return sc

    def test_colony_bootstrap_skips_scout_ant_when_disabled(self, monkeypatch):
        sc = self._get_module()

        class DummyScope:
            symbols = ["BTC-EUR"]

        class DummyMission:
            mission_id = "scout-test"
            ttl = 60
            heartbeat_interval = 10
            market_scope = DummyScope()

        start_supervised = MagicMock()
        monkeypatch.setattr(sc, "_SCOUT_ANT_ENABLED", False)
        monkeypatch.setattr(sc, "_start_supervised_ant", start_supervised)
        log = MagicMock(spec=logging.Logger)

        started = sc._start_scout_ant_if_enabled(
            scout_mission=DummyMission(),
            scheduler=MagicMock(),
            node_id="node-test",
            make_ant=lambda ant_id: object(),
            log=log,
        )

        assert started is False
        start_supervised.assert_not_called()
        log.info.assert_called_with("ScoutAnt uitgeschakeld (_SCOUT_ANT_ENABLED=False).")


# ---------------------------------------------------------------------------
# start_colony dashboard port preflight
# ---------------------------------------------------------------------------

class TestDashboardPortPreflight:
    """start_colony stopt alleen oude Colony-processen en faalt anders gesloten."""

    def _get_module(self):
        import importlib, sys
        if "scripts.start_colony" in sys.modules:
            del sys.modules["scripts.start_colony"]
        import scripts.start_colony as sc
        return sc

    def test_unknown_listener_fails_closed(self, monkeypatch):
        sc = self._get_module()
        log = MagicMock(spec=logging.Logger)
        monkeypatch.setattr(sc, "_list_port_listener_pids", lambda port, log: {1234})
        monkeypatch.setattr(sc, "_process_command_line", lambda pid, log: "C:\\Other\\server.exe")
        stop = MagicMock()
        monkeypatch.setattr(sc, "_stop_pid", stop)

        assert sc._prepare_dashboard_port(8000, log) is False
        stop.assert_not_called()

    def test_old_start_colony_listener_is_stopped(self, monkeypatch, tmp_path):
        sc = self._get_module()
        log = MagicMock(spec=logging.Logger)
        calls = {"count": 0}

        def fake_list(port, log):
            calls["count"] += 1
            return {2222} if calls["count"] == 1 else set()

        monkeypatch.setattr(sc, "_list_port_listener_pids", fake_list)
        monkeypatch.setattr(
            sc,
            "_process_command_line",
            lambda pid, log: str(tmp_path / "scripts" / "start_colony.py"),
        )
        monkeypatch.setattr(sc, "_stop_pid", MagicMock(return_value=True))
        monkeypatch.setattr(sc, "_can_bind_dashboard_port", lambda host, port: True)
        monkeypatch.setattr(sc.time, "sleep", MagicMock())

        assert sc._prepare_dashboard_port(8000, log, repo_root=tmp_path) is True
        sc._stop_pid.assert_called_once_with(2222, 8000, log)
