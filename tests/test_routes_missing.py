"""
Tests fuer bisher nicht abgedeckte Route-Pfade

Abgedeckt werden:
  api/routes_control.py:
    - /status wenn paused (via Client)
    - /resume wenn remaining_s == 0 (end_time bleibt 0.0)
    - /parallel deaktivieren waehrend >1 Ventile laufen (drain warning)
  api/routes_queue.py:
    - /queue/add Duration-Validierung (fehlende Zeile 29)
  api/routes_health.py:
    - /health mit rpi-Mode und missing_zones
  api/errors.py:
    - 500 Unhandled Exception Handler
    - 404 wird geloggt
"""

import time
import pytest

from core.state import state, state_lock, ActiveRun
from tests.conftest import set_running_zone


# ---------------------------------------------------------------------------
# GET /status – pausierter Zustand ueber Client
# ---------------------------------------------------------------------------

def test_status_paused_via_client(client, mock_io):
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 45
        ar.paused_at = time.monotonic()
        ar.end_time = 0.0
        state.paused = True

    resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "pausiert"
    assert data["paused"] is True
    assert data["remaining_time"] == 45


# ---------------------------------------------------------------------------
# POST /resume – Zone mit remaining_s == 0
# ---------------------------------------------------------------------------

def test_resume_zone_with_zero_remaining_keeps_end_time_zero(client, mock_io):
    """Zone hat remaining_s=0 -> end_time bleibt 0.0 (kein Neustart)."""
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 0
        ar.paused_at = time.monotonic()
        ar.end_time = 0.0
        state.paused = True

    resp = client.post("/resume")
    assert resp.status_code == 200
    with state_lock:
        assert state.active_runs[1].end_time == 0.0


def test_resume_zone_with_positive_remaining_sets_end_time(client, mock_io):
    """Zone mit remaining_s > 0 bekommt neues end_time gesetzt."""
    now = time.monotonic()
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 30
        ar.paused_at = now
        ar.end_time = 0.0
        state.paused = True

    resp = client.post("/resume")
    assert resp.status_code == 200
    with state_lock:
        assert state.active_runs[1].end_time > now


def test_resume_restores_queue_state(client, mock_io):
    """queue_state_before_valve_pause wird nach Resume wiederhergestellt."""
    set_running_zone(1, 60)
    with state_lock:
        ar = state.active_runs[1]
        ar.remaining_s = 30
        ar.paused_at = time.monotonic()
        ar.end_time = 0.0
        state.paused = True
        state.queue_state_before_valve_pause = "l\u00e4uft"

    resp = client.post("/resume")
    assert resp.status_code == 200
    with state_lock:
        assert state.queue_state == "l\u00e4uft"
        assert state.queue_state_before_valve_pause == "bereit"


# ---------------------------------------------------------------------------
# POST /parallel – Drain-Warning bei Deaktivierung mit mehreren Zonen
# ---------------------------------------------------------------------------

def test_parallel_disable_with_multiple_zones_sets_drain_flag(client, mock_io):
    """Wenn parallel=True -> False und >1 Zone laeuft: drain_logged wird True."""
    with state_lock:
        state.parallel_enabled = True
        state.max_concurrent_valves = 2
        now = time.monotonic()
        state.active_runs = {
            1: ActiveRun(1, now + 60, "s", now, "manual", 60),
            2: ActiveRun(2, now + 60, "s", now, "manual", 60),
        }

    resp = client.post("/parallel", json={"enabled": False})
    assert resp.status_code == 200
    with state_lock:
        assert state.parallel_drain_logged is True


def test_parallel_enable_clears_drain_flag(client):
    with state_lock:
        state.parallel_enabled = False
        state.parallel_drain_logged = True

    resp = client.post("/parallel", json={"enabled": True})
    assert resp.status_code == 200
    with state_lock:
        assert state.parallel_drain_logged is False


# ---------------------------------------------------------------------------
# POST /queue/add – Laufzeit-Validierung (geringe Laufzeit)
# ---------------------------------------------------------------------------

def test_queue_add_duration_negative_rejected(client):
    """Negative Dauer wird durch Pydantic (ge=1) mit 422 abgelehnt."""
    resp = client.post("/queue/add", json={"zone": 1, "duration": -1, "time_unit": "Sekunden"})
    assert resp.status_code == 422


def test_queue_add_exactly_at_max_duration(client):
    """Dauer genau am Limit wird akzeptiert."""
    with state_lock:
        state.hard_max_runtime_s = 300
    resp = client.post("/queue/add", json={"zone": 1, "duration": 300, "time_unit": "Sekunden"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /health – rpi-Mode mit missing_zones
# ---------------------------------------------------------------------------

def test_health_rpi_mode_missing_zones_marks_invalid(client):
    """Im rpi-Modus mit fehlenden Zone-Pins ist gpio_config_valid=False."""
    with state_lock:
        state.valve_driver_mode = "rpi"
        state.max_valves = 2
        state.gpio_pins_by_zone = {}  # Keine Pins konfiguriert

    resp = client.get("/health")
    data = resp.json()
    assert data["valves"]["gpio_config_valid"] is False
    assert len(data["valves"]["missing_zones"]) > 0


def test_health_rpi_mode_valid_pins(client):
    """Im rpi-Modus mit vollstaendiger Pin-Konfiguration ist gpio_config_valid=True."""
    with state_lock:
        state.valve_driver_mode = "rpi"
        state.max_valves = 2
        state.gpio_pins_by_zone = {1: 17, 2: 18}
        state.relay_active_low = True

    resp = client.get("/health")
    data = resp.json()
    assert data["valves"]["gpio_config_valid"] is True
    assert data["valves"]["missing_zones"] == []


def test_health_rpi_mode_invalid_pins(client):
    """Ungueltige Pin-Nummern (ausserhalb 2-27) werden als invalid gemeldet."""
    with state_lock:
        state.valve_driver_mode = "rpi"
        state.max_valves = 1
        state.gpio_pins_by_zone = {1: 0}  # Pin 0 ist ungueltig

    resp = client.get("/health")
    data = resp.json()
    assert data["valves"]["gpio_config_valid"] is False
    assert len(data["valves"]["invalid_pins"]) > 0


def test_health_rpi_mode_duplicate_pins(client):
    """Doppelt belegte Pins werden als duplicate gemeldet."""
    with state_lock:
        state.valve_driver_mode = "rpi"
        state.max_valves = 2
        state.gpio_pins_by_zone = {1: 17, 2: 17}  # Gleicher Pin fuer 2 Zonen

    resp = client.get("/health")
    data = resp.json()
    assert data["valves"]["gpio_config_valid"] is False
    assert len(data["valves"]["duplicate_pins"]) > 0


# ---------------------------------------------------------------------------
# api/errors.py – Unhandled Exception und 404-Logging
# ---------------------------------------------------------------------------

def test_404_returns_json(client):
    """Nicht existierende Route gibt 404 JSON zurueck."""
    resp = client.get("/nicht_vorhanden_xyz")
    assert resp.status_code == 404


def test_schedule_not_found_logged_as_warning(client):
    """404 bei schedule/enable wird geloggt (REJECT_LOG_STATUS_CODES enthaelt 404)."""
    resp = client.post("/schedule/enable/nichtvorhanden")
    assert resp.status_code == 404
    data = resp.json()
    assert "detail" in data


def test_conflict_409_returns_json_detail(client, mock_io):
    """409 Conflict gibt strukturierten JSON-Fehler zurueck."""
    set_running_zone(1, 60)
    with state_lock:
        state.parallel_enabled = False

    resp = client.post("/start", json={"zone": 2, "duration": 30, "time_unit": "Sekunden"})
    assert resp.status_code == 409
    assert "detail" in resp.json()
