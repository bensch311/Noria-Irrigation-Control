"""
Tests fuer services/engine.py - fehlende Pfade aus Coverage-Report

Abgedeckt werden:
  - engine_status_payload_locked: pausierter Zustand
  - engine_status_payload_locked: hw_fault-Felder
  - _active_runs_snapshot_locked: end_time == 0.0
  - _calc_actual_run_s_primary mit Pause-Unterbrechungen
"""

import time
import pytest

from core.state import state, state_lock, ActiveRun
from services.engine import (
    _active_runs_snapshot_locked,
    _calc_actual_run_s_primary,
    engine_status_payload_locked,
    _sync_legacy_single_fields_locked,
)
from tests.conftest import set_running_zone


class TestStatusPayloadPaused:
    def test_paused_state_returns_pausiert(self):
        set_running_zone(1, 60)
        with state_lock:
            ar = state.active_runs[1]
            ar.remaining_s = 35
            ar.paused_at = time.monotonic()
            ar.end_time = 0.0
            state.paused = True
            _sync_legacy_single_fields_locked()
            payload = engine_status_payload_locked()
        assert payload["state"] == "pausiert"
        assert payload["paused"] is True
        assert payload["remaining_time"] == 35

    def test_running_state_returns_lauft(self):
        set_running_zone(1, 60)
        with state_lock:
            payload = engine_status_payload_locked()
        assert payload["state"] == "l\u00e4uft"
        assert payload["paused"] is False

    def test_idle_state_returns_bereit(self):
        with state_lock:
            payload = engine_status_payload_locked()
        assert payload["state"] == "bereit"
        assert payload["running_zone"] is None

    def test_hw_fault_fields_present(self):
        with state_lock:
            state.hw_faulted = True
            state.hw_fault_reason = "close_failed_max_retries"
            state.hw_fault_zone = 3
            state.hw_fault_since = "2025-01-01T06:00:00+01:00"
            payload = engine_status_payload_locked()
        assert payload["hw_faulted"] is True
        assert payload["hw_fault_reason"] == "close_failed_max_retries"
        assert payload["hw_fault_zone"] == 3

    def test_hw_fault_false_when_clean(self):
        with state_lock:
            payload = engine_status_payload_locked()
        assert payload["hw_faulted"] is False
        assert payload["hw_fault_zone"] is None

    def test_active_runs_in_payload(self):
        set_running_zone(2, 90)
        with state_lock:
            payload = engine_status_payload_locked()
        assert 2 in payload["active_runs"]
        assert payload["active_runs"][2]["remaining_s"] > 0

    def test_parallel_fields_in_payload(self):
        with state_lock:
            state.parallel_enabled = True
            state.max_concurrent_valves = 2
            payload = engine_status_payload_locked()
        assert payload["parallel_enabled"] is True
        assert payload["max_concurrent_valves"] == 2

    def test_schedules_count_in_payload(self, make_schedule):
        with state_lock:
            state.schedules = [make_schedule(zone=1), make_schedule(zone=2)]
            payload = engine_status_payload_locked()
        assert payload["schedules_count"] == 2


class TestActiveRunsSnapshotEdgeCases:
    def test_end_time_zero_uses_stored_remaining(self):
        now = time.monotonic()
        with state_lock:
            ar = ActiveRun(
                zone=1, end_time=0.0, time_unit="Sekunden",
                started_at=now, started_source="manual", started_planned_s=60
            )
            ar.remaining_s = 27
            state.active_runs = {1: ar}
            state.paused = False
            snap = _active_runs_snapshot_locked()
        assert snap[1]["remaining_s"] == 27

    def test_planned_s_in_snapshot(self):
        now = time.monotonic()
        with state_lock:
            ar = ActiveRun(
                zone=1, end_time=now + 90, time_unit="Sekunden",
                started_at=now, started_source="manual", started_planned_s=90
            )
            state.active_runs = {1: ar}
            state.paused = False
            snap = _active_runs_snapshot_locked()
        assert snap[1]["planned_s"] == 90

    def test_source_in_snapshot(self):
        now = time.monotonic()
        with state_lock:
            ar = ActiveRun(
                zone=1, end_time=now + 60, time_unit="Sekunden",
                started_at=now, started_source="schedule", started_planned_s=60
            )
            state.active_runs = {1: ar}
            state.paused = False
            snap = _active_runs_snapshot_locked()
        assert snap[1]["started_source"] == "schedule"

    def test_empty_active_runs_returns_empty_dict(self):
        with state_lock:
            state.active_runs = {}
            snap = _active_runs_snapshot_locked()
        assert snap == {}


class TestCalcActualRunS:
    def test_no_started_at_returns_zero(self):
        with state_lock:
            state.started_at = 0.0
            state.paused_total_s = 0.0
            state.paused_at = 0.0
            result = _calc_actual_run_s_primary(time.monotonic())
        assert result == 0

    def test_simple_run_without_pause(self):
        now = time.monotonic()
        elapsed = 30.0
        with state_lock:
            state.started_at = now - elapsed
            state.paused_total_s = 0.0
            state.paused_at = 0.0
            result = _calc_actual_run_s_primary(now)
        assert 29 <= result <= 31

    def test_run_with_completed_pause(self):
        now = time.monotonic()
        with state_lock:
            state.started_at = now - 60.0
            state.paused_total_s = 20.0
            state.paused_at = 0.0
            result = _calc_actual_run_s_primary(now)
        assert 38 <= result <= 42

    def test_run_currently_paused_includes_ongoing_pause(self):
        now = time.monotonic()
        with state_lock:
            state.started_at = now - 60.0
            state.paused_total_s = 0.0
            state.paused_at = now - 10.0
            result = _calc_actual_run_s_primary(now)
        assert 48 <= result <= 52

    def test_run_with_both_past_and_current_pause(self):
        now = time.monotonic()
        with state_lock:
            state.started_at = now - 120.0
            state.paused_total_s = 30.0
            state.paused_at = now - 15.0
            result = _calc_actual_run_s_primary(now)
        assert 73 <= result <= 77
