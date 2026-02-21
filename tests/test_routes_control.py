"""
Tests für api/routes_control.py

Getestet werden:
  GET  /status
  POST /start
  POST /stop
  POST /pause
  POST /resume
  POST /fault/clear
  GET/POST /automation
  GET/POST /parallel
"""

import time
import pytest

from core.state import state, state_lock, ActiveRun
from services.engine import _sync_legacy_single_fields_locked
from tests.conftest import set_running_zone


# ─────────────────────────────────────────────────────────────────────────────
# GET /status
# ─────────────────────────────────────────────────────────────────────────────


def test_status_idle(client):
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running_zone"] is None
    assert data["paused"] is False
    assert data["queue_length"] == 0
    assert "active_runs" in data
    assert "hw_faulted" in data


def test_status_running(client, mock_io):
    set_running_zone(1, 60)
    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running_zone"] == 1
    assert 1 in data["running_zones"]


# ─────────────────────────────────────────────────────────────────────────────
# POST /start
# ─────────────────────────────────────────────────────────────────────────────


def test_start_success(client, mock_io):
    resp = client.post("/start", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["running_zone"] == 1

    with state_lock:
        assert 1 in state.active_runs
        assert state.running_zone == 1


def test_start_calls_io_open(client, mock_io):
    client.post("/start", json={"zone": 2, "duration": 30, "time_unit": "Sekunden"})

    cmd = mock_io.send_command.call_args[0][0]
    assert cmd.action == "open"
    assert cmd.zone == 2


def test_start_zone_out_of_range_low(client):
    resp = client.post("/start", json={"zone": 0, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_start_zone_out_of_range_high(client):
    with state_lock:
        state.max_valves = 3
    resp = client.post("/start", json={"zone": 4, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_start_duration_exceeds_max(client):
    with state_lock:
        state.hard_max_runtime_s = 600
    resp = client.post("/start", json={"zone": 1, "duration": 601, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_start_zone_already_running_returns_409(client, mock_io):
    set_running_zone(1, 60)
    resp = client.post("/start", json={"zone": 1, "duration": 30, "time_unit": "Sekunden"})
    assert resp.status_code == 409


def test_start_serial_mode_busy_returns_409(client, mock_io):
    with state_lock:
        state.parallel_enabled = False
    set_running_zone(1, 60)
    resp = client.post("/start", json={"zone": 2, "duration": 30, "time_unit": "Sekunden"})
    assert resp.status_code == 409


def test_start_hw_faulted_returns_423(client):
    with state_lock:
        state.hw_faulted = True
    resp = client.post("/start", json={"zone": 1, "duration": 30, "time_unit": "Sekunden"})
    assert resp.status_code == 423


def test_start_hw_error_returns_503(client, failing_io):
    resp = client.post("/start", json={"zone": 1, "duration": 30, "time_unit": "Sekunden"})
    assert resp.status_code == 503
    with state_lock:
        assert state.active_runs == {}


# ─────────────────────────────────────────────────────────────────────────────
# POST /stop
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_running_zone(client, mock_io):
    set_running_zone(1, 60)

    resp = client.post("/stop")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert 1 in data["stopped_zones"]

    with state_lock:
        assert state.active_runs == {}
        assert state.running_zone is None


def test_stop_adds_history_entry(client, mock_io):
    set_running_zone(1, 60)
    client.post("/stop")

    with state_lock:
        assert len(state.run_history) == 1
        assert state.run_history[0].zone == 1


def test_stop_nothing_running(client):
    resp = client.post("/stop")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_stop_clears_pause_state(client, mock_io):
    set_running_zone(1, 60)
    with state_lock:
        state.paused = True

    resp = client.post("/stop")
    assert resp.status_code == 200
    with state_lock:
        assert state.paused is False


def test_stop_calls_io_close(client, mock_io):
    set_running_zone(1, 60)
    client.post("/stop")

    cmd = mock_io.send_command.call_args[0][0]
    assert cmd.action == "close"
    assert cmd.zone == 1


def test_stop_hw_error_returns_503(client, failing_io):
    set_running_zone(1, 60)
    resp = client.post("/stop")
    assert resp.status_code == 503
    data = resp.json()
    assert "failed" in data["detail"]


def test_stop_multiple_zones(client, mock_io):
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "s", now, "manual", 60),
            2: ActiveRun(2, now + 60, "s", now, "manual", 60),
        }
        _sync_legacy_single_fields_locked()

    resp = client.post("/stop")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["stopped_zones"]) == {1, 2}

    with state_lock:
        assert state.active_runs == {}
        assert len(state.run_history) == 2


# ─────────────────────────────────────────────────────────────────────────────
# POST /pause
# ─────────────────────────────────────────────────────────────────────────────


def test_pause_running_zone(client, mock_io):
    set_running_zone(1, 60)

    resp = client.post("/pause")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with state_lock:
        assert state.paused is True
        ar = state.active_runs[1]
        assert ar.paused_at > 0
        assert ar.end_time == 0.0


def test_pause_calls_io_close(client, mock_io):
    set_running_zone(1, 60)
    client.post("/pause")

    cmd = mock_io.send_command.call_args[0][0]
    assert cmd.action == "close"
    assert cmd.zone == 1


def test_pause_nothing_running_returns_409(client):
    resp = client.post("/pause")
    assert resp.status_code == 409


def test_pause_already_paused_returns_409(client, mock_io):
    set_running_zone(1, 60)
    with state_lock:
        state.paused = True

    resp = client.post("/pause")
    assert resp.status_code == 409


def test_pause_hw_error_returns_503(client, failing_io):
    set_running_zone(1, 60)
    resp = client.post("/pause")
    assert resp.status_code == 503
    with state_lock:
        assert state.paused is False  # Kein State-Change bei HW-Fehler


# ─────────────────────────────────────────────────────────────────────────────
# POST /resume
# ─────────────────────────────────────────────────────────────────────────────


def test_resume_paused_zone(client, mock_io):
    set_running_zone(1, 60)

    # Pause einrichten
    with state_lock:
        now = time.monotonic()
        ar = state.active_runs[1]
        ar.remaining_s = 30
        ar.paused_at = now
        ar.end_time = 0.0
        state.paused = True
        state.queue_state_before_valve_pause = "läuft"

    resp = client.post("/resume")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    with state_lock:
        assert state.paused is False
        ar = state.active_runs[1]
        assert ar.end_time > 0
        assert ar.paused_at == 0.0


def test_resume_calls_io_open(client, mock_io):
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 30
        ar.paused_at = time.monotonic()
        ar.end_time = 0.0
        state.paused = True

    client.post("/resume")

    cmd = mock_io.send_command.call_args[0][0]
    assert cmd.action == "open"
    assert cmd.zone == 1


def test_resume_not_paused_returns_409(client, mock_io):
    set_running_zone(1, 60)
    resp = client.post("/resume")
    assert resp.status_code == 409


def test_resume_no_active_runs_returns_409(client):
    resp = client.post("/resume")
    assert resp.status_code == 409


def test_resume_hw_faulted_returns_423(client):
    set_running_zone(1, 60)
    with state_lock:
        state.paused = True
        state.hw_faulted = True

    resp = client.post("/resume")
    assert resp.status_code == 423


def test_resume_hw_error_returns_503(client, failing_io):
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 30
        ar.paused_at = time.monotonic()
        ar.end_time = 0.0
        state.paused = True

    resp = client.post("/resume")
    assert resp.status_code == 503
    with state_lock:
        assert state.paused is True  # Kein State-Change bei HW-Fehler


# ─────────────────────────────────────────────────────────────────────────────
# POST /fault/clear
# ─────────────────────────────────────────────────────────────────────────────


def test_fault_clear_success(client):
    with state_lock:
        state.hw_faulted = True
        state.hw_fault_reason = "close_failed_max_retries"
        state.hw_fault_zone = 2

    resp = client.post("/fault/clear")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleared"] is True

    with state_lock:
        assert state.hw_faulted is False
        assert state.hw_fault_reason == ""
        assert state.hw_fault_zone is None


def test_fault_clear_no_fault_returns_not_cleared(client):
    resp = client.post("/fault/clear")
    assert resp.status_code == 200
    assert resp.json()["cleared"] is False


def test_fault_clear_with_running_zones_returns_409(client, mock_io):
    set_running_zone(1, 60)
    with state_lock:
        state.hw_faulted = True

    resp = client.post("/fault/clear")
    assert resp.status_code == 409


# ─────────────────────────────────────────────────────────────────────────────
# Automation
# ─────────────────────────────────────────────────────────────────────────────


def test_get_automation_default_enabled(client):
    resp = client.get("/automation")
    assert resp.status_code == 200
    assert resp.json()["automation_enabled"] is True


def test_automation_disable(client):
    resp = client.post("/automation/disable")
    assert resp.status_code == 200
    with state_lock:
        assert state.automation_enabled is False


def test_automation_enable(client):
    with state_lock:
        state.automation_enabled = False

    resp = client.post("/automation/enable")
    assert resp.status_code == 200
    with state_lock:
        assert state.automation_enabled is True


def test_automation_toggle(client):
    resp = client.post("/automation/toggle")
    assert resp.status_code == 200
    with state_lock:
        assert state.automation_enabled is False

    resp = client.post("/automation/toggle")
    with state_lock:
        assert state.automation_enabled is True


def test_automation_enable_sets_block_run_key(client):
    """Nach Enable muss automation_block_run_key gesetzt sein (verhindert Doppelstart)."""
    with state_lock:
        state.automation_enabled = False

    client.post("/automation/enable")
    with state_lock:
        assert state.automation_block_run_key is not None


# ─────────────────────────────────────────────────────────────────────────────
# Parallel Mode
# ─────────────────────────────────────────────────────────────────────────────


def test_get_parallel_default(client):
    resp = client.get("/parallel")
    assert resp.status_code == 200
    data = resp.json()
    assert data["parallel_enabled"] is False
    assert "max_concurrent_valves" in data


def test_set_parallel_enable(client):
    resp = client.post("/parallel", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["parallel_enabled"] is True
    with state_lock:
        assert state.parallel_enabled is True


def test_set_parallel_disable(client):
    with state_lock:
        state.parallel_enabled = True

    resp = client.post("/parallel", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["parallel_enabled"] is False
