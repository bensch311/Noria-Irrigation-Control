# app/services/sensor_engine.py
"""
Sensor-Engine: Sensorgesteuerte Auslösung von Bewässerungsläufen.

Läuft als Background-Thread (gestartet in core/lifecycle.py).
Liest periodisch alle konfigurierten Sensoren und stellt bei Bedarf
QueueItems für alle zugeordneten Zonen ein.

Schlüsselkonzept – Sensor vs. Zone:
  Ein Sensor (sensor_id, GPIO-Pin) kann mehrere Ventil-Zonen steuern.
  Die Zuordnung Sensor → Zonen wird in sensor_assignments.json persistiert
  und im Einstellungs-Tab konfiguriert.

  Beispiel: Sensor 1 (Pin 24) → Zonen [1, 2, 3]
  Wenn Sensor 1 "trocken" meldet, werden Zonen 1, 2 und 3 in die Queue gestellt.

Polling-Zyklus (alle sensor_polling_interval_s Sekunden):
  Phase 1 (unter Lock):  Polling-Intervall, Sensor-Pins und Zuordnungen lesen.
  Phase 2 (OHNE Lock):   Sensor-Hardware lesen (kann blockieren/fehlschlagen).
  Phase 3 (unter Lock):  Readings auswerten, Queue-Items einstellen, State updaten.

Auslöse-Bedingungen (alle müssen erfüllt sein):
  1. Sensor meldet needs_irrigation=True
  2. Sensor hat mindestens eine Zonen-Zuordnung
  3. Kein Hardware-Fault aktiv (hw_faulted=False)
  4. Cooldown abgelaufen (sensor_last_triggered[sensor_id] + sensor_cooldown_s < now)
  Pro zugeordneter Zone zusätzlich:
  5. Zone nicht bereits aktiv (nicht in active_runs)
  6. Zone nicht bereits in Queue (beliebige Quelle)
  7. Queue nicht voll (DoS-Schutz, MAX_QUEUE_ITEMS)

Cooldown-Logik:
  Der Cooldown gilt pro Sensor (nicht pro Zone). Wenn ein Sensor feuert, werden
  alle zugeordneten Zonen eingestellt und der Sensor-Cooldown startet. Erst nach
  Ablauf des Cooldowns kann derselbe Sensor erneut Zonen in die Queue stellen.

Naming-Konvention:
  _process_sensor_cycle_locked() MUSS unter state_lock aufgerufen werden.
  _read_all_sensors() MUSS OHNE state_lock aufgerufen werden.
"""

import time

from core.state import state, state_lock, QueueItem
from core.config import MAX_QUEUE_ITEMS
from core.logging import log_event
from services.sensor_driver import SensorReading, SensorDriverError, get_sensor_driver


def _process_sensor_cycle_locked(
    readings: list[SensorReading],
    now_m: float,
) -> tuple[list[QueueItem], dict[int, bool]]:
    """Wertet Sensor-Readings aus und bestimmt einzustellende Queue-Items.

    Arbeitet pro Sensor (nicht pro Zone). Für jeden Sensor der needs_irrigation=True
    meldet, werden alle zugeordneten Zonen in die Queue gestellt.

    MUSS unter state_lock aufgerufen werden.
    """
    if state.sensor_readings is None:
        state.sensor_readings = {}
    if state.sensor_last_triggered is None:
        state.sensor_last_triggered = {}

    cooldown_s         = max(0, int(getattr(state, "sensor_cooldown_s", 600)))
    default_duration_s = max(1, int(getattr(state, "sensor_default_duration_s", 300)))
    assignments        = dict(state.sensor_zone_assignments or {})

    new_readings: dict[int, bool] = {}
    items_to_queue: list[QueueItem] = []

    # queue_zones und active_zones EINMALIG vor dem Sensor-Loop initialisieren.
    # So sehen spätere Sensoren in derselben Iteration was frühere Sensoren
    # bereits in items_to_queue eingestellt haben – verhindert Doppel-Einstellung
    # wenn zwei Sensoren dieselbe Zone zugeordnet haben.
    # queue_zones wird innerhalb der Zone-Schleife via queue_zones.add() aktuell gehalten.
    queue_zones  = {item.zone for item in (state.queue or [])}
    active_zones = set(state.active_runs or {})

    for reading in readings:
        sensor_id = reading.zone  # zone-Feld trägt die sensor_id
        new_readings[sensor_id] = reading.needs_irrigation

        if not reading.needs_irrigation:
            continue

        # Bedingung 2: Sensor hat Zonen-Zuordnung
        zones_for_sensor = [int(z) for z in assignments.get(sensor_id, [])]
        if not zones_for_sensor:
            log_event("sensor_skip_no_assignment", source="sensor", sensor_id=sensor_id)
            continue

        # Bedingung 3: Hardware-Fault
        if getattr(state, "hw_faulted", False):
            log_event("sensor_skip_hw_faulted", source="sensor", sensor_id=sensor_id)
            continue

        # Bedingung 4: Cooldown pro Sensor
        last_triggered = state.sensor_last_triggered.get(sensor_id, 0.0)
        elapsed = now_m - last_triggered
        if elapsed < cooldown_s:
            log_event(
                "sensor_skip_cooldown",
                source="sensor",
                sensor_id=sensor_id,
                remaining_cooldown_s=int(cooldown_s - elapsed),
                cooldown_s=cooldown_s,
            )
            continue

        # Zonen des Sensors einzeln prüfen und einreihen
        zones_queued: list[int] = []

        for zone in zones_for_sensor:
            if zone in active_zones:
                log_event("sensor_skip_zone_already_active", source="sensor",
                          sensor_id=sensor_id, zone=zone)
                continue
            if zone in queue_zones:
                log_event("sensor_skip_zone_already_queued", source="sensor",
                          sensor_id=sensor_id, zone=zone)
                continue
            if len(state.queue or []) + len(items_to_queue) >= MAX_QUEUE_ITEMS:
                log_event("sensor_skip_queue_full", level="warning", source="sensor",
                          sensor_id=sensor_id, zone=zone,
                          queue_length=len(state.queue or []))
                continue

            items_to_queue.append(QueueItem(
                zone=zone,
                duration=default_duration_s,
                time_unit="Sekunden",
                source="sensor",
            ))
            queue_zones.add(zone)
            zones_queued.append(zone)

        if zones_queued:
            # Cooldown-Timestamp NUR setzen wenn mindestens eine Zone eingestellt wurde
            state.sensor_last_triggered[sensor_id] = now_m
            log_event(
                "sensor_trigger",
                source="sensor",
                sensor_id=sensor_id,
                zones_queued=zones_queued,
                duration_s=default_duration_s,
                cooldown_s=cooldown_s,
            )

    state.sensor_readings.update(new_readings)
    return items_to_queue, new_readings


def _read_all_sensors(sensor_ids: list[int]) -> list[SensorReading]:
    """Liest alle angegebenen Sensoren. Fehlschlagende Sensoren werden übersprungen.

    MUSS OHNE state_lock aufgerufen werden.
    """
    driver = get_sensor_driver()
    readings: list[SensorReading] = []

    for sensor_id in sensor_ids:
        try:
            reading = driver.read(sensor_id)
            readings.append(reading)
        except SensorDriverError as e:
            log_event("sensor_read_error", level="warning", source="sensor",
                      sensor_id=sensor_id, error=repr(e))

    return readings


def sensor_engine_loop():
    """Polling-Loop: liest Sensoren und stellt QueueItems ein.

    Terminiert sauber wenn shutdown_event gesetzt wird.
    """
    from core.state import shutdown_event
    from core.logging import logger

    log_event("sensor_engine_started", source="system")

    while not shutdown_event.is_set():
        with state_lock:
            interval_s = max(5, int(getattr(state, "sensor_polling_interval_s", 30)))
            pins       = dict(state.sensor_gpio_pins or {})

        sensor_ids = sorted(pins.keys())

        if shutdown_event.wait(interval_s):
            break

        if not sensor_ids:
            continue

        try:
            readings = _read_all_sensors(sensor_ids)
            if not readings:
                continue

            now_m = time.monotonic()
            with state_lock:
                items_to_queue, _ = _process_sensor_cycle_locked(readings, now_m)
                if items_to_queue:
                    state.queue = state.queue or []
                    state.queue.extend(items_to_queue)
                    state.queue_dirty = True
                    if state.queue_state in ("bereit", "fertig"):
                        state.queue_state = "läuft"

        except Exception:
            logger.exception("sensor_engine_loop crashed")
            log_event("sensor_engine_error", level="error", source="system")

    log_event("sensor_engine_stopped", source="system")
