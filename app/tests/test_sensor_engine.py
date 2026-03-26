"""
Tests für services/sensor_engine.py

Getestet werden:
  - _process_sensor_cycle_locked  (Kern-Logik, direkt unter state_lock testbar)
    - Feuchter Sensor → kein Item
    - Trockener Sensor mit Zuordnung → Items für alle zugeordneten Zonen
    - Trockener Sensor ohne Zuordnung → kein Item (sensor_skip_no_assignment)
    - Cooldown pro Sensor: blockiert; nach Ablauf: feuert
    - Pending-Zones-Sperre: Sensor blockiert solange eigene Zonen in Queue/active_runs
    - Zone bereits aktiv → wird übersprungen, andere Zonen laufen durch
    - Zone bereits in Queue → wird übersprungen
    - hw_faulted → alle Sensoren blockiert
    - Queue-Limit → weitere Items werden übersprungen
    - sensor_readings State-Update immer (auch bei Skip)
    - sensor_last_triggered NICHT in _process_sensor_cycle_locked gesetzt
      (erst in engine.py start_valve COMMIT-Phase)
    - sensor_pending_zones wird beim Trigger befüllt
    - sensor_id wird in QueueItem gesetzt
    - Mehrere Sensoren: unabhängig voneinander
  - _read_all_sensors  (Fehlerresistenz)
"""

import time
import pytest
from unittest.mock import patch

from core.state import state, state_lock, QueueItem, ActiveRun
from core.config import MAX_QUEUE_ITEMS
from services.sensor_driver import SensorReading, SensorDriverError, SimSensorDriver, set_sensor_driver
from services.sensor_engine import _process_sensor_cycle_locked, _read_all_sensors


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _make_reading(sensor_id: int, needs_irrigation: bool) -> SensorReading:
    return SensorReading(
        zone=sensor_id,  # zone-Feld trägt die sensor_id
        needs_irrigation=needs_irrigation,
        raw_gpio_value=0 if needs_irrigation else 1,
        timestamp=time.monotonic(),
        driver_name="sim",
    )


def _run_cycle(readings, now_m=None):
    if now_m is None:
        now_m = time.monotonic()
    with state_lock:
        return _process_sensor_cycle_locked(readings, now_m)


def _set_assignments(assignments: dict):
    """Setzt sensor_zone_assignments im State."""
    with state_lock:
        state.sensor_zone_assignments = assignments


# ─────────────────────────────────────────────────────────────────────────────
# Grundverhalten
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessSensorCycleBasic:
    def test_moist_sensor_produces_no_item(self):
        _set_assignments({1: [1, 2]})
        items, _ = _run_cycle([_make_reading(1, False)])
        assert items == []

    def test_dry_sensor_with_assignment_produces_items_for_all_zones(self):
        _set_assignments({1: [1, 2, 3]})
        items, _ = _run_cycle([_make_reading(1, True)])
        zones = {i.zone for i in items}
        assert zones == {1, 2, 3}

    def test_all_items_have_source_sensor(self):
        _set_assignments({1: [1, 2]})
        items, _ = _run_cycle([_make_reading(1, True)])
        assert all(i.source == "sensor" for i in items)

    def test_all_items_use_default_duration(self):
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_default_duration_s = 120
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items[0].duration == 120

    def test_all_items_time_unit_sekunden(self):
        _set_assignments({1: [1]})
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items[0].time_unit == "Sekunden"

    def test_dry_sensor_without_assignment_produces_no_item(self):
        _set_assignments({})  # Sensor 1 hat keine Zuordnung
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items == []

    def test_empty_readings_produces_no_items(self):
        items, readings = _run_cycle([])
        assert items == []
        assert readings == {}

    def test_two_independent_sensors(self):
        _set_assignments({1: [1], 2: [2]})
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        zones = {i.zone for i in items}
        assert zones == {1, 2}


# ─────────────────────────────────────────────────────────────────────────────
# sensor_readings State-Update
# ─────────────────────────────────────────────────────────────────────────────

class TestSensorReadingsUpdate:
    def test_readings_updated_for_moist_sensor(self):
        _run_cycle([_make_reading(1, False)])
        with state_lock:
            assert state.sensor_readings.get(1) is False

    def test_readings_updated_for_dry_sensor(self):
        _run_cycle([_make_reading(1, True)])
        with state_lock:
            assert state.sensor_readings.get(1) is True

    def test_readings_updated_even_when_skipped_by_cooldown(self):
        now_m = time.monotonic()
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m}
        _run_cycle([_make_reading(1, True)], now_m=now_m + 1.0)
        with state_lock:
            assert state.sensor_readings.get(1) is True

    def test_new_readings_dict_returned(self):
        _, new_readings = _run_cycle([
            _make_reading(1, True),
            _make_reading(2, False),
        ])
        assert new_readings[1] is True
        assert new_readings[2] is False


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown (pro Sensor)
# ─────────────────────────────────────────────────────────────────────────────

class TestCooldown:
    def test_within_cooldown_produces_no_item(self):
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 60.0}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert items == []

    def test_after_cooldown_produces_items(self):
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 700.0}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert len(items) == 1

    def test_no_prior_trigger_not_blocked(self):
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {}
        items, _ = _run_cycle([_make_reading(1, True)])
        assert len(items) == 1

    def test_last_triggered_NOT_set_by_process_cycle(self):
        """_process_sensor_cycle_locked setzt sensor_last_triggered NICHT mehr.

        Der Cooldown-Timestamp wird erst beim tatsächlichen Ventilstart gesetzt
        (engine.py start_valve COMMIT-Phase), damit ein langer Queue-Rückstau
        die Cooldown-Zeit nicht vorzeitig verbraucht.
        """
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_last_triggered = {}
        _run_cycle([_make_reading(1, True)], now_m=now_m)
        with state_lock:
            # Sensor hat Zonen in die Queue gestellt, aber last_triggered bleibt leer –
            # erst start_valve (COMMIT) setzt den Timestamp beim Ventilöffnen.
            assert 1 not in state.sensor_last_triggered

    def test_last_triggered_NOT_set_when_all_zones_skipped(self):
        """Wenn alle Zonen des Sensors übersprungen werden (z.B. alle aktiv),
        darf sensor_last_triggered NICHT gesetzt werden.
        Gilt nach wie vor: _process_sensor_cycle_locked setzt last_triggered nie."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_last_triggered = {}
            state.active_runs = {
                1: ActiveRun(
                    zone=1, end_time=now_m+60, time_unit="Sekunden",
                    started_at=now_m, started_source="manual", started_planned_s=60,
                )
            }
        _run_cycle([_make_reading(1, True)], now_m=now_m)
        with state_lock:
            assert 1 not in state.sensor_last_triggered

    def test_cooldown_zero_means_no_cooldown(self):
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {1: now_m - 0.001}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert len(items) == 1

    def test_cooldown_independent_per_sensor(self):
        now_m = time.monotonic()
        _set_assignments({1: [1], 2: [2]})
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {
                1: now_m - 60.0,   # Sensor 1: noch im Cooldown
                2: now_m - 700.0,  # Sensor 2: Cooldown abgelaufen
            }
        items, _ = _run_cycle([
            _make_reading(1, True),
            _make_reading(2, True),
        ], now_m=now_m)
        zones = {i.zone for i in items}
        assert 1 not in zones  # Sensor 1 blockiert
        assert 2 in zones      # Sensor 2 durchgelassen


# ─────────────────────────────────────────────────────────────────────────────
# Pending-Zones-Sperre (neu: Cooldown startet erst beim Ventilstart)
# ─────────────────────────────────────────────────────────────────────────────

class TestPendingZonesBlock:
    """Sensor-Sperre über sensor_pending_zones.

    Solange Zonen, die von einem Sensor ausgelöst wurden, noch in der Queue
    warten oder aktiv laufen, darf derselbe Sensor nicht erneut feuern –
    unabhängig davon, ob der Cooldown bereits abgelaufen wäre.

    Dies ist der Kern-Fix für das Doppelbewässerungs-Problem: War die Queue
    voll und lief der Cooldown ab während Zonen noch warteten, konnte der
    Sensor sofort nach dem Lauf erneut triggern. Jetzt startet der Cooldown
    erst beim tatsächlichen Ventilstart (engine.py COMMIT).
    """

    def test_sensor_blocked_while_own_zone_in_queue(self):
        """Sensor hat Zone in Queue → Neu-Trigger gesperrt, auch wenn kein Cooldown."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0           # kein Cooldown
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {1: {1}}  # Zone 1 durch Sensor 1 eingestellt
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden",
                                     source="sensor", sensor_id=1)]
        items, _ = _run_cycle([_make_reading(1, True)])
        # Sensor 1 hat Zone 1 noch pending → gesperrt
        assert items == []

    def test_sensor_blocked_while_own_zone_in_active_runs(self):
        """Zone ist aus Queue gestartet (active_runs) → Sensor immer noch gesperrt."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {1: {1}}  # Zone 1 noch als pending markiert
            state.active_runs = {
                1: ActiveRun(zone=1, end_time=now_m+60, time_unit="Sekunden",
                             started_at=now_m, started_source="sensor",
                             started_planned_s=60)
            }
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert items == []

    def test_sensor_allowed_when_pending_zones_cleared(self):
        """Sobald keine eigenen Zonen mehr pending/aktiv → Sensor darf wieder feuern."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {}
            # Pending-Set vorhanden, aber Zone 1 ist nicht mehr in Queue/active_runs
            state.sensor_pending_zones = {1: {1}}
            state.queue = []       # Zone 1 wurde bereits abgearbeitet
            state.active_runs = {}
        items, _ = _run_cycle([_make_reading(1, True)])
        # Keine aktiven Pending-Zonen → Sensor kann erneut feuern
        assert len(items) == 1

    def test_pending_check_uses_only_own_sensor_zones(self):
        """Sensor 2 wird nicht durch Pending-Zonen von Sensor 1 blockiert."""
        _set_assignments({1: [1], 2: [2]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {1: {1}}  # Sensor 1 hat Zone 1 pending
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden",
                                     source="sensor", sensor_id=1)]
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        zones = {i.zone for i in items}
        # Sensor 1 blockiert (eigene Pending-Zone), Sensor 2 frei
        assert 1 not in zones
        assert 2 in zones

    def test_manual_active_zone_does_not_block_sensor_for_other_zones(self):
        """Zone 1 läuft manuell → Sensor 1 darf Zone 2 noch in Queue stellen.

        sensor_pending_zones ist leer (Sensor hat nie gefeuert), also greift
        die Pending-Sperre nicht. Zone 2 wird eingestellt, Zone 1 übersprungen.
        """
        now_m = time.monotonic()
        _set_assignments({1: [1, 2]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_last_triggered = {}
            state.sensor_pending_zones  = {}  # keine Sensor-eigenen Pending-Zonen
            state.active_runs = {
                1: ActiveRun(zone=1, end_time=now_m+60, time_unit="Sekunden",
                             started_at=now_m, started_source="manual",
                             started_planned_s=60)
            }
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        zones = {i.zone for i in items}
        # Zone 1 aktiv (übersprungen), Zone 2 frei → eingereihtt
        assert 1 not in zones
        assert 2 in zones

    def test_pending_zones_populated_on_trigger(self):
        """Nach einem erfolgreichen Trigger enthält sensor_pending_zones die Zonen."""
        _set_assignments({1: [1, 2]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_pending_zones = {}
        _run_cycle([_make_reading(1, True)])
        with state_lock:
            pending = state.sensor_pending_zones.get(1, set())
        assert 1 in pending
        assert 2 in pending

    def test_pending_zones_not_populated_when_all_zones_skipped(self):
        """Wenn alle Zonen übersprungen werden, bleibt sensor_pending_zones leer."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_pending_zones = {}
            state.active_runs = {
                1: ActiveRun(zone=1, end_time=now_m+60, time_unit="Sekunden",
                             started_at=now_m, started_source="manual",
                             started_planned_s=60)
            }
        _run_cycle([_make_reading(1, True)], now_m=now_m)
        with state_lock:
            pending = state.sensor_pending_zones.get(1, set())
        assert len(pending) == 0

    def test_cooldown_expired_but_pending_zone_still_blocks(self):
        """Kernfall: Cooldown ist abgelaufen, Zone ist aber noch in Queue → gesperrt.

        Dies reproduziert das ursprüngliche Doppelbewässerungs-Problem:
        Ohne den Fix konnte ein Sensor nach Ablauf des Cooldowns erneut feuern,
        obwohl die erste Trigger-Runde noch nicht vollständig abgearbeitet war.
        """
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            # Simuliere: Cooldown wurde irgendwann in der Vergangenheit gesetzt
            # und ist jetzt abgelaufen – aber Zone 1 ist noch in Queue
            state.sensor_last_triggered = {1: now_m - 700.0}  # Cooldown abgelaufen
            state.sensor_pending_zones  = {1: {1}}
            state.queue = [QueueItem(zone=1, duration=600, time_unit="Sekunden",
                                     source="sensor", sensor_id=1)]
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        # Trotz abgelaufenem Cooldown: Pending-Zone blockiert Neu-Trigger
        assert items == []

    def test_sensor_can_fire_after_pending_zone_runs_and_cooldown_expires(self):
        """Nach Ablauf aller Pending-Zonen und Cooldown ist Sensor wieder frei."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            # Ventil hat bereits gelaufen: last_triggered gesetzt, pending leer,
            # keine aktiven Läufe, keine Queue-Einträge
            state.sensor_last_triggered = {1: now_m - 700.0}  # Cooldown abgelaufen
            state.sensor_pending_zones  = {1: set()}           # leer = alles erledigt
            state.queue = []
            state.active_runs = {}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert len(items) == 1


# ─────────────────────────────────────────────────────────────────────────────
# sensor_id in QueueItem
# ─────────────────────────────────────────────────────────────────────────────

class TestSensorIdInQueueItem:
    """Sensor-ausgelöste QueueItems tragen die sensor_id des auslösenden Sensors.

    Diese ID wird in engine.py start_valve COMMIT-Phase genutzt um
    sensor_last_triggered zu setzen und die Zone aus sensor_pending_zones
    zu entfernen.
    """

    def test_queued_items_carry_sensor_id(self):
        """Alle Items, die Sensor 1 auslöst, haben sensor_id=1."""
        _set_assignments({1: [1, 2, 3]})
        items, _ = _run_cycle([_make_reading(1, True)])
        assert all(i.sensor_id == 1 for i in items)

    def test_sensor_id_matches_triggering_sensor(self):
        """Zwei Sensoren feuern; Items haben jeweils die richtige sensor_id."""
        _set_assignments({1: [1], 2: [2]})
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        by_zone = {i.zone: i.sensor_id for i in items}
        assert by_zone[1] == 1
        assert by_zone[2] == 2

    def test_sensor_id_none_for_non_sensor_items(self):
        """QueueItems die nicht von Sensor stammen, haben sensor_id=None."""
        item = QueueItem(zone=1, duration=60, time_unit="Sekunden", source="manual")
        assert item.sensor_id is None


# ─────────────────────────────────────────────────────────────────────────────
# Überspringen-Bedingungen
# ─────────────────────────────────────────────────────────────────────────────

class TestSkipConditions:
    def test_hw_faulted_blocks_all(self):
        _set_assignments({1: [1], 2: [2]})
        with state_lock:
            state.hw_faulted = True
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        assert items == []

    def test_hw_faulted_does_not_block_readings_update(self):
        with state_lock:
            state.hw_faulted = True
        _, new_readings = _run_cycle([_make_reading(1, True)])
        assert new_readings.get(1) is True

    def test_zone_already_active_skipped_others_queued(self):
        """Zone 1 ist aktiv → wird übersprungen; Zone 2 kommt durch."""
        now_m = time.monotonic()
        _set_assignments({1: [1, 2]})
        with state_lock:
            state.active_runs = {
                1: ActiveRun(
                    zone=1, end_time=now_m+60, time_unit="Sekunden",
                    started_at=now_m, started_source="manual", started_planned_s=60,
                )
            }
        items, _ = _run_cycle([_make_reading(1, True)])
        zones = {i.zone for i in items}
        assert 1 not in zones
        assert 2 in zones

    def test_zone_already_queued_skipped(self):
        _set_assignments({1: [1, 2]})
        with state_lock:
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden", source="manual")]
        items, _ = _run_cycle([_make_reading(1, True)])
        zones = {i.zone for i in items}
        assert 1 not in zones
        assert 2 in zones

    def test_queue_full_skips_remaining_zones(self):
        _set_assignments({1: list(range(1, MAX_QUEUE_ITEMS + 2))})
        with state_lock:
            state.queue = [
                QueueItem(zone=z, duration=60, time_unit="Sekunden", source="manual")
                for z in range(1, MAX_QUEUE_ITEMS + 1)
            ]
        items, _ = _run_cycle([_make_reading(1, True)])
        # Queue bereits voll → kein weiteres Item
        assert items == []

    def test_no_double_entry_for_same_zone_two_sensors(self):
        """Zwei Sensoren sind beide trocken und beide haben Zone 1 zugeordnet.
        Zone 1 darf nur EINMAL in die Queue kommen."""
        _set_assignments({1: [1], 2: [1]})
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        zone1_items = [i for i in items if i.zone == 1]
        assert len(zone1_items) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

class TestLogging:
    def test_sensor_trigger_logged(self):
        _set_assignments({1: [1, 2]})
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle([_make_reading(1, True)])
        events = [c.args[0] for c in mock_log.call_args_list if c.args]
        assert "sensor_trigger" in events

    def test_sensor_trigger_contains_sensor_id_and_zones(self):
        _set_assignments({1: [1, 2]})
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle([_make_reading(1, True)])
        trigger = next(c for c in mock_log.call_args_list if c.args and c.args[0] == "sensor_trigger")
        assert trigger.kwargs["sensor_id"] == 1
        assert set(trigger.kwargs["zones_queued"]) == {1, 2}

    def test_no_assignment_logged(self):
        _set_assignments({})
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle([_make_reading(1, True)])
        events = [c.args[0] for c in mock_log.call_args_list if c.args]
        assert "sensor_skip_no_assignment" in events

    def test_cooldown_skip_logged(self):
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 600
            state.sensor_last_triggered = {1: now_m - 10.0}
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle([_make_reading(1, True)], now_m=now_m)
        events = [c.args[0] for c in mock_log.call_args_list if c.args]
        assert "sensor_skip_cooldown" in events

    def test_pending_zones_skip_logged(self):
        """sensor_skip_zones_pending wird geloggt wenn Pending-Zonen Trigger sperren."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_cooldown_s = 0
            state.sensor_pending_zones = {1: {1}}
            state.queue = [QueueItem(zone=1, duration=60, time_unit="Sekunden",
                                     source="sensor", sensor_id=1)]
        with patch("services.sensor_engine.log_event") as mock_log:
            _run_cycle([_make_reading(1, True)])
        events = [c.args[0] for c in mock_log.call_args_list if c.args]
        assert "sensor_skip_zones_pending" in events


# ─────────────────────────────────────────────────────────────────────────────
# _read_all_sensors
# ─────────────────────────────────────────────────────────────────────────────

class TestReadAllSensors:
    def test_reads_all_sensor_ids(self, sim_sensor_driver):
        readings = _read_all_sensors([1, 2, 3])
        assert len(readings) == 3
        ids = {r.zone for r in readings}
        assert ids == {1, 2, 3}

    def test_failed_sensor_skipped_others_continue(self):
        call_count = 0
        class _PartialFail(SimSensorDriver):
            def read(self, zone):
                nonlocal call_count
                call_count += 1
                if zone == 2:
                    raise SensorDriverError("Ausfall")
                return super().read(zone)
        set_sensor_driver(_PartialFail())
        readings = _read_all_sensors([1, 2, 3])
        assert len(readings) == 2
        ids = {r.zone for r in readings}
        assert 2 not in ids

    def test_empty_list_returns_empty(self, sim_sensor_driver):
        assert _read_all_sensors([]) == []


# ─────────────────────────────────────────────────────────────────────────────
# Per-Sensor-Betriebsparameter (sensor_settings_by_id)
# ─────────────────────────────────────────────────────────────────────────────

class TestPerSensorSettings:
    """Cooldown und duration_s werden pro Sensor aus sensor_settings_by_id gelesen."""

    def test_per_sensor_duration_used(self):
        """Sensor 1 hat eigene duration_s → wird für QueueItem verwendet."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_settings_by_id = {1: {"cooldown_s": 0, "duration_s": 240}}
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items[0].duration == 240

    def test_per_sensor_cooldown_blocks(self):
        """Sensor 1 hat eigenen cooldown_s=600 → blockiert wenn nicht abgelaufen."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_settings_by_id    = {1: {"cooldown_s": 600, "duration_s": 60}}
            state.sensor_last_triggered    = {1: now_m - 60.0}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert items == []

    def test_per_sensor_cooldown_allows_after_elapsed(self):
        """Sensor 1 eigener cooldown_s=600, 700s vergangen → durchgelassen."""
        now_m = time.monotonic()
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_settings_by_id    = {1: {"cooldown_s": 600, "duration_s": 60}}
            state.sensor_last_triggered    = {1: now_m - 700.0}
        items, _ = _run_cycle([_make_reading(1, True)], now_m=now_m)
        assert len(items) == 1

    def test_different_settings_per_sensor(self):
        """Sensor 1 und 2 haben unterschiedliche cooldown_s."""
        now_m = time.monotonic()
        _set_assignments({1: [1], 2: [2]})
        with state_lock:
            state.sensor_settings_by_id = {
                1: {"cooldown_s": 600, "duration_s": 60},   # Sensor 1: langer Cooldown
                2: {"cooldown_s": 30,  "duration_s": 120},  # Sensor 2: kurzer Cooldown
            }
            state.sensor_last_triggered = {
                1: now_m - 60.0,   # Sensor 1: im Cooldown
                2: now_m - 40.0,   # Sensor 2: Cooldown abgelaufen
            }
        items, _ = _run_cycle(
            [_make_reading(1, True), _make_reading(2, True)], now_m=now_m
        )
        zones = {i.zone for i in items}
        assert 1 not in zones   # Sensor 1 blockiert
        assert 2 in zones       # Sensor 2 durch

    def test_different_duration_per_sensor(self):
        """Zwei Sensoren haben unterschiedliche duration_s."""
        _set_assignments({1: [1], 2: [2]})
        with state_lock:
            state.sensor_settings_by_id = {
                1: {"cooldown_s": 0, "duration_s": 180},
                2: {"cooldown_s": 0, "duration_s": 360},
            }
        items, _ = _run_cycle([_make_reading(1, True), _make_reading(2, True)])
        dur_by_zone = {i.zone: i.duration for i in items}
        assert dur_by_zone[1] == 180
        assert dur_by_zone[2] == 360

    def test_global_fallback_when_no_per_sensor_setting(self):
        """Kein Eintrag in sensor_settings_by_id → globale Defaults werden verwendet."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_settings_by_id    = {}    # leer → Fallback
            state.sensor_cooldown_s         = 0
            state.sensor_default_duration_s = 99
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items[0].duration == 99

    def test_none_settings_dict_uses_global_fallback(self):
        """sensor_settings_by_id ist None → globale Defaults."""
        _set_assignments({1: [1]})
        with state_lock:
            state.sensor_settings_by_id    = None
            state.sensor_cooldown_s         = 0
            state.sensor_default_duration_s = 77
        items, _ = _run_cycle([_make_reading(1, True)])
        assert items[0].duration == 77


# ─────────────────────────────────────────────────────────────────────────────
# Fall 2b: Neuer Sensor feuert während Prioritätsmodus aktiv ist
# ─────────────────────────────────────────────────────────────────────────────

class TestSensorQueueStrategyFall2b:
    """Fall 2b: Sensor feuert während queue_priority_mode=True und queue läuft.

    Ohne Fix: Items landen hinter non-priority Items → timer_loop stoppt davor.
    Mit Fix:  Items werden mit priority=True nach dem letzten priority-Item eingefügt.
    """

    def _apply_sensor_loop_logic(self, items_to_queue: list):
        """Simuliert den Einfüge-Block aus sensor_engine_loop direkt unter Lock."""
        with state_lock:
            state.queue = state.queue or []
            queue_had_items  = bool(state.queue)
            queue_is_running = (state.queue_state == "läuft")

            if queue_had_items and not queue_is_running:
                for item in items_to_queue:
                    item.priority = True
                state.queue = items_to_queue + state.queue
                state.queue_priority_mode = True
            elif queue_had_items and state.queue_priority_mode:
                for item in items_to_queue:
                    item.priority = True
                insert_pos = next(
                    (i for i, x in enumerate(state.queue) if not x.priority),
                    len(state.queue),
                )
                state.queue[insert_pos:insert_pos] = items_to_queue
            else:
                state.queue.extend(items_to_queue)

            state.queue_dirty = True
            if state.queue_state in ("bereit", "fertig"):
                state.queue_state = "läuft"

    def test_bug_scenario_sensor2_fires_while_priority_mode_active(self):
        """Reproduziert den ursprünglichen Bug:
        Sensor 2 feuert nachdem Sensor 1 Fall 3 ausgelöst hat und die Queue
        bereits läuft. Ohne Fix: s2_zone landet hinter manual_item.
        """
        manual_item = QueueItem(zone=5, duration=300, time_unit="Sekunden",
                                source="manual", priority=False)
        s1_zone     = QueueItem(zone=1, duration=600, time_unit="Sekunden",
                                source="sensor", priority=True)

        # Zustand nach Fall-3 Trigger von Sensor 1 + timer hat s1_zone bereits
        # gepoppt (läuft gerade), manual_item ist noch in Queue
        with state_lock:
            state.queue               = [manual_item]
            state.queue_state         = "läuft"
            state.queue_priority_mode = True

        # Sensor 2 feuert im nächsten Polling-Zyklus
        s2_zone = QueueItem(zone=2, duration=600, time_unit="Sekunden",
                            source="sensor", priority=False)
        self._apply_sensor_loop_logic([s2_zone])

        with state_lock:
            # Mit Fix: s2_zone VOR manual_item (priority=True)
            assert state.queue[0].zone == 2
            assert state.queue[0].priority is True
            # manual_item bleibt hinten (priority=False)
            assert state.queue[1].zone == 5
            assert state.queue[1].priority is False
            # Prioritätsmodus bleibt aktiv
            assert state.queue_priority_mode is True

    def test_fall2b_insert_after_last_priority_item(self):
        """Fall 2b: Einfügeposition ist direkt nach dem letzten priority=True Item."""
        p1 = QueueItem(zone=1, duration=60, time_unit="Sekunden",
                       source="sensor", priority=True)
        p2 = QueueItem(zone=2, duration=60, time_unit="Sekunden",
                       source="sensor", priority=True)
        m1 = QueueItem(zone=9, duration=60, time_unit="Sekunden",
                       source="manual", priority=False)

        with state_lock:
            state.queue               = [p1, p2, m1]
            state.queue_state         = "läuft"
            state.queue_priority_mode = True

        new_item = QueueItem(zone=3, duration=60, time_unit="Sekunden",
                             source="sensor", priority=False)
        self._apply_sensor_loop_logic([new_item])

        with state_lock:
            zones = [item.zone for item in state.queue]
            # Reihenfolge: p1, p2, new_item(zone=3), m1
            assert zones == [1, 2, 3, 9]
            assert state.queue[2].priority is True   # new_item hat priority=True
            assert state.queue[3].priority is False  # m1 unverändert

    def test_fall2b_no_non_priority_items_appends_at_end(self):
        """Fall 2b: Wenn alle Queue-Items priority=True, wird hinten angehängt."""
        p1 = QueueItem(zone=1, duration=60, time_unit="Sekunden",
                       source="sensor", priority=True)

        with state_lock:
            state.queue               = [p1]
            state.queue_state         = "läuft"
            state.queue_priority_mode = True

        new_item = QueueItem(zone=2, duration=60, time_unit="Sekunden",
                             source="sensor", priority=False)
        self._apply_sensor_loop_logic([new_item])

        with state_lock:
            assert len(state.queue) == 2
            assert state.queue[1].zone == 2
            assert state.queue[1].priority is True

    def test_fall2_without_priority_mode_still_appends(self):
        """Fall 2 ohne priority_mode: Items werden normal hinten angehängt (kein Regression)."""
        existing = QueueItem(zone=5, duration=60, time_unit="Sekunden",
                             source="manual", priority=False)
        with state_lock:
            state.queue               = [existing]
            state.queue_state         = "läuft"
            state.queue_priority_mode = False   # kein priority_mode

        new_item = QueueItem(zone=1, duration=60, time_unit="Sekunden",
                             source="sensor", priority=False)
        self._apply_sensor_loop_logic([new_item])

        with state_lock:
            assert state.queue[0].zone == 5
            assert state.queue[1].zone == 1
            assert state.queue[1].priority is False
            assert state.queue_priority_mode is False
