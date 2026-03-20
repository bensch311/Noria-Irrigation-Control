"""
Tests für api/routes_sensors.py

Getestet werden:
  GET /sensors/readings
    - Basis-Response-Struktur und Felder
    - Leerer Zustand (kein Polling gelaufen)
    - Readings aus State korrekt gespiegelt (moist / dry)
    - last_triggered: nur Zonen mit bekanntem Timestamp, elapsed_s positiv
    - zones_configured aus sensor_gpio_pins_by_zone
    - Konfigurationsfelder (cooldown_s, polling_interval_s)
    - Authentifizierung (401 ohne Key)

  GET /sensors/config
    - Basis-Response-Struktur und Felder
    - sim-Modus: gpio_config_valid=True, leere Validation-Listen
    - rpi_switch-Modus mit validen Pins
    - rpi_switch-Modus mit ungültigen Pins → gpio_config_valid=False
    - rpi_switch-Modus mit Duplikaten → gpio_config_valid=False
    - Konfigurationsfelder korrekt gespiegelt
    - Authentifizierung (401 ohne Key)
"""

import time
import pytest

from core.state import state, state_lock


# ─────────────────────────────────────────────────────────────────────────────
# GET /sensors/readings
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSensorReadings:
    def test_returns_200(self, client):
        resp = client.get("/sensors/readings")
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.get("/sensors/readings", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_response_has_required_fields(self, client):
        data = client.get("/sensors/readings").json()
        assert "sensor_driver" in data
        assert "zones_configured" in data
        assert "readings" in data
        assert "cooldown_s" in data
        assert "last_triggered" in data
        assert "polling_interval_s" in data

    def test_empty_readings_when_no_polling_ran(self, client):
        """Vor dem ersten Polling-Zyklus ist readings ein leeres Dict."""
        with state_lock:
            state.sensor_readings = {}
        data = client.get("/sensors/readings").json()
        assert data["readings"] == {}

    def test_readings_reflect_moist_zone(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24}
            state.sensor_readings = {1: False}  # feucht
        data = client.get("/sensors/readings").json()
        assert data["readings"]["1"] is False

    def test_readings_reflect_dry_zone(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {2: 25}
            state.sensor_readings = {2: True}  # trocken
        data = client.get("/sensors/readings").json()
        assert data["readings"]["2"] is True

    def test_readings_keys_are_strings(self, client):
        """JSON-Keys sind immer Strings – auch wenn Zone ein int ist."""
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24, 3: 26}
            state.sensor_readings = {1: False, 3: True}
        data = client.get("/sensors/readings").json()
        # JSON-Objekt-Keys sind immer Strings
        assert "1" in data["readings"]
        assert "3" in data["readings"]

    def test_zones_configured_from_gpio_pins(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24, 2: 25, 3: 26}
        data = client.get("/sensors/readings").json()
        assert sorted(data["zones_configured"]) == [1, 2, 3]

    def test_zones_configured_empty_when_no_pins(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {}
        data = client.get("/sensors/readings").json()
        assert data["zones_configured"] == []

    def test_zones_configured_sorted(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {3: 26, 1: 24, 2: 25}
        data = client.get("/sensors/readings").json()
        assert data["zones_configured"] == [1, 2, 3]

    def test_last_triggered_empty_when_no_triggers(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24}
            state.sensor_last_triggered = {}
        data = client.get("/sensors/readings").json()
        assert data["last_triggered"] == {}

    def test_last_triggered_contains_elapsed_seconds(self, client):
        """elapsed_s muss positiv sein und der vergangenen Zeit entsprechen."""
        now_m = time.monotonic()
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24}
            state.sensor_last_triggered = {1: now_m - 120.0}  # Vor 120s getriggert

        data = client.get("/sensors/readings").json()
        elapsed = data["last_triggered"]["1"]
        # Toleranz von 2s für Ausführungszeit des Tests
        assert 118.0 <= elapsed <= 125.0

    def test_last_triggered_only_for_configured_zones(self, client):
        """last_triggered enthält nur Zonen die auch in sensor_gpio_pins_by_zone sind."""
        now_m = time.monotonic()
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24}  # nur Zone 1 konfiguriert
            # Zone 2 hat einen Trigger-Timestamp, ist aber nicht konfiguriert
            state.sensor_last_triggered = {1: now_m - 60.0, 2: now_m - 30.0}

        data = client.get("/sensors/readings").json()
        assert "1" in data["last_triggered"]
        assert "2" not in data["last_triggered"]

    def test_last_triggered_omits_zones_with_no_prior_trigger(self, client):
        """Zonen ohne Trigger-Timestamp tauchen NICHT im last_triggered-Dict auf."""
        now_m = time.monotonic()
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24, 2: 25}
            state.sensor_last_triggered = {1: now_m - 60.0}  # Zone 2 noch nie getriggert

        data = client.get("/sensors/readings").json()
        assert "1" in data["last_triggered"]
        assert "2" not in data["last_triggered"]

    def test_cooldown_s_reflects_state(self, client):
        with state_lock:
            state.sensor_cooldown_s = 900
        data = client.get("/sensors/readings").json()
        assert data["cooldown_s"] == 900

    def test_polling_interval_s_reflects_state(self, client):
        with state_lock:
            state.sensor_polling_interval_s = 60
        data = client.get("/sensors/readings").json()
        assert data["polling_interval_s"] == 60

    def test_sensor_driver_name_in_response(self, client):
        """sensor_driver muss den Namen des aktiven Treibers enthalten."""
        data = client.get("/sensors/readings").json()
        # Im Test läuft SimSensorDriver (autouse-Fixture)
        assert data["sensor_driver"] == "sim"

    def test_multiple_zones_mixed_states(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {1: 24, 2: 25, 3: 26}
            state.sensor_readings = {1: False, 2: True, 3: False}
        data = client.get("/sensors/readings").json()
        assert data["readings"]["1"] is False
        assert data["readings"]["2"] is True
        assert data["readings"]["3"] is False

    def test_readings_none_in_state_returns_empty_dict(self, client):
        """sensor_readings=None (nie initialisiert) → leeres Dict, kein 500."""
        with state_lock:
            state.sensor_readings = None
        resp = client.get("/sensors/readings")
        assert resp.status_code == 200
        assert resp.json()["readings"] == {}

    def test_sensor_last_triggered_none_returns_empty_dict(self, client):
        """sensor_last_triggered=None → leeres last_triggered, kein 500."""
        with state_lock:
            state.sensor_last_triggered = None
        resp = client.get("/sensors/readings")
        assert resp.status_code == 200
        assert resp.json()["last_triggered"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# GET /sensors/config
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSensorConfig:
    def test_returns_200(self, client):
        resp = client.get("/sensors/config")
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.get("/sensors/config", headers={"X-API-Key": ""})
        assert resp.status_code == 401

    def test_response_has_required_fields(self, client):
        data = client.get("/sensors/config").json()
        assert "sensor_driver" in data
        assert "configured_driver_mode" in data
        assert "sensor_internal_pull_up" in data
        assert "zones_configured" in data
        assert "polling_interval_s" in data
        assert "cooldown_s" in data
        assert "default_duration_s" in data
        assert "gpio_config_valid" in data
        assert "invalid_pins" in data
        assert "duplicate_pins" in data

    def test_sim_mode_gpio_config_valid_true(self, client):
        """Im Sim-Modus ist gpio_config_valid immer True."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is True
        assert data["invalid_pins"] == []
        assert data["duplicate_pins"] == []

    def test_sim_mode_with_no_pins_valid(self, client):
        """Im Sim-Modus ohne Pins bleibt gpio_config_valid=True."""
        with state_lock:
            state.sensor_driver_mode = "sim"
            state.sensor_gpio_pins_by_zone = {}
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is True

    def test_rpi_switch_mode_valid_pins(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {1: 24, 2: 25}
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is True
        assert data["invalid_pins"] == []
        assert data["duplicate_pins"] == []

    def test_rpi_switch_mode_invalid_pin_out_of_range(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {1: 1}  # Pin 1 < 2 → ungültig
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is False
        assert len(data["invalid_pins"]) == 1
        assert data["invalid_pins"][0]["reason"] == "out_of_range_2_27"

    def test_rpi_switch_mode_duplicate_pins(self, client):
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {1: 24, 2: 24}  # Duplikat
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is False
        assert len(data["duplicate_pins"]) == 1
        assert data["duplicate_pins"][0]["pin"] == 24

    def test_configured_driver_mode_reflects_state(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        data = client.get("/sensors/config").json()
        assert data["configured_driver_mode"] == "sim"

    def test_internal_pull_up_false_by_default(self, client):
        with state_lock:
            state.sensor_internal_pull_up = False
        data = client.get("/sensors/config").json()
        assert data["sensor_internal_pull_up"] is False

    def test_internal_pull_up_true_reflected(self, client):
        with state_lock:
            state.sensor_internal_pull_up = True
        data = client.get("/sensors/config").json()
        assert data["sensor_internal_pull_up"] is True

    def test_zones_configured_from_gpio_pins(self, client):
        with state_lock:
            state.sensor_gpio_pins_by_zone = {2: 25, 4: 27}
        data = client.get("/sensors/config").json()
        assert sorted(data["zones_configured"]) == [2, 4]

    def test_polling_interval_s_reflects_state(self, client):
        with state_lock:
            state.sensor_polling_interval_s = 45
        data = client.get("/sensors/config").json()
        assert data["polling_interval_s"] == 45

    def test_cooldown_s_reflects_state(self, client):
        with state_lock:
            state.sensor_cooldown_s = 1200
        data = client.get("/sensors/config").json()
        assert data["cooldown_s"] == 1200

    def test_default_duration_s_reflects_state(self, client):
        with state_lock:
            state.sensor_default_duration_s = 600
        data = client.get("/sensors/config").json()
        assert data["default_duration_s"] == 600

    def test_sensor_driver_name_in_response(self, client):
        data = client.get("/sensors/config").json()
        # Im Test läuft SimSensorDriver (autouse-Fixture)
        assert data["sensor_driver"] == "sim"

    def test_rpi_switch_valid_boundary_pins(self, client):
        """Grenzwert-Pins 2 und 27 müssen als gültig durchgehen."""
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {1: 2, 2: 27}
        data = client.get("/sensors/config").json()
        assert data["gpio_config_valid"] is True

    def test_rpi_switch_empty_pins_no_crash(self, client):
        """rpi_switch ohne konfigurierte Pins → kein 500, gpio_config_valid=True
        (leeres Dict hat keine invaliden Pins)."""
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
            state.sensor_gpio_pins_by_zone = {}
        resp = client.get("/sensors/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gpio_config_valid"] is True


# ─────────────────────────────────────────────────────────────────────────────
# POST /sensors/sim/set
# ─────────────────────────────────────────────────────────────────────────────


class TestPostSensorsSimSet:
    def test_returns_200_in_sim_mode(self, client):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [], "moist_zones": []})
        assert resp.status_code == 200

    def test_requires_api_key(self, client):
        resp = client.post(
            "/sensors/sim/set",
            json={"dry_zones": [], "moist_zones": []},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    def test_returns_404_when_not_sim_mode(self, client):
        """Im rpi_switch-Modus muss der Endpunkt 404 zurückgeben."""
        with state_lock:
            state.sensor_driver_mode = "rpi_switch"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [1]})
        assert resp.status_code == 404

    def test_set_zone_dry(self, client, sim_sensor_driver):
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [1, 2]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert 1 in data["dry_zones"]
        assert 2 in data["dry_zones"]

    def test_set_zone_moist_clears_dry(self, client, sim_sensor_driver):
        """Zone erst trocken, dann wieder feucht setzen."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        sim_sensor_driver.set_zone_dry(3)

        resp = client.post("/sensors/sim/set", json={"moist_zones": [3]})
        assert resp.status_code == 200
        # Zone 3 darf nicht mehr in dry_zones sein
        assert 3 not in resp.json()["dry_zones"]

    def test_empty_body_is_valid(self, client):
        """Leerer Body (keine Zonen gesetzt) → 200, kein Fehler."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_overlap_dry_and_moist_returns_422(self, client):
        """Zone in dry_zones UND moist_zones → 422 Validation Error."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post(
            "/sensors/sim/set",
            json={"dry_zones": [1], "moist_zones": [1]},
        )
        assert resp.status_code == 422

    def test_zone_less_than_1_returns_422(self, client):
        """Zone 0 ist ungültig (Zonen beginnen bei 1)."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [0]})
        assert resp.status_code == 422

    def test_driver_state_actually_changed(self, client, sim_sensor_driver):
        """Prüft dass der SimSensorDriver-Singleton wirklich verändert wurde."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        client.post("/sensors/sim/set", json={"dry_zones": [5]})

        # Direkt am Driver prüfen – das ist der Singleton den sensor_engine liest
        reading = sim_sensor_driver.read(5)
        assert reading.needs_irrigation is True

    def test_multiple_zones_set_at_once(self, client, sim_sensor_driver):
        """Mehrere Zonen gleichzeitig auf trocken setzen."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [1, 2, 3]})
        assert resp.status_code == 200
        dry = resp.json()["dry_zones"]
        assert sorted(dry) == [1, 2, 3]

    def test_response_dry_zones_sorted(self, client):
        """dry_zones im Response sind sortiert."""
        with state_lock:
            state.sensor_driver_mode = "sim"
        resp = client.post("/sensors/sim/set", json={"dry_zones": [3, 1, 2]})
        assert resp.json()["dry_zones"] == [1, 2, 3]

    def test_sim_set_triggers_queue_item_on_next_poll(self, client, sim_sensor_driver):
        """End-to-End: Zone trocken setzen → sensor_engine_loop stellt Queue-Item ein.

        Dieser Test simuliert einen vollständigen Polling-Zyklus manuell, ohne
        den Background-Thread zu starten. Er prüft die Kernlogik:
          1. Zone via API auf trocken setzen
          2. _read_all_sensors() liest den gesetzten Zustand vom SimDriver
          3. _process_sensor_cycle_locked() stellt ein QueueItem ein

        Der Timer-Loop der das Item tatsächlich startet wird NICHT getestet –
        das ist der Zuständigkeit von test_timer.py und Integration-Tests.
        """
        import time as _time
        from core.state import QueueItem
        from services.sensor_engine import _read_all_sensors, _process_sensor_cycle_locked

        with state_lock:
            state.sensor_driver_mode = "sim"
            state.sensor_gpio_pins_by_zone = {1: 24}
            state.sensor_cooldown_s = 0       # kein Cooldown für den Test
            state.sensor_default_duration_s = 60
            state.sensor_readings = {}
            state.sensor_last_triggered = {}
            state.queue = []
            state.queue_state = "bereit"

        # Zone 1 auf trocken setzen
        resp = client.post("/sensors/sim/set", json={"dry_zones": [1]})
        assert resp.status_code == 200

        # Polling-Zyklus manuell durchführen
        zones = [1]
        readings = _read_all_sensors(zones)
        assert len(readings) == 1
        assert readings[0].needs_irrigation is True

        now_m = _time.monotonic()
        with state_lock:
            items_to_queue, _ = _process_sensor_cycle_locked(readings, now_m)
            if items_to_queue:
                state.queue.extend(items_to_queue)

        # Queue muss ein Item für Zone 1 enthalten
        with state_lock:
            queue_snapshot = list(state.queue)

        assert len(queue_snapshot) == 1
        item = queue_snapshot[0]
        assert item.zone == 1
        assert item.source == "sensor"
        assert item.duration == 60
