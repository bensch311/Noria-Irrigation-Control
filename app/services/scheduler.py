# services/scheduler.py
"""
Scheduler-Loop: Zeitgesteuerte Auslösung von Bewässerungsregeln.

Läuft als Background-Thread (gestartet in core/lifecycle.py).
Prüft sekündlich, ob für die aktuelle Uhrzeit und den aktuellen Wochentag
eine Zeitplan-Regel ausgelöst werden soll.

Auslöse-Logik (pro Iteration, unter state_lock):
  1. automation_enabled prüfen – wenn deaktiviert: überspringen
  2. Für jede aktivierte Regel prüfen: Wochentag ✓, Uhrzeit ✓, nicht schon gelaufen?
  3. automation_block_run_key: verhindert Auslösung für die Startminute des Servers
     (verhindert ungewollten Sofortstart nach Neustart zur vollen Minute)
  4. Einzel-Regeln (repeat=False): Einträge aus once_pending entfernen, Regel
     löschen wenn once_pending leer ist
  5. Gruppen-Regeln (zone=0): immer in Queue einfügen (Reihenfolge sichern)
  6. Einzel-Zonen-Regeln: direkt starten wenn Kapazität frei, sonst in Queue

Concurrency-Modell (Prepare / Execute):
  Phase 1 (unter Lock): Jobs-Liste zusammenstellen, last_run_on setzen
  Phase 2 (OHNE Lock):  Jobs via start_queue_item() → io_worker starten
  Bei Start-Fehler in Phase 2: Job wird vorne in die Queue eingefügt (Retry).
"""

from datetime import datetime

from core.state import state, state_lock, QueueItem, ScheduleRule
from core.config import TZ, MAX_VALVES
from core.logging import log_event
from services.engine import _can_start_new_valve_locked, start_queue_item


def _jobs_for_schedule_rule(rule: ScheduleRule) -> list[QueueItem]:
    """Expandiert eine Zeitplan-Regel in eine Liste von Queue-Items.

    Bei zone=0 (alle Ventile): gibt Items für Zone 1..max_valves zurück.
    Bei zone>0: gibt genau ein Item zurück.

    Args:
        rule: Die auszulösende Zeitplan-Regel

    Returns:
        Liste von QueueItems (mindestens 1 Element)
    """
    max_v = int(getattr(state, "max_valves", MAX_VALVES))
    if rule.zone == 0:
        return [QueueItem(zone=z, duration=rule.duration_s, time_unit=rule.time_unit, source="schedule")
                for z in range(1, max_v + 1)]
    return [QueueItem(zone=rule.zone, duration=rule.duration_s, time_unit=rule.time_unit, source="schedule")]


def scheduler_loop():
    """Hauptschleife des Schedulers.

    Prüft jede Sekunde alle aktivierten Zeitplan-Regeln auf Auslösung.
    Terminiert sauber wenn shutdown_event gesetzt wird.

    Fehlerbehandlung: Exceptions innerhalb des Loops werden geloggt und
    die Schleife läuft weiter (kein ungesteuerter Thread-Absturz).
    """
    from core.state import shutdown_event  # avoid circular import

    while not shutdown_event.is_set():
        if shutdown_event.wait(1.0):
            break

        try:
            now = datetime.now(TZ)
            weekday = now.weekday()
            hhmm = now.strftime("%H:%M")
            today_key = now.strftime("%Y-%m-%d")

            # Phase 1: Sammle Jobs die gestartet werden sollen (unter Lock)
            jobs_to_start = []

            with state_lock:
                if not getattr(state, "automation_enabled", True):
                    continue
                if state.schedules is None:
                    continue

                to_delete_ids: list[str] = []

                for rule in list(state.schedules):
                    if not rule.enabled:
                        continue
                    if weekday not in rule.weekdays:
                        continue
                    if hhmm not in rule.start_times:
                        continue

                    run_key = f"{today_key} {hhmm}"
                    if rule.last_run_on == run_key:
                        continue

                    # automation_block_run_key: verhindert Auslösung in der Startminute.
                    # Wird in persistence.load_schedules_from_disk() auf die aktuelle
                    # Minute gesetzt und von keiner anderen Stelle zurückgesetzt.
                    if state.automation_block_run_key == run_key:
                        log_event(
                            "schedule_skipped",
                            level="warning",
                            source="system",
                            reason="automation_block_minute",
                            schedule_id=rule.id,
                            zone=rule.zone,
                            weekday=weekday,
                            hhmm=hhmm,
                        )
                        continue

                    combo_key = f"{weekday} {hhmm}"
                    if not rule.repeat:
                        if not rule.once_pending:
                            to_delete_ids.append(rule.id)
                            continue
                        if combo_key not in rule.once_pending:
                            continue

                    rule.last_run_on = run_key
                    jobs = _jobs_for_schedule_rule(rule)

                    log_event(
                        "schedule_trigger",
                        source="schedule",
                        schedule_id=rule.id,
                        zone=rule.zone,
                        expanded_zones=[j.zone for j in jobs],
                        jobs_count=len(jobs),
                        duration_s=rule.duration_s,
                        time_unit=rule.time_unit,
                        weekday=weekday,
                        hhmm=hhmm,
                        repeat=rule.repeat,
                        queue_state=state.queue_state,
                        queue_length=len(state.queue or []),
                        automation_enabled=state.automation_enabled,
                    )

                    if rule.zone == 0:
                        # Gruppe immer in Queue (Reihenfolge sichern).
                        # Gleiche Vier-Fall-Strategie wie sensor_engine.
                        state.queue = state.queue or []
                        queue_had_items  = bool(state.queue)
                        queue_is_running = (state.queue_state == "läuft")

                        if queue_had_items and not queue_is_running:
                            # Fall 3: Zeitplan-Items als Priorität vorne einstellen
                            for job in jobs:
                                job.priority = True
                            state.queue = jobs + state.queue
                            state.queue_priority_mode = True
                            log_event(
                                "schedule_queue_priority_prepend",
                                source="schedule",
                                schedule_id=rule.id,
                                items_prepended=len(jobs),
                                queue_length_after=len(state.queue),
                            )
                        elif queue_had_items and state.queue_priority_mode:
                            # Fall 2b: Queue läuft im Prioritätsmodus – nach letztem
                            # priority-Item einfügen (identische Logik wie sensor_engine).
                            for job in jobs:
                                job.priority = True
                            insert_pos = next(
                                (i for i, x in enumerate(state.queue) if not x.priority),
                                len(state.queue),
                            )
                            state.queue[insert_pos:insert_pos] = jobs
                            log_event(
                                "schedule_queue_priority_insert",
                                source="schedule",
                                schedule_id=rule.id,
                                items_inserted=len(jobs),
                                insert_pos=insert_pos,
                                queue_length_after=len(state.queue),
                            )
                        else:
                            # Fall 1 (leere Queue) oder Fall 2 (Queue läuft, kein PM)
                            state.queue.extend(jobs)

                        state.queue_dirty = True
                        if state.queue_state in ("bereit", "fertig"):
                            state.queue_state = "läuft"
                            state.queue_dirty = True
                    else:
                        job = jobs[0]
                        if _can_start_new_valve_locked() and (not state.paused) and state.queue_state != "pausiert":
                            # Merke Job für Start OHNE Lock
                            jobs_to_start.append(job)
                        else:
                            # Kein Direktstart möglich → Queue-Einfüge-Strategie.
                            # Gleiche Vier-Fall-Logik wie sensor_engine.
                            state.queue = state.queue or []
                            queue_had_items  = bool(state.queue)
                            queue_is_running = (state.queue_state == "läuft")

                            if queue_had_items and not queue_is_running:
                                # Fall 3: Zeitplan-Item als Priorität vorne einstellen
                                job.priority = True
                                state.queue.insert(0, job)
                                state.queue_priority_mode = True
                                log_event(
                                    "schedule_queue_priority_prepend",
                                    source="schedule",
                                    schedule_id=rule.id,
                                    items_prepended=1,
                                    queue_length_after=len(state.queue),
                                )
                            elif queue_had_items and state.queue_priority_mode:
                                # Fall 2b: Queue läuft im Prioritätsmodus – nach letztem
                                # priority-Item einfügen (identische Logik wie sensor_engine).
                                job.priority = True
                                insert_pos = next(
                                    (i for i, x in enumerate(state.queue) if not x.priority),
                                    len(state.queue),
                                )
                                state.queue.insert(insert_pos, job)
                                log_event(
                                    "schedule_queue_priority_insert",
                                    source="schedule",
                                    schedule_id=rule.id,
                                    items_inserted=1,
                                    insert_pos=insert_pos,
                                    queue_length_after=len(state.queue),
                                )
                            else:
                                # Fall 1 (leere Queue) oder Fall 2 (Queue läuft, kein PM)
                                state.queue.append(job)

                            state.queue_dirty = True
                            if state.queue_state in ("bereit", "fertig"):
                                state.queue_state = "läuft"
                                state.queue_dirty = True

                    if not rule.repeat:
                        rule.once_pending.remove(combo_key)
                        if len(rule.once_pending) == 0:
                            to_delete_ids.append(rule.id)

                if to_delete_ids:
                    state.schedules = [r for r in (state.schedules or []) if r.id not in to_delete_ids]
                    state.schedules_dirty = True
                    log_event("schedule_done", source="system", deleted_ids=to_delete_ids)

            # Phase 2: Starte Jobs OHNE Lock
            for job in jobs_to_start:
                try:
                    start_queue_item(job)
                except Exception as e:
                    # Bei Fehler: Job in Queue einfügen (vorne) für späteren Retry
                    with state_lock:
                        state.queue = state.queue or []
                        state.queue.insert(0, job)
                        state.queue_dirty = True
                        if state.queue_state in ("bereit", "fertig"):
                            state.queue_state = "läuft"

                    log_event(
                        "schedule_start_failed",
                        level="error",
                        source="system",
                        zone=job.zone,
                        error=str(e),
                        action="moved_to_queue"
                    )

        except Exception:
            from core.logging import logger
            logger.exception("scheduler_loop crashed")
            log_event("scheduler_error", level="error", source="system")
