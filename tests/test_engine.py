"""
Tests für services/engine.py

Getestet werden:
  - _can_start_new_valve_locked
  - _start_valve_locked
  - _history_add_locked
  - _sync_legacy_single_fields_locked
  - _active_runs_snapshot_locked
  - start_queue_item
"""

import time
import pytest
from fastapi import HTTPException

from core.state import state, state_lock, ActiveRun, QueueItem, HistoryItem
from services.engine import (
    _can_start_new_valve_locked,
    _start_valve_locked,
    _history_add_locked,
    _sync_legacy_single_fields_locked,
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
# _start_valve_locked
# ─────────────────────────────────────────────────────────────────────────────


class TestStartValveLocked:
    def test_success_updates_active_runs(self, mock_io):
        _start_valve_locked(zone=1, duration_s=60, time_unit="Sekunden", source="manual")

        with state_lock:
            assert 1 in state.active_runs
            ar = state.active_runs[1]
        assert ar.zone == 1
        assert ar.started_planned_s == 60
        assert ar.started_source == "manual"
        assert ar.time_unit == "Sekunden"

    def test_success_syncs_legacy_fields(self, mock_io):
        _start_valve_locked(zone=1, duration_s=120, time_unit="Sekunden", source="schedule")

        with state_lock:
            assert state.running_zone == 1
            assert state.started_planned_s == 120

    def test_success_calls_io_open(self, mock_io):
        _start_valve_locked(zone=2, duration_s=30, time_unit="Sekunden", source="manual")

        call_args = mock_io.send_command.call_args
        cmd = call_args[0][0]
        assert cmd.action == "open"
        assert cmd.zone == 2

    def test_zone_already_running_raises_409(self, mock_io, make_active_run):
        with state_lock:
            state.active_runs = {1: make_active_run(zone=1)}
            _sync_legacy_single_fields_locked()

        with pytest.raises(HTTPException) as exc_info:
            _start_valve_locked(zone=1, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 409

    def test_serial_mode_busy_raises_409(self, mock_io, make_active_run):
        with state_lock:
            state.parallel_enabled = False
            state.active_runs = {1: make_active_run(zone=1)}
            _sync_legacy_single_fields_locked()

        with pytest.raises(HTTPException) as exc_info:
            _start_valve_locked(zone=2, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 409

    def test_hw_faulted_raises_423(self, mock_io):
        with state_lock:
            state.hw_faulted = True

        with pytest.raises(HTTPException) as exc_info:
            _start_valve_locked(zone=1, duration_s=30, time_unit="Sekunden", source="manual")
        assert exc_info.value.status_code == 423

    def test_hw_error_raises_503_and_no_state_change(self, failing_io):
        with pytest.raises(HTTPException) as exc_info:
            _start_valve_locked(zone=1, duration_s=30, time_unit="Sekunden", source="manual")

        assert exc_info.value.status_code == 503
        with state_lock:
            assert state.active_runs == {}
            assert state.running_zone is None

    def test_parallel_mode_allows_second_zone(self, mock_io, make_active_run):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            state.active_runs = {1: make_active_run(zone=1)}
            _sync_legacy_single_fields_locked()

        _start_valve_locked(zone=2, duration_s=30, time_unit="Sekunden", source="manual")

        with state_lock:
            assert 2 in state.active_runs


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
# _sync_legacy_single_fields_locked
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncLegacyFields:
    def test_no_active_runs_resets_fields(self):
        with state_lock:
            state.running_zone = 3
            state.end_time = 999.0
            state.active_runs = {}
            _sync_legacy_single_fields_locked()
            assert state.running_zone is None
            assert state.end_time == 0.0

    def test_single_run_sets_primary_zone(self, make_active_run):
        with state_lock:
            state.active_runs = {3: make_active_run(zone=3, duration_s=90)}
            _sync_legacy_single_fields_locked()
            assert state.running_zone == 3
            assert state.started_planned_s == 90

    def test_multiple_runs_uses_lowest_zone(self, make_active_run):
        with state_lock:
            state.active_runs = {
                5: make_active_run(zone=5),
                2: make_active_run(zone=2),
            }
            _sync_legacy_single_fields_locked()
            assert state.running_zone == 2


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
