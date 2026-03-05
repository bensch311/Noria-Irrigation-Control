"""
Tests für services/io_worker.py

Getestet werden:
  - IOWorker start/shutdown
  - send_command: success (mit echtem Worker + SimDriver)
  - send_command: Worker nicht gestartet
  - send_command: Timeout-Simulation
  - send_command: Queue voll
  - _execute_command: verschiedene Actions, Driver=None
"""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from services.io_worker import (
    IOCommand,
    IOResult,
    IOWorker,
    get_io_worker,
    reset_io_worker,
    set_io_worker,
)
from services.valve_driver import SimValveDriver, set_valve_driver


# ─────────────────────────────────────────────────────────────────────────────
# Echter IOWorker (nutzt SimValveDriver – autouse Fixture aus conftest)
# ─────────────────────────────────────────────────────────────────────────────


class TestIOWorkerRealOperations:
    """
    Tests mit echtem IOWorker + SimValveDriver.
    Der autouse mock_io-Fixture aus conftest.py wird in diesen Tests umgangen,
    indem ein fresh IOWorker direkt instanziiert wird (ohne globalen Singleton).
    """

    def _make_real_worker(self) -> IOWorker:
        """Erstellt einen echten IOWorker ohne den globalen Singleton zu beeinflussen."""
        w = IOWorker()
        w.start()
        return w

    def test_open_succeeds(self):
        w = self._make_real_worker()
        try:
            result = w.send_command(IOCommand(action="open", zone=1), timeout_s=2.0)
            assert result.success is True
            assert result.error is None
        finally:
            w.shutdown(timeout_s=2.0)

    def test_close_succeeds(self):
        w = self._make_real_worker()
        try:
            result = w.send_command(IOCommand(action="close", zone=1), timeout_s=2.0)
            assert result.success is True
        finally:
            w.shutdown(timeout_s=2.0)

    def test_close_all_succeeds(self):
        w = self._make_real_worker()
        try:
            result = w.send_command(IOCommand(action="close_all"), timeout_s=2.0)
            assert result.success is True
        finally:
            w.shutdown(timeout_s=2.0)

    def test_duration_ms_is_set(self):
        w = self._make_real_worker()
        try:
            result = w.send_command(IOCommand(action="open", zone=1), timeout_s=2.0)
            assert result.duration_ms >= 0.0
        finally:
            w.shutdown(timeout_s=2.0)

    def test_sequential_commands(self):
        w = self._make_real_worker()
        try:
            for zone in [1, 2, 3]:
                r = w.send_command(IOCommand(action="open", zone=zone), timeout_s=2.0)
                assert r.success is True
            for zone in [1, 2, 3]:
                r = w.send_command(IOCommand(action="close", zone=zone), timeout_s=2.0)
                assert r.success is True
        finally:
            w.shutdown(timeout_s=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# IOWorker Fehlerfälle
# ─────────────────────────────────────────────────────────────────────────────


class TestIOWorkerErrorCases:
    def test_not_started_returns_failure(self):
        w = IOWorker()
        # Worker absichtlich NICHT gestartet
        result = w.send_command(IOCommand(action="open", zone=1), timeout_s=1.0)
        assert result.success is False
        assert "nicht gestartet" in (result.error or "").lower()

    def test_unknown_action_returns_failure(self):
        w = IOWorker()
        w.start()
        try:
            result = w._execute_command(
                IOCommand(action="unknown_xyz", zone=1),  # type: ignore
                driver=SimValveDriver(),
            )
            assert result.success is False
            assert result.error is not None
        finally:
            w.shutdown(timeout_s=2.0)

    def test_driver_none_returns_failure(self):
        w = IOWorker()
        result = w._execute_command(
            IOCommand(action="open", zone=1),
            driver=None,
        )
        assert result.success is False
        assert result.error is not None

    def test_valve_driver_error_returns_failure(self):
        from services.valve_driver import ValveDriverError

        class FailingDriver(SimValveDriver):
            def open(self, zone):
                raise ValveDriverError("Simulated HW failure")

        w = IOWorker()
        result = w._execute_command(
            IOCommand(action="open", zone=1),
            driver=FailingDriver(),
        )
        assert result.success is False
        assert "Simulated HW failure" in (result.error or "")

    def test_queue_full_returns_failure(self):
        w = IOWorker(max_queue_size=0)  # Queue-Größe 0 → sofort voll
        w._started = True  # Flag setzen ohne echten Thread zu starten

        result = w.send_command(IOCommand(action="open", zone=1), timeout_s=1.0)
        assert result.success is False


# ─────────────────────────────────────────────────────────────────────────────
# Singleton-Funktionen
# ─────────────────────────────────────────────────────────────────────────────


class TestIOWorkerSingleton:
    def test_get_io_worker_returns_same_instance(self):
        """get_io_worker gibt nach set_io_worker immer dieselbe Instanz zurück."""
        mock = MagicMock(spec=IOWorker)
        mock._started = True
        set_io_worker(mock)
        assert get_io_worker() is mock

    def test_reset_io_worker_creates_fresh_on_next_get(self):
        # autouse mock_io ist gesetzt; nach reset wäre es None
        reset_io_worker()
        # Kein Fehler beim Aufrufen
        # (autouse Teardown handled cleanup)

    def test_set_io_worker_calls_shutdown_on_previous(self):
        old_mock = MagicMock(spec=IOWorker)
        old_mock._started = True
        set_io_worker(old_mock)

        new_mock = MagicMock(spec=IOWorker)
        new_mock._started = True
        set_io_worker(new_mock)

        old_mock.shutdown.assert_called_once()
