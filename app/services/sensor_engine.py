# app/services/sensor_engine.py
"""
Sensor-Engine: Sensorgesteuerte Auslösung von Bewässerungsläufen.

Läuft als Background-Thread (gestartet in core/lifecycle.py).
Liest periodisch alle konfigurierten Sensor-Zonen und stellt bei Bedarf
QueueItems ein – exakt wie der Scheduler, nur bedarfsgesteuert statt zeitgesteuert.

Polling-Zyklus (alle sensor_polling_interval_s Sekunden):
  Phase 1 (unter Lock):  Polling-Intervall und Zonen-Liste aus State lesen.
  Phase 2 (OHNE Lock):   Sensor-Hardware lesen (kann blockieren/fehlschlagen).
  Phase 3 (unter Lock):  Readings auswerten, Queue-Items einstellen, State updaten.

Auslöse-Bedingungen (alle müssen erfüllt sein):
  1. Sensor meldet needs_irrigation=True
  2. Kein Hardware-Fault aktiv (hw_faulted=False)
  3. Zone nicht bereits aktiv (nicht in active_runs)
  4. Zone nicht bereits in Queue (beliebige Quelle)
  5. Cooldown abgelaufen (sensor_last_triggered[zone] + sensor_cooldown_s < now)

Cooldown-Logik:
  Verhindert dass eine Zone bei dauerhaft trockenem Boden alle 30 Sekunden
  neu in die Queue gestellt wird. sensor_last_triggered[zone] wird beim
  Einstellen eines Items gesetzt und ist rein in-memory – überlebt keinen
  Neustart (bewusste Entscheidung: ein einzelner Doppelstart nach Restart
  ist akzeptabler als persistente Cooldown-Daten).

Concurrency-Modell:
  Sensor-Reads laufen im Sensor-Engine-Thread, NICHT über den IO-Worker.
  Sensor-Pins sind GPIO-Inputs, die keinerlei Interferenz mit den
  Valve-Output-Pins des IO-Workers haben. Die beiden lgpio-Handles
  (Sensor-Driver und Valve-Driver) sind unabhängig und thread-safe.

Naming-Konvention:
  _process_sensor_cycle_locked() MUSS unter state_lock aufgerufen werden.
  _read_all_sensors() MUSS OHNE state_lock aufgerufen werden.

Konfiguration (aus device_config.json, via state):
  sensor_polling_interval_s  – Polling-Intervall in Sekunden (default: 30)
  sensor_cooldown_s          – Mindestabstand zwischen zwei Sensor-Triggern pro Zone (default: 600)
  sensor_default_duration_s  – Laufzeit sensor-getriggerter Bewässerungsläufe (default: 300)
  sensor_gpio_pins_by_zone   – Welche Zonen mit Sensoren bestückt sind
"""

import time

from core.state import state, state_lock, QueueItem
from core.config import MAX_QUEUE_ITEMS
from core.logging import log_event
from services.sensor_driver import SensorReading, SensorDriverError, get_sensor_driver


# ─────────────────────────────────────────────────────────────────────────────
# Interne Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _process_sensor_cycle_locked(
    readings: list[SensorReading],
    now_m: float,
) -> tuple[list[QueueItem], dict[int, bool]]:
    """Wertet Sensor-Readings aus und bestimmt einzustellende Queue-Items.

    Prüft pro Lesung die Bedingungen für eine Sensor-Bewässerung:
      1. needs_irrigation=True?
      2. hw_faulted=False? (bei Fault kein Item – Ventile können nicht öffnen)
      3. Zone nicht bereits aktiv (active_runs)?
      4. Zone nicht bereits in Queue (beliebige Quelle)?
      5. Cooldown abgelaufen?
      6. Queue nicht voll (DoS-Schutz, MAX_QUEUE_ITEMS)?

    State-Updates (immer, unabhängig vom Trigger):
      state.sensor_readings wird mit den aktuellen needs_irrigation-Werten befüllt.

    State-Updates (nur beim Trigger):
      state.sensor_last_triggered[zone] = now_m

    MUSS unter state_lock aufgerufen werden.

    Args:
        readings: Liste der aktuellen Sensor-Lesungen (alle Zonen eines Zyklus).
        now_m:    Aktueller time.monotonic()-Wert.

    Returns:
        Tuple (items_to_queue, new_readings_dict):
          items_to_queue:    QueueItems die in die Queue eingestellt werden sollen.
          new_readings_dict: Aktueller needs_irrigation-Status pro Zone (für Dashboard).
    """
    # Lazy-Initialisierung der State-Dicts (können beim ersten Aufruf noch None sein)
    if state.sensor_readings is None:
        state.sensor_readings = {}
    if state.sensor_last_triggered is None:
        state.sensor_last_triggered = {}

    cooldown_s = max(0, int(getattr(state, "sensor_cooldown_s", 600)))
    default_duration_s = max(1, int(getattr(state, "sensor_default_duration_s", 300)))

    new_readings: dict[int, bool] = {}
    items_to_queue: list[QueueItem] = []

    for reading in readings:
        zone = reading.zone
        new_readings[zone] = reading.needs_irrigation

        if not reading.needs_irrigation:
            continue

        # ── Bedingung 2: Hardware-Fault ───────────────────────────────────────
        if getattr(state, "hw_faulted", False):
            log_event(
                "sensor_skip_hw_faulted",
                source="sensor",
                zone=zone,
            )
            continue

        # ── Bedingung 3: Zone bereits aktiv ──────────────────────────────────
        if zone in (state.active_runs or {}):
            log_event(
                "sensor_skip_already_active",
                source="sensor",
                zone=zone,
            )
            continue

        # ── Bedingung 4: Zone bereits in Queue (beliebige Quelle) ─────────────
        # Verhindert Doppel-Einstellung egal ob durch Zeitplan, manuell oder Sensor.
        queue_zones = {item.zone for item in (state.queue or [])}
        if zone in queue_zones:
            log_event(
                "sensor_skip_already_queued",
                source="sensor",
                zone=zone,
            )
            continue

        # ── Bedingung 5: Cooldown prüfen ──────────────────────────────────────
        last_triggered = state.sensor_last_triggered.get(zone, 0.0)
        elapsed = now_m - last_triggered
        if elapsed < cooldown_s:
            remaining_cooldown = int(cooldown_s - elapsed)
            log_event(
                "sensor_skip_cooldown",
                source="sensor",
                zone=zone,
                remaining_cooldown_s=remaining_cooldown,
                cooldown_s=cooldown_s,
            )
            continue

        # ── Bedingung 6: Queue-Limit (DoS-Schutz) ────────────────────────────
        if len(state.queue or []) + len(items_to_queue) >= MAX_QUEUE_ITEMS:
            log_event(
                "sensor_skip_queue_full",
                level="warning",
                source="sensor",
                zone=zone,
                queue_length=len(state.queue or []),
            )
            continue

        # ── Alle Bedingungen erfüllt → QueueItem erzeugen ────────────────────
        item = QueueItem(
            zone=zone,
            duration=default_duration_s,
            time_unit="Sekunden",
            source="sensor",
        )
        items_to_queue.append(item)
        state.sensor_last_triggered[zone] = now_m

        log_event(
            "sensor_trigger",
            source="sensor",
            zone=zone,
            duration_s=default_duration_s,
            cooldown_s=cooldown_s,
        )

    # Sensor-Readings immer in den State schreiben (Dashboard-Anzeige)
    state.sensor_readings.update(new_readings)

    return items_to_queue, new_readings


def _read_all_sensors(zones: list[int]) -> list[SensorReading]:
    """Liest alle angegebenen Sensor-Zonen über den aktiven Sensor-Driver.

    Fehlerresistenz: Schlägt der Read einer Zone fehl (SensorDriverError),
    wird die Zone geloggt und übersprungen – die restlichen Zonen werden
    weiterhin gelesen. Kein Abbruch des gesamten Zyklus.

    MUSS OHNE state_lock aufgerufen werden – GPIO-Reads können blockieren.

    Args:
        zones: Liste der zu lesenden Zonen-Nummern.

    Returns:
        Liste der erfolgreich gelesenen SensorReadings (kann kürzer als zones sein).
    """
    driver = get_sensor_driver()
    readings: list[SensorReading] = []

    for zone in zones:
        try:
            reading = driver.read(zone)
            readings.append(reading)
        except SensorDriverError as e:
            log_event(
                "sensor_read_error",
                level="warning",
                source="sensor",
                zone=zone,
                error=repr(e),
            )

    return readings


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Loop
# ─────────────────────────────────────────────────────────────────────────────

def sensor_engine_loop():
    """Polling-Loop des Sensor-Engines.

    Prüft in konfigurierbaren Abständen alle Sensor-Zonen auf Bewässerungsbedarf
    und stellt bei Bedarf QueueItems ein – exakt wie scheduler_loop, aber
    bedarfsgesteuert statt zeitgesteuert.

    Terminiert sauber wenn shutdown_event gesetzt wird.

    Fehlerbehandlung: Exceptions innerhalb des Loops werden geloggt und
    die Schleife läuft weiter (kein ungesteuerter Thread-Absturz).
    """
    from core.state import shutdown_event
    from core.logging import logger

    log_event("sensor_engine_started", source="system")

    while not shutdown_event.is_set():
        # Phase 1: Polling-Intervall und Zonen-Liste lesen (unter Lock, kurz)
        with state_lock:
            interval_s = max(5, int(getattr(state, "sensor_polling_interval_s", 30)))
            pins = dict(state.sensor_gpio_pins_by_zone or {})

        zones = sorted(pins.keys())

        # Polling-Pause (mit Shutdown-Check).
        # wait() gibt True zurück wenn shutdown_event gesetzt wurde → sofort beenden.
        # Die Pause steht VOR dem Read, damit der erste Zyklus nach dem Start
        # nicht sofort feuert (verhindert Trigger direkt nach Systemstart).
        if shutdown_event.wait(interval_s):
            break

        # Keine Sensoren konfiguriert → Loop läuft weiter, tut nichts
        if not zones:
            continue

        try:
            # Phase 2: Sensoren lesen (OHNE Lock – kann blockieren)
            readings = _read_all_sensors(zones)

            if not readings:
                # Alle Reads fehlgeschlagen (z.B. GPIO-Fehler) → Zyklus überspringen
                continue

            # Phase 3: Readings auswerten und Queue aktualisieren (unter Lock)
            now_m = time.monotonic()

            with state_lock:
                items_to_queue, _ = _process_sensor_cycle_locked(readings, now_m)

                if items_to_queue:
                    state.queue = state.queue or []
                    state.queue.extend(items_to_queue)
                    state.queue_dirty = True
                    # Queue-State auf "läuft" setzen wenn sie vorher idle war
                    if state.queue_state in ("bereit", "fertig"):
                        state.queue_state = "läuft"

        except Exception:
            logger.exception("sensor_engine_loop crashed")
            log_event("sensor_engine_error", level="error", source="system")

    log_event("sensor_engine_stopped", source="system")
