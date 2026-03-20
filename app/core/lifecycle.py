# core/lifecycle.py
"""
FastAPI Lifespan: Startup- und Shutdown-Sequenz des Bewässerungscomputers.

Dieser Modul verwaltet den gesamten Lebenszyklus der Anwendung als
asynccontextmanager (`lifespan`), der an die FastAPI-App übergeben wird.

Startup-Reihenfolge (kritisch – nicht umstellen!):
  1. API-Key laden/generieren       (Sicherheit vor allem anderen)
  2. State initialisieren           (Defaults setzen)
  3. Konfigurationen laden          (device_config, user_settings, runtime_state)
  4. Persistierte Daten laden       (schedules, queue, history)
  5. Sentinel-Check                 (running.lock → unclean_restart erkennen)
  6. IO-Worker starten              (MUSS vor Hardware-Ops starten)
  7. Fail-Safe close_all            (Ventile nach Absturz/Stromausfall schließen)
  8. Runtime-State zurücksetzen     (active_runs leeren, paused=False)
  9. Background-Threads starten     (timer_loop, scheduler_loop, persistence_loop,
                                     sensor_engine_loop)
 10. Watchdog-Thread starten        (systemd WATCHDOG=1)
 11. running.lock anlegen           (signalisiert: Service läuft sauber)
 12. READY=1 an systemd senden

Shutdown-Reihenfolge:
  1. running.lock löschen           (SOFORT – vor STOPPING=1; sichert saubere Erkennung)
  2. STOPPING=1 an systemd senden
  3. shutdown_event setzen           (alle Threads terminieren)
  4. Fail-Safe close_all             (Ventile bei Shutdown schließen)
  5. Dirty-State flushen             (letzte Saves für schedules/queue/history)
  6. Background-Threads joinen       (max. 2s pro Thread)
  7. IO-Worker stoppen               (ZULETZT – nach allen anderen Threads)
  8. GPIO-Cleanup                    (Valve-Driver und Sensor-Driver Handles freigeben)

Sentinel-File-Muster (Neustart-Erkennung):
  running.lock wird beim Start angelegt und beim Shutdown als ERSTES gelöscht.
  Existiert die Datei beim nächsten Startup → letzter Shutdown war nicht sauber
  (Stromausfall, SIGKILL, OOM-Kill). Der State-Wert unclean_restart wird gesetzt
  und über POST /system/ack-restart quittiert. Muster analog zu PostgreSQL WAL,
  SQLite lock-File und Redis RDB-Prüfung.

systemd-Integration:
  Nutzt sd_notify() für READY=1, STOPPING=1 und WATCHDOG=1.
  Auf Nicht-systemd-Systemen (Entwicklung, Tests) ist _sd_notify() ein No-Op.
  Erforderliche .service-Einstellungen: Type=notify, WatchdogSec=30.
"""

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI

from core.state import state, state_lock, shutdown_event, threads
from core.logging import log_event, logger
from core.config import (
    DEFAULT_PARALLEL_ENABLED,
    MAX_CONCURRENT_VALVES,
    RUNNING_LOCK_FILE,
    TZ,
)

from services.persistence import (
    load_schedules_from_disk, load_queue_from_disk, load_history_from_disk,
    save_schedules_to_disk, save_queue_to_disk, save_history_to_disk,
    load_device_config_from_disk,
    load_user_settings_from_disk,
    load_runtime_state_from_disk,
    save_runtime_state_to_disk,
)
from services.timer import timer_loop
from services.scheduler import scheduler_loop
from services.persistence import persistence_loop
from services.sensor_engine import sensor_engine_loop

from services.io_worker import get_io_worker, IOCommand
from core.security import load_or_create_api_key


# ---------------------------------------------------------------------------
# systemd sd_notify Integration
#
# Sendet Statussignale an systemd wenn die systemd-python Bibliothek verfügbar
# ist. Auf Nicht-systemd-Systemen (Entwicklung, Tests) ist _sd_notify() ein
# reines No-Op – kein Import-Fehler, kein Crash.
#
# Wichtige Signale:
#   READY=1     → Service ist bereit (ExecStart abgeschlossen)
#   STOPPING=1  → Graceful Shutdown beginnt
#   WATCHDOG=1  → Watchdog-Keepalive (muss innerhalb WatchdogSec/2 gesendet werden)
#
# Voraussetzung in der .service-Datei:
#   Type=notify
#   WatchdogSec=30
# ---------------------------------------------------------------------------

def _sd_notify(msg: str) -> None:
    """Sendet eine Statusmeldung an systemd (No-Op wenn systemd nicht verfügbar)."""
    try:
        import systemd.daemon  # type: ignore
        systemd.daemon.notify(msg)
    except ImportError:
        pass  # Nicht auf systemd-System (Dev/Test) → erwartetes Verhalten
    except Exception:
        logger.debug("sd_notify(%r) fehlgeschlagen", msg)


def _watchdog_loop(shutdown_ev: threading.Event, interval_s: float = 10.0) -> None:
    """Sendet periodisch WATCHDOG=1 an systemd.

    Hält den systemd Hardware-Watchdog am Leben (WatchdogSec=30 in .service).
    Wenn dieser Thread stoppt oder der Prozess hängt, erkennt systemd das nach
    WatchdogSec Sekunden und startet den Service neu (Restart=on-failure).

    interval_s MUSS deutlich kleiner als WatchdogSec/2 sein (10s << 15s).
    Der Loop terminiert sauber wenn shutdown_ev gesetzt wird.
    """
    log_event("watchdog_loop_started", source="system", interval_s=interval_s)
    while not shutdown_ev.wait(timeout=interval_s):
        _sd_notify("WATCHDOG=1")
    log_event("watchdog_loop_stopped", source="system")


# ---------------------------------------------------------------------------
# Sentinel-File-Hilfsfunktionen (Neustart-Erkennung)
#
# Muster: running.lock liegt in data/. Beim Start prüfen ob sie existiert
# (= unclean shutdown). Beim Start anlegen, beim Shutdown sofort löschen.
# ---------------------------------------------------------------------------

def _check_sentinel_file() -> None:
    """Prüft ob running.lock existiert – signalisiert unclean Shutdown.

    Setzt state.unclean_restart=True wenn die Datei existiert (Stromausfall,
    SIGKILL, OOM-Kill). Bei sauberem ersten Start ist die Datei nicht vorhanden.

    Muss NACH den load_*_from_disk()-Aufrufen und VOR dem State-Reset aufgerufen
    werden, damit das Flag beim Start korrekt gesetzt ist.
    """
    lock_exists = os.path.exists(RUNNING_LOCK_FILE)
    now_str = datetime.now(TZ).isoformat(timespec="seconds")

    with state_lock:
        if lock_exists:
            state.unclean_restart = True
            state.restart_detected_at = now_str
        else:
            state.unclean_restart = False
            state.restart_detected_at = ""

    if lock_exists:
        log_event(
            "unclean_restart_detected",
            source="system",
            detected_at=now_str,
        )
    else:
        log_event("clean_restart_detected", source="system")


def _create_running_lock() -> None:
    """Legt running.lock an – signalisiert dass der Service sauber läuft.

    Wird kurz VOR READY=1 aufgerufen, nach Fail-Safe close_all und
    State-Reset. Schreib-Fehler werden geloggt aber nicht als fatal behandelt
    (der Service startet trotzdem; Neustart-Erkennung funktioniert nur nicht).
    """
    try:
        with open(RUNNING_LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.now(TZ).isoformat(timespec="seconds"))
        log_event("running_lock_created", source="system")
    except Exception as e:
        logger.exception("running.lock konnte nicht angelegt werden")
        log_event(
            "running_lock_create_failed",
            level="error",
            source="system",
            error=repr(e),
        )


def _delete_running_lock() -> None:
    """Löscht running.lock – signalisiert sauberen Shutdown.

    Wird als ALLERERSTE Aktion im Shutdown aufgerufen, damit auch bei sehr
    kurzen Shutdown-Fenstern (z.B. systemd TimeoutStopSec) die Datei weg ist,
    bevor der Prozess beendet wird.

    Best-effort: Fehler werden geloggt aber nicht weiter propagiert.
    """
    try:
        if os.path.exists(RUNNING_LOCK_FILE):
            os.remove(RUNNING_LOCK_FILE)
            log_event("running_lock_deleted", source="system")
    except Exception as e:
        logger.exception("running.lock konnte nicht gelöscht werden")
        log_event(
            "running_lock_delete_failed",
            level="error",
            source="system",
            error=repr(e),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # =========================================================================
    # STARTUP
    # =========================================================================

    # 1. Sicherheit: API-Key laden/generieren (vor allem anderen)
    load_or_create_api_key()

    # 2. State initialisieren (Defaults setzen)
    with state_lock:
        state.queue = state.queue or []
        state.schedules = state.schedules or []
        state.run_history = state.run_history or []
        state.active_runs = state.active_runs or {}

        state.parallel_enabled = DEFAULT_PARALLEL_ENABLED
        state.max_concurrent_valves = MAX_CONCURRENT_VALVES

        from core.config import MAX_VALVES
        state.max_valves = int(MAX_VALVES)

        state.paused = False

    # 3. + 4. Konfigurationen und persistierte Daten laden
    load_device_config_from_disk()   # Admin hardware config (read-only)
    load_user_settings_from_disk()   # user editable settings
    load_runtime_state_from_disk()   # persisted runtime toggles
    load_schedules_from_disk()
    load_queue_from_disk()
    load_history_from_disk()

    # 5. Sentinel-Check: Stromausfall / Crash-Erkennung
    # Muss NACH load_*_from_disk() (DATA_DIR ist dann garantiert vorhanden)
    # und VOR dem State-Reset aufgerufen werden.
    _check_sentinel_file()

    # 6. IO-Worker ZUERST starten (vor Hardware-Operationen!)
    io_worker = get_io_worker()
    io_worker.start()
    log_event("lifecycle_io_worker_started", source="system")

    # 7. FAIL-SAFE: after a crash/power loss, valves might be physically open.
    # Always try to close everything on startup (best effort).
    try:
        cmd = IOCommand(action="close_all")
        result = io_worker.send_command(cmd, timeout_s=10.0)

        if result.success:
            log_event(
                "failsafe_close_all_startup",
                source="system",
                duration_ms=result.duration_ms
            )
        else:
            log_event(
                "failsafe_close_all_startup_failed",
                level="error",
                source="system",
                error=result.error
            )
    except Exception as e:
        logger.exception("failsafe close_all on startup failed")
        log_event(
            "failsafe_close_all_startup_exception",
            level="error",
            source="system",
            error=repr(e),
        )

    # 8. Reset runtime-only state (safety-first). Persisted queue/schedules/history remain.
    # unclean_restart bleibt erhalten – wird erst durch ACK zurückgesetzt.
    with state_lock:
        state.active_runs = {}
        state.paused = False
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.parallel_drain_logged = False
        # Sensor-Laufzeitdaten zurücksetzen: Readings und Cooldown-Timestamps
        # sind nach einem Neustart nicht mehr gültig.
        state.sensor_readings = {}
        state.sensor_last_triggered = {}

    shutdown_event.clear()
    threads.clear()

    # 9. Background-Threads starten
    for fn, name in [
        (timer_loop,         "timer_loop"),
        (scheduler_loop,     "scheduler_loop"),
        (persistence_loop,   "persistence_loop"),
        (sensor_engine_loop, "sensor_engine_loop"),
    ]:
        th = threading.Thread(target=fn, daemon=True, name=name)
        th.start()
        threads.append(th)

    # 10. Watchdog-Thread: sendet periodisch WATCHDOG=1 an systemd.
    # Läuft als Daemon-Thread, terminiert sauber wenn shutdown_event gesetzt wird.
    th_wd = threading.Thread(
        target=_watchdog_loop,
        args=(shutdown_event, 10.0),
        daemon=True,
        name="watchdog_loop",
    )
    th_wd.start()
    threads.append(th_wd)

    # 11. running.lock anlegen (nach allem anderen, kurz vor READY=1)
    # Ab diesem Punkt gilt: wenn der Prozess ohne sauberen Shutdown stirbt,
    # wird beim nächsten Start unclean_restart erkannt.
    _create_running_lock()

    log_event("service_start", source="system", version="v1", persistence=True)

    # 12. systemd signalisieren: Service ist bereit (Type=notify in .service-Datei)
    _sd_notify("READY=1")

    yield

    # =========================================================================
    # SHUTDOWN
    # =========================================================================

    # 1. running.lock SOFORT löschen – allererste Shutdown-Aktion.
    # Selbst bei sehr kurzem TimeoutStopSec ist die Datei damit garantiert weg,
    # bevor der Prozess beendet wird → nächster Start erkennt sauberen Shutdown.
    _delete_running_lock()

    # 2. systemd signalisieren: Graceful Shutdown beginnt
    _sd_notify("STOPPING=1")

    shutdown_event.set()

    # 3. Best-effort: try to close everything on shutdown as well.
    try:
        cmd = IOCommand(action="close_all")
        result = io_worker.send_command(cmd, timeout_s=10.0)

        if result.success:
            log_event(
                "failsafe_close_all_shutdown",
                source="system",
                duration_ms=result.duration_ms
            )
        else:
            log_event(
                "failsafe_close_all_shutdown_failed",
                level="error",
                source="system",
                error=result.error
            )
    except Exception as e:
        logger.exception("failsafe close_all on shutdown failed")
        log_event(
            "failsafe_close_all_shutdown_exception",
            level="error",
            source="system",
            error=repr(e),
        )

    # 4. Dirty-State flushen
    try:
        with state_lock:
            do_sched = bool(state.schedules_dirty)
            do_queue = bool(state.queue_dirty)
            do_hist = bool(state.history_dirty)

        if do_sched:
            save_schedules_to_disk()
        if do_queue:
            save_queue_to_disk()
        if do_hist:
            save_history_to_disk()

    except Exception:
        logger.exception("shutdown flush failed")
        log_event("shutdown_flush_error", level="error", source="system")

    # 5. Background-Threads joinen (inkl. sensor_engine_loop)
    for th in threads:
        try:
            th.join(timeout=2.0)
        except Exception:
            pass

    # 6. IO-Worker ZULETZT stoppen (nach allen anderen Threads)
    try:
        io_worker.shutdown(timeout_s=5.0)
        log_event("lifecycle_io_worker_stopped", source="system")
    except Exception as e:
        logger.exception("IO-Worker shutdown failed")
        log_event(
            "lifecycle_io_worker_shutdown_failed",
            level="error",
            source="system",
            error=repr(e)
        )

    # 7. GPIO-Cleanup nach IO-Worker-Shutdown.
    # Reihenfolge ist sicherheitskritisch: close_all() → io_worker.shutdown() → cleanup().
    # Valve-Driver zuerst (safety-critical), dann Sensor-Driver.

    # Valve-Driver: Pins auf Input zurücksetzen.
    # SimValveDriver.cleanup() ist ein No-Op.
    try:
        from services.valve_driver import get_valve_driver
        get_valve_driver().cleanup()
    except Exception as e:
        logger.exception("valve driver cleanup failed")
        log_event(
            "valve_driver_cleanup_failed",
            level="error",
            source="system",
            error=repr(e)
        )

    # Sensor-Driver: GPIO-Handle freigeben.
    # SimSensorDriver.cleanup() ist ein No-Op.
    # Sensor-Engine-Thread ist bereits gejoint (Schritt 5) → kein Use-after-Free.
    try:
        from services.sensor_driver import get_sensor_driver
        get_sensor_driver().cleanup()
    except Exception as e:
        logger.exception("sensor driver cleanup failed")
        log_event(
            "sensor_driver_cleanup_failed",
            level="error",
            source="system",
            error=repr(e)
        )

    log_event("service_stop", source="system")
