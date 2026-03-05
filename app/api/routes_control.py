# api/routes_control.py
"""
Steuerungs-Routen: Manuelle Ventilkontrolle und System-Konfiguration.

Endpunkte:
  GET  /status              – vollständiger Systemzustand (aktive Zonen, Queue, Fault)
  POST /start               – einzelne Zone manuell starten
  POST /stop                – alle aktiven Ventile sofort stoppen
  POST /pause               – alle aktiven Ventile pausieren (Restzeit gespeichert)
  POST /resume              – pausierte Ventile fortsetzen
  POST /fault/clear         – Hardware-Fault quittieren (nach Operator-Prüfung)
  GET  /automation          – Automatikmodus abfragen
  POST /automation/enable   – Automatikmodus aktivieren
  POST /automation/disable  – Automatikmodus deaktivieren
  POST /automation/toggle   – Automatikmodus umschalten
  GET  /parallel            – Parallelmodus abfragen
  POST /parallel            – Parallelmodus setzen

Sicherheitskritische Semantiken:
  /stop: Nur Zonen die hardware-seitig erfolgreich geschlossen wurden, werden
    aus active_runs entfernt. Fehlgeschlagene Zonen bleiben in active_runs
    mit end_time=jetzt-1s und werden vom timer_loop via Backoff-Retry geschlossen.
    Damit ist garantiert: logisch "gestoppt" ↔ Hardware physisch zu.

  /pause: Nur wenn ALLE Zonen erfolgreich geschlossen wurden → State wird
    pausiert. Bei Hardware-Fehler: kein State-Update (Rollback-Semantik).

  /resume: Nur wenn ALLE Zonen erfolgreich geöffnet wurden → State wird
    fortgesetzt. Bei Hardware-Fehler: State bleibt pausiert (Rollback-Semantik).

  /fault/clear: Cooldown HW_FAULT_COOLDOWN_S (60s) verhindert vorschnelles
    Quittieren. Nur wenn keine Ventile laufen.

Alle Routen erfordern API-Key-Authentifizierung (X-API-Key Header).
Schreibende Routen unterliegen MUTATION_LIMIT (Rate-Limiting).
"""
import time
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request

from core.state import state, state_lock
from core.config import MAX_RUNTIME_S, TZ, HW_FAULT_COOLDOWN_S
from core.logging import log_event
from core.security import require_api_key
from core.limiter import limiter, MUTATION_LIMIT
from models.requests import StartRequest, ParallelModeRequest

from services.engine import (
    start_valve,
    engine_status_payload_locked,
    _history_add_locked,
)
from services.persistence import save_runtime_state_to_disk

router = APIRouter(dependencies=[Depends(require_api_key)])


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
@limiter.limit(MUTATION_LIMIT)
def start(request: Request, req: StartRequest):
    # Validierungen (unter Lock)
    with state_lock:
        max_v = int(getattr(state, "max_valves", 1))
        max_runtime_s = int(getattr(state, "hard_max_runtime_s", MAX_RUNTIME_S))

    # Input-Validierung (ohne Lock)
    if req.zone < 1 or req.zone > max_v:
        raise HTTPException(status_code=400, detail=f"zone muss 1..{max_v} sein.")

    if req.duration <= 0:
        raise HTTPException(status_code=400, detail="Die Laufzeit muss > 0 sein!")

    if req.duration > max_runtime_s:
        raise HTTPException(status_code=400, detail=f"Die Maximale Laufzeit ist {max_runtime_s // 60} Minuten!")

    # Start Ventil OHNE state_lock (Funktion holt Lock intern via Prepare-Execute-Commit)
    start_valve(
        zone=req.zone,
        duration_s=req.duration,
        time_unit=req.time_unit,
        source="manual",
    )

    # Response zusammenbauen (unter Lock)
    with state_lock:
        return {
            "ok": True,
            "running_zone": req.zone,
            "duration": req.duration,
            "time_unit": req.time_unit,
            "parallel_enabled": state.parallel_enabled,
            "max_concurrent_valves": state.max_concurrent_valves,
        }


# ---------------------------
# POST /stop -> Stoppt sofort alle Ventile
#
# Teilfehler-Semantik (sicherheitskritisch):
#   Nur Zonen die hardware-seitig erfolgreich geschlossen wurden, werden aus
#   active_runs entfernt und in die Historie geschrieben.
#   Fehlgeschlagene Zonen verbleiben in active_runs mit end_time = jetzt - 1 s,
#   damit der Timer sie beim nächsten Durchlauf via Backoff-Mechanismus erneut
#   damit der Timer sie beim nächsten Durchlauf via Backoff-Mechanismus erneut
#   versucht zu schließen – identisch zum normalen Timeout-Pfad.
#
#   Damit ist garantiert: logischer Zustand "gestoppt" <=> Hardware ist zu.
# ---------------------------
@router.post("/stop")
@limiter.limit(MUTATION_LIMIT)
def stop(request: Request):
    # Phase 1: Prepare (unter Lock) - sammle alle Infos für Historie
    with state_lock:
        if not state.active_runs:
            # Invariant: active_runs is the source of truth; nothing to stop.
            return {"ok": True, "stopped_zones": []}

        now_m = time.monotonic()
        zones_to_close = sorted(list(state.active_runs.keys()))

        # Sammle Infos für Historie-Berechnung (vor dem Hardware-Close)
        zones_info = {}
        for zone in zones_to_close:
            ar = state.active_runs.get(zone)
            if not ar:
                continue

            paused_total = ar.paused_total_s
            if ar.paused_at:
                paused_total += (now_m - ar.paused_at)

            active = (now_m - ar.started_at) - paused_total
            actual_s = max(0, int(active + 1e-6))

            zones_info[zone] = {
                "actual_s": actual_s,
                "source": ar.started_source or "manual",
                "time_unit": ar.time_unit,
                "was_paused": ar.paused_at > 0.0,  # benötigt für end_time-Korrektur bei Fehler
            }

        # Logisches Unpause vorab – wir versuchen den Stop, paused=False ist der Intent.
        # Fehlgeschlagene Zonen werden so vom Timer gefunden (end_time-Prüfung greift).
        state.paused = False

    # Phase 2: Execute (OHNE Lock) - Hardware close via IO-Worker
    from services.io_worker import get_io_worker, IOCommand
    io_worker = get_io_worker()

    failed = []
    stopped = []
    for zone in zones_to_close:
        cmd = IOCommand(action="close", zone=zone)
        result = io_worker.send_command(cmd, timeout_s=5.0)

        if result.success:
            stopped.append(zone)
        else:
            failed.append({"zone": zone, "error": result.error})

    # Phase 3: Commit (unter Lock)
    #
    # Sicherheitsinvariante: Nur Zonen die hardware-seitig geschlossen wurden
    # (stopped-Liste) werden aus active_runs entfernt und in die Historie geschrieben.
    # Fehlgeschlagene Zonen (failed-Liste) bleiben in active_runs und werden vom
    # Timer beim nächsten Durchlauf über den Backoff-Retry-Pfad erneut geschlossen.
    with state_lock:
        now_m = time.monotonic()

        # Erfolgreich gestoppte Zonen: Historie schreiben + aus active_runs entfernen
        for zone in stopped:
            info = zones_info.get(zone)
            if info:
                _history_add_locked(
                    zone=zone,
                    duration_s=info["actual_s"],
                    source=info["source"],
                    time_unit=info["time_unit"],
                )
            state.active_runs.pop(zone, None)

        # Fehlgeschlagene Zonen: in active_runs belassen, end_time für sofortigen
        # Timer-Retry setzen. Paused-Felder leeren damit der Timer-Check greift
        # (Timer überspringt Zonen mit end_time == 0.0, da 0.0 falsy ist).
        for zone in [f["zone"] for f in failed]:
            ar = state.active_runs.get(zone)
            if ar is None:
                continue
            # Paused-Accounting abschließen (paused_at leeren) damit actual_s im
            # Timer korrekt berechnet wird, falls close beim nächsten Retry klappt.
            if ar.paused_at:
                ar.paused_total_s += (now_m - ar.paused_at)
                ar.paused_at = 0.0
            # end_time in die Vergangenheit setzen → Timer greift sofort
            ar.end_time = now_m - 1.0
            log_event(
                "valve_stop_hw_error_retry_scheduled",
                level="error",
                source="manual",
                zone=zone,
                error=next((f["error"] for f in failed if f["zone"] == zone), "unknown"),
                action="timer_will_retry",
            )

        state.paused = False  # idempotent, aber explizit

        if not state.active_runs:
            state.queue_state_before_valve_pause = "bereit"


        log_event(
            "valve_stop",
            source="manual",
            zone="all",
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
            parallel_enabled=state.parallel_enabled,
            automation_enabled=state.automation_enabled,
            stopped_count=len(stopped),
            failed_count=len(failed),
        )

    if failed:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Nicht alle Ventile konnten gestoppt werden. "
                           "Fehlgeschlagene Zonen werden automatisch nachgeschlossen.",
                "stopped": stopped,
                "failed": failed,
            },
        )

    return {"ok": True, "stopped_zones": stopped}


# ---------------------------
# POST /pause -> Pausiert alle aktuell laufenden Ventile (global pause)
# ---------------------------
@router.post("/pause")
@limiter.limit(MUTATION_LIMIT)
def pause_current(request: Request):
    # Phase 1: Prepare (unter Lock) - sammle Zonen die geschlossen werden müssen
    with state_lock:
        if not state.active_runs:
            raise HTTPException(status_code=409, detail="Kein Ventil läuft gerade.")

        if state.paused:
            raise HTTPException(status_code=409, detail="Ventile sind bereits pausiert.")

        zones_to_close = sorted(list(state.active_runs.keys()))
        now_m = time.monotonic()

        # Berechne remaining_s für jede Zone (für später)
        zones_info = {}
        for z in zones_to_close:
            ar = state.active_runs[z]
            if ar.paused_at == 0.0:
                remaining_s = max(0, int(ar.end_time - now_m))
                zones_info[z] = {
                    "remaining_s": remaining_s,
                    "paused_at": now_m,
                }

    # Phase 2: Execute (OHNE Lock) - Hardware close via IO-Worker
    from services.io_worker import get_io_worker, IOCommand
    io_worker = get_io_worker()

    failed = []
    for z in zones_to_close:
        cmd = IOCommand(action="close", zone=z)
        result = io_worker.send_command(cmd, timeout_s=5.0)

        if not result.success:
            failed.append({"zone": z, "error": result.error})

    # Bei Hardware-Fehler → Rollback, keine State-Änderung
    if failed:
        log_event(
            "valve_hw_error",
            level="error",
            source="manual",
            action="close",
            zone="multiple",
            reason="pause",
            failed=failed,
        )
        raise HTTPException(
            status_code=503,
            detail={"message": "Hardware Fehler beim Pausieren", "failed": failed}
        )

    # Phase 3: Commit (unter Lock) - nur wenn Hardware erfolgreich
    with state_lock:
        state.paused = True
        state.queue_state_before_valve_pause = state.queue_state

        # Update ActiveRuns mit berechneten Werten
        for z, info in zones_info.items():
            if z in state.active_runs:
                ar = state.active_runs[z]
                ar.remaining_s = info["remaining_s"]
                ar.paused_at = info["paused_at"]
                ar.end_time = 0.0  # logisch "stoppen" (Timer zählt nicht weiter)


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
@limiter.limit(MUTATION_LIMIT)
def resume_current(request: Request):
    # Phase 1: Prepare (unter Lock)
    with state_lock:
        if bool(getattr(state, "hw_faulted", False)):
            raise HTTPException(
                status_code=423,
                detail="Hardware-Fault aktiv. Resume gesperrt. Bitte prüfen und /fault/clear ausführen."
            )

        if not state.active_runs:
            raise HTTPException(status_code=409, detail="Kein Ventil ist aktiv/pausiert.")

        if not state.paused:
            raise HTTPException(status_code=409, detail="Ventile sind nicht pausiert.")

        # Sammle Zonen die geöffnet werden müssen (nur die mit remaining_s > 0)
        zones_to_open = []
        for z, ar in state.active_runs.items():
            if (ar.remaining_s or 0) > 0:
                zones_to_open.append(z)

    # Phase 2: Execute (OHNE Lock) - Hardware open via IO-Worker
    from services.io_worker import get_io_worker, IOCommand
    io_worker = get_io_worker()

    failed = []
    for z in zones_to_open:
        cmd = IOCommand(action="open", zone=z)
        result = io_worker.send_command(cmd, timeout_s=5.0)

        if not result.success:
            failed.append({"zone": int(z), "error": result.error})

    # Bei Hardware-Fehler → Rollback, State bleibt pausiert
    if failed:
        log_event(
            "valve_hw_error",
            level="error",
            source="manual",
            action="open",
            zone="multiple",
            reason="resume",
            failed=failed,
        )
        raise HTTPException(
            status_code=503,
            detail={"message": "Hardware Fehler beim Fortsetzen", "failed": failed}
        )

    # Phase 3: Commit (unter Lock) - nur wenn Hardware erfolgreich
    with state_lock:
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
# POST /fault/clear -> Quittiert Hardware-Fault (Operator-Ack)
#
# Cooldown-Design:
#   HW_FAULT_COOLDOWN_S (aktuell 60 s) verhindert vorschnelles Quittieren direkt
#   nach dem Fault. Der Operator soll Zeit haben, die Hardware wirklich zu
#   prüfen bevor die Sperre aufgehoben wird.
#   Ist hw_fault_since nicht gesetzt oder nicht parsierbar, wird der Cooldown
#   übersprungen (fail open) – damit bestehende Betriebsabläufe nicht blockiert
#   werden, wenn das Feld fehlt (z.B. Migration von Altdaten).
# ---------------------------
@router.post("/fault/clear")
@limiter.limit(MUTATION_LIMIT)
def clear_fault(request: Request):
    with state_lock:
        if not bool(getattr(state, "hw_faulted", False)):
            return {"ok": True, "cleared": False, "reason": "no_fault"}

        # Fault nur quittieren wenn keine Ventile laufen (sicherer)
        if state.active_runs:
            raise HTTPException(
                status_code=409,
                detail="Fault kann nur quittiert werden, wenn keine Ventile laufen."
            )

        # Cooldown-Prüfung: verhindert vorschnelles Quittieren.
        # Nur wenn hw_fault_since gesetzt UND parsierbar (fail open bei fehlendem Wert).
        if state.hw_fault_since:
            try:
                fault_dt = datetime.fromisoformat(state.hw_fault_since)
                elapsed_s = (datetime.now(TZ) - fault_dt).total_seconds()
                if elapsed_s < HW_FAULT_COOLDOWN_S:
                    remaining = int(HW_FAULT_COOLDOWN_S - elapsed_s) + 1
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Fault-Quittierung gesperrt: Cooldown noch {remaining}s aktiv "
                            f"(gesamt {int(HW_FAULT_COOLDOWN_S)}s). "
                            "Bitte Hardware prüfen und dann erneut versuchen."
                        ),
                    )
            except HTTPException:
                raise  # HTTPException weiterwerfen, nicht schlucken
            except Exception:
                pass  # hw_fault_since nicht parsierbar → fail open (Operator-Convenience)

        state.hw_faulted = False
        state.hw_fault_reason = ""
        state.hw_fault_zone = None
        state.hw_fault_since = ""
        state.hw_fault_close_all_attempted = False

        log_event("hw_fault_cleared", level="warning", source="manual")

    return {"ok": True, "cleared": True}


# ---------------------------
# Automatik global: enable / disable / toggle / status
# ---------------------------
@router.get("/automation")
def get_automation():
    with state_lock:
        return {"automation_enabled": state.automation_enabled}


@router.post("/automation/enable")
@limiter.limit(MUTATION_LIMIT)
def enable_automation(request: Request):
    with state_lock:
        state.automation_enabled = True
        state.schedules_dirty = True
        now = datetime.now(TZ)
        state.automation_block_run_key = now.strftime("%Y-%m-%d %H:%M")

        log_event("automation_enable", source="manual", automation_enabled=True)
        return {"ok": True, "automation_enabled": state.automation_enabled}


@router.post("/automation/disable")
@limiter.limit(MUTATION_LIMIT)
def disable_automation(request: Request):
    with state_lock:
        state.automation_enabled = False
        state.schedules_dirty = True
        state.automation_block_run_key = None

        log_event("automation_disable", source="manual", automation_enabled=False)
        return {"ok": True, "automation_enabled": state.automation_enabled}


@router.post("/automation/toggle")
@limiter.limit(MUTATION_LIMIT)
def toggle_automation(request: Request):
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
@limiter.limit(MUTATION_LIMIT)
def set_parallel_mode(request: Request, req: ParallelModeRequest):
    with state_lock:
        prev = bool(state.parallel_enabled)
        state.parallel_enabled = bool(req.enabled)

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
