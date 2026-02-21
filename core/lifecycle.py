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

        state.running_zone = None
        state.end_time = 0.0
        state.paused = False
        state.remaining_s = 0

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
        state.running_zone = None
        state.end_time = 0.0
        state.paused = False
        state.remaining_s = 0
        state.started_at = 0.0
        state.started_source = "manual"
        state.started_planned_s = 0
        state.paused_at = 0.0
        state.paused_total_s = 0.0
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.parallel_drain_logged = False

    shutdown_event.clear()
    threads.clear()

    for fn, name in [(timer_loop, "timer_loop"), (scheduler_loop, "scheduler_loop"), (persistence_loop, "persistence_loop")]:
        th = threading.Thread(target=fn, daemon=True, name=name)
        th.start()
        threads.append(th)

    log_event("service_start", source="system", version="v1", persistence=True)

    yield

    # SHUTDOWN
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

    log_event("service_stop", source="system")
