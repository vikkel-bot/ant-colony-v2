"""
tests/test_heartbeat.py

Unit tests for HeartbeatThread.

Tested behaviour:
  - Heartbeat is sent after the interval expires
  - Multiple heartbeats are sent over multiple intervals
  - stop() before first beat → no beat sent
  - stop() signals the thread cleanly (no hang)
  - Exceptions in _send_heartbeat are swallowed (fail-closed P2)
  - No heartbeat sent when ant status != RUNNING
  - Daemon flag is set
  - Thread name includes ant_id prefix
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.schemas.ant import AntStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ant(status: AntStatus = AntStatus.RUNNING) -> MagicMock:
    ant = MagicMock()
    ant.ant_id = "abcdef1234567890"
    ant._status = status
    return ant


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHeartbeatThreadBasics:
    def test_daemon_flag(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=60.0)
        assert hb.daemon is True

    def test_thread_name_includes_ant_id_prefix(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=60.0)
        assert hb.name == "hb-abcdef12"

    def test_stop_before_start_does_not_raise(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=60.0)
        hb.start()
        hb.stop()
        assert not hb.is_alive()


class TestHeartbeatFiring:
    def test_heartbeat_sent_after_interval(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.15)
        hb.stop()
        assert ant._send_heartbeat.call_count >= 1

    def test_multiple_heartbeats_over_time(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.30)
        hb.stop()
        assert ant._send_heartbeat.call_count >= 3

    def test_stop_before_first_interval_prevents_beat(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=10.0)
        hb.start()
        hb.stop()
        assert ant._send_heartbeat.call_count == 0


class TestHeartbeatFailClosed:
    def test_exception_in_send_heartbeat_is_swallowed(self):
        ant = _make_ant()
        ant._send_heartbeat.side_effect = RuntimeError("boom")
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.15)
        hb.stop()
        assert ant._send_heartbeat.call_count >= 1

    def test_thread_survives_repeated_exceptions(self):
        ant = _make_ant()
        ant._send_heartbeat.side_effect = Exception("persistent error")
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.25)
        hb.stop()
        assert ant._send_heartbeat.call_count >= 3


class TestHeartbeatStatusGate:
    def test_no_heartbeat_when_status_not_running(self):
        ant = _make_ant(status=AntStatus.ABORTED)
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.15)
        hb.stop()
        assert ant._send_heartbeat.call_count == 0

    def test_no_heartbeat_when_status_completed(self):
        ant = _make_ant(status=AntStatus.COMPLETED)
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.15)
        hb.stop()
        assert ant._send_heartbeat.call_count == 0

    def test_heartbeats_stop_when_status_changes_to_aborted(self):
        ant = _make_ant(status=AntStatus.RUNNING)
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        time.sleep(0.12)
        ant._status = AntStatus.ABORTED
        count_at_abort = ant._send_heartbeat.call_count
        time.sleep(0.15)
        hb.stop()
        assert ant._send_heartbeat.call_count == count_at_abort


class TestHeartbeatStop:
    def test_stop_is_idempotent(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        hb.stop()
        hb.stop()
        assert not hb.is_alive()

    def test_thread_not_alive_after_stop(self):
        ant = _make_ant()
        hb = HeartbeatThread(ant, interval=0.05)
        hb.start()
        assert hb.is_alive()
        hb.stop()
        assert not hb.is_alive()
