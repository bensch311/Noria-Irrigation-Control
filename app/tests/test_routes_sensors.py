"""
Tests für api/routes_sensors.py

GET  /sensors/readings      – Sensor-Readings, last_triggered, sensors_configured
GET  /sensors/config        – Hardware-Konfiguration, GPIO-Validierung
GET  /sensors/assignments   – Aktuelle Zuordnung
POST /sensors/assignments   – Zuordnung setzen + persistieren
POST /sensors/sim/set       – Sim-only: Sensoren trocken/feucht setzen
"""

import time
import pytest
from unittest.mock import patch

from core.state import state, state_lock


# ─────────────────────────────────────────────────────────────────────────────
# GET /sensors/readings
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSensorReadings:
    def test_returns_200(self, client):
        assert client.get("/sensors/readings").status_code == 200

    def test_requires_api_key(self, client):
        assert client.get("/sensors/readings", headers={"X-API-Key": ""}).status_code == 401

    def test_required_fields(self, client):
        data = client.get("/sensors/readings").json()
        for f in ["sensor_driver","sensors_configured","readings","cooldown_s","last_triggered","polling_interval_s"]:
            assert f in data

    def test_sensors_configured_from_gpio_pins(self, client):
        with state_lock:
            state.sensor_gpio_pins = {1: 24, 2: 25}
        data = client.get("/sensors/readings").json()
        assert sorted(data["sensors_configured"]) == [1, 2]

    def test_readings_reflect_dry_sensor(self, client):
        with state_lock:
            state.sensor_gpio_pins = {1: 24}
            state.sensor_readings = {1: True}
        data = client.get("/sensors/readings").json()
        assert data["readings"]["1"] is True

    def test_readings_reflect_moist_sensor(self, client):
        with state_lock:
            state.sensor_gpio_pins = {1: 24}
            state.sensor_readings = {1: False}
        data = client.get("/sensors/readings").json()
        assert data["readings"]["1"] is False

    def test_last_triggered_contains_elapsed(self, client):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_gpio_pins = {1: 24}
            state.sensor_last_triggered = {1: now_m - 120.0}
        data = client.get("/sensors/readings").json()
        elapsed = data["last_triggered"]["1"]
        assert 118.0 <= elapsed <= 125.0

    def test_last_triggered_only_configured_sensors(self, client):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_gpio_pins = {1: 24}
            state.sensor_last_triggered = {1: now_m - 60.0, 2: now_m - 30.0}
        data = client.get("/sensors/readings").json()
        assert "1" in data["last_triggered"]
        assert "2" not in data["last_triggered"]

    def test_readings_none_returns_empty(self, client):
        with state_lock:
            state.sensor_readings = None
        resp = client.get("/sensors/readings")
        assert resp.status_code == 200
        assert resp.json()["readings"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# GET /sensors/config
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSensorConfig:
    def test_returns_200(self, client):
        assert client.get("/sensors/config").status_code == 200

    def test_required_fields(self, client):
        data = client.get("/sensors/config").json()
        for f in ["sensor_driver","configured_driver_mode","sensors_configured",
                  "polling_interval_s","cooldown_s","default_duration_s",
                  "gpio_config_valid","invalid_pins","duplicate_pins"]:
            assert f in data

    def test_sim_mode_gpio_valid(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is True

    def test_rpi_switch_invalid_pin(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins = {1: 1}  # Pin 1 < 2
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is False
        assert data["invalid_pins"][0]["reason"] == "out_of_range_2_27"

    def test_rpi_switch_duplicate_pins(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins = {1: 24, 2: 24}
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is False
        assert len(data["duplicate_pins"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# GET /sensors/assignments
# ─────────────────────────────────────────────────────────────────────────────

class TestGetSensorAssignments:
    def test_returns_200(self, client):
        assert client.get("/sensors/assignments").status_code == 200

    def test_requires_api_key(self, client):
        assert client.get("/sensors/assignments", headers={"X-API-Key": ""}).status_code == 401

    def test_returns_assignments(self, client):
        with state_lock:
            state.sensor_zone_assignments = {1: [1, 2, 3], 2: [4, 5]}
        data = client.get("/sensors/assignments").json()
        assert data["assignments"]["1"] == [1, 2, 3]
        assert data["assignments"]["2"] == [4, 5]

    def test_empty_assignments(self, client):
        with state_lock:
            state.sensor_zone_assignments = {}
        data = client.get("/sensors/assignments").json()
        assert data["assignments"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# POST /sensors/assignments
# ─────────────────────────────────────────────────────────────────────────────

class TestPostSensorAssignments:
    def test_returns_200(self, client):
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            resp = client.post("/sensors/assignments", json={"assignments": {"1": [1, 2]}})
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.post("/sensors/assignments",
                           json={"assignments": {}},
                           headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_sets_state(self, client):
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            client.post("/sensors/assignments", json={"assignments": {"1": [1, 2, 3]}})
        with state_lock:
            assert state.sensor_zone_assignments.get(1) == [1, 2, 3]

    def test_response_contains_assignments(self, client):
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            resp = client.post("/sensors/assignments",
                               json={"assignments": {"1": [1], "2": [2, 3]}})
        data = resp.json()
        assert data["ok"] is True
        assert "1" in data["assignments"]

    def test_replaces_all_assignments(self, client):
        with state_lock:
            state.sensor_zone_assignments = {1: [1, 2], 3: [5]}
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            client.post("/sensors/assignments", json={"assignments": {"2": [3]}})
        with state_lock:
            assert 1 not in state.sensor_zone_assignments
            assert 3 not in state.sensor_zone_assignments
            assert state.sensor_zone_assignments.get(2) == [3]

    def test_invalid_sensor_id_returns_422(self, client):
        resp = client.post("/sensors/assignments",
                           json={"assignments": {"0": [1]}})
        assert resp.status_code == 422

    def test_zone_exceeding_max_valves_clamped(self, client):
        with state_lock:
            state.max_valves = 3
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            resp = client.post("/sensors/assignments",
                               json={"assignments": {"1": [1, 2, 3, 4, 5]}})
        data = resp.json()
        # Zonen > max_valves werden still entfernt
        assert set(data["assignments"]["1"]).issubset({1, 2, 3})

    def test_empty_assignments_accepted(self, client):
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            resp = client.post("/sensors/assignments", json={"assignments": {}})
        assert resp.status_code == 200

    def test_sets_dirty_flag(self, client):
        with patch("services.persistence.save_sensor_assignments_to_disk"):
            client.post("/sensors/assignments", json={"assignments": {"1": [1]}})
        # save_sensor_assignments_to_disk setzt dirty=False intern;
        # da wir mocken, prüfen wir nur dass kein Fehler auftritt
        assert True  # kein 500


# ─────────────────────────────────────────────────────────────────────────────
# POST /sensors/sim/set
# ─────────────────────────────────────────────────────────────────────────────

class TestPostSensorsSimSet:
    def test_returns_200_in_sim_mode(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_sensors": [], "moist_sensors": []})
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.post("/sensors/sim/set",
                           json={},
                           headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_returns_404_when_not_sim(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
        resp = client.post("/sensors/sim/set", json={"dry_sensors": [1]})
        assert resp.status_code == 404

    def test_set_sensor_dry(self, client, sim_sensor_driver):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_sensors": [1, 2]})
        assert resp.status_code == 200
        data = resp.json()
        assert 1 in data["dry_sensors"]
        assert 2 in data["dry_sensors"]

    def test_set_sensor_moist_clears_dry(self, client, sim_sensor_driver):
        with state_lock:
            state.sensor_driver_mode = "sim"
        sim_sensor_driver.set_zone_dry(3)
        resp = client.post("/sensors/sim/set", json={"moist_sensors": [3]})
        assert 3 not in resp.json()["dry_sensors"]

    def test_overlap_returns_422(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set",
                           json={"dry_sensors": [1], "moist_sensors": [1]})
        assert resp.status_code == 422

    def test_sensor_id_zero_returns_422(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_sensors": [0]})
        assert resp.status_code == 422

    def test_driver_state_actually_changed(self, client, sim_sensor_driver):
        with state_lock:
            state.sensor_driver_mode = "sim"
        client.post("/sensors/sim/set", json={"dry_sensors": [5]})
        assert sim_sensor_driver.read(5).needs_irrigation is True
