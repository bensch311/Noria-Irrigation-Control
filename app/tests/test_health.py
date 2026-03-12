"""
Tests für api/routes_health.py

Getestet werden:
  GET /health – Basis-Response, Ventilstatus, Queue-Info, GPIO-Validierung,
                hw_fault-Felder, Neustart-Erkennungs-Felder
"""

import pytest

from core.state import state, state_lock
from tests.conftest import set_running_zone


def test_health_basic_structure(client):
    resp = client.get("/health")
    assert resp.status_code == 200

    data = resp.json()
    assert data["ok"] is True
    assert data["service"] == "irrigation"
    assert "ts" in data
    assert "running_zones" in data
    assert "queue_length" in data
    assert "valves" in data
    assert "parallel_enabled" in data
    assert "max_concurrent_valves" in data
    # hw_fault-Felder müssen immer vorhanden sein (auch im Normalbetrieb)
    assert "hw_faulted" in data
    assert "hw_fault_reason" in data
    assert "hw_fault_zone" in data
    assert "hw_fault_since" in data
    # Neustart-Erkennungs-Felder müssen immer vorhanden sein
    assert "unclean_restart" in data
    assert "restart_detected_at" in data


def test_health_idle_running_zones_empty(client):
    resp = client.get("/health")
    assert resp.json()["running_zones"] == []


def test_health_with_running_zone(client):
    set_running_zone(1, 60)

    resp = client.get("/health")
    data = resp.json()
    assert 1 in data["running_zones"]


def test_health_queue_length(client):
    from core.state import QueueItem
    with state_lock:
        state.queue = [
            QueueItem(zone=1, duration=60, time_unit="Sekunden", source="queue"),
            QueueItem(zone=2, duration=60, time_unit="Sekunden", source="queue"),
        ]

    resp = client.get("/health")
    assert resp.json()["queue_length"] == 2


def test_health_sim_mode_gpio_config_valid(client):
    """
    Im Sim-Modus ist gpio_config_valid immer True – unabhaengig von missing_zones.
    missing_zones wird technisch korrekt befuellt (zeigt Zonen ohne GPIO-Pin),
    aber im Sim-Modus zaehlt das nicht als Fehler (gpio_config_valid bleibt True).
    """
    with state_lock:
        state.valve_driver_mode = "sim"

    resp = client.get("/health")
    data = resp.json()
    assert data["valves"]["valve_driver"] == "sim"
    assert data["valves"]["gpio_config_valid"] is True
    # missing_zones im Sim-Modus ist nur informativ, kein Fehlerindikator


def test_health_valves_section_content(client):
    with state_lock:
        state.max_valves = 4
        state.valve_driver_mode = "sim"

    resp = client.get("/health")
    valves = resp.json()["valves"]
    assert valves["max_valves"] == 4
    assert "configured_zones" in valves
    assert "invalid_pins" in valves
    assert "duplicate_pins" in valves


# ─────────────────────────────────────────────────────────────────────────────
# hw_fault-Felder und ok-Semantik
# ─────────────────────────────────────────────────────────────────────────────

def test_health_no_fault_ok_true_and_defaults(client):
    """Im Normalbetrieb (kein Fault): ok=True, hw_fault-Felder mit sicheren Defaults."""
    resp = client.get("/health")
    data = resp.json()
    assert data["ok"] is True
    assert data["hw_faulted"] is False
    assert data["hw_fault_reason"] == ""
    assert data["hw_fault_zone"] is None
    assert data["hw_fault_since"] == ""


def test_health_hw_faulted_sets_ok_false(client):
    """
    Bei aktivem HW-Fault muss ok=False zurückgegeben werden.

    HTTP 200 bleibt erhalten – der ok-Wert im Body ist das eigentliche
    Monitoring-Signal. ok=False signalisiert: System hat ein Problem,
    das Operator-Aktion erfordert.
    """
    with state_lock:
        state.hw_faulted = True
        state.hw_fault_reason = "close_failed_max_retries"
        state.hw_fault_zone = 3
        state.hw_fault_since = "2024-06-01T08:00:00+02:00"

    resp = client.get("/health")
    assert resp.status_code == 200  # HTTP 200 immer – Endpoint selbst ist erreichbar
    assert resp.json()["ok"] is False


def test_health_hw_fault_fields_populated(client):
    """hw_fault_*-Felder müssen den gesetzten State korrekt widerspiegeln."""
    with state_lock:
        state.hw_faulted = True
        state.hw_fault_reason = "close_failed_max_retries"
        state.hw_fault_zone = 2
        state.hw_fault_since = "2024-06-01T08:00:00+02:00"

    data = client.get("/health").json()
    assert data["hw_faulted"] is True
    assert data["hw_fault_reason"] == "close_failed_max_retries"
    assert data["hw_fault_zone"] == 2
    assert data["hw_fault_since"] == "2024-06-01T08:00:00+02:00"


def test_health_ok_recovers_after_fault_cleared(client):
    """Nach /fault/clear muss ok wieder True sein."""
    with state_lock:
        state.hw_faulted = True

    assert client.get("/health").json()["ok"] is False

    with state_lock:
        state.hw_faulted = False
        state.hw_fault_reason = ""
        state.hw_fault_zone = None
        state.hw_fault_since = ""

    assert client.get("/health").json()["ok"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Neustart-Erkennungs-Felder (unclean_restart, restart_detected_at)
# ─────────────────────────────────────────────────────────────────────────────

def test_health_unclean_restart_default_false(client):
    """Im Normalbetrieb: unclean_restart=False, restart_detected_at=''."""
    resp = client.get("/health")
    data = resp.json()
    assert data["unclean_restart"] is False
    assert data["restart_detected_at"] == ""


def test_health_unclean_restart_set_true(client):
    """Wenn unclean_restart gesetzt: korrekte Werte im Response."""
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2024-06-01T08:00:00+02:00"

    data = client.get("/health").json()
    assert data["unclean_restart"] is True
    assert data["restart_detected_at"] == "2024-06-01T08:00:00+02:00"


def test_health_unclean_restart_does_not_affect_ok(client):
    """unclean_restart beeinflusst ok NICHT – ok hängt nur von hw_faulted ab."""
    with state_lock:
        state.unclean_restart = True
        state.hw_faulted = False

    data = client.get("/health").json()
    # ok=True obwohl unclean_restart=True: nur hw_faulted entscheidet über ok
    assert data["ok"] is True
    assert data["unclean_restart"] is True


def test_health_unclean_restart_cleared_after_ack(client):
    """Nach POST /system/ack-restart: unclean_restart=False im nächsten /health."""
    with state_lock:
        state.unclean_restart = True
        state.restart_detected_at = "2024-06-01T08:00:00+02:00"

    # Vor ACK: Flag gesetzt
    assert client.get("/health").json()["unclean_restart"] is True

    # ACK senden
    ack_resp = client.post("/system/ack-restart")
    assert ack_resp.status_code == 200

    # Nach ACK: Flag gelöscht
    data = client.get("/health").json()
    assert data["unclean_restart"] is False
    assert data["restart_detected_at"] == ""
