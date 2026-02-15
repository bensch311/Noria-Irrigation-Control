from fastapi import APIRouter, HTTPException

from core.state import state, state_lock, QueueItem
from core.config import MAX_RUNTIME_S
from core.logging import log_event
from models.requests import QueueAddRequest
from services.engine import _can_start_new_valve_locked, start_queue_item

router = APIRouter()

@router.get("/queue")
def get_queue():
    with state_lock:
        q = state.queue or []
        return {
            "queue_state": state.queue_state,
            "queue_length": len(q),
            "items": [{"zone": i.zone, "duration": i.duration, "time_unit": i.time_unit} for i in q],
        }

@router.post("/queue/add")
def queue_add(req: QueueAddRequest):
    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))
    if req.zone < 1 or req.zone > max_v:
        raise HTTPException(status_code=400, detail=f"zone muss 1..{max_v} sein.")

    if req.duration <= 0:
        raise HTTPException(status_code=400, detail="Die Laufzeit muss > 0 sein!")
    if req.duration > MAX_RUNTIME_S:
        raise HTTPException(status_code=400, detail=f"Die Maximale Laufzeit ist {MAX_RUNTIME_S // 60} Minuten!")

    with state_lock:
        if state.queue_state == "fertig":
            state.queue_state = "bereit"
        state.queue = state.queue or []
        state.queue.append(QueueItem(zone=req.zone, duration=req.duration, time_unit=req.time_unit, source="queue"))
        state.queue_dirty = True

        log_event(
            "queue_add",
            source="manual",
            zone=req.zone,
            duration_s=req.duration,
            time_unit=req.time_unit,
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
        )

        return {"ok": True, "queue_length": len(state.queue)}

@router.post("/queue/start")
def queue_start():
    with state_lock:
        if not state.queue:
            raise HTTPException(status_code=400, detail="Die Warteschlange ist leer.")

        state.queue_state = "läuft"
        state.queue_dirty = True

        if not state.paused and state.queue_state != "pausiert":
            while state.queue and _can_start_new_valve_locked():
                next_item = state.queue.pop(0)
                state.queue_dirty = True
                start_queue_item(next_item)

        log_event("queue_start", source="manual", queue_state=state.queue_state, queue_length=len(state.queue or []))
        return {"ok": True, "queue_state": state.queue_state}

@router.post("/queue/pause")
def queue_pause():
    with state_lock:
        state.queue_state = "pausiert"
        state.queue_dirty = True

    log_event("queue_pause", source="manual", queue_state=state.queue_state, queue_length=len(state.queue or []))
    return {"ok": True, "queue_state": state.queue_state, "message": "Warteschlange pausiert"}

@router.post("/queue/clear")
def queue_clear():
    with state_lock:
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.queue = state.queue or []
        state.queue.clear()
        state.queue_dirty = True

    log_event("queue_clear", source="manual", queue_state=state.queue_state, queue_length=0)
    return {"ok": True, "queue_state": state.queue_state, "queue_length": 0}
