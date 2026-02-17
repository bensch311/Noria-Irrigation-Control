import time
import math
from typing import Dict
from fastapi import HTTPException

from core.state import state, state_lock, ActiveRun, QueueItem, HistoryItem
from core.config import MAX_HISTORY_ITEMS
from core.logging import log_event
from core.config import TZ
from datetime import datetime

def _sync_legacy_single_fields_locked():
    if not state.active_runs:
        state.running_zone = None
        state.end_time = 0.0
        state.time_unit = "Minuten"
        state.remaining_s = 0
        state.started_at = 0.0
        state.started_source = "manual"
        state.started_planned_s = 0
        return

    primary_zone = sorted(state.active_runs.keys())[0]
    ar = state.active_runs[primary_zone]

    state.running_zone = ar.zone
    state.end_time = ar.end_time
    state.time_unit = ar.time_unit
    state.remaining_s = ar.remaining_s
    state.started_at = ar.started_at
    state.started_source = ar.started_source
    state.started_planned_s = ar.started_planned_s

def _can_start_new_valve_locked() -> bool:
    # If hardware is faulted, block any new starts (manual/queue/schedule)
    if getattr(state, "hw_faulted", False):
        return False
    if not state.parallel_enabled:
        return len(state.active_runs or {}) == 0
    return len(state.active_runs or {}) < max(1, int(state.max_concurrent_valves))

def _active_runs_snapshot_locked() -> Dict[int, dict]:
    now_m = time.monotonic()
    out: Dict[int, dict] = {}
    for zone, ar in (state.active_runs or {}).items():
        if state.paused:
            remaining_s = int(ar.remaining_s or 0)
        else:
            if ar.end_time and ar.end_time > 0.0:
                remaining_s = max(0, int(ar.end_time - now_m))
            else:
                remaining_s = int(ar.remaining_s or 0)

        out[int(zone)] = {
            "remaining_s": int(remaining_s),
            "time_unit": ar.time_unit,
            "started_source": ar.started_source,
            "planned_s": int(ar.started_planned_s),
        }
    return out

def _history_add_locked(zone: int, duration_s: int, source: str, time_unit: str):
    if state.run_history is None:
        state.run_history = []

    item = HistoryItem(
        ts_end=datetime.now(TZ).isoformat(timespec="seconds"),
        zone=zone,
        duration_s=max(0, int(duration_s)),
        source=source,
        time_unit=time_unit or "Sekunden",
    )
    state.run_history.insert(0, item)
    limit = int(getattr(state, "max_history_items", MAX_HISTORY_ITEMS))
    limit = max(1, limit)
    if len(state.run_history) > limit:
        state.run_history = state.run_history[:limit]

    state.history_dirty = True

def _calc_actual_run_s_primary(now_m: float) -> int:
    if not state.started_at:
        return 0

    paused_total = state.paused_total_s
    if state.paused_at:
        paused_total += (now_m - state.paused_at)

    active = (now_m - state.started_at) - paused_total
    if active <= 0:
        return 0

    had_pause = (state.paused_total_s > 0.0) or (state.paused_at > 0.0)
    eps = 1e-6
    return int(math.ceil(active - eps)) if had_pause else int(math.floor(active + eps))

def _start_valve_locked(zone: int, duration_s: int, time_unit: str, source: str):
    if state.active_runs is None:
        state.active_runs = {}

    if zone in state.active_runs:
        raise HTTPException(status_code=409, detail=f"Ventil {zone} läuft bereits!")

    if not _can_start_new_valve_locked():
        if state.parallel_enabled:
            raise HTTPException(status_code=409, detail=f"Max. parallele Ventile erreicht ({state.max_concurrent_valves}).")
        raise HTTPException(status_code=409, detail=f"Es läuft bereits Ventil {state.running_zone}!")

    # --- Hardware open MUST succeed before we consider the run active ---
    from services.valve_driver import get_valve_driver, ValveDriverError  # local import to avoid cycles
    driver = get_valve_driver()
    try:
        driver.open(zone)
    except ValveDriverError as e:
        log_event(
            "valve_hw_error",
            level="error",
            source=source,
            action="open",
            zone=zone,
            driver=getattr(driver, "name", "unknown"),
            error=str(e),
        )
        raise HTTPException(status_code=503, detail=f"Hardware Fehler beim Öffnen von Ventil {zone}: {e}")
    except Exception as e:
        log_event(
            "valve_hw_error",
            level="error",
            source=source,
            action="open",
            zone=zone,
            driver=getattr(driver, "name", "unknown"),
            error=repr(e),
        )
        raise HTTPException(status_code=503, detail=f"Unerwarteter Hardware Fehler beim Öffnen von Ventil {zone}")

    now_m = time.monotonic()
    state.active_runs[zone] = ActiveRun(
        zone=zone,
        end_time=now_m + duration_s,
        time_unit=time_unit,
        started_at=now_m,
        started_source=source,
        started_planned_s=int(duration_s),
    )

    _sync_legacy_single_fields_locked()

    log_event(
        "valve_start",
        source=source,
        zone=zone,
        duration_s=duration_s,
        time_unit=time_unit,
        queue_state=state.queue_state,
        queue_length=len(state.queue or []),
        automation_enabled=state.automation_enabled,
        parallel_enabled=state.parallel_enabled,
        max_concurrent_valves=state.max_concurrent_valves,
    )

def start_queue_item(item: QueueItem):
    _start_valve_locked(
        zone=item.zone,
        duration_s=item.duration,
        time_unit=item.time_unit,
        source=item.source,
    )

def engine_status_payload_locked() -> dict:
    # lazy import: verhindert unnötige Import-Ketten beim Modul-Import
    from services.valve_driver import get_valve_driver
    driver_name = get_valve_driver().name

    q = state.queue or []
    schedules_count = len(state.schedules or [])
    automation_enabled = getattr(state, "automation_enabled", True)

    if state.running_zone is None:
        return {
            "state": "bereit",
            "running_zone": None,
            "remaining_time": 0,
            "paused": False,
            "queue_state": state.queue_state,
            "queue_length": len(q),
            "schedules_count": schedules_count,
            "automation_enabled": automation_enabled,
            "parallel_enabled": state.parallel_enabled,
            "max_concurrent_valves": state.max_concurrent_valves,
            "running_zones": sorted(list((state.active_runs or {}).keys())),
            "active_runs": _active_runs_snapshot_locked(),
            "valve_driver": driver_name,
            "hw_faulted": bool(getattr(state, "hw_faulted", False)),
            "hw_fault_reason": getattr(state, "hw_fault_reason", ""),
            "hw_fault_zone": getattr(state, "hw_fault_zone", None),
            "hw_fault_since": getattr(state, "hw_fault_since", ""),
        }

    if state.paused:
        remaining = state.remaining_s
        valve_state = "pausiert"
    else:
        remaining = max(0, int(state.end_time - time.monotonic()))
        valve_state = "läuft"

    return {
        "state": valve_state,
        "running_zone": state.running_zone,
        "remaining_time": remaining,
        "time_unit": state.time_unit,
        "paused": state.paused,
        "queue_state": state.queue_state,
        "queue_length": len(q),
        "schedules_count": schedules_count,
        "automation_enabled": automation_enabled,
        "parallel_enabled": state.parallel_enabled,
        "max_concurrent_valves": state.max_concurrent_valves,
        "running_zones": sorted(list((state.active_runs or {}).keys())),
        "active_runs": _active_runs_snapshot_locked(),
        "valve_driver": driver_name,
        "hw_faulted": bool(getattr(state, "hw_faulted", False)),
        "hw_fault_reason": getattr(state, "hw_fault_reason", ""),
        "hw_fault_zone": getattr(state, "hw_fault_zone", None),
        "hw_fault_since": getattr(state, "hw_fault_since", ""),
    }

# Exports (für andere services/api)
__all__ = [
    "_sync_legacy_single_fields_locked",
    "_can_start_new_valve_locked",
    "_start_valve_locked",
    "_history_add_locked",
    "_calc_actual_run_s_primary",
    "start_queue_item",
    "engine_status_payload_locked",
    "_active_runs_snapshot_locked",
]
