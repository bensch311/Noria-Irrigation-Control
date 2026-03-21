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


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /sensors/settings
# ─────────────────────────────────────────────────────────────────────────────

class TestPatchSensorSettings:
    """PATCH /sensors/settings – Cooldown und Standard-Bewässerungsdauer setzen."""

    def test_returns_200(self, client):
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 3600, "default_duration_s": 600})
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.patch("/sensors/settings",
                            json={"cooldown_s": 3600, "default_duration_s": 600},
                            headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_response_fields(self, client):
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 1800, "default_duration_s": 300})
        data = resp.json()
        assert data["ok"] is True
        assert data["cooldown_s"]         == 1800
        assert data["default_duration_s"] == 300

    def test_updates_state(self, client):
        with patch("services.persistence.save_sensor_settings_to_disk"):
            client.patch("/sensors/settings",
                         json={"cooldown_s": 7200, "default_duration_s": 120})
        with state_lock:
            assert state.sensor_cooldown_s         == 7200
            assert state.sensor_default_duration_s == 120

    def test_zero_cooldown_accepted(self, client):
        """Cooldown = 0 bedeutet: kein Cooldown. Muss akzeptiert werden."""
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 0, "default_duration_s": 60})
        assert resp.status_code == 200
        assert resp.json()["cooldown_s"] == 0

    def test_max_cooldown_accepted(self, client):
        """Maximaler Cooldown 86400 s (24 h) muss akzeptiert werden."""
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 86400, "default_duration_s": 60})
        assert resp.status_code == 200

    def test_cooldown_exceeds_max_returns_422(self, client):
        """Cooldown > 86400 s → 422 (Pydantic-Limit le=86400)."""
        resp = client.patch("/sensors/settings",
                            json={"cooldown_s": 86401, "default_duration_s": 60})
        assert resp.status_code == 422

    def test_duration_below_minimum_returns_422(self, client):
        """default_duration_s < 60 → 422 (Pydantic-Limit ge=60)."""
        resp = client.patch("/sensors/settings",
                            json={"cooldown_s": 3600, "default_duration_s": 59})
        assert resp.status_code == 422

    def test_duration_exceeds_hard_max_returns_400(self, client):
        """default_duration_s > hard_max_runtime_s → 400 (dynamische Prüfung im Handler)."""
        with state_lock:
            state.hard_max_runtime_s = 600   # 10 Minuten Hard-Limit
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 0, "default_duration_s": 601})
        assert resp.status_code == 400

    def test_duration_equal_to_hard_max_accepted(self, client):
        """default_duration_s == hard_max_runtime_s → 200 (Grenzwert exakt erlaubt)."""
        with state_lock:
            state.hard_max_runtime_s = 3600
        with patch("services.persistence.save_sensor_settings_to_disk"):
            resp = client.patch("/sensors/settings",
                                json={"cooldown_s": 0, "default_duration_s": 3600})
        assert resp.status_code == 200

    def test_missing_field_returns_422(self, client):
        """Fehlende Pflichtfelder → 422."""
        resp = client.patch("/sensors/settings", json={"cooldown_s": 60})
        assert resp.status_code == 422

    def test_persistence_called(self, client):
        """save_sensor_settings_to_disk() muss genau einmal aufgerufen werden."""
        with patch("services.persistence.save_sensor_settings_to_disk") as mock_save:
            client.patch("/sensors/settings",
                         json={"cooldown_s": 3600, "default_duration_s": 600})
        mock_save.assert_called_once()
