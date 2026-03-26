"""
Tests für services/engine.py

Getestet werden:
  - _can_start_new_valve_locked
  - start_valve
  - start_valve mit sensor_id (Cooldown-Tracking im COMMIT)
  - _history_add_locked
  - _active_runs_snapshot_locked
  - start_queue_item
"""

import time
import pytest
from fastapi import HTTPException

from core.state import state, state_lock, ActiveRun, QueueItem, HistoryItem
from services.engine import (
    _can_start_new_valve_locked,
    start_valve,
    _history_add_locked,
    _active_runs_snapshot_locked,
    start_queue_item,
)
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# _can_start_new_valve_locked
# ─────────────────────────────────────────────────────────────────────────────


class TestCanStartNewValve:
    def test_serial_mode_empty_runs_returns_true(self):
        with state_lock:
            state.parallel_enabled = False
            state.active_runs = {}
            result = _can_start_new_valve_locked()
        assert result is True

    def test_serial_mode_one_running_returns_false(self):
        with state_lock:
            state.parallel_enabled = False
            now = time.monotonic()
            state.active_runs = {
                1: ActiveRun(1, now + 60, "s", now, "manual", 60)
            }
            result = _can_start_new_valve_locked()
        assert result is False

    def test_parallel_mode_below_limit_returns_true(self):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            now = time.monotonic()
            state.active_runs = {
                1: ActiveRun(1, now + 60, "s", now, "manual", 60)
            }
            result = _can_start_new_valve_locked()
        assert result is True

    def test_parallel_mode_at_limit_returns_false(self):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            now = time.monotonic()
            state.active_runs = {
                1: ActiveRun(1, now + 60, "s", now, "manual", 60),
                2: ActiveRun(2, now + 60, "s", now, "manual", 60),
            }
            result = _can_start_new_valve_locked()
        assert result is False

    def test_hw_faulted_returns_false(self):
        with state_lock:
            state.parallel_enabled = False
            state.active_runs = {}
            state.hw_faulted = True
            result = _can_start_new_valve_locked()
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# start_valve
# ─────────────────────────────────────────────────────────────────────────────


class TestStartValve:
    def test_success_updates_active_runs(self, mock_io):
        start_valve(zone=1, duration_s=60, time_unit="Sekunden", source="manual")

        with state_lock:
            assert 1 in state.active_runs
            ar = state.active_runs[1]
        assert ar.zone == 1
        assert ar.started_planned_s == 60
        assert ar.started_source == "manual"
        assert ar.time_unit == "Sekunden"

    def test_success_updates_active_runs_fields(self, mock_io):
        start_valve(zone=1, duration_s=120, time_unit="Sekunden", source="schedule")

        with state_lock:
            assert 1 in state.active_runs
            ar = state.active_runs[1]
            assert ar.zone == 1
            assert ar.started_planned_s == 120
            assert ar.started_source == "schedule"

    def test_success_calls_io_open(self, mock_io):
        start_valve(zone=2, duration_s=30, time_unit="Sekunden", source="manual")

        call_args = mock_io.send_command.call_args
        cmd = call_args[0][0]
        assert cmd.action == "open"
        assert cmd.zone == 2

    def test_zone_already_running_raises_409(self, mock_io, make_active_run):
        with state_lock:
            state.active_runs = {1: make_active_run(zone=1)}

        with pytest.raises(HTTPException) as exc_info:
            start_valve(zone=1, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 409

    def test_serial_mode_busy_raises_409(self, mock_io, make_active_run):
        with state_lock:
            state.parallel_enabled = False
            state.active_runs = {1: make_active_run(zone=1)}

        with pytest.raises(HTTPException) as exc_info:
            start_valve(zone=2, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 409

    def test_hw_faulted_raises_423(self, mock_io):
        with state_lock:
            state.hw_faulted = True

        with pytest.raises(HTTPException) as exc_info:
            start_valve(zone=1, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 423

    def test_hw_error_raises_503_and_no_state_change(self, failing_io):
        with pytest.raises(HTTPException) as exc_info:
            start_valve(zone=1, duration_s=30, time_unit="Sekunden", source="manual")

        assert exc_info.value.status_code == 503
        with state_lock:
            assert state.active_runs == {}

    def test_parallel_mode_allows_second_zone(self, mock_io, make_active_run):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {1: make_active_run(zone=1)}

        start_valve(zone=2, duration_s=30, time_unit="Sekunden", source="manual")

        with state_lock:
            assert 2 in state.active_runs

    def test_no_sensor_id_leaves_last_triggered_unchanged(self, mock_io):
        """Manueller Start (sensor_id=None) berührt sensor_last_triggered nicht."""
        with state_lock:
            state.sensor_last_triggered = {1: 999.0}

        start_valve(zone=1, duration_s=30, time_unit="Sekunden", source="manual")

        with state_lock:
            # Wert unverändert – start_valve ohne sensor_id fasst das Dict nicht an
            assert state.sensor_last_triggered[1] == 999.0


# ─────────────────────────────────────────────────────────────────────────────
# start_valve – Sensor-Cooldown-Tracking (COMMIT-Phase)
# ─────────────────────────────────────────────────────────────────────────────


class TestStartValveSensorTracking:
    """sensor_last_triggered und sensor_pending_zones werden in start_valve COMMIT gesetzt.

    Der Cooldown-Timestamp wird bewusst erst beim echten Ventilstart gesetzt,
    nicht schon beim Einreihen in die Queue (sensor_engine.py). Damit verbraucht
    ein langer Queue-Rückstau die Cooldown-Zeit nicht vorzeitig.
    """

    def test_sensor_id_sets_last_triggered_on_commit(self, mock_io):
        """start_valve mit sensor_id setzt sensor_last_triggered[sensor_id]."""
        now_before = time.monotonic()
        with state_lock:
            state.sensor_last_triggered = {}

        start_valve(zone=1, duration_s=60, time_unit="Sekunden",
                    source="sensor", sensor_id=1)
        now_after = time.monotonic()

        with state_lock:
            ts = state.sensor_last_triggered.get(1)
        assert ts is not None
        # Timestamp liegt im Fenster [now_before, now_after]
        assert now_before <= ts <= now_after

    def test_sensor_id_removes_zone_from_pending(self, mock_io):
        """Ventilstart entfernt die gestartete Zone aus sensor_pending_zones."""
        with state_lock:
            state.sensor_pending_zones = {1: {1, 2}}  # Zone 1 und 2 noch pending

        start_valve(zone=1, duration_s=60, time_unit="Sekunden",
                    source="sensor", sensor_id=1)

        with state_lock:
            pending = state.sensor_pending_zones.get(1, set())
        # Zone 1 wurde gestartet → aus Pending entfernt
        assert 1 not in pending
        # Zone 2 wartet noch
        assert 2 in pending

    def test_last_triggered_updated_for_each_zone_of_same_sensor(self, mock_io):
        """Bei mehreren Zonen desselben Sensors wird last_triggered bei jedem Start aktualisiert.

        In der Praxis startet timer_loop Ventile sequenziell (ein Start pro
        timer_loop-Iteration). Jeder Start aktualisiert last_triggered, sodass
        der Cooldown immer vom zuletzt gestarteten Ventil des Sensors gemessen wird.
        """
        with state_lock:
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {1: {1, 2}}
            state.parallel_enabled = True
            state.max_concurrent_valves = 2

        # Zwei Starts nacheinander (gleiches Sensor 1, Zonen 1 und 2)
        start_valve(zone=1, duration_s=60, time_unit="Sekunden",
                    source="sensor", sensor_id=1)
        ts_after_zone1 = time.monotonic()

        start_valve(zone=2, duration_s=60, time_unit="Sekunden",
                    source="sensor", sensor_id=1)

        with state_lock:
            ts = state.sensor_last_triggered.get(1)
            pending = state.sensor_pending_zones.get(1, set())

        # Beide Zonen aus Pending entfernt
        assert 1 not in pending
        assert 2 not in pending
        # Timestamp liegt nach dem ersten Start (wurde beim zweiten Start nochmals gesetzt)
        assert ts >= ts_after_zone1

    def test_hw_error_does_not_set_last_triggered(self, failing_io):
        """Bei Hardware-Fehler (503) bleibt sensor_last_triggered unberührt."""
        with state_lock:
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {1: {1}}

        with pytest.raises(HTTPException) as exc_info:
            start_valve(zone=1, duration_s=60, time_unit="Sekunden",
                        source="sensor", sensor_id=1)

        assert exc_info.value.status_code == 503
        with state_lock:
            # COMMIT wurde nicht erreicht → kein Timestamp gesetzt
            assert 1 not in state.sensor_last_triggered
            # Zone bleibt in Pending (sensor_engine entscheidet beim nächsten
            # Polling-Zyklus erneut, ob sie eingestellt werden soll)
            assert 1 in state.sensor_pending_zones.get(1, set())

    def test_sensor_id_none_does_not_touch_pending_zones(self, mock_io):
        """start_valve ohne sensor_id lässt sensor_pending_zones vollständig unberührt."""
        with state_lock:
            state.sensor_pending_zones = {1: {1, 2}}

        start_valve(zone=1, duration_s=60, time_unit="Sekunden",
                    source="manual", sensor_id=None)

        with state_lock:
            pending = state.sensor_pending_zones.get(1, set())
        # Unverändert – manueller Start berührt Sensor-Tracking nicht
        assert pending == {1, 2}

    def test_sensor_lazy_init_last_triggered_none(self, mock_io):
        """start_valve initialisiert sensor_last_triggered lazy wenn None."""
        with state_lock:
            state.sensor_last_triggered = None

        start_valve(zone=1, duration_s=30, time_unit="Sekunden",
                    source="sensor", sensor_id=5)

        with state_lock:
            assert state.sensor_last_triggered is not None
            assert 5 in state.sensor_last_triggered

    def test_sensor_lazy_init_pending_zones_none(self, mock_io):
        """start_valve initialisiert sensor_pending_zones lazy wenn None."""
        with state_lock:
            state.sensor_pending_zones = None

        # Kein Fehler, kein Crash – lediglich discard auf nicht-vorhandener Zone
        start_valve(zone=1, duration_s=30, time_unit="Sekunden",
                    source="sensor", sensor_id=5)

        with state_lock:
            assert state.sensor_pending_zones is not None


# ─────────────────────────────────────────────────────────────────────────────
# _history_add_locked
# ─────────────────────────────────────────────────────────────────────────────


class TestHistoryAddLocked:
    def test_adds_entry(self):
        with state_lock:
            _history_add_locked(zone=1, duration_s=45, source="manual", time_unit="Sekunden")
            assert len(state.run_history) == 1
            entry = state.run_history[0]
        assert entry.zone == 1
        assert entry.duration_s == 45
        assert entry.source == "manual"

    def test_newest_first(self):
        with state_lock:
            _history_add_locked(zone=1, duration_s=10, source="manual", time_unit="Sekunden")
            _history_add_locked(zone=2, duration_s=20, source="schedule", time_unit="Sekunden")
            assert state.run_history[0].zone == 2  # neuester zuerst

    def test_respects_max_history_items(self):
        with state_lock:
            state.max_history_items = 3
            for z in range(1, 7):
                _history_add_locked(zone=z, duration_s=10, source="manual", time_unit="Sekunden")
            assert len(state.run_history) == 3

    def test_sets_history_dirty_flag(self):
        with state_lock:
            state.history_dirty = False
            _history_add_locked(zone=1, duration_s=10, source="manual", time_unit="Sekunden")
            assert state.history_dirty is True

    def test_negative_duration_clamped_to_zero(self):
        with state_lock:
            _history_add_locked(zone=1, duration_s=-5, source="manual", time_unit="Sekunden")
            assert state.run_history[0].duration_s == 0


# ─────────────────────────────────────────────────────────────────────────────
# _active_runs_snapshot_locked
# ─────────────────────────────────────────────────────────────────────────────


class TestActiveRunsSnapshot:
    def test_running_zone_has_positive_remaining(self, make_active_run):
        with state_lock:
            state.active_runs = {1: make_active_run(zone=1, duration_s=100)}
            state.paused = False
            snap = _active_runs_snapshot_locked()
        assert snap[1]["remaining_s"] > 0

    def test_paused_zone_uses_stored_remaining(self):
        now = time.monotonic()
        with state_lock:
            ar = ActiveRun(
                zone=1, end_time=0.0, time_unit="Sekunden",
                started_at=now, started_source="manual", started_planned_s=60
            )
            ar.remaining_s = 42
            ar.paused_at = now
            state.active_runs = {1: ar}
            state.paused = True
            snap = _active_runs_snapshot_locked()
        assert snap[1]["remaining_s"] == 42

    def test_elapsed_zone_returns_zero_remaining(self, make_active_run):
        with state_lock:
            state.active_runs = {1: make_active_run(zone=1, elapsed=True)}
            state.paused = False
            snap = _active_runs_snapshot_locked()
        assert snap[1]["remaining_s"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# start_queue_item
# ─────────────────────────────────────────────────────────────────────────────


def test_start_queue_item_starts_zone(mock_io):
    item = QueueItem(zone=3, duration=45, time_unit="Sekunden", source="queue")
    start_queue_item(item)

    with state_lock:
        assert 3 in state.active_runs
        assert state.active_runs[3].started_planned_s == 45


def test_start_queue_item_passes_sensor_id(mock_io):
    """start_queue_item leitet sensor_id aus QueueItem an start_valve weiter."""
    with state_lock:
        state.sensor_last_triggered = {}
        state.sensor_pending_zones  = {7: {3}}

    item = QueueItem(zone=3, duration=60, time_unit="Sekunden",
                     source="sensor", sensor_id=7)
    start_queue_item(item)

    with state_lock:
        # COMMIT hat sensor_last_triggered gesetzt und Zone 3 aus Pending entfernt
        assert 7 in state.sensor_last_triggered
        assert 3 not in state.sensor_pending_zones.get(7, set())


def test_start_queue_item_sensor_id_none_no_tracking(mock_io):
    """start_queue_item mit sensor_id=None hinterlässt kein Sensor-Tracking."""
    with state_lock:
        state.sensor_last_triggered = {}

    item = QueueItem(zone=2, duration=30, time_unit="Sekunden",
                     source="queue", sensor_id=None)
    start_queue_item(item)

    with state_lock:
        assert state.sensor_last_triggered == {}
