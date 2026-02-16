# app/api/routes_control.py
import time
from datetime import datetime
from fastapi import APIRouter, HTTPException

from core.state import state, state_lock
from core.config import MAX_RUNTIME_S, TZ
from core.logging import log_event
from models.requests import StartRequest, ParallelModeRequest

from services.engine import (
    _start_valve_locked,
    _sync_legacy_single_fields_locked,
    engine_status_payload_locked,
)
from services.persistence import save_runtime_state_to_disk

router = APIRouter()

# ---------------------------
# GET /status
# ---------------------------
@router.get("/status")
def status():
    with state_lock:
        return engine_status_payload_locked()


# ---------------------------
# POST /start -> Startet eine Zone
# ---------------------------
@router.post("/start")
def start(req: StartRequest):
    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))
    if req.zone < 1 or req.zone > max_v:
        raise HTTPException(status_code=400, detail=f"zone muss 1..{max_v} sein.")

    if req.duration <= 0:
        raise HTTPException(status_code=400, detail="Die Laufzeit muss > 0 sein!")
    
    with state_lock:
        max_runtime_s = int(getattr(state, "hard_max_runtime_s", MAX_RUNTIME_S))
    if req.duration > max_runtime_s:
        raise HTTPException(status_code=400, detail=f"Die Maximale Laufzeit ist {max_runtime_s // 60} Minuten!")

    with state_lock:
        _start_valve_locked(
            zone=req.zone,
            duration_s=req.duration,
            time_unit=req.time_unit,
            source="manual",
        )

        # Optional: extra event — _start_valve_locked loggt bereits valve_start.
        # Ich lasse es absichtlich NICHT doppelt loggen.

        return {
            "ok": True,
            "running_zone": req.zone,
            "duration": req.duration,
            "time_unit": req.time_unit,
            "parallel_enabled": state.parallel_enabled,
            "max_concurrent_valves": state.max_concurrent_valves,
        }


# ---------------------------
# POST /pause -> Pausiert alle aktuell laufenden Ventile (global pause)
# ---------------------------
@router.post("/pause")
def pause_current():
    with state_lock:
        if state.running_zone is None or not state.active_runs:
            raise HTTPException(status_code=409, detail="Kein Ventil läuft gerade.")

        if state.paused:
            raise HTTPException(status_code=409, detail="Ventile sind bereits pausiert.")

        from services.valve_driver import get_valve_driver, ValveDriverError
        driver = get_valve_driver()

        failed = []
        for z in sorted(list(state.active_runs.keys())):
            try:
                driver.close(z)
            except Exception as e:
                failed.append({"zone": z, "error": str(e)})

        if failed:
            log_event(
                "valve_hw_error",
                level="error",
                source="manual",
                action="close",
                zone="multiple",
                driver=getattr(driver, "name", "unknown"),
                reason="pause",
                failed=failed,
            )
            raise HTTPException(status_code=503, detail={"message": "Hardware Fehler beim Pausieren", "failed": failed})


        state.paused = True
        state.queue_state_before_valve_pause = state.queue_state

        now_m = time.monotonic()
        for ar in state.active_runs.values():
            if ar.paused_at == 0.0:
                ar.remaining_s = max(0, int(ar.end_time - now_m))
                ar.paused_at = now_m
                ar.end_time = 0.0  # logisch "stoppen" (Timer zählt nicht weiter)

        _sync_legacy_single_fields_locked()

        log_event(
            "valve_pause",
            source="manual",
            zone="all",
            remaining_s=[{"zone": z, "remaining_s": r.remaining_s} for z, r in state.active_runs.items()],
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
            parallel_enabled=state.parallel_enabled,
        )

        return {"ok": True, "paused_zones": sorted(list(state.active_runs.keys()))}


# ---------------------------
# POST /resume -> Setzt pausierte Ventile fort
# ---------------------------
@router.post("/resume")
def resume_current():
    with state_lock:
        if not state.active_runs:
            raise HTTPException(status_code=409, detail="Kein Ventil ist aktiv/pausiert.")

        if not state.paused:
            raise HTTPException(status_code=409, detail="Ventile sind nicht pausiert.")

        from services.valve_driver import get_valve_driver, ValveDriverError
        driver = get_valve_driver()

        failed = []
        # Öffne alle Zonen, die wirklich weiterlaufen sollen
        for z, ar in state.active_runs.items():
            if (ar.remaining_s or 0) <= 0:
                continue
            try:
                driver.open(z)
            except Exception as e:
                failed.append({"zone": int(z), "error": str(e)})

        if failed:
            log_event(
                "valve_hw_error",
                level="error",
                source="manual",
                action="open",
                zone="multiple",
                driver=getattr(driver, "name", "unknown"),
                reason="resume",
                failed=failed,
            )
            # State bleibt pausiert (wir gehen NICHT weiter)
            raise HTTPException(status_code=503, detail={"message": "Hardware Fehler beim Fortsetzen", "failed": failed})


        now_m = time.monotonic()

        for ar in state.active_runs.values():
            if ar.paused_at:
                ar.paused_total_s += (now_m - ar.paused_at)
                ar.paused_at = 0.0

            if ar.remaining_s <= 0:
                ar.end_time = 0.0
            else:
                ar.end_time = time.monotonic() + ar.remaining_s

        state.paused = False
        state.queue_state = state.queue_state_before_valve_pause
        state.queue_state_before_valve_pause = "bereit"

        _sync_legacy_single_fields_locked()

        log_event(
            "valve_resume",
            source="manual",
            zone="all",
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
            parallel_enabled=state.parallel_enabled,
        )

        return {"ok": True, "resumed_zones": sorted(list(state.active_runs.keys()))}


# ---------------------------
# POST /stop -> Stoppt sofort alle Ventile
# ---------------------------
@router.post("/stop")
def stop():
    # History-Update: hier übernehmen wir deine alte Logik 1:1 (pro Zone Duration)
    # Damit es sauber bleibt, schreiben wir Historie hier direkt.
    from services.engine import _history_add_locked  # local import to avoid circular

    with state_lock:
        if not state.active_runs:
            z = state.running_zone
            state.running_zone = None
            _sync_legacy_single_fields_locked()
            return {"ok": True, "stopped_zone": z}

        from services.valve_driver import get_valve_driver
        driver = get_valve_driver()

        now_m = time.monotonic()
        zones = sorted(list(state.active_runs.keys()))
        stopped = []
        failed = []

        # Stop should unpause so the system can recover/retry if needed
        state.paused = False

        for zone in zones:
            ar = state.active_runs.get(zone)
            if not ar:
                continue

            # 1) Hardware close
            try:
                driver.close(zone)
            except Exception as e:
                failed.append({"zone": zone, "error": str(e)})
                continue

            # 2) Only if close succeeded -> compute duration & history & remove run
            paused_total = ar.paused_total_s
            if ar.paused_at:
                paused_total += (now_m - ar.paused_at)

            active = (now_m - ar.started_at) - paused_total
            actual_s = max(0, int(active + 1e-6))

            _history_add_locked(
                zone=zone,
                duration_s=actual_s,
                source=ar.started_source or "manual",
                time_unit=ar.time_unit,
            )

            del state.active_runs[zone]
            stopped.append(zone)

        # Reset pause bookkeeping regardless
        state.remaining_s = 0
        state.queue_state_before_valve_pause = "bereit"

        _sync_legacy_single_fields_locked()

        if failed:
            log_event(
                "valve_hw_error",
                level="error",
                source="manual",
                action="close",
                zone="multiple",
                driver=getattr(driver, "name", "unknown"),
                reason="manual_stop",
                failed=failed,
                stopped=stopped,
            )
            raise HTTPException(
                status_code=503,
                detail={"message": "Nicht alle Ventile konnten gestoppt werden", "stopped": stopped, "failed": failed},
            )

        state.paused = False
        state.remaining_s = 0
        state.queue_state_before_valve_pause = "bereit"

        _sync_legacy_single_fields_locked()

    log_event(
        "valve_stop",
        source="manual",
        zone="all",
        reason="manual_stop",
        queue_state=state.queue_state,
        queue_length=len(state.queue or []),
        parallel_enabled=state.parallel_enabled,
        automation_enabled=state.automation_enabled,
    )

    return {"ok": True, "stopped_zones": stopped}


# ---------------------------
# Automatik global: enable / disable / toggle / status
# ---------------------------
@router.get("/automation")
def get_automation():
    with state_lock:
        return {"automation_enabled": state.automation_enabled}


@router.post("/automation/enable")
def enable_automation():
    with state_lock:
        state.automation_enabled = True
        state.schedules_dirty = True
        now = datetime.now(TZ)
        state.automation_block_run_key = now.strftime("%Y-%m-%d %H:%M")

        log_event("automation_enable", source="manual", automation_enabled=True)
        return {"ok": True, "automation_enabled": state.automation_enabled}


@router.post("/automation/disable")
def disable_automation():
    with state_lock:
        state.automation_enabled = False
        state.schedules_dirty = True
        state.automation_block_run_key = None

        log_event("automation_disable", source="manual", automation_enabled=False)
        return {"ok": True, "automation_enabled": state.automation_enabled}


@router.post("/automation/toggle")
def toggle_automation():
    with state_lock:
        state.automation_enabled = not state.automation_enabled
        state.schedules_dirty = True

        if state.automation_enabled:
            now = datetime.now(TZ)
            state.automation_block_run_key = now.strftime("%Y-%m-%d %H:%M")

        log_event("automation_toggle", source="manual", automation_enabled=state.automation_enabled)
        return {"ok": True, "automation_enabled": state.automation_enabled}


# ---------------------------
# GET /parallel + POST /parallel -> Parallelmodus umschalten
# ---------------------------
@router.get("/parallel")
def get_parallel_mode():
    with state_lock:
        return {
            "parallel_enabled": state.parallel_enabled,
            "max_concurrent_valves": state.max_concurrent_valves,
        }


@router.post("/parallel")
def set_parallel_mode(req: ParallelModeRequest):
    with state_lock:
        prev = bool(state.parallel_enabled)
        state.parallel_enabled = bool(req.enabled)
        _sync_legacy_single_fields_locked()

        log_event(
            "parallel_mode_set",
            source="manual",
            parallel_enabled=state.parallel_enabled,
            max_concurrent_valves=state.max_concurrent_valves,
        )

        # True -> False und es laufen >1 Ventile: nichts abbrechen, aber Queue soll "drain" abwarten
        if prev and (not state.parallel_enabled):
            running = sorted(list((state.active_runs or {}).keys()))
            if len(running) > 1:
                state.parallel_drain_logged = True
                log_event(
                    "parallel_disabled_waiting_for_drain",
                    level="warning",
                    source="manual",
                    running_zones=running,
                    message="Parallelbetrieb deaktiviert, warte bis laufende Ventile 'ausgedünnt' sind.",
                )
        else:
            state.parallel_drain_logged = False

    # Runtime setting persistieren
    save_runtime_state_to_disk()


    return {
        "ok": True,
        "parallel_enabled": state.parallel_enabled,
        "max_concurrent_valves": state.max_concurrent_valves,
    }
