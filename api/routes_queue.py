# app/api/routes_queue.py
from fastapi import APIRouter, Depends, HTTPException, Request

from core.state import state, state_lock, QueueItem
from core.config import MAX_RUNTIME_S, MAX_QUEUE_ITEMS
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT
from models.requests import QueueAddRequest
from services.engine import _can_start_new_valve_locked, start_queue_item

router = APIRouter(dependencies=[Depends(require_api_key)])


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
@limiter.limit(MUTATION_LIMIT)
def queue_add(request: Request, req: QueueAddRequest):
    """Fügt ein oder mehrere Items zur Warteschlange hinzu.

    zone=0  → Alle Ventile (1..max_valves) werden als einzelne Items
              sequenziell in die Queue eingereiht. Entspricht dem
              Verhalten von ScheduleAddRequest mit zone=0.
    zone>=1 → Einzelnes Ventil wie bisher.

    Kapazitätslimit:
    Die Queue darf MAX_QUEUE_ITEMS Einträge nicht überschreiten (DoS-Schutz).
    Wird das Limit überschritten, gibt die Route 400 zurück. Der Prüfung
    findet UNTER dem State-Lock statt, um TOCTOU-Races zu vermeiden.
    """
    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))

    if req.zone > max_v:
        raise HTTPException(status_code=400, detail=f"zone muss 0 (alle) oder 1..{max_v} sein.")

    if req.duration <= 0:
        raise HTTPException(status_code=400, detail="Die Laufzeit muss > 0 sein!")

    with state_lock:
        max_runtime_s = int(getattr(state, "hard_max_runtime_s", MAX_RUNTIME_S))
    if req.duration > max_runtime_s:
        raise HTTPException(status_code=400, detail=f"Die Maximale Laufzeit ist {max_runtime_s // 60} Minuten!")

    # zone=0: alle Ventile sequenziell einreihen
    zones_to_add = list(range(1, max_v + 1)) if req.zone == 0 else [req.zone]

    with state_lock:
        state.queue = state.queue or []

        # Kapazitätsprüfung BEVOR der Zustand verändert wird (TOCTOU-sicher).
        if len(state.queue) + len(zones_to_add) > MAX_QUEUE_ITEMS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Queue voll: {len(state.queue)} Einträge vorhanden, "
                    f"maximal {MAX_QUEUE_ITEMS} erlaubt. "
                    "Bitte zuerst die Queue leeren oder abarbeiten."
                ),
            )

        if state.queue_state == "fertig":
            state.queue_state = "bereit"
        for z in zones_to_add:
            state.queue.append(QueueItem(zone=z, duration=req.duration, time_unit=req.time_unit, source="queue"))
        state.queue_dirty = True

        log_event(
            "queue_add",
            source="manual",
            zone=req.zone,          # 0 = alle, sonst konkrete Zone
            zones_added=zones_to_add,
            duration_s=req.duration,
            time_unit=req.time_unit,
            queue_state=state.queue_state,
            queue_length=len(state.queue),
        )

        return {"ok": True, "queue_length": len(state.queue), "zones_added": len(zones_to_add)}


@router.post("/queue/start")
@limiter.limit(MUTATION_LIMIT)
def queue_start(request: Request):
    # Phase 1: Items sammeln die gestartet werden können (unter Lock)
    items_to_start = []
    with state_lock:
        if not state.queue:
            raise HTTPException(status_code=400, detail="Die Warteschlange ist leer.")

        state.queue_state = "läuft"
        state.queue_dirty = True

        # Sammle Items die gestartet werden können.
        # WICHTIG: Wir müssen manuell tracken wie viele wir schon gesammelt haben,
        # weil active_runs erst nach Lock-Release upgedatet wird!
        if not state.paused and state.queue_state != "pausiert":
            collected_count = 0

            while state.queue:
                # Simuliere _can_start_new_valve_locked() für ZUKÜNFTIGE active_runs
                current_running = len(state.active_runs or {})
                future_running = current_running + collected_count

                # Check ob wir noch mehr starten können
                can_start_more = False
                if getattr(state, "hw_faulted", False):
                    # Hardware-Fault → nichts mehr starten
                    can_start_more = False
                elif not state.parallel_enabled:
                    # Seriell-Modus: nur wenn nichts läuft (aktuell + zukünftig)
                    can_start_more = (future_running == 0)
                else:
                    # Parallel-Modus: Check gegen Limit
                    max_conc = max(1, int(state.max_concurrent_valves))
                    can_start_more = (future_running < max_conc)

                if not can_start_more:
                    break  # Keine weiteren Items sammeln

                # Item aus Queue nehmen und zum Start vormerken
                next_item = state.queue.pop(0)
                items_to_start.append(next_item)
                collected_count += 1
                state.queue_dirty = True

        log_event(
            "queue_start",
            source="manual",
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
            items_to_start=len(items_to_start)
        )
        queue_state_snapshot = state.queue_state

    # Phase 2: Items starten (OHNE Lock)
    for item in items_to_start:
        try:
            start_queue_item(item)
        except HTTPException:
            # Bei Fehler: Item wieder vorne in Queue einfügen
            with state_lock:
                state.queue.insert(0, item)
                state.queue_dirty = True
            raise

    return {"ok": True, "queue_state": queue_state_snapshot, "started_count": len(items_to_start)}


@router.post("/queue/pause")
@limiter.limit(MUTATION_LIMIT)
def queue_pause(request: Request):
    with state_lock:
        state.queue_state = "pausiert"
        state.queue_dirty = True

    log_event("queue_pause", source="manual", queue_state=state.queue_state, queue_length=len(state.queue or []))
    return {"ok": True, "queue_state": state.queue_state, "message": "Warteschlange pausiert"}


@router.post("/queue/clear")
@limiter.limit(MUTATION_LIMIT)
def queue_clear(request: Request):
    with state_lock:
        state.queue_state = "bereit"
        state.queue_state_before_valve_pause = "bereit"
        state.queue = state.queue or []
        state.queue.clear()
        state.queue_dirty = True

    log_event("queue_clear", source="manual", queue_state=state.queue_state, queue_length=0)
    return {"ok": True, "queue_state": state.queue_state, "queue_length": 0}
