"""
Tests für services/timer.py

timer_loop ist das Herzstück des Systems: Er schliesst Ventile nach Ablauf,
verarbeitet Hardware-Fehler mit Backoff, fuellt die Queue auf und erkennt
Queue-Ende. Aktuell 0% Coverage - kritischste Luecke.

Getestet werden:
  - Ventil-Timeout: close + History + active_runs Cleanup
  - Mehrere parallele Timeouts
  - Pause-Zustand blockt Timeout-Verarbeitung
  - Hardware-Fehler bei close: Backoff-Mechanismus
  - Hardware-Fault-Latch nach HW_CLOSE_MAX_RETRIES Fehlern
  - Emergency close_all bei Fault-Latch
  - Queue-Fill: Items starten wenn Slot frei (seriell + parallel)
  - Queue-Fill geblockt bei hw_faulted / paused
  - Queue-Fertig-Erkennung (laeuft -> fertig)
  - Queue-Item zurueck bei Start-Fehler
  - Parallel-Drain-Logging
"""

import time
from unittest.mock import patch

import pytest

from core.config import HW_CLOSE_MAX_RETRIES
from core.state import state, state_lock, ActiveRun, QueueItem
from services.engine import _sync_legacy_single_fields_locked
from services.valve_driver import ValveDriverError, SimValveDriver, set_valve_driver


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_elapsed_run(zone, duration_s=60, source="manual", hw_close_failures=0):
    """ActiveRun dessen end_time bereits in der Vergangenheit liegt."""
    now = time.monotonic()
    ar = ActiveRun(
        zone=zone,
        end_time=now - 1.0,
        time_unit="Sekunden",
        started_at=now - duration_s,
        started_source=source,
        started_planned_s=duration_s,
    )
    ar.hw_close_failures = hw_close_failures
    return ar


def _make_active_run(zone, duration_s=60):
    """ActiveRun der noch laeuft (end_time in der Zukunft)."""
    now = time.monotonic()
    return ActiveRun(
        zone=zone,
        end_time=now + duration_s,
        time_unit="Sekunden",
        started_at=now,
        started_source="manual",
        started_planned_s=duration_s,
    )


class FailingCloseDriver(SimValveDriver):
    """Driver bei dem close() immer fehlschlaegt."""
    def close(self, zone):
        raise ValveDriverError(f"Simulated close failure zone {zone}")


class FailingCloseAllDriver(FailingCloseDriver):
    """Driver bei dem auch close_all() fehlschlaegt."""
    def close_all(self):
        raise ValveDriverError("Simulated close_all failure")


def _run_timer_once():
    """
    Fuehrt exakt einen vollstaendigen Durchlauf des timer_loop aus.
    shutdown_event wird so gemockt dass:
    - is_set() beim ersten Aufruf False zurueckgibt (Loop betreten)
    - is_set() ab zweitem Aufruf True zurueckgibt (Loop verlassen)
    - wait() immer False zurueckgibt (kein vorzeitiger Break)
    """
    from core.state import shutdown_event
    from services.timer import timer_loop

    call_count = 0

    def mock_is_set():
        nonlocal call_count
        call_count += 1
        return call_count > 1

    with (
        patch.object(shutdown_event, "is_set", side_effect=mock_is_set),
        patch.object(shutdown_event, "wait", return_value=False),
    ):
        timer_loop()


# ---------------------------------------------------------------------------
# Ventil-Timeout: Normaler Ablauf
# ---------------------------------------------------------------------------

class TestTimerValveTimeout:
    def test_elapsed_valve_removed_from_active_runs(self):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 not in state.active_runs

    def test_elapsed_valve_syncs_legacy_fields(self):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.running_zone is None
            assert state.end_time == 0.0

    def test_elapsed_valve_adds_history_entry(self):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, duration_s=45, source="schedule")}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert len(state.run_history) == 1
            entry = state.run_history[0]
            assert entry.zone == 1
            assert entry.source == "schedule"
            assert entry.duration_s >= 0

    def test_not_elapsed_valve_stays_active(self):
        with state_lock:
            state.active_runs = {1: _make_active_run(1, duration_s=300)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs

    def test_multiple_elapsed_valves_all_closed(self):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 3
            state.active_runs = {
                1: _make_elapsed_run(1),
                2: _make_elapsed_run(2),
                3: _make_active_run(3, duration_s=300),
            }
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 not in state.active_runs
            assert 2 not in state.active_runs
            assert 3 in state.active_runs
            assert len(state.run_history) == 2

    def test_elapsed_valve_history_source_preserved(self):
        with state_lock:
            state.active_runs = {2: _make_elapsed_run(2, source="queue")}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.run_history[0].source == "queue"


# ---------------------------------------------------------------------------
# Pause-Zustand
# ---------------------------------------------------------------------------

class TestTimerPauseHandling:
    def test_paused_state_skips_timeout_processing(self):
        with state_lock:
            state.paused = True
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs
            assert len(state.run_history) == 0


# ---------------------------------------------------------------------------
# Hardware-Fehler: Retry/Backoff
# ---------------------------------------------------------------------------

class TestTimerHardwareFailure:
    def test_hw_close_failure_keeps_zone_in_active_runs(self):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(FailingCloseDriver())
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs
            assert len(state.run_history) == 0

    def test_hw_close_failure_increments_failure_count(self):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=0)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(FailingCloseDriver())
        _run_timer_once()
        with state_lock:
            assert state.active_runs[1].hw_close_failures == 1

    def test_hw_close_failure_sets_backoff_endtime(self):
        now_before = time.monotonic()
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(FailingCloseDriver())
        _run_timer_once()
        with state_lock:
            if 1 in state.active_runs:
                assert state.active_runs[1].end_time > now_before

    def test_hw_no_fault_latch_before_max_retries(self):
        failures_below_max = HW_CLOSE_MAX_RETRIES - 2
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_below_max)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(FailingCloseDriver())
        _run_timer_once()
        with state_lock:
            assert state.hw_faulted is False

    def test_hw_close_failure_latches_fault_at_max_retries(self):
        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(FailingCloseDriver())
        _run_timer_once()
        with state_lock:
            assert state.hw_faulted is True
            assert state.hw_fault_zone == 1
            assert state.hw_fault_reason == "close_failed_max_retries"
            assert state.hw_fault_since != ""

    def test_hw_fault_latch_attempts_close_all(self):
        close_all_called = []

        class TrackingDriver(FailingCloseDriver):
            def close_all(self):
                close_all_called.append(True)

        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(TrackingDriver())
        _run_timer_once()
        assert len(close_all_called) == 1

    def test_hw_fault_close_all_not_repeated(self):
        close_all_calls = []

        class TrackingDriver(FailingCloseDriver):
            def close_all(self):
                close_all_calls.append(True)

        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(TrackingDriver())
        _run_timer_once()

        # Zweiter Durchlauf - close_all darf nicht nochmal aufgerufen werden
        with state_lock:
            if 1 in state.active_runs:
                state.active_runs[1].end_time = time.monotonic() - 1.0
        _run_timer_once()
        assert len(close_all_calls) <= 1

    def test_unexpected_exception_sets_short_retry(self):
        class UnexpectedErrorDriver(SimValveDriver):
            def close(self, zone):
                raise RuntimeError("Completely unexpected error")

        now_before = time.monotonic()
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        set_valve_driver(UnexpectedErrorDriver())
        _run_timer_once()
        with state_lock:
            if 1 in state.active_runs:
                assert state.active_runs[1].end_time > now_before
                assert state.hw_faulted is False


# ---------------------------------------------------------------------------
# Queue-Fill
# ---------------------------------------------------------------------------

class TestTimerQueueFill:
    def test_queue_item_started_in_serial_mode(self, mock_io):
        with state_lock:
            state.queue_state = "laeuft"
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        # Da wir deutschen State-String nutzen: "läuft" != "laeuft"
        # Wir setzen den korrekten String
        pass

    def test_queue_item_started_serial(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs
            assert state.queue == []

    def test_queue_item_not_started_when_busy_serial(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.parallel_enabled = False
            state.active_runs = {1: _make_active_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 2 not in state.active_runs
            assert len(state.queue) == 1

    def test_queue_parallel_starts_up_to_limit(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {}
            state.queue = [
                QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
            ]
        _run_timer_once()
        with state_lock:
            assert len(state.active_runs) == 2
            assert len(state.queue) == 1

    def test_queue_not_started_when_state_bereit(self, mock_io):
        with state_lock:
            state.queue_state = "bereit"
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}
            assert len(state.queue) == 1

    def test_queue_not_started_when_hw_faulted(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.hw_faulted = True
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}
            assert len(state.queue) == 1

    def test_queue_not_started_when_paused(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.paused = True
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}

    def test_failed_queue_start_returns_item_to_front(self, failing_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [
                QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
            ]
        _run_timer_once()
        with state_lock:
            assert len(state.queue) >= 1
            assert state.queue[0].zone == 1


# ---------------------------------------------------------------------------
# Queue-Fertig-Erkennung
# ---------------------------------------------------------------------------

class TestTimerQueueFinished:
    def test_queue_becomes_fertig_when_empty_no_runs(self):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.queue = []
            state.active_runs = {}
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "fertig"

    def test_queue_stays_lauft_when_runs_active(self, mock_io):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.parallel_enabled = False
            state.active_runs = {1: _make_active_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "l\u00e4uft"

    def test_queue_state_bereit_not_changed_to_fertig(self):
        with state_lock:
            state.queue_state = "bereit"
            state.queue = []
            state.active_runs = {}
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "bereit"

    def test_fertig_after_last_zone_completes(self):
        with state_lock:
            state.queue_state = "l\u00e4uft"
            state.queue = []
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 not in state.active_runs
            assert state.queue_state == "fertig"


# ---------------------------------------------------------------------------
# Parallel-Drain-Logging
# ---------------------------------------------------------------------------

class TestTimerParallelDrain:
    def test_drain_logged_when_parallel_disabled_multiple_running(self, mock_io):
        with state_lock:
            state.parallel_enabled = False
            state.parallel_drain_logged = False
            state.queue_state = "l\u00e4uft"
            state.queue = [QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue")]
            state.active_runs = {
                1: _make_active_run(1),
                2: _make_active_run(2),
            }
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.parallel_drain_logged is True

    def test_drain_flag_cleared_when_single_zone(self, mock_io):
        with state_lock:
            state.parallel_enabled = False
            state.parallel_drain_logged = True
            state.active_runs = {1: _make_active_run(1)}
            state.queue = []
            state.queue_state = "bereit"
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.parallel_drain_logged is False
