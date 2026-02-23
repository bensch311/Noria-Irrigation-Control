# tests/test_timer.py
"""
Tests für services/timer.py

timer_loop ist das Herzstück des Systems: Er schliesst Ventile nach Ablauf,
verarbeitet Hardware-Fehler mit Backoff, füllt die Queue auf und erkennt
Queue-Ende.

Nach dem Thread-Safety-Bugfix ruft timer_loop ALLE Hardware-Operationen
ausschließlich über io_worker.send_command() auf (kein direkter Driver-Zugriff
mehr). Die autouse mock_io-Fixture aus conftest.py interceptiert alle Calls.

Getestet werden:
  - Ventil-Timeout: close via io_worker + History + active_runs Cleanup
  - Mehrere parallele Timeouts
  - Pause-Zustand blockt Timeout-Verarbeitung
  - Hardware-Fehler bei close: Backoff-Mechanismus
  - Hardware-Fault-Latch nach HW_CLOSE_MAX_RETRIES Fehlern
  - Emergency close_all via io_worker bei Fault-Latch
  - close_all wird nicht wiederholt (hw_fault_close_all_attempted Guard)
  - Zone durch API gestoppt während Unlock-Fenster → kein Doppel-History
  - Queue-Fill: Items starten wenn Slot frei (seriell + parallel)
  - Queue-Fill geblockt bei hw_faulted / paused
  - Queue-Fertig-Erkennung
  - Queue-Item zurück bei Start-Fehler
  - Parallel-Drain-Logging
"""

import time
from unittest.mock import call, patch

import pytest

from core.config import HW_CLOSE_MAX_RETRIES
from core.state import state, state_lock, ActiveRun, QueueItem
from services.engine import _sync_legacy_single_fields_locked
from services.io_worker import IOResult, IOCommand


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _make_elapsed_run(zone: int, duration_s: int = 60, source: str = "manual", hw_close_failures: int = 0) -> ActiveRun:
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


def _make_active_run(zone: int, duration_s: int = 60) -> ActiveRun:
    """ActiveRun der noch läuft (end_time in der Zukunft)."""
    now = time.monotonic()
    return ActiveRun(
        zone=zone,
        end_time=now + duration_s,
        time_unit="Sekunden",
        started_at=now,
        started_source="manual",
        started_planned_s=duration_s,
    )


def _make_close_fail(mock_io) -> None:
    """
    Konfiguriert mock_io so, dass alle 'close'-Commands fehlschlagen.
    Andere Actions (open, close_all) liefern Erfolg – damit Queue-Start-Tests
    nicht beeinträchtigt werden.
    """
    def _side_effect(cmd: IOCommand, timeout_s: float = 5.0) -> IOResult:
        if cmd.action == "close":
            return IOResult(success=False, zone=cmd.zone, error="Simulated GPIO failure", duration_ms=1.0)
        return IOResult(success=True, duration_ms=1.0)

    mock_io.send_command.side_effect = _side_effect


def _make_close_all_fail(mock_io) -> None:
    """
    Konfiguriert mock_io so, dass 'close' UND 'close_all' fehlschlagen.
    """
    def _side_effect(cmd: IOCommand, timeout_s: float = 5.0) -> IOResult:
        if cmd.action in ("close", "close_all"):
            return IOResult(success=False, zone=cmd.zone, error="Simulated GPIO failure", duration_ms=1.0)
        return IOResult(success=True, duration_ms=1.0)

    mock_io.send_command.side_effect = _side_effect


def _run_timer_once() -> None:
    """
    Führt exakt einen vollständigen Durchlauf des timer_loop aus.
    shutdown_event wird so gemockt, dass:
      - is_set() beim ersten Aufruf False zurückgibt (Loop betreten)
      - is_set() ab zweitem Aufruf True zurückgibt (Loop verlassen)
      - wait() immer False zurückgibt (kein vorzeitiger Break)
    """
    from core.state import shutdown_event
    from services.timer import timer_loop

    call_count = 0

    def mock_is_set() -> bool:
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
    def test_elapsed_valve_removed_from_active_runs(self, mock_io):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 not in state.active_runs

    def test_elapsed_valve_syncs_legacy_fields(self, mock_io):
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.running_zone is None
            assert state.end_time == 0.0

    def test_elapsed_valve_adds_history_entry(self, mock_io):
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

    def test_not_elapsed_valve_stays_active(self, mock_io):
        with state_lock:
            state.active_runs = {1: _make_active_run(1, duration_s=300)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs

    def test_elapsed_valve_sends_close_via_io_worker(self, mock_io):
        """Timer muss io_worker.send_command('close', zone) aufrufen – KEIN direkter Driver-Zugriff."""
        with state_lock:
            state.active_runs = {2: _make_elapsed_run(2)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()

        # Prüfe ob close-Command gesendet wurde
        sent_cmds = [c for c in mock_io.send_command.call_args_list]
        close_cmds = [c for c in sent_cmds if c[0][0].action == "close" and c[0][0].zone == 2]
        assert len(close_cmds) == 1, "Genau ein close-Command für zone=2 erwartet"

    def test_multiple_elapsed_valves_all_removed(self, mock_io):
        with state_lock:
            state.parallel_enabled = True
            state.active_runs = {
                1: _make_elapsed_run(1),
                2: _make_elapsed_run(2),
                3: _make_elapsed_run(3),
            }
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}

    def test_multiple_elapsed_valves_add_history(self, mock_io):
        with state_lock:
            state.parallel_enabled = True
            state.active_runs = {
                1: _make_elapsed_run(1, source="manual"),
                2: _make_elapsed_run(2, source="schedule"),
            }
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert len(state.run_history) == 2
            zones_in_history = {e.zone for e in state.run_history}
            assert zones_in_history == {1, 2}

    def test_paused_state_blocks_timeout(self, mock_io):
        """Im Pause-Zustand dürfen keine Timeouts verarbeitet werden."""
        with state_lock:
            state.paused = True
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            # Zone darf nicht entfernt worden sein
            assert 1 in state.active_runs
            # io_worker darf kein 'close' erhalten haben
        close_cmds = [c for c in mock_io.send_command.call_args_list
                      if c[0][0].action == "close"]
        assert len(close_cmds) == 0


# ---------------------------------------------------------------------------
# Zone durch API gestoppt während Unlock-Fenster (Race-Condition-Schutz)
# ---------------------------------------------------------------------------

class TestTimerRaceConditionProtection:
    def test_zone_stopped_by_api_during_unlock_no_double_history(self, mock_io):
        """
        Wenn die API eine Zone während des io_worker-Unlock-Fensters (Phase B)
        stoppt und aus active_runs entfernt, darf timer_loop in Phase C keine
        doppelte History-Eintrag erzeugen und keinen KeyError werfen.
        """
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            state.run_history = []
            _sync_legacy_single_fields_locked()

        # Simuliere: io_worker.send_command "close" löscht Zone aus active_runs
        # (entspricht dem Verhalten, wenn die API /stop während Phase B aufgerufen wird)
        original_send_command = mock_io.send_command.return_value

        def _stop_zone_during_close(cmd: IOCommand, timeout_s: float = 5.0) -> IOResult:
            if cmd.action == "close" and cmd.zone == 1:
                # Simuliere API-Stop: Zone bereits aus active_runs entfernt
                with state_lock:
                    state.active_runs.pop(1, None)
            return IOResult(success=True, duration_ms=1.0)

        mock_io.send_command.side_effect = _stop_zone_during_close

        _run_timer_once()

        with state_lock:
            # Keine Doppel-History durch den timer (API hat bereits einen Eintrag gemacht)
            # Der Timer muss 0 Einträge haben (API hat active_runs geleert)
            assert 1 not in state.active_runs
            # run_history bleibt bei 0 da der Timer die Zone nicht mehr sieht
            assert len(state.run_history) == 0


# ---------------------------------------------------------------------------
# Hardware-Fehler und Backoff
# ---------------------------------------------------------------------------

class TestTimerHardwareErrors:
    def test_hw_close_failure_keeps_zone_active(self, mock_io):
        """Bei io_worker-Fehler muss die Zone aktiv bleiben (Retry)."""
        _make_close_fail(mock_io)
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert 1 in state.active_runs

    def test_hw_close_failure_increments_failure_count(self, mock_io):
        """hw_close_failures muss nach jedem Fehlschlag inkrementiert werden."""
        _make_close_fail(mock_io)
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=0)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.active_runs[1].hw_close_failures == 1

    def test_hw_close_failure_sets_backoff_end_time(self, mock_io):
        """Nach einem Fehlschlag muss end_time in der Zukunft liegen (Backoff)."""
        _make_close_fail(mock_io)
        now_before = time.monotonic()
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.active_runs[1].end_time > now_before

    def test_hw_close_failure_no_history_entry(self, mock_io):
        """Bei Hardware-Fehler darf kein History-Eintrag erstellt werden."""
        _make_close_fail(mock_io)
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1)}
            state.run_history = []
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert len(state.run_history) == 0

    def test_hw_fault_latched_after_max_retries(self, mock_io):
        """Nach HW_CLOSE_MAX_RETRIES Fehlern muss hw_faulted=True gesetzt werden."""
        _make_close_fail(mock_io)
        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.hw_faulted is True
            assert state.hw_fault_reason == "close_failed_max_retries"
            assert state.hw_fault_zone == 1

    def test_hw_fault_not_latched_before_max_retries(self, mock_io):
        """Vor Erreichen von HW_CLOSE_MAX_RETRIES darf hw_faulted nicht gesetzt werden."""
        _make_close_fail(mock_io)
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=0)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.hw_faulted is False

    def test_hw_fault_triggers_emergency_close_all(self, mock_io):
        """
        Wenn Fault gelatcht wird, muss io_worker.send_command('close_all') aufgerufen werden.
        """
        _make_close_fail(mock_io)
        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()

        close_all_cmds = [c for c in mock_io.send_command.call_args_list
                          if c[0][0].action == "close_all"]
        assert len(close_all_cmds) == 1

    def test_hw_fault_close_all_not_repeated_on_second_run(self, mock_io):
        """
        hw_fault_close_all_attempted-Guard muss verhindern, dass close_all
        beim nächsten Timer-Durchlauf nochmals gesendet wird.
        """
        _make_close_fail(mock_io)
        failures_before_last = HW_CLOSE_MAX_RETRIES - 1
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=failures_before_last)}
            _sync_legacy_single_fields_locked()

        # Erster Durchlauf: Fault wird gelatcht, close_all wird einmalig gesendet
        _run_timer_once()

        # Zone bleibt in active_runs (close schlug fehl), end_time auf Backoff setzen
        with state_lock:
            if 1 in state.active_runs:
                state.active_runs[1].end_time = time.monotonic() - 1.0  # wieder abgelaufen

        # Zweiter Durchlauf: close_all darf NICHT nochmals gesendet werden
        _run_timer_once()

        close_all_cmds = [c for c in mock_io.send_command.call_args_list
                          if c[0][0].action == "close_all"]
        assert len(close_all_cmds) == 1, (
            "close_all darf nur einmalig pro Fault-Ereignis gesendet werden"
        )

    def test_unexpected_exception_in_io_worker_sets_short_retry(self, mock_io):
        """
        Wenn io_worker.send_command für 'close' einen unerwarteten Fehler
        zurückgibt (success=False, error=...), muss end_time > now gesetzt werden
        und hw_faulted darf noch nicht True sein (Backoff-Phase).
        """
        _make_close_fail(mock_io)
        now_before = time.monotonic()
        with state_lock:
            state.active_runs = {1: _make_elapsed_run(1, hw_close_failures=0)}
            _sync_legacy_single_fields_locked()
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
        """Im Seriell-Modus mit freiem System soll das erste Queue-Item gestartet werden."""
        with state_lock:
            state.queue_state = "läuft"
            state.parallel_enabled = False
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()

        # start_queue_item ruft io_worker.send_command("open", zone) auf
        open_cmds = [c for c in mock_io.send_command.call_args_list
                     if c[0][0].action == "open"]
        assert len(open_cmds) >= 1
        assert open_cmds[0][0][0].zone == 1

    def test_queue_item_not_started_in_serial_mode_with_running(self, mock_io):
        """Im Seriell-Modus mit laufender Zone darf kein Queue-Item gestartet werden."""
        with state_lock:
            state.queue_state = "läuft"
            state.parallel_enabled = False
            state.active_runs = {1: _make_active_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()

        open_cmds = [c for c in mock_io.send_command.call_args_list
                     if c[0][0].action == "open"]
        assert len(open_cmds) == 0

    def test_queue_not_started_when_hw_faulted(self, mock_io):
        """Bei hw_faulted darf kein Queue-Item gestartet werden."""
        with state_lock:
            state.queue_state = "läuft"
            state.hw_faulted = True
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}
            assert len(state.queue) == 1

    def test_queue_not_started_when_paused(self, mock_io):
        """Bei paused=True darf kein Queue-Item gestartet werden."""
        with state_lock:
            state.queue_state = "läuft"
            state.paused = True
            state.active_runs = {}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.active_runs == {}

    def test_failed_queue_start_returns_item_to_front(self, failing_io):
        """Wenn start_queue_item fehlschlägt, muss das Item wieder vorne in die Queue."""
        with state_lock:
            state.queue_state = "läuft"
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

    def test_parallel_queue_starts_multiple_items(self, mock_io):
        """Im Parallel-Modus mit max_concurrent_valves=2 können zwei Items gleichzeitig starten."""
        with state_lock:
            state.queue_state = "läuft"
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {}
            state.queue = [
                QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
                QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue"),
            ]
        _run_timer_once()

        open_cmds = [c for c in mock_io.send_command.call_args_list
                     if c[0][0].action == "open"]
        # Exakt 2 starts erwartet (Limit = 2)
        assert len(open_cmds) == 2


# ---------------------------------------------------------------------------
# Queue-Fertig-Erkennung
# ---------------------------------------------------------------------------

class TestTimerQueueFinished:
    def test_queue_becomes_fertig_when_empty_no_runs(self):
        """queue_state muss auf 'fertig' wechseln wenn Queue leer und nichts läuft."""
        with state_lock:
            state.queue_state = "läuft"
            state.queue = []
            state.active_runs = {}
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "fertig"

    def test_queue_stays_lauft_when_runs_active(self, mock_io):
        """queue_state darf nicht 'fertig' werden solange Zonen aktiv sind."""
        with state_lock:
            state.queue_state = "läuft"
            state.parallel_enabled = False
            state.active_runs = {1: _make_active_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "läuft"

    def test_queue_stays_lauft_when_queue_not_empty(self, mock_io):
        """queue_state darf nicht 'fertig' werden wenn noch Items in der Queue sind."""
        with state_lock:
            state.queue_state = "läuft"
            state.parallel_enabled = False
            state.active_runs = {1: _make_active_run(1)}
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue")]
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "läuft"

    def test_queue_bereit_not_changed_to_fertig(self):
        """Wenn queue_state != 'läuft', soll er nicht auf 'fertig' geändert werden."""
        with state_lock:
            state.queue_state = "bereit"
            state.queue = []
            state.active_runs = {}
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "bereit"

    def test_last_valve_timeout_triggers_fertig(self, mock_io):
        """Wenn die letzte Zone abläuft und Queue leer ist, soll 'fertig' gesetzt werden."""
        with state_lock:
            state.queue_state = "läuft"
            state.queue = []
            state.active_runs = {1: _make_elapsed_run(1)}
            _sync_legacy_single_fields_locked()
        _run_timer_once()
        with state_lock:
            assert state.queue_state == "fertig"


# ---------------------------------------------------------------------------
# Parallel-Drain-Logging
# ---------------------------------------------------------------------------

class TestTimerParallelDrain:
    def test_drain_warning_logged_when_parallel_disabled_multi_zone(self, mock_io):
        """Drain-Warnung soll genau einmal geloggt werden."""
        logged_events = []

        import services.timer as timer_mod
        original_log = timer_mod.log_event

        def capture_log(event, **kwargs):
            logged_events.append(event)
            return original_log(event, **kwargs)

        with patch.object(timer_mod, "log_event", side_effect=capture_log):
            with state_lock:
                state.parallel_enabled = False
                state.parallel_drain_logged = False
                state.queue_state = "läuft"
                state.queue = [QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue")]
                state.active_runs = {
                    1: _make_active_run(1),
                    2: _make_active_run(2),
                }
            _run_timer_once()

        assert "parallel_disabled_waiting_for_drain" in logged_events

    def test_drain_warning_not_repeated_if_already_logged(self, mock_io):
        """Drain-Warnung darf nicht doppelt geloggt werden."""
        logged_events = []

        import services.timer as timer_mod
        original_log = timer_mod.log_event

        def capture_log(event, **kwargs):
            logged_events.append(event)
            return original_log(event, **kwargs)

        with patch.object(timer_mod, "log_event", side_effect=capture_log):
            with state_lock:
                state.parallel_enabled = False
                state.parallel_drain_logged = True  # bereits geloggt
                state.queue_state = "läuft"
                state.queue = [QueueItem(zone=3, duration=60, time_unit="Sekunden", source="queue")]
                state.active_runs = {
                    1: _make_active_run(1),
                    2: _make_active_run(2),
                }
            _run_timer_once()

        drain_events = [e for e in logged_events if e == "parallel_disabled_waiting_for_drain"]
        assert len(drain_events) == 0
