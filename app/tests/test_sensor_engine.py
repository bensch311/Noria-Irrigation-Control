"""
Tests für services/sensor_engine.py

Getestet werden:
  - _process_sensor_cycle_locked  (Kern-Logik, direkt unter state_lock testbar)
    - Auslöse-Bedingungen: moist / dry / cooldown / already_active / already_queued /
                           hw_faulted / queue_full
    - State-Updates: sensor_readings, sensor_last_triggered
    - Queue-Item-Inhalt: zone, duration, source, time_unit
    - Mehrere Zonen: unabhängige Verarbeitung
  - _read_all_sensors             (Fehlerresistenz, Sensor-Treiber-Integration)
    - Alle Zonen werden gelesen
    - Ausgefallene Zonen werden übersprungen, Rest läuft durch

Hinweis zur Test-Strategie:
  _process_sensor_cycle_locked() ist eine reine State-Manipulations-Funktion
  die unter state_lock aufgerufen wird. Sie ist vollständig ohne Threads testbar.
  sensor_engine_loop() als Thread wird NICHT direkt getestet – die Logik ist
  vollständig in _process_sensor_cycle_locked() gekapselt und dort abgedeckt.
"""

import time
import pytest
from unittest.mock import patch, MagicMock

from core.state import state, state_lock, QueueItem
from core.config import MAX_QUEUE_ITEMS
from services.sensor_driver import SensorReading, SensorDriverError, SimSensorDriver, set_sensor_driver
from services.sensor_engine import _process_sensor_cycle_locked, _read_all_sensors


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _make_reading(
    zone: int,
    needs_irrigation: bool,
    raw_gpio_value: int | None = None,
    driver_name: str = "sim",
) -> SensorReading:
    """Factory für SensorReading-Testobjekte."""
    if raw_gpio_value is None:
        raw_gpio_value = 0 if needs_irrigation else 1
    return SensorReading(
        zone=zone,
        needs_irrigation=needs_irrigation,
        raw_gpio_value=raw_gpio_value,
        timestamp=time.monotonic(),
        driver_name=driver_name,
    )


def _run_cycle(
    readings: list[SensorReading],
    now_m: float | None = None,
) -> tuple[list[QueueItem], dict[int, bool]]:
    """Führt _process_sensor_cycle_locked unter state_lock aus."""
    if now_m is None:
        now_m = time.monotonic()
    with state_lock:
        return _process_sensor_cycle_locked(readings, now_m)


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – Grundverhalten
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleBasic:
    def test_moist_zone_produces_no_queue_item(self):
        readings = [_make_reading(zone=1, needs_irrigation=False)]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_dry_zone_produces_queue_item(self):
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert len(items) == 1

    def test_queue_item_source_is_sensor(self):
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items[0].source == "sensor"

    def test_queue_item_has_correct_zone(self):
        readings = [_make_reading(zone=3, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items[0].zone == 3

    def test_queue_item_uses_sensor_default_duration(self):
        with state_lock:
            state.sensor_default_duration_s = 450
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items[0].duration == 450

    def test_queue_item_time_unit_is_sekunden(self):
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items[0].time_unit == "Sekunden"

    def test_multiple_dry_zones_produce_multiple_items(self):
        readings = [
            _make_reading(zone=1, needs_irrigation=True),
            _make_reading(zone=2, needs_irrigation=True),
            _make_reading(zone=3, needs_irrigation=False),
        ]
        items, _ = _run_cycle(readings)
        assert len(items) == 2
        zones = {item.zone for item in items}
        assert zones == {1, 2}

    def test_empty_readings_produces_no_items(self):
        items, new_readings = _run_cycle([])
        assert items == []
        assert new_readings == {}


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – sensor_readings State-Update
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleReadingsUpdate:
    def test_sensor_readings_updated_for_moist_zone(self):
        readings = [_make_reading(zone=2, needs_irrigation=False)]
        _run_cycle(readings)
        with state_lock:
            assert state.sensor_readings is not None
            assert state.sensor_readings.get(2) is False

    def test_sensor_readings_updated_for_dry_zone(self):
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        _run_cycle(readings)
        with state_lock:
            assert state.sensor_readings.get(1) is True

    def test_sensor_readings_updated_even_when_skipped(self):
        """Readings werden aktualisiert, auch wenn kein Item eingestellt wird."""
        # Cooldown setzen damit Zone übersprungen wird
        now_m = time.monotonic()
        with state_lock:
            state.sensor_last_triggered = {1: now_m}  # gerade erst getriggert
            state.sensor_cooldown_s = 600

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        _run_cycle(readings, now_m=now_m + 1.0)  # Nur 1s nach letztem Trigger

        with state_lock:
            # Kein Item, aber Readings müssen trotzdem aktualisiert sein
            assert state.sensor_readings.get(1) is True

    def test_new_readings_dict_returned_correctly(self):
        readings = [
            _make_reading(zone=1, needs_irrigation=True),
            _make_reading(zone=2, needs_irrigation=False),
        ]
        _, new_readings = _run_cycle(readings)
        assert new_readings[1] is True
        assert new_readings[2] is False

    def test_sensor_readings_initialised_if_none(self):
        with state_lock:
            state.sensor_readings = None
        readings = [_make_reading(zone=1, needs_irrigation=False)]
        _run_cycle(readings)
        with state_lock:
            assert state.sensor_readings is not None


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – Cooldown
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleCooldown:
    def test_dry_zone_within_cooldown_produces_no_item(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 60.0}  # Vor 60s getriggert

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings, now_m=now_m)
        assert items == []

    def test_dry_zone_after_cooldown_produces_item(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 700.0}  # Vor 700s → abgelaufen

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings, now_m=now_m)
        assert len(items) == 1

    def test_zone_with_no_prior_trigger_is_not_blocked(self):
        """Kein vorheriger Trigger → Cooldown gilt als abgelaufen."""
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {}  # Kein Eintrag für Zone 1

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert len(items) == 1

    def test_last_triggered_set_when_item_created(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_last_triggered = {}

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        _run_cycle(readings, now_m=now_m)

        with state_lock:
            assert 1 in state.sensor_last_triggered
            assert abs(state.sensor_last_triggered[1] - now_m) < 0.01

    def test_last_triggered_not_set_when_skipped_by_cooldown(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            original_ts = now_m - 60.0
            state.sensor_last_triggered = {1: original_ts}

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        _run_cycle(readings, now_m=now_m)

        with state_lock:
            # Timestamp darf nicht verändert worden sein
            assert abs(state.sensor_last_triggered[1] - original_ts) < 0.01

    def test_cooldown_zero_means_no_cooldown(self):
        """Cooldown von 0s = kein Cooldown, sofort wieder triggerbar."""
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {1: now_m - 0.001}  # Gerade erst

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings, now_m=now_m)
        assert len(items) == 1

    def test_cooldown_per_zone_independent(self):
        """Cooldown gilt pro Zone, nicht global."""
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {
                1: now_m - 60.0,   # Zone 1: noch im Cooldown
                2: now_m - 700.0,  # Zone 2: Cooldown abgelaufen
            }

        readings = [
            _make_reading(zone=1, needs_irrigation=True),
            _make_reading(zone=2, needs_irrigation=True),
        ]
        items, _ = _run_cycle(readings, now_m=now_m)
        zones = {item.zone for item in items}
        assert 1 not in zones
        assert 2 in zones


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – Überspringen-Bedingungen
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleSkipConditions:
    def test_dry_zone_already_active_produces_no_item(self):
        """Zone in active_runs → kein Queue-Item."""
        from core.state import ActiveRun
        with state_lock:
            now_m = time.monotonic()
            state.active_runs = {
                1: ActiveRun(
                    zone=1,
                    end_time=now_m + 60,
                    time_unit="Sekunden",
                    started_at=now_m,
                    started_source="manual",
                    started_planned_s=60,
                )
            }

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_dry_zone_already_in_queue_any_source_produces_no_item(self):
        """Zone bereits in Queue (egal welche Quelle) → kein Item."""
        with state_lock:
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="schedule")]

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_dry_zone_already_queued_by_sensor_no_double_entry(self):
        """Zone bereits von Sensor in Queue → kein Doppel-Eintrag."""
        with state_lock:
            state.queue = [QueueItem(zone=2, duration=300, time_unit="Sekunden", source="sensor")]

        readings = [_make_reading(zone=2, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_hw_faulted_blocks_all_items(self):
        """Bei hw_faulted=True darf kein Queue-Item erzeugt werden."""
        with state_lock:
            state.hw_faulted = True

        readings = [
            _make_reading(zone=1, needs_irrigation=True),
            _make_reading(zone=2, needs_irrigation=True),
        ]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_hw_faulted_does_not_block_readings_update(self):
        """hw_faulted blockiert Items, aber Readings werden trotzdem aktualisiert."""
        with state_lock:
            state.hw_faulted = True

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        _, new_readings = _run_cycle(readings)
        assert new_readings.get(1) is True

    def test_queue_full_skips_item(self):
        """Queue-Limit erreicht → kein neues Item."""
        with state_lock:
            state.queue = [
                QueueItem(zone=z, duration=60, time_unit="Sekunden", source="manual")
                for z in range(1, MAX_QUEUE_ITEMS + 1)
            ]

        # Zone MAX_QUEUE_ITEMS + 1 ist nicht in der Queue
        readings = [_make_reading(zone=MAX_QUEUE_ITEMS + 1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert items == []

    def test_other_zone_in_queue_does_not_block_this_zone(self):
        """Nur die eigene Zone in Queue zählt, nicht andere Zonen."""
        with state_lock:
            state.queue = [QueueItem(zone=2, duration=60, time_unit="Sekunden", source="sensor")]

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        assert len(items) == 1
        assert items[0].zone == 1


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – Logging
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleLogging:
    def test_trigger_logged_on_item_creation(self):
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings)
        trigger_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_trigger"
        ]
        assert len(trigger_events) == 1
        assert trigger_events[0].kwargs["zone"] == 1

    def test_cooldown_skip_logged(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 10.0}

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings, now_m=now_m)
        skip_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_skip_cooldown"
        ]
        assert len(skip_events) == 1

    def test_already_active_skip_logged(self):
        from core.state import ActiveRun
        with state_lock:
            now_m = time.monotonic()
            state.active_runs = {
                1: ActiveRun(
                    zone=1, end_time=now_m + 60, time_unit="Sekunden",
                    started_at=now_m, started_source="manual", started_planned_s=60,
                )
            }
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings)
        skip_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_skip_already_active"
        ]
        assert len(skip_events) == 1

    def test_already_queued_skip_logged(self):
        with state_lock:
            state.queue = [QueueItem(zone=3, duration=60, time_unit="Sekunden", source="schedule")]

        readings = [_make_reading(zone=3, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings)
        skip_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_skip_already_queued"
        ]
        assert len(skip_events) == 1

    def test_hw_faulted_skip_logged(self):
        with state_lock:
            state.hw_faulted = True
        readings = [_make_reading(zone=1, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings)
        skip_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_skip_hw_faulted"
        ]
        assert len(skip_events) == 1

    def test_queue_full_skip_logged(self):
        with state_lock:
            state.queue = [
                QueueItem(zone=z, duration=60, time_unit="Sekunden", source="manual")
                for z in range(1, MAX_QUEUE_ITEMS + 1)
            ]
        readings = [_make_reading(zone=MAX_QUEUE_ITEMS + 1, needs_irrigation=True)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle(readings)
        skip_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_skip_queue_full"
        ]
        assert len(skip_events) == 1


# ─────────────────────────────────────────────────────────────────────────────
# _process_sensor_cycle_locked – Grenzfälle
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessSensorCycleEdgeCases:
    def test_sensor_last_triggered_initialised_if_none(self):
        with state_lock:
            state.sensor_last_triggered = None

        readings = [_make_reading(zone=1, needs_irrigation=True)]
        items, _ = _run_cycle(readings)
        # Zone sollte trotzdem getriggert werden (sensor_last_triggered wird initialisiert)
        assert len(items) == 1
        with state_lock:
            assert state.sensor_last_triggered is not None

    def test_queue_counter_includes_items_from_same_cycle(self):
        """
        Wenn in einem Zyklus mehrere Zonen triggern, muss das Queue-Limit
        auch die Items aus demselben Zyklus mitzählen.
        Hier: MAX_QUEUE_ITEMS - 1 Items schon in Queue + 2 neue Zonen →
        nur eine Zone darf durch, die zweite wird blockiert.
        """
        with state_lock:
            state.queue = [
                QueueItem(zone=z, duration=60, time_unit="Sekunden", source="manual")
                for z in range(1, MAX_QUEUE_ITEMS)  # MAX_QUEUE_ITEMS - 1 Items
            ]

        readings = [
            _make_reading(zone=MAX_QUEUE_ITEMS + 10, needs_irrigation=True),
            _make_reading(zone=MAX_QUEUE_ITEMS + 11, needs_irrigation=True),
        ]
        items, _ = _run_cycle(readings)
        # Erste Zone passt rein, zweite überschreitet Limit
        assert len(items) == 1

    def test_mixed_moist_and_dry_zones(self):
        readings = [
            _make_reading(zone=1, needs_irrigation=False),
            _make_reading(zone=2, needs_irrigation=True),
            _make_reading(zone=3, needs_irrigation=False),
            _make_reading(zone=4, needs_irrigation=True),
        ]
        items, new_readings = _run_cycle(readings)
        zones = {item.zone for item in items}
        assert zones == {2, 4}
        assert new_readings == {1: False, 2: True, 3: False, 4: True}


# ─────────────────────────────────────────────────────────────────────────────
# _read_all_sensors
# ─────────────────────────────────────────────────────────────────────────────


class TestReadAllSensors:
    def test_returns_readings_for_all_zones(self, sim_sensor_driver):
        """SimSensorDriver gibt für alle angegebenen Zonen ein Reading zurück."""
        readings = _read_all_sensors([1, 2, 3])
        assert len(readings) == 3
        zones = {r.zone for r in readings}
        assert zones == {1, 2, 3}

    def test_returns_needs_irrigation_false_by_default(self, sim_sensor_driver):
        """SimSensorDriver meldet standardmäßig alle Zonen als feucht."""
        readings = _read_all_sensors([1, 2])
        assert all(not r.needs_irrigation for r in readings)

    def test_dry_zone_returns_needs_irrigation_true(self, sim_sensor_driver):
        sim_sensor_driver.set_zone_dry(2)
        readings = _read_all_sensors([1, 2])
        by_zone = {r.zone: r for r in readings}
        assert by_zone[2].needs_irrigation is True
        assert by_zone[1].needs_irrigation is False

    def test_failed_zone_is_skipped_others_continue(self):
        """
        Schlägt der Read für eine Zone fehl (SensorDriverError),
        müssen die anderen Zonen trotzdem gelesen werden.
        """
        call_count = 0

        class _PartiallyFailingDriver(SimSensorDriver):
            def read(self, zone: int):
                nonlocal call_count
                call_count += 1
                if zone == 2:
                    raise SensorDriverError("Simulierter Sensor-Ausfall Zone 2")
                return super().read(zone)

        failing_driver = _PartiallyFailingDriver()
        set_sensor_driver(failing_driver)

        readings = _read_all_sensors([1, 2, 3])

        # Zone 2 schlägt fehl, Zone 1 und 3 müssen trotzdem da sein
        assert len(readings) == 2
        zones = {r.zone for r in readings}
        assert 2 not in zones
        assert 1 in zones
        assert 3 in zones

    def test_failed_zone_logged_as_warning(self):
        """SensorDriverError bei read() muss als Warning geloggt werden."""
        class _FailingDriver(SimSensorDriver):
            def read(self, zone: int):
                raise SensorDriverError(f"Ausfall Zone {zone}")

        set_sensor_driver(_FailingDriver())

        with patch("services.sensor_engine.log_event") as mock_log:
            _read_all_sensors([1])

        error_events = [
            c for c in mock_log.call_args_list
            if c.args and c.args[0] == "sensor_read_error"
        ]
        assert len(error_events) == 1
        assert error_events[0].kwargs.get("level") == "warning"

    def test_empty_zone_list_returns_empty_list(self, sim_sensor_driver):
        readings = _read_all_sensors([])
        assert readings == []
