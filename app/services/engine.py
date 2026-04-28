# services/engine.py
"""
Bewässerungs-Engine: Ventilsteuerung und Status-Aufbau.

Dieses Modul enthält die zentralen Steuerungsfunktionen:
  - start_valve()          Ventil starten (Prepare / Execute / Commit)
  - start_queue_item()     Queue-Item starten (Wrapper um start_valve)
  - engine_status_payload_locked()  Status-Dict für GET /status

Sowie interne Hilfsfunktionen:
  - _can_start_new_valve_locked()   Prüft Kapazität und HW-Fault
  - _active_runs_snapshot_locked()  Serialisierbarer Snapshot der active_runs
  - _history_add_locked()           Verlaufseintrag hinzufügen
  - _calc_actual_run_s_ar()         Tatsächliche Laufzeit berechnen

Naming-Konvention:
  - Funktionen mit "_locked" im Namen MÜSSEN unter state_lock aufgerufen werden.
  - start_valve() und start_queue_item() MÜSSEN OHNE state_lock aufgerufen werden
    (sie holen den Lock intern via Prepare-Execute-Commit).

Concurrency-Modell (Prepare / Execute / Commit):
  Hardware-Operationen (GPIO) laufen ausschließlich über den io_worker-Thread.
  Der state_lock wird NIE während eines Hardware-Calls gehalten, um Deadlocks
  und Lock-Contention zu vermeiden.

Sensor-Cooldown-Tracking:
  Wenn start_valve() mit sensor_id aufgerufen wird (nur via start_queue_item),
  setzt die COMMIT-Phase:
    1. state.sensor_last_triggered[sensor_id] = started_at
       (Cooldown-Messung beginnt zum tatsächlichen Ventilstart-Zeitpunkt)
    2. Entfernt die Zone aus state.sensor_pending_zones[sensor_id]
       (gibt den Slot frei sobald das Ventil läuft)
  Damit startet die Cooldown-Zeit erst bei realem Bewässerungsbeginn,
  nicht schon beim Einreihen in die Queue.
"""

import time
import math
from typing import Dict, Optional
from fastapi import HTTPException

from core.state import state, state_lock, ActiveRun, QueueItem, HistoryItem
from core.config import MAX_HISTORY_ITEMS
from core.logging import log_event
from core.config import TZ
from datetime import datetime


def _can_start_new_valve_locked() -> bool:
    """Prüft, ob ein weiteres Ventil gestartet werden darf.

    Berücksichtigt:
      - Hardware-Fault-Latch (hw_faulted): blockiert jeden neuen Start
      - Parallel-Modus aus: maximal 1 gleichzeitiges Ventil
      - Parallel-Modus ein: maximal max_concurrent_valves gleichzeitige Ventile

    MUSS unter state_lock aufgerufen werden.

    Returns:
        True wenn ein neues Ventil gestartet werden darf, sonst False.
    """
    if getattr(state, "hw_faulted", False):
        return False
    if not state.parallel_enabled:
        return len(state.active_runs or {}) == 0
    return len(state.active_runs or {}) < max(1, int(state.max_concurrent_valves))


def _active_runs_snapshot_locked() -> Dict[int, dict]:
    """Erstellt einen JSON-serialisierbaren Snapshot aller aktiven Läufe.

    Berechnet remaining_s live aus end_time (Lauf läuft) oder remaining_s
    (Lauf pausiert). Das Ergebnis wird direkt in engine_status_payload_locked()
    für das `active_runs`-Feld im /status-Response verwendet.

    MUSS unter state_lock aufgerufen werden.

    Returns:
        Dict {zone: {"remaining_s", "time_unit", "started_source", "planned_s"}}
    """
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
    """Fügt einen abgeschlossenen Lauf zur Verlaufsliste hinzu.

    Begrenzt die Liste automatisch auf max_history_items (aus state).
    Setzt state.history_dirty = True → persistence_loop schreibt beim
    nächsten Durchlauf.

    MUSS unter state_lock aufgerufen werden.

    Args:
        zone:       Zonen-Nummer (1..MAX_VALVES)
        duration_s: Tatsächlich gelaufene Sekunden
        source:     Ursprung ("manual" | "queue" | "schedule")
        time_unit:  Anzeigeeinheit ("Sekunden" | "Minuten")
    """
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


def _calc_actual_run_s_ar(ar: ActiveRun, now_m: float) -> int:
    """Berechnet die tatsächliche Laufzeit (Sekunden) direkt aus einem ActiveRun.

    Liest ausschließlich aus dem AR-Objekt – korrekt für alle Zonen.

    Rundungslogik:
      - Ohne Pause : floor(active + eps)  → konservative Unterschätzung
      - Mit Pause  : ceil(active - eps)   → macht abgerundete Pause-Sekunden
                                            wieder sichtbar

    Args:
        ar:    Das ActiveRun-Objekt der Zone
        now_m: Aktueller time.monotonic()-Wert

    Returns:
        Tatsächliche Laufzeit in Sekunden (>= 0)
    """
    if not ar.started_at:
        return 0

    paused_total = ar.paused_total_s
    if ar.paused_at:
        paused_total += (now_m - ar.paused_at)

    active = (now_m - ar.started_at) - paused_total
    if active <= 0:
        return 0

    had_pause = (ar.paused_total_s > 0.0) or (ar.paused_at > 0.0)
    eps = 1e-6
    return int(math.ceil(active - eps)) if had_pause else int(math.floor(active + eps))


# ==================== REFACTORED: PREPARE-EXECUTE-COMMIT ====================

def start_valve(
    zone: int,
    duration_s: int,
    time_unit: str,
    source: str,
    sensor_id: Optional[int] = None,
):
    """Startet ein Ventil via IO-Worker mit Prepare-Execute-Commit Pattern.

    WICHTIG: Diese Funktion muss OHNE state_lock aufgerufen werden!
    Sie holt sich den Lock intern für Prepare und Commit.

    Ablauf:
    1. PREPARE (unter Lock): Validierung, Context erstellen
    2. EXECUTE (ohne Lock): Hardware-Operation via IO-Worker
    3. COMMIT (unter Lock):  State-Update wenn Hardware erfolgreich

    Args:
        zone:       Zonen-Nummer (1..MAX_VALVES)
        duration_s: Laufzeit in Sekunden
        time_unit:  Anzeigeeinheit ("Sekunden" | "Minuten")
        source:     Ursprung ("manual" | "queue" | "schedule" | "sensor")
        sensor_id:  Sensor-ID die dieses Ventil ausgelöst hat (nur wenn
                    source="sensor", sonst None). Wird in der COMMIT-Phase
                    genutzt um sensor_last_triggered zu setzen und die Zone
                    aus sensor_pending_zones zu entfernen.

    Raises:
        HTTPException 409: Zone läuft bereits, parallele Kapazität erschöpft
        HTTPException 423: Hardware-Fault aktiv
        HTTPException 503: Hardware-Fehler beim Öffnen
    """

    # ============ PHASE 1: PREPARE (unter Lock) ============
    with state_lock:
        if state.active_runs is None:
            state.active_runs = {}

        # Validierungen
        if zone in state.active_runs:
            raise HTTPException(status_code=409, detail=f"Ventil {zone} läuft bereits!")

        if not _can_start_new_valve_locked():
            if getattr(state, "hw_faulted", False):
                raise HTTPException(
                    status_code=423,
                    detail="Hardware-Fault aktiv. Start gesperrt. Bitte prüfen und /fault/clear ausführen."
                )
            if state.parallel_enabled:
                raise HTTPException(
                    status_code=409,
                    detail=f"Max. parallele Ventile erreicht ({state.max_concurrent_valves})."
                )
            already = sorted(state.active_runs.keys())[0] if state.active_runs else "?"
            raise HTTPException(
                status_code=409,
                detail=f"Es läuft bereits Ventil {already}!"
            )

        # Context für Hardware-Op + späteren Commit erstellen
        now_m = time.monotonic()
        context = {
            "zone": zone,
            "duration_s": duration_s,
            "time_unit": time_unit,
            "source": source,
            "started_at": now_m,
            "end_time": now_m + duration_s,
        }

    # ============ PHASE 2: EXECUTE (OHNE Lock via IO-Worker) ============
    from services.io_worker import get_io_worker, IOCommand

    io_worker = get_io_worker()
    cmd = IOCommand(action="open", zone=zone)
    result = io_worker.send_command(cmd, timeout_s=5.0)

    if not result.success:
        # Hardware-Fehler → Event loggen, Exception werfen
        log_event(
            "valve_hw_error",
            level="error",
            source=source,
            action="open",
            zone=zone,
            error=result.error,
            duration_ms=result.duration_ms
        )
        raise HTTPException(
            status_code=503,
            detail=f"Hardware Fehler beim Öffnen von Ventil {zone}: {result.error}"
        )

    # ============ PHASE 3: COMMIT (unter Lock) ============
    with state_lock:
        # Double-check: Zone könnte theoretisch jetzt schon belegt sein
        # (wenn zwischen Phase 1 und Phase 3 jemand anderes gestartet hat)
        if zone in state.active_runs:
            # Ups! Jemand war schneller. Hardware wieder schließen.
            log_event(
                "valve_start_race_condition",
                level="warning",
                source=source,
                zone=zone,
                message="Zone wurde zwischen Prepare und Commit von anderem Thread gestartet"
            )

            # Hardware-Cleanup via IO-Worker (best effort, ohne Lock-Block)
            # Wir machen das async/non-blocking
            import threading
            def cleanup():
                cleanup_cmd = IOCommand(action="close", zone=zone)
                io_worker.send_command(cleanup_cmd, timeout_s=5.0)
            threading.Thread(target=cleanup, daemon=True).start()

            raise HTTPException(
                status_code=409,
                detail=f"Ventil {zone} wurde von anderem Request gestartet (Race Condition)"
            )

        # Alles OK → State updaten
        state.active_runs[zone] = ActiveRun(
            zone=zone,
            end_time=context["end_time"],
            time_unit=time_unit,
            started_at=context["started_at"],
            started_source=source,
            started_planned_s=int(duration_s),
        )

        # Sensor-Cooldown-Tracking: Cooldown-Timestamp setzen und Zone aus
        # Pending-Liste entfernen. Dies geschieht bewusst hier (beim echten
        # Ventilstart), NICHT beim Einreihen in die Queue – damit verbraucht
        # ein langer Queue-Rückstau die Cooldown-Zeit nicht vorzeitig.
        # sensor_last_triggered wird bei jedem Ventilstart des Sensors aktualisiert
        # (also auch beim 2. und 3. Ventil eines Sensor-Triggers), sodass der
        # Cooldown immer vom zuletzt gestarteten Ventil dieses Triggers gemessen wird.
        if sensor_id is not None:
            if state.sensor_last_triggered is None:
                state.sensor_last_triggered = {}
            if state.sensor_pending_zones is None:
                state.sensor_pending_zones = {}
            state.sensor_last_triggered[sensor_id] = context["started_at"]
            pending = state.sensor_pending_zones.get(sensor_id)
            if pending is not None:
                pending.discard(zone)

        log_event(
            "valve_start",
            source=source,
            zone=zone,
            duration_s=duration_s,
            time_unit=time_unit,
            hw_duration_ms=result.duration_ms,
            queue_state=state.queue_state,
            queue_length=len(state.queue or []),
            automation_enabled=state.automation_enabled,
            parallel_enabled=state.parallel_enabled,
            max_concurrent_valves=state.max_concurrent_valves,
        )


def start_queue_item(item: QueueItem):
    """Startet ein Queue-Item.

    Wrapper um start_valve() – stellt sicher dass das Argument-Mapping
    konsistent bleibt, inklusive der Sensor-ID für Cooldown-Tracking.

    WICHTIG: Muss OHNE state_lock aufgerufen werden!
    """
    start_valve(
        zone=item.zone,
        duration_s=item.duration,
        time_unit=item.time_unit,
        source=item.source,
        sensor_id=item.sensor_id,   # None wenn nicht sensor-ausgelöst
    )


def engine_status_payload_locked() -> dict:
    """Baut das vollständige Status-Dict für GET /status.

    Enthält sowohl Legacy-Felder (running_zone, remaining_time für
    Single-Zone-Kompatibilität) als auch Multi-Zone-Felder (running_zones,
    active_runs) und Hardware-Fault-Status.

    Primary Zone Convention:
      running_zone / remaining_time beziehen sich immer auf die Zone mit
      der niedrigsten Nummer (kleinster Key in active_runs). Dies ist die
      Kompatibilitätskonvention für Clients die nur eine Zone kennen.

    MUSS unter state_lock aufgerufen werden.

    Returns:
        Dict mit allen Status-Feldern (direkt als JSON-Response verwendbar).
    """
    # lazy import: verhindert unnötige Import-Ketten beim Modul-Import
    from services.valve_driver import get_valve_driver
    driver_name = get_valve_driver().name

    q = state.queue or []
    schedules_count = len(state.schedules or [])
    automation_enabled = getattr(state, "automation_enabled", True)

    active_runs = state.active_runs or {}
    running_zones = sorted(active_runs.keys())

    # primary zone: lowest zone number (convention for legacy single-zone compat)
    primary_zone = running_zones[0] if running_zones else None
    primary_ar = active_runs.get(primary_zone) if primary_zone is not None else None

    if not active_runs:
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
            "running_zones": [],
            "active_runs": {},
            "valve_driver": driver_name,
            "max_valves": int(getattr(state, "max_valves", 6)),
            "hw_faulted": bool(getattr(state, "hw_faulted", False)),
            "hw_fault_reason": getattr(state, "hw_fault_reason", ""),
            "hw_fault_zone": getattr(state, "hw_fault_zone", None),
            "hw_fault_since": getattr(state, "hw_fault_since", ""),
        }

    if state.paused:
        remaining = int(primary_ar.remaining_s or 0) if primary_ar else 0
        valve_state = "pausiert"
    else:
        remaining = max(0, int(primary_ar.end_time - time.monotonic())) if primary_ar else 0
        valve_state = "läuft"

    return {
        "state": valve_state,
        "running_zone": primary_zone,
        "remaining_time": remaining,
        "time_unit": primary_ar.time_unit if primary_ar else "Sekunden",
        "paused": state.paused,
        "queue_state": state.queue_state,
        "queue_length": len(q),
        "schedules_count": schedules_count,
        "automation_enabled": automation_enabled,
        "parallel_enabled": state.parallel_enabled,
        "max_concurrent_valves": state.max_concurrent_valves,
        "running_zones": running_zones,
        "active_runs": _active_runs_snapshot_locked(),
        "valve_driver": driver_name,
        "max_valves": int(getattr(state, "max_valves", 6)),
        "hw_faulted": bool(getattr(state, "hw_faulted", False)),
        "hw_fault_reason": getattr(state, "hw_fault_reason", ""),
        "hw_fault_zone": getattr(state, "hw_fault_zone", None),
        "hw_fault_since": getattr(state, "hw_fault_since", ""),
    }


# Exports (für andere services/api)
__all__ = [
    "_can_start_new_valve_locked",
    "start_valve",
    "_history_add_locked",
    "_calc_actual_run_s_ar",
    "start_queue_item",
    "engine_status_payload_locked",
    "_active_runs_snapshot_locked",
]
