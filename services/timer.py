# services/timer.py
"""
Timer-Loop: Schließt Ventile nach Ablauf, verarbeitet HW-Fehler mit Backoff,
füllt die Queue auf und erkennt Queue-Ende.

Thread-Safety-Design (Prepare / Execute / Commit):
─────────────────────────────────────────────────
Alle Hardware-Operationen (GPIO-close, close_all) laufen ausschließlich
über den io_worker-Thread. Der timer_loop-Thread berührt die Hardware nie
direkt. Das gilt symmetrisch zu routes_control.py (pause/stop/resume).

Ablauf pro Iteration:
  1. Lock  – Queue-Fill sammeln (items_to_start)
  2. Unlock– Queue-Items starten (start_queue_item → io_worker)
  3. Lock  – Abgelaufene Zonen (finished_zones) sammeln
  4. Unlock– io_worker.send_command("close", zone) pro Zone
  5. Lock  – Ergebnisse committen (History, active_runs, Fault-Latch)
             + _sync_legacy + Queue-fertig-Check (immer, auch ohne Timeouts)
  6. Unlock– Notfall close_all wenn Fault neu gelatcht (einmalig)
"""

import time
from datetime import datetime

from core.config import (
    HW_CLOSE_MAX_RETRIES,
    HW_RETRY_BACKOFF_BASE_S,
    HW_RETRY_BACKOFF_MAX_S,
    TZ,
)
from core.logging import log_event, logger
from core.state import state, state_lock
from services.engine import (
    _calc_actual_run_s_primary,
    _history_add_locked,
    _sync_legacy_single_fields_locked,
    start_queue_item,
)
from services.io_worker import IOCommand, get_io_worker
from services.valve_driver import get_valve_driver


def _hw_backoff_s(failures: int) -> float:
    """Exponentieller Backoff: 1 s, 2 s, 4 s, … gedeckelt auf MAX."""
    try:
        f = max(0, int(failures))
    except Exception:
        f = 0
    return min(HW_RETRY_BACKOFF_MAX_S, HW_RETRY_BACKOFF_BASE_S * (2 ** f))


def timer_loop() -> None:
    from core.state import shutdown_event

    while not shutdown_event.is_set():
        try:
            if shutdown_event.wait(0.1):
                break

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 1: Queue-Fill – Collect (unter Lock)
            # ──────────────────────────────────────────────────────────────
            items_to_start: list = []

            with state_lock:
                # Drain-Warnung: Parallel-Modus deaktiviert, aber mehrere Zonen laufen
                if (not state.parallel_enabled) and (state.active_runs and len(state.active_runs) > 1):
                    if state.queue_state == "läuft" and (state.queue and len(state.queue) > 0):
                        if not state.parallel_drain_logged:
                            state.parallel_drain_logged = True
                            log_event(
                                "parallel_disabled_waiting_for_drain",
                                level="warning",
                                source="system",
                                running_zones=sorted(list(state.active_runs.keys())),
                                queue_length=len(state.queue or []),
                            )
                else:
                    if state.parallel_drain_logged:
                        state.parallel_drain_logged = False

                # Queue-Items sammeln
                if state.queue_state == "läuft" and not state.paused and state.queue_state != "pausiert":
                    collected_count = 0
                    while state.queue:
                        current_running = len(state.active_runs or {})
                        future_running = current_running + collected_count

                        can_start_more = False
                        if getattr(state, "hw_faulted", False):
                            can_start_more = False
                        elif not state.parallel_enabled:
                            can_start_more = (future_running == 0)
                        else:
                            max_conc = max(1, int(state.max_concurrent_valves))
                            can_start_more = (future_running < max_conc)

                        if not can_start_more:
                            break

                        next_item = state.queue.pop(0)
                        items_to_start.append(next_item)
                        collected_count += 1
                        state.queue_dirty = True

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 2: Queue-Items starten (ohne Lock, via io_worker)
            # ──────────────────────────────────────────────────────────────
            for item in items_to_start:
                try:
                    start_queue_item(item)
                except Exception as e:
                    with state_lock:
                        state.queue.insert(0, item)
                        state.queue_dirty = True
                    log_event(
                        "queue_item_start_failed",
                        level="error",
                        source="system",
                        zone=item.zone,
                        error=str(e),
                        action="returned_to_queue",
                    )

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 3: Timeout-Verarbeitung – Phase A: Collect (unter Lock)
            # ──────────────────────────────────────────────────────────────
            finished_zones: list[int] = []
            paused_snapshot: bool = False

            with state_lock:
                paused_snapshot = bool(state.paused)
                if not paused_snapshot:
                    now_m = time.monotonic()
                    for zone, ar in (state.active_runs or {}).items():
                        if ar.end_time and now_m >= ar.end_time:
                            finished_zones.append(zone)

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 4: Hardware close via io_worker (ohne Lock)
            #
            # KERN DES BUG-FIX: GPIO wird ausschließlich vom io_worker-Thread
            # angesprochen. Der timer_loop berührt die Hardware nie direkt.
            # Damit ist die gesamte GPIO-Nutzung auf einen einzigen Thread
            # (io_worker) serialisiert – identisch zu stop/pause/resume.
            # ──────────────────────────────────────────────────────────────
            close_results: dict = {}
            io_worker = get_io_worker()

            if finished_zones:
                for zone in finished_zones:
                    cmd = IOCommand(
                        action="close",
                        zone=zone,
                        request_id=f"timer-close-{zone}",
                    )
                    close_results[zone] = io_worker.send_command(cmd, timeout_s=5.0)

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 5: Commit + Legacy-Sync + Queue-fertig-Check (unter Lock)
            #
            # Dieser Block läuft in JEDER Iteration, auch wenn keine Zones
            # abgelaufen sind – so bleiben Legacy-Felder und queue_state stets
            # konsistent (entspricht dem Verhalten vor dem Refactor).
            # ──────────────────────────────────────────────────────────────
            need_emergency_close_all = False
            # driver_name VOR dem Lock berechnen: get_valve_driver() kann intern
            # _read_driver_settings_from_state() aufrufen, welche state_lock
            # anfordert. Da threading.Lock() nicht re-entrant ist, würde der
            # Aufruf innerhalb des Locks zum Deadlock führen, sobald der Driver
            # noch nicht gecacht ist. In der Praxis ist er nach IO-Worker-Start
            # gecacht – wir dürfen diese Annahme aber nicht im Sicherheitspfad
            # voraussetzen.
            driver_name = getattr(get_valve_driver(), "name", "unknown")

            with state_lock:
                if not paused_snapshot and finished_zones:
                    now_m = time.monotonic()

                    for zone in finished_zones:
                        ar = state.active_runs.get(zone)
                        if ar is None:
                            # Zone wurde während Phase B (Unlock-Fenster) von der API
                            # gestoppt → keine doppelte History-Eintrag, kein State-Fehler.
                            log_event(
                                "timer_zone_already_stopped",
                                level="warning",
                                source="system",
                                zone=zone,
                                reason="stopped_by_api_during_hw_close",
                            )
                            continue

                        result = close_results[zone]

                        if not result.success:
                            # ── Hardware-Fehler ──────────────────────────
                            ar.hw_close_failures = int(getattr(ar, "hw_close_failures", 0)) + 1
                            ar.hw_last_error = str(result.error or "unknown")

                            log_event(
                                "valve_hw_error",
                                level="error",
                                source="system",
                                action="close",
                                zone=zone,
                                driver=driver_name,
                                reason="duration_elapsed",
                                error=str(result.error),
                                failures=ar.hw_close_failures,
                            )

                            if ar.hw_close_failures >= int(HW_CLOSE_MAX_RETRIES):
                                # Fault latchen – verhindert alle weiteren Starts
                                state.hw_faulted = True
                                state.hw_fault_reason = "close_failed_max_retries"
                                state.hw_fault_zone = zone
                                state.hw_fault_since = datetime.now(TZ).isoformat(timespec="seconds")

                                log_event(
                                    "hw_fault_latched",
                                    level="critical",
                                    source="system",
                                    zone=zone,
                                    failures=ar.hw_close_failures,
                                    reason=str(result.error),
                                )

                                # hw_fault_close_all_attempted VOR dem Unlock setzen,
                                # damit kein zweiter Timer-Durchlauf ebenfalls close_all
                                # triggert (race-free guard).
                                if not bool(getattr(state, "hw_fault_close_all_attempted", False)):
                                    state.hw_fault_close_all_attempted = True
                                    need_emergency_close_all = True

                            backoff = _hw_backoff_s(ar.hw_close_failures - 1)
                            ar.hw_next_retry_at = now_m + backoff
                            ar.end_time = ar.hw_next_retry_at
                            continue

                        # ── Erfolg: History + active_runs bereinigen ────────
                        if zone == state.running_zone:
                            actual_s = _calc_actual_run_s_primary(now_m)
                        else:
                            paused_total = ar.paused_total_s + (
                                (now_m - ar.paused_at) if ar.paused_at else 0.0
                            )
                            active = (now_m - ar.started_at) - paused_total
                            actual_s = max(0, int(active + 1e-6))

                        _history_add_locked(
                            zone=zone,
                            duration_s=actual_s,
                            source=ar.started_source or "manual",
                            time_unit=ar.time_unit,
                        )
                        del state.active_runs[zone]

                        log_event(
                            "valve_stop",
                            source="system",
                            zone=zone,
                            reason="duration_elapsed",
                            queue_state=state.queue_state,
                            queue_length=len(state.queue or []),
                            actual_s=actual_s,
                            automation_enabled=state.automation_enabled,
                            parallel_enabled=state.parallel_enabled,
                        )

                # Legacy-Felder immer synchronisieren (auch ohne Timeouts)
                _sync_legacy_single_fields_locked()

                # Queue "fertig" prüfen – erst NACH dem Entfernen abgelaufener Zonen,
                # damit active_runs den aktuellen (korrekten) Zustand widerspiegelt.
                if state.queue_state == "läuft":
                    if (not state.queue) and not (state.active_runs and len(state.active_runs) > 0):
                        state.queue_state = "fertig"
                        state.queue_dirty = True
                        log_event(
                            "queue_finished",
                            source="system",
                            queue_state=state.queue_state,
                            queue_length=0,
                            parallel_enabled=state.parallel_enabled,
                            max_concurrent_valves=state.max_concurrent_valves,
                        )

            # ──────────────────────────────────────────────────────────────
            # SCHRITT 6: Notfall close_all (ohne Lock, via io_worker)
            #
            # Nur einmalig pro Fault-Ereignis (Guard: hw_fault_close_all_attempted
            # wurde in Schritt 5 unter Lock gesetzt → race-free).
            # ──────────────────────────────────────────────────────────────
            if need_emergency_close_all:
                try:
                    cmd = IOCommand(
                        action="close_all",
                        request_id="timer-fault-close-all",
                    )
                    result = io_worker.send_command(cmd, timeout_s=10.0)
                    if result.success:
                        log_event(
                            "hw_fault_emergency_close_all_ok",
                            level="critical",
                            source="system",
                            duration_ms=result.duration_ms,
                        )
                    else:
                        log_event(
                            "hw_fault_emergency_close_all_failed",
                            level="critical",
                            source="system",
                            error=result.error,
                        )
                except Exception as ee:
                    log_event(
                        "hw_fault_emergency_close_all_failed",
                        level="critical",
                        source="system",
                        error=repr(ee),
                    )

        except Exception:
            logger.exception("timer_loop crashed")
            log_event("timer_error", level="error", source="system")
            shutdown_event.wait(0.5)
