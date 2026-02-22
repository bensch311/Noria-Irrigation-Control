# app/api/routes_schedule.py
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request

from core.state import state, state_lock, ScheduleRule
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT
from models.requests import ScheduleAddRequest

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get("/schedule")
def get_schedules():
    with state_lock:
        rules = state.schedules or []
        return {
            "count": len(rules),
            "items": [
                {
                    "id": r.id,
                    "zone": r.zone,
                    "weekdays": r.weekdays,
                    "start_times": r.start_times,
                    "duration_s": r.duration_s,
                    "time_unit": r.time_unit,
                    "repeat": r.repeat,
                    "enabled": r.enabled,
                    "last_run_on": r.last_run_on,
                    "once_pending": r.once_pending,
                }
                for r in rules
            ],
        }


@router.post("/schedule/add")
@limiter.limit(MUTATION_LIMIT)
def add_schedule(request: Request, req: ScheduleAddRequest):
    with state_lock:
        max_runtime_s = int(getattr(state, "hard_max_runtime_s", 3600))
        max_v = int(getattr(state, "max_valves", 1))

    if req.zone != 0 and (req.zone < 1 or req.zone > max_v):
        raise HTTPException(status_code=400, detail=f"zone muss 0 (alle) oder 1..{max_v} sein.")

    for wd in req.weekdays:
        if wd < 0 or wd > 6:
            raise HTTPException(status_code=400, detail="weekdays muss Werte 0..6 enthalten (0=Mo..6=So).")

    for t in req.start_times:
        if len(t) != 5 or t[2] != ":":
            raise HTTPException(status_code=400, detail="start_times muss Format 'HH:MM' haben.")
        hh, mm = t.split(":")
        if not (hh.isdigit() and mm.isdigit()):
            raise HTTPException(status_code=400, detail="start_times muss Format 'HH:MM' haben.")
        hhi, mmi = int(hh), int(mm)
        if hhi < 0 or hhi > 23 or mmi < 0 or mmi > 59:
            raise HTTPException(status_code=400, detail="start_times muss gültige Uhrzeiten enthalten.")

    if req.duration_s > max_runtime_s:
        raise HTTPException(status_code=400, detail=f"duration_s darf max. {max_runtime_s} Sekunden sein.")

    once_pending = None
    if not req.repeat:
        once_pending = [f"{wd} {t}" for wd in req.weekdays for t in req.start_times]

    rule = ScheduleRule(
        id=str(uuid.uuid4())[:8],
        zone=req.zone,
        weekdays=req.weekdays,
        start_times=req.start_times,
        duration_s=req.duration_s,
        time_unit=req.time_unit,
        repeat=req.repeat,
        enabled=True,
        once_pending=once_pending,
    )

    with state_lock:
        state.schedules = state.schedules or []
        state.schedules.append(rule)
        state.schedules_dirty = True

    log_event(
        "schedule_add",
        source="manual",
        schedule_id=rule.id,
        zone=rule.zone,
        weekdays=rule.weekdays,
        start_times=rule.start_times,
        duration_s=rule.duration_s,
        time_unit=rule.time_unit,
        repeat=rule.repeat,
    )
    return {"ok": True, "id": rule.id}


@router.post("/schedule/enable/{schedule_id}")
@limiter.limit(MUTATION_LIMIT)
def enable_schedule(request: Request, schedule_id: str):
    with state_lock:
        for r in state.schedules or []:
            if r.id == schedule_id:
                r.enabled = True
                state.schedules_dirty = True
                log_event("schedule_enable", source="manual", schedule_id=r.id, zone=r.zone)
                return {"ok": True, "enabled": True}
    raise HTTPException(status_code=404, detail="Schedule nicht gefunden.")


@router.post("/schedule/disable/{schedule_id}")
@limiter.limit(MUTATION_LIMIT)
def disable_schedule(request: Request, schedule_id: str):
    with state_lock:
        for r in state.schedules or []:
            if r.id == schedule_id:
                r.enabled = False
                state.schedules_dirty = True
                log_event("schedule_disable", source="manual", schedule_id=r.id, zone=r.zone)
                return {"ok": True, "enabled": False}
    raise HTTPException(status_code=404, detail="Schedule nicht gefunden.")


@router.delete("/schedule")
@limiter.limit(MUTATION_LIMIT)
def delete_schedules(request: Request, ids: list[str]):
    with state_lock:
        rules = state.schedules or []
        new_rules = [r for r in rules if r.id not in ids]
        if len(new_rules) == len(rules):
            raise HTTPException(status_code=404, detail="Keine Schedules gefunden.")
        state.schedules = new_rules
        state.schedules_dirty = True

    log_event("schedule_delete", source="manual", deleted_ids=ids, remaining_count=len(state.schedules or []))
    return {"deleted": ids}
