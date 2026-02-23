# tests/test_routes_control.py
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
from services.io_worker import IOResult, IOCommand
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
    # zone=0 verletzt das Pydantic-Constraint ge=1 -> 422 (Pydantic validiert vor dem Handler).
    # 422 ist korrekt: Strukturell ungueltige Eingabe wird von Pydantic abgelehnt.
    resp = client.post("/start", json={"zone": 0, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 422


def test_start_zone_out_of_range_high(client):
    resp = client.post("/start", json={"zone": 999, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 400


def test_start_negative_duration(client):
    resp = client.post("/start", json={"zone": 1, "duration": -1, "time_unit": "Sekunden"})
    assert resp.status_code == 422


def test_start_hw_error_returns_503(client, failing_io):
    resp = client.post("/start", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 503


def test_start_hw_faulted_returns_423(client, mock_io):
    with state_lock:
        state.hw_faulted = True
    resp = client.post("/start", json={"zone": 1, "duration": 60, "time_unit": "Sekunden"})
    assert resp.status_code == 423


# ─────────────────────────────────────────────────────────────────────────────
# POST /stop
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_success(client, mock_io):
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


def test_stop_hw_error_returns_503(client, failing_io):
    """
    Wenn close fehlschlägt → 503.
    KRITISCH: Zone muss in active_runs verbleiben (Hardware evtl. noch offen).
    Vor dem Bug-Fix wurde die Zone fälschlicherweise aus active_runs entfernt
    obwohl die Hardware-Close-Operation fehlgeschlagen war.
    """
    set_running_zone(1, 60)
    resp = client.post("/stop")
    assert resp.status_code == 503
    data = resp.json()
    assert "failed" in data["detail"]

    # SICHERHEITSINVARIANTE: Zone muss in active_runs bleiben
    with state_lock:
        assert 1 in state.active_runs, (
            "Zone muss nach fehlgeschlagenem close in active_runs bleiben "
            "(Hardware könnte noch offen sein)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# POST /stop – Teilfehler-Semantik (Kernfunktionalität des Bug-Fix)
# ─────────────────────────────────────────────────────────────────────────────


def _make_partial_fail_io(mock_io, failing_zone: int):
    """
    Konfiguriert mock_io so, dass close für failing_zone fehlschlägt,
    alle anderen Commands aber erfolgreich sind.
    """
    def _side_effect(cmd: IOCommand, timeout_s: float = 5.0) -> IOResult:
        if cmd.action == "close" and cmd.zone == failing_zone:
            return IOResult(success=False, zone=cmd.zone, error="GPIO Fehler", duration_ms=1.0)
        return IOResult(success=True, duration_ms=1.0)

    mock_io.send_command.side_effect = _side_effect
    return mock_io


def test_stop_partial_failure_returns_503(client, mock_io):
    """Wenn mindestens eine Zone nicht geschlossen werden kann → 503."""
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "Sekunden", now, "manual", 60),
            2: ActiveRun(2, now + 60, "Sekunden", now, "manual", 60),
        }
        _sync_legacy_single_fields_locked()

    _make_partial_fail_io(mock_io, failing_zone=2)

    resp = client.post("/stop")
    assert resp.status_code == 503


def test_stop_partial_failure_response_contains_stopped_and_failed(client, mock_io):
    """503-Response enthält welche Zonen gestoppt wurden und welche fehlschlugen."""
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "Sekunden", now, "manual", 60),
            2: ActiveRun(2, now + 60, "Sekunden", now, "manual", 60),
        }
        _sync_legacy_single_fields_locked()

    _make_partial_fail_io(mock_io, failing_zone=2)

    resp = client.post("/stop")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert 1 in detail["stopped"]
    assert any(f["zone"] == 2 for f in detail["failed"])


def test_stop_partial_failure_commits_only_successful_zones(client, mock_io):
    """
    SICHERHEITSINVARIANTE: Nur Zonen die hardware-seitig geschlossen wurden,
    werden aus active_runs entfernt. Die fehlgeschlagene Zone bleibt drin.
    """
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "Sekunden", now, "manual", 60),
            2: ActiveRun(2, now + 60, "Sekunden", now, "manual", 60),
        }
        _sync_legacy_single_fields_locked()

    _make_partial_fail_io(mock_io, failing_zone=2)

    client.post("/stop")

    with state_lock:
        assert 1 not in state.active_runs, "Erfolgreich gestoppte Zone 1 muss entfernt sein"
        assert 2 in state.active_runs, "Fehlgeschlagene Zone 2 muss in active_runs bleiben"


def test_stop_partial_failure_history_only_for_stopped_zones(client, mock_io):
    """
    History-Eintrag darf NUR für erfolgreich geschlossene Zonen angelegt werden.
    Für fehlgeschlagene Zonen keinen Eintrag → sonst wäre der Audit-Trail falsch.
    """
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "Sekunden", now, "manual", 60),
            2: ActiveRun(2, now + 60, "Sekunden", now, "manual", 60),
        }
        state.run_history = []
        _sync_legacy_single_fields_locked()

    _make_partial_fail_io(mock_io, failing_zone=2)

    client.post("/stop")

    with state_lock:
        assert len(state.run_history) == 1, "Genau ein History-Eintrag (nur Zone 1)"
        assert state.run_history[0].zone == 1


def test_stop_partial_failure_failed_zone_gets_immediate_end_time(client, mock_io):
    """
    Fehlgeschlagene Zone bekommt end_time in der Vergangenheit gesetzt,
    damit der Timer sie beim nächsten Durchlauf via Backoff-Retry aufgreift.
    end_time == 0.0 würde vom Timer ignoriert (0.0 ist falsy im end_time-Check).
    """
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "Sekunden", now, "manual", 60),
            2: ActiveRun(2, now + 60, "Sekunden", now, "manual", 60),
        }
        _sync_legacy_single_fields_locked()

    before_stop = time.monotonic()
    _make_partial_fail_io(mock_io, failing_zone=2)

    client.post("/stop")

    with state_lock:
        ar = state.active_runs.get(2)
        assert ar is not None
        # end_time muss in der Vergangenheit und nicht 0.0 sein
        assert ar.end_time != 0.0, "end_time darf nicht 0.0 sein (Timer würde Zone ignorieren)"
        assert ar.end_time < before_stop, "end_time muss in der Vergangenheit liegen (sofortiger Retry)"


def test_stop_partial_failure_paused_zone_gets_end_time_set(client, mock_io):
    """
    Spezialfall: pausierte Zone (end_time == 0.0, paused_at > 0) die close nicht
    übersteht, muss danach end_time > 0 haben – sonst findet der Timer sie nie.
    """
    with state_lock:
        now = time.monotonic()
        ar = ActiveRun(1, 0.0, "Sekunden", now - 30, "manual", 60)
        ar.paused_at = now - 10
        ar.remaining_s = 30
        state.active_runs = {1: ar}
        state.paused = True
        _sync_legacy_single_fields_locked()

    def _fail_all_close(cmd: IOCommand, timeout_s: float = 5.0) -> IOResult:
        if cmd.action == "close":
            return IOResult(success=False, zone=cmd.zone, error="GPIO", duration_ms=1.0)
        return IOResult(success=True, duration_ms=1.0)

    mock_io.send_command.side_effect = _fail_all_close

    before_stop = time.monotonic()
    client.post("/stop")

    with state_lock:
        ar = state.active_runs.get(1)
        assert ar is not None
        assert ar.end_time != 0.0, "end_time darf nicht 0.0 sein nach fehlgeschlagenem Stop"
        assert ar.end_time < before_stop, "end_time muss in der Vergangenheit liegen"
        assert ar.paused_at == 0.0, "paused_at muss nach Stop-Versuch geleert sein"


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
    with state_lock:
        assert state.parallel_enabled is False
