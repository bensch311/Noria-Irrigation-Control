"""
Tests für api/routes_health.py

Getestet werden:
  GET /health – Basis-Response, Ventilstatus, Queue-Info, GPIO-Validierung
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
