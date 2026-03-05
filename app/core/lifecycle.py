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
  5. IO-Worker starten              (MUSS vor Hardware-Ops starten)
  6. Fail-Safe close_all            (Ventile nach Absturz/Stromausfall schließen)
  7. Runtime-State zurücksetzen     (active_runs leeren, paused=False)
  8. Background-Threads starten     (timer_loop, scheduler_loop, persistence_loop)
  9. Watchdog-Thread starten        (systemd WATCHDOG=1)
 10. READY=1 an systemd senden

Shutdown-Reihenfolge:
  1. STOPPING=1 an systemd senden
  2. shutdown_event setzen           (alle Threads terminieren)
  3. Fail-Safe close_all             (Ventile bei Shutdown schließen)
  4. Dirty-State flushen             (letzte Saves für schedules/queue/history)
  5. Background-Threads joinen       (max. 2s pro Thread)
  6. IO-Worker stoppen               (ZULETZT – nach allen anderen Threads)
  7. GPIO-Cleanup                    (Pins auf Input zurücksetzen)

systemd-Integration:
  Nutzt sd_notify() für READY=1, STOPPING=1 und WATCHDOG=1.
  Auf Nicht-systemd-Systemen (Entwicklung, Tests) ist _sd_notify() ein No-Op.
  Erforderliche .service-Einstellungen: Type=notify, WatchdogSec=30.
"""

import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI

from core.state import state, state_lock, shutdown_event, threads
from core.logging import log_event, logger
from core.config import DEFAULT_PARALLEL_ENABLED, MAX_CONCURRENT_VALVES

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP

    # Sicherheit: API-Key laden/generieren (vor allem anderen)
    load_or_create_api_key()

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

    load_device_config_from_disk()   # Admin hardware config (read-only)
    load_user_settings_from_disk()   # user editable settings
    load_runtime_state_from_disk()   # persisted runtime toggles
    load_schedules_from_disk()
    load_queue_from_disk()
    load_history_from_disk()

    # IO-Worker ZUERST starten (vor Hardware-Operationen!)
    io_worker = get_io_worker()
    io_worker.start()
    log_event("lifecycle_io_worker_started", source="system")

    # FAIL-SAFE: after a crash/power loss, valves might be physically open.
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

    # Reset runtime-only state (safety-first). Persisted queue/schedules/history remain.
    with state_lock:
        state.active_runs = {}
        state.paused = False
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.parallel_drain_logged = False

    shutdown_event.clear()
    threads.clear()

    for fn, name in [(timer_loop, "timer_loop"), (scheduler_loop, "scheduler_loop"), (persistence_loop, "persistence_loop")]:
        th = threading.Thread(target=fn, daemon=True, name=name)
        th.start()
        threads.append(th)

    # Watchdog-Thread: sendet periodisch WATCHDOG=1 an systemd.
    # Läuft als Daemon-Thread, terminiert sauber wenn shutdown_event gesetzt wird.
    th_wd = threading.Thread(
        target=_watchdog_loop,
        args=(shutdown_event, 10.0),
        daemon=True,
        name="watchdog_loop",
    )
    th_wd.start()
    threads.append(th_wd)

    log_event("service_start", source="system", version="v1", persistence=True)

    # systemd signalisieren: Service ist bereit (Type=notify in .service-Datei)
    _sd_notify("READY=1")

    yield

    # SHUTDOWN
    # systemd signalisieren: Graceful Shutdown beginnt (stoppt Watchdog-Timer in systemd)
    _sd_notify("STOPPING=1")

    shutdown_event.set()

    # Best-effort: try to close everything on shutdown as well.
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

    for th in threads:
        try:
            th.join(timeout=2.0)
        except Exception:
            pass

    # IO-Worker ZULETZT stoppen (nach allen anderen Threads)
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

    # GPIO-Cleanup nach IO-Worker-Shutdown: Pins auf Input zurücksetzen.
    # Reihenfolge ist sicherheitskritisch: close_all() (oben) → io_worker.shutdown() → cleanup().
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

    log_event("service_stop", source="system")
